"""日程模板服务（2026-11-17 批 2）：CRUD + 应用到某日生成普通 Task。

模板存相对时刻（start_min），应用时按 date 换算绝对时间；落库后完全走既有
任务链路（提醒/冲突/统计无需感知模板）。name 唯一，同名 409 由 service 抛
ValueError（router 翻 409）。
"""

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import ScheduleTemplate, Subtask, Task
from schemas import TaskCreate, TemplateApplyIn, TemplateIn, TemplateUpdate
from services.tasks import _attach_subtasks, create_task

logger = logging.getLogger(__name__)


class TemplateConflictError(Exception):
    """模板名重复（router 翻 409）。"""


def list_templates(db: Session) -> list[ScheduleTemplate]:
    return list(db.scalars(
        select(ScheduleTemplate).order_by(ScheduleTemplate.sort_order,
                                          ScheduleTemplate.created_at)))


def get_template(db: Session, template_id: str) -> ScheduleTemplate | None:
    return db.get(ScheduleTemplate, template_id)


def _check_name(db: Session, name: str, exclude_id: str | None = None) -> None:
    stmt = select(ScheduleTemplate.id).where(ScheduleTemplate.name == name)
    if exclude_id:
        stmt = stmt.where(ScheduleTemplate.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise TemplateConflictError(f"模板名已存在：{name}")


def create_template(db: Session, body: TemplateIn) -> ScheduleTemplate:
    _check_name(db, body.name)
    sort_order = db.scalar(
        select(func.coalesce(func.max(ScheduleTemplate.sort_order), -1))) + 1
    row = ScheduleTemplate(
        name=body.name, title=body.title, duration_mins=body.duration_mins,
        start_min=body.start_min, priority=body.priority, notes=body.notes,
        recurrence_rule=body.recurrence_rule, location=body.location,
        travel_mins=body.travel_mins, subtasks=body.subtasks, sort_order=sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("新建日程模板「%s」id=%s", row.name, row.id)
    return row


def update_template(db: Session, template_id: str,
                    patch: TemplateUpdate) -> ScheduleTemplate | None:
    row = get_template(db, template_id)
    if row is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    for key in ("name", "title"):
        if key in data and data[key] is None:
            raise ValueError(f"{'模板名' if key == 'name' else '标题'}不允许置空")
    if "name" in data and data["name"] != row.name:
        _check_name(db, data["name"], exclude_id=template_id)
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def delete_template(db: Session, template_id: str) -> bool:
    row = get_template(db, template_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("删除日程模板 id=%s", template_id)
    return True


def apply_template(db: Session, template_id: str,
                   body: TemplateApplyIn) -> Task | None:
    """应用模板生成任务：已排期（模板有 start_min 或显式 start_time）或未排期待办。"""
    tpl = get_template(db, template_id)
    if tpl is None:
        return None
    start: datetime | None = None
    if body.start_time is not None:
        start = body.start_time
    elif tpl.start_min is not None:
        anchor = (date.fromisoformat(body.date) if body.date else date.today())
        start = datetime.combine(anchor, time(tpl.start_min // 60, tpl.start_min % 60))
    duration = tpl.duration_mins
    if start is not None and duration is None:
        duration = 60  # 有开始时刻时给默认 1 小时（与日历一小时卡展示口径一致）
    task = create_task(db, TaskCreate(
        title=tpl.title,
        start_time=start,
        end_time=start + timedelta(minutes=duration) if start is not None and duration else None,
        duration_mins=duration if start is not None else tpl.duration_mins,
        priority=tpl.priority,
        notes=tpl.notes,
        recurrence_rule=tpl.recurrence_rule,
        location=tpl.location,
        travel_mins=tpl.travel_mins,
    ))
    for idx, title in enumerate(tpl.subtasks or []):
        if isinstance(title, str) and title.strip():
            db.add(Subtask(task_id=task.id, title=title.strip()[:30], sort_order=idx))
    if tpl.subtasks:
        db.commit()
        db.refresh(task)
        _attach_subtasks(db, [task])  # 响应回填清单（TaskOut 非 ORM 关系）
    logger.info("应用模板「%s」→ 任务 id=%s", tpl.name, task.id)
    return task
