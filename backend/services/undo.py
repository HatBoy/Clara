"""最近一次操作撤销（2026-11-17 新功能批 1：进程内快照栈）。

设计取舍：
- **不建表、不参与同步**：撤销是短时（数秒）的交互补救，不是数据资产；
  进程重启后栈为空，GET /api/undo 返回 available=false（不报错）。
- **只暴露栈顶**：队列留 20 条仅为并发/连续操作兜底，产品上只撤销最近一次。
- **撤销自身 suppress**：apply_last 期间的写库不再产生新条目，避免“撤销再撤销”。
- 快照用 Core SELECT 取整行（含主行、重复系列子行、subtasks、reminders），
  恢复用 merge（物理删的行以原 id 重插）；本次新建的行按 id 反向删除。
"""

import logging
import threading
from collections import deque
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update as sa_update
from sqlalchemy.orm import Session

from models import BehaviorEvent, Project, Reminder, Subtask, Task

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 20
_stack: deque[dict] = deque(maxlen=_MAX_ENTRIES)
_lock = threading.Lock()
_local = threading.local()
_suppressed = False


def _new_entry(kind: str, label: str) -> dict:
    return {
        "kind": kind, "label": label,
        "at": datetime.now(),
        "tasks": [], "subtasks": [], "reminders": [], "projects": [],
        "created_task_ids": [], "event_ids": [], "task_projects": {},
    }


def begin(kind: str, label: str) -> dict:
    """开始捕获一次操作（同线程内有效；异常路径由调用方 abort 丢弃）。"""
    entry = _new_entry(kind, label)
    _local.entry = entry
    return entry


def abort(entry: dict) -> None:
    if getattr(_local, "entry", None) is entry:
        _local.entry = None


def finish(entry: dict) -> None:
    """操作成功落库后调用：压入撤销栈（suppress 期间不记）。"""
    if getattr(_local, "entry", None) is entry:
        _local.entry = None
    if _suppressed:
        return
    if not (entry["tasks"] or entry["projects"]):
        return
    with _lock:
        _stack.append(entry)


def _dedup(rows: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for row in rows:
        key = row["id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _select_rows(db: Session, table, cond) -> list[dict]:
    return [dict(r) for r in db.execute(select(table).where(cond)).mappings().all()]


def capture_task(entry: dict, db: Session, task_id: str) -> None:
    """捕获任务及其派生行（重复系列的墓碑/活体例外子行一并快照）。"""
    mains = _select_rows(db, Task.__table__, Task.__table__.c.id == task_id)
    if not mains:
        return
    children = _select_rows(db, Task.__table__, Task.__table__.c.parent_id == task_id)
    tasks = _dedup(entry["tasks"] + mains + children)
    entry["tasks"] = tasks
    ids = [r["id"] for r in tasks]
    entry["subtasks"] = _dedup(
        entry["subtasks"] + _select_rows(db, Subtask.__table__,
                                         Subtask.__table__.c.task_id.in_(ids)))
    entry["reminders"] = _dedup(
        entry["reminders"] + _select_rows(db, Reminder.__table__,
                                          Reminder.__table__.c.task_id.in_(ids)))


def capture_project(entry: dict, db: Session, project_id: str) -> None:
    """捕获项目行与归属任务的 project_id（项目删除会清空归属并改墓碑名）。"""
    rows = _select_rows(db, Project.__table__, Project.__table__.c.id == project_id)
    if not rows:
        return
    entry["projects"] = _dedup(entry["projects"] + rows)
    for task_id, pid in db.execute(
        select(Task.__table__.c.id, Task.__table__.c.project_id)
        .where(Task.__table__.c.project_id == project_id)
    ).all():
        entry["task_projects"][task_id] = pid


def note_created_task(task_id: str | None) -> None:
    """记录本次操作新建的行（模式话例外 / 系列拆分新主行）。"""
    entry = getattr(_local, "entry", None)
    if entry is None or not task_id:
        return
    if task_id not in entry["created_task_ids"]:
        entry["created_task_ids"].append(task_id)


def note_event(event_id: str | None) -> None:
    """记录本次操作新增的行为遥测行（撤销时一并删除，避免脏遥测）。"""
    entry = getattr(_local, "entry", None)
    if entry is None or not event_id:
        return
    entry["event_ids"].append(event_id)


def state() -> dict:
    """当前可撤销状态（栈顶）。"""
    with _lock:
        top = _stack[-1] if _stack else None
    return {
        "available": top is not None,
        "label": top["label"] if top else None,
        "at": top["at"].isoformat(timespec="seconds") if top else None,
    }


def reset_for_tests() -> None:
    global _suppressed
    with _lock:
        _stack.clear()
    _local.entry = None
    _suppressed = False


def apply_last(db: Session) -> str | None:
    """撤销最近一次操作；返回被撤销操作的文案，空栈返回 None。"""
    global _suppressed
    with _lock:
        if not _stack:
            return None
        entry = _stack.pop()
    _suppressed = True
    try:
        # 1) 恢复快照行（父行先于子行：重复系列的 parent_id 外键）
        for row in sorted(entry["tasks"],
                          key=lambda r: 0 if r.get("parent_id") is None else 1):
            db.merge(Task(**row))
        # 2) 删除本次新建的行（子行先清引用行）
        created = entry["created_task_ids"]
        if created:
            db.execute(sa_delete(Reminder).where(Reminder.task_id.in_(created)))
            db.execute(sa_delete(Subtask).where(Subtask.task_id.in_(created)))
            db.execute(sa_delete(Task).where(Task.id.in_(created)))
        # 3) 子行（清单/提醒）恢复：先清当前行再插快照，避免重复入列/唯一键冲突
        task_ids = [r["id"] for r in entry["tasks"]]
        if task_ids:
            db.execute(sa_delete(Subtask).where(Subtask.task_id.in_(task_ids)))
            db.execute(sa_delete(Reminder).where(Reminder.task_id.in_(task_ids)))
            for row in entry["subtasks"]:
                db.execute(Subtask.__table__.insert().values(**row))
            now = datetime.now()
            for row in entry["reminders"]:
                # 快照后已到点的自动提醒不复活（否则撤销立刻补响一次）；manual
                # 稍后提醒保留原语义（宽限 30 分钟内仍有效）
                if (row.get("fire_at") is not None and row["fire_at"] <= now
                        and (row.get("kind") or "auto") == "auto"):
                    continue
                db.execute(Reminder.__table__.insert().values(**row))
        # 4) 项目恢复 + 归属回写
        for row in entry["projects"]:
            db.merge(Project(**row))
        for task_id, project_id in entry["task_projects"].items():
            db.execute(sa_update(Task).where(Task.id == task_id)
                       .values(project_id=project_id))
        # 5) 删除本次操作产生的行为遥测
        if entry["event_ids"]:
            db.execute(sa_delete(BehaviorEvent).where(
                BehaviorEvent.id.in_(entry["event_ids"])))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        _suppressed = False

    from services import sync_engine

    sync_engine.mark_tables_dirty(
        ["tasks", "subtasks", "reminders", "projects", "behavior_events"])
    sync_engine.schedule_now()
    logger.info("[undo] 已撤销：%s", entry["label"])
    return entry["label"]
