"""任务业务层：读路径（列表/单条）+ 写路径（部分更新/删除 6 场景判定）。

分层红线：阶段一被 routers/tasks.py 调用；阶段二 agents/nodes.py 复用本层
读数据（禁止 node 重写查询、禁止 node import router）。

删除策略（2026-09-14 定稿。动机：删除数据是阶段二 AI 的学习资产——闪建闪删
是噪音物理抹除、草稿流产复盘后清理、排期取消永不抹除）：

    场景            判定                        动作
    1 闪建闪删      非重复·从未排期·创建<300s   物理删
    2 草稿流产      非重复·从未排期             软删 + deleted_at，7 天 TTL 后物理清理
    3 排期取消      非重复·曾经排期             永久软删（TTL 永不清理）
    4 仅删本次      重复·mode=this&date=D       墓碑行（parent_id 指向主任务），规则不动
    5 删及后续      重复·mode=following&date=D  规则尾拼 UNTIL=D-1T235959 截断
    6 删除所有      重复·mode=all/auto          创建<300s 物理删（连带清墓碑）；
                                                              否则永久软删

编辑的 Clone & Override（update_task 的 mode 分流，与墓碑统一建模——例外行 =
parent_id + original_time 锚定主任务，墓碑是 is_deleted=True 变体、活体例外是
is_deleted=False 变体）：
    A 仅修改此日程  PATCH ?mode=this&date=D      实体化单次例外行（字段继承主记录，
                                                patch 覆盖，recurrence 置空），主记录不动
    B 此日程及后续  PATCH ?mode=following&date=D 老系列 UNTIL 截断 + 新建主记录
                                                （基准重锚被点实例日，规则复制去 UNTIL）
    C 所有日程      PATCH（默认）                直接更新主记录本体
读路径虚实结合：_attach_cancelled_dates 聚合两类例外行的日期挖空该日虚拟实例，
活体例外行自带 start_time + recurrence NULL，被窗口查询当普通单次任务返回渲染。
"""

import difflib
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, delete as sa_delete, func, or_, select, update as sa_update
from sqlalchemy.orm import Session

from models import Project, Subtask, Task
from schemas import PRIORITY_INT, SubtaskOut, TaskCreate, TaskUpdate
from services.preference_slots import hits_window  # 避让窗判定单源（优化批 6）

logger = logging.getLogger(__name__)

# 场景 1 判定窗口：创建不足该秒数且从未排期 = 闪建闪删（纯噪音，物理抹除）
FLASH_DELETE_SECONDS = 300
# 场景 2 保留期：草稿流产软删后、物理清理前留供 AI 复盘"畏难情绪/伪需求"的天数
SOFT_DELETE_TTL_DAYS = 7

# DELETE/PATCH mode=this/following 的 date 参数格式（本地绝对日期，与前端 dateKeyOfOffset 同口径）
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# RRULE 星期码 → ISO 星期序号（1=周一…7=周日，与前端 weekdays 同口径）——
# _validate_series_mode 校验"该日真实存在实例"时解析 BYDAY 用
_BYDAY_TO_ISO = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}
_ISO_TO_BYDAY = {v: k for k, v in _BYDAY_TO_ISO.items()}


def _shift_weekly_byday(rule: str, old: datetime, new: datetime) -> str:
    """weekly 系列跨天重锚（拖拽/改期 mode=all 且日期变化）时 BYDAY 跟随新基准日
    平移：weekday 差 mod 7 逐项平移（单日集合即「跟随新日」，多日集合保持相对
    关系整体平移）——Google「所有事件」拖动语义。2026-11-12 修复：此前重锚定
    不动 BYDAY，周三系列拖到周五后规则仍是 WE、前端按 BYDAY 展开 → 实例仍
    渲染在周三，用户视角「拖不动」。非 weekly 原样返回（daily 无 BYDAY、
    monthly 按日号天然跟随基准日）。"""
    parts = rule.split(";")
    if not parts or parts[0] != "FREQ=WEEKLY":
        return rule
    delta = (new.weekday() - old.weekday()) % 7
    if delta == 0:
        return rule
    shifted = []
    for p in parts:
        if p.startswith("BYDAY="):
            days = [x for x in p[len("BYDAY="):].split(",") if x in _BYDAY_TO_ISO]
            p = "BYDAY=" + ",".join(
                _ISO_TO_BYDAY[(_BYDAY_TO_ISO[x] - 1 + delta) % 7 + 1] for x in days
            )
        shifted.append(p)
    return ";".join(shifted)


def _apply_range(stmt: Select, start: datetime | None, end: datetime | None) -> Select:
    """给查询叠加时间范围过滤（窗口内单次任务 ∪ 全部重复系列）。

    单次任务按与区间 (start, end) 的交集：无结束时间的任务视为"点事件"
    （仅开始时刻），点落在区间内才返回——避免后端硬编码前端的"默认展示
    一小时"规则；未排期任务 start_time 为 NULL，与任何区间比较均为假 →
    给了范围参数时自然只返回已排期任务。

    重复系列恒返回（OR）：前端 occursOn 以"基准日 + 规则"在前端展开任意
    日期，基准在窗外但实例在窗内的系列必须命中——按基准日当普通事件过滤
    会漏。副作用：`recurrence_rule IS NOT NULL` 会连带"未排期但带规则"的
    行（seed 无此数据、写路径未建）——前端 isScheduled 判 false 只进收件
    箱，无害。注：OR 谓词使 ix_tasks_start_time 无法纯范围扫（全表扫过
    滤），本机 SQLite 万级行毫秒级；量大后可改 UNION 双查询各走索引。
    """
    span = []
    if start is not None:
        # 任务实际结束（无 end 按开始时刻）必须晚于区间起点
        span.append(func.coalesce(Task.end_time, Task.start_time) > start)
    if end is not None:
        span.append(Task.start_time < end)
    if not span:
        return stmt
    # OR 必须包住两个边界谓词的整体——逐谓词 OR 会把"结束早于 start 的任务"错放行
    return stmt.where(or_(Task.recurrence_rule.is_not(None), and_(*span)))


def recent_done(db: Session, *, days: int = 7, limit: int = 10) -> list[dict]:
    """近 N 天完成任务（标题 + 完成时刻，新→旧）——「完成节奏」画像输入
    （2026-10-26 提速批 4：排期剖析的 done 历史 + QA 过去窗共用；done_at 由
    收件箱勾选/日历完成钮/Toast 写入，手动勾选确信度高可直接引用）。"""
    lo = datetime.now() - timedelta(days=days)
    rows = list(db.scalars(
        select(Task).where(Task.is_deleted.is_(False), Task.done_at.is_not(None),
                           Task.done_at >= lo)
        .order_by(Task.done_at.desc()).limit(limit)))
    return [{"title": t.title, "done_at": t.done_at.isoformat(timespec="minutes")}
            for t in rows]


def similar_done_durations(db: Session, title: str, *, days: int = 90,
                           limit: int = 3) -> int | None:
    """近 N 天与标题相似（difflib ≥0.6，与 similar_title 同口径）的已完成任务
    实际时长均值（至多 limit 次，新→旧）——排期剖析 est_hint 键的数据源
    （2026-10-26 批 5）：用户连做三次 90 分钟的方案评审，第四次再按提示词
    经验值 120 分钟估就脱离实际，「同类任务历史耗时优先于经验值」。无相似
    命中或命中行无时长返回 None（调用方降级经验值）。"""
    norm = normalize_title(title)
    if not norm:
        return None
    lo = datetime.now() - timedelta(days=days)
    rows = list(db.scalars(
        select(Task).where(Task.is_deleted.is_(False), Task.done_at.is_not(None),
                           Task.done_at >= lo, Task.duration_mins.is_not(None))
        .order_by(Task.done_at.desc())))
    durs = [t.duration_mins for t in rows
            if t.duration_mins and difflib.SequenceMatcher(
                None, norm, normalize_title(t.title)).ratio() >= SIMILAR_TITLE_RATIO]
    if not durs:
        return None
    return round(sum(durs[:limit]) / len(durs[:limit]))


def estimate_hint(db: Session, title: str, *, days: int = 90) -> dict:
    """预估校准提示（2026-11-06 批 E1）：同类（difflib≥0.6）已完成任务近 N 天
    实际时长均值 + 样本数。count ≥ 2 才值得提示（单样本偶然性大，前端按此判定）。
    与 similar_done_durations 同口径（est_hint 键）——两处消费不同字段故并存：
    排期剖析吃标量均值，校准提示要样本数。"""
    norm = normalize_title(title)
    empty = {"avg_min": None, "count": 0}
    if not norm:
        return empty
    lo = datetime.now() - timedelta(days=days)
    rows = list(db.scalars(
        select(Task).where(Task.is_deleted.is_(False), Task.done_at.is_not(None),
                           Task.done_at >= lo, Task.duration_mins.is_not(None))
        .order_by(Task.done_at.desc())))
    durs = [t.duration_mins for t in rows
            if t.duration_mins and difflib.SequenceMatcher(
                None, norm, normalize_title(t.title)).ratio() >= SIMILAR_TITLE_RATIO]
    if not durs:
        return empty
    return {"avg_min": round(sum(durs) / len(durs)), "count": len(durs)}


def list_tasks(
    db: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    unscheduled: bool = False,
) -> list[Task]:
    """查任务，三种口径（互斥）：

    - unscheduled=True：只返回未排期待办（start_time 为空），忽略 start/end；已完成
      （done_at 非空）不再出现——收件箱/排期候选/高难挑选的"待办"语义统一在此收口
    - 给了 start/end（至少一项）：窗口内单次任务 ∪ 全部重复系列；已完成任务**保留**
      （busy 避让源——已开完的会时段照样被占用，剔除会双订）
    - 无参数：全量（含未排期；调试口径，前端不使用）

    恒过滤软删；按 sort_order 稳定排序（收件箱"默认排序"口径 = seed 数组序）。
    重复系列行附带聚合墓碑日期（cancelled_dates 属性，见 _attach_cancelled_dates）。
    """
    base = select(Task).where(Task.is_deleted.is_(False))
    if unscheduled:
        tasks = list(db.scalars(
            base.where(Task.start_time.is_(None), Task.done_at.is_(None)).order_by(Task.sort_order)
        ))
        _attach_cancelled_dates(db, tasks)
        _attach_subtasks(db, tasks)
        # 高频路径 info→debug（优化批 3）：排期/收件箱每轮多查，日志回归流程图定位
        logger.debug("查询任务：未排期 %d 条", len(tasks))
        return tasks
    tasks = list(db.scalars(_apply_range(base, start, end).order_by(Task.sort_order)))
    _attach_cancelled_dates(db, tasks)
    _attach_subtasks(db, tasks)
    if start is not None or end is not None:
        n_series = sum(1 for t in tasks if t.recurrence_rule is not None)
        logger.debug(
            "查询任务：范围 [%s, %s)（单次按交集，重复系列全量），命中 %d 条（单次 %d + 系列 %d）",
            start, end, len(tasks), len(tasks) - n_series, n_series,
        )
    else:
        logger.debug("查询任务：全量 %d 条", len(tasks))
    return tasks


def _normalize_all_day_times(
    start: datetime, end: datetime | None
) -> tuple[datetime, datetime | None]:
    """全天/跨天行时刻归一（纯函数，2026-11-06 批 B）：start 取当日 0 点；end
    非 0 点视为「含该日」（"11.5~11.7 出差"式输入的 23:59/12:00 一律归到次日
    0 点独占界），恰为 0 点原样。end 允许 None（单日全天，渲染按 start+24h）。"""
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if end is not None and (end.hour, end.minute, end.second) != (0, 0, 0):
        end = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return start, end


def _fetch_active(db: Session, task_id: str) -> Task | None:
    """取活跃任务（软删视为不存在）——update/delete 内部复用，不做墓碑聚合：
    聚合会把日期写回 ORM 属性弄脏对象，随写路径 commit 落进已废弃的列。"""
    task = db.get(Task, task_id)
    return task if task is not None and not task.is_deleted else None


def _attach_cancelled_dates(db: Session, tasks: list[Task]) -> None:
    """把例外行的实例日期聚合写回主任务的 cancelled_dates 属性（仅内存：本函数
    不 commit、SessionLocal autoflush=False 不落库）——TaskOut from_attributes
    构造时自然带出，API 契约与前端零改动。例外行两类一并聚合（Clone & Override
    统一建模）：墓碑（is_deleted=True，实例被删除）与活体例外（is_deleted=False，
    实例已实体化为独立单次任务）——两类日期都挖空该日的虚拟实例（活体例外自身
    按普通任务行渲染）。set 去重：同日先删后编辑会并存墓碑与活体两行。"""
    series_ids = [t.id for t in tasks if t.recurrence_rule is not None]
    if not series_ids:
        return
    by_parent: dict[str, set[str]] = {}
    for parent_id, original in db.execute(
        select(Task.parent_id, Task.original_time).where(
            Task.parent_id.in_(series_ids)
        )
    ).all():
        if original is not None:
            by_parent.setdefault(parent_id, set()).add(original.date().isoformat())
    for t in tasks:
        if t.id in by_parent:
            t.cancelled_dates = sorted(by_parent[t.id])


def _attach_subtasks(db: Session, tasks: list[Task]) -> None:
    """批量装配子任务清单（2026-11-06 批 C，模式同 _attach_cancelled_dates：仅内存
    挂属性不 commit）——TaskOut.subtasks 的数据源。单条 IN 查询分组，避免 ORM
    relationship 的 N+1（list_tasks 是批量主路径）。SubtaskOut 直接构造挂上，
    TaskOut from_attributes 序列化时原样通过。"""
    ids = [t.id for t in tasks]
    if not ids:
        return
    by_task: dict[str, list[SubtaskOut]] = {t.id: [] for t in tasks}
    for row in db.scalars(
        select(Subtask).where(Subtask.task_id.in_(ids)).order_by(Subtask.sort_order)
    ):
        by_task[row.task_id].append(
            SubtaskOut(id=row.id, title=row.title, done=row.done_at is not None)
        )
    for t in tasks:
        t.subtasks = by_task[t.id]


def _replace_subtasks(db: Session, task: Task, items: list[dict] | None) -> None:
    """全量替换任务清单（PATCH subtasks 键出现即调用）：物理删旧行 + 按 items 重插
    （done → done_at=now，sort_order = 数组序）。空列表 = 清空；不 commit（随调用
    方事务，注释同 _attach_cancelled_dates）。"""
    if items is None:
        return
    db.execute(sa_delete(Subtask).where(Subtask.task_id == task.id))
    now = datetime.now()
    for idx, item in enumerate(items):
        db.add(Subtask(
            task_id=task.id,
            title=item["title"],
            done_at=now if item.get("done") else None,
            sort_order=idx,
        ))


def _copy_subtasks(db: Session, src_id: str, dst_id: str) -> None:
    """复制源任务的清单到目标任务（例外实体化/系列拆分的继承路径；不 commit）。"""
    for row in db.scalars(
        select(Subtask).where(Subtask.task_id == src_id).order_by(Subtask.sort_order)
    ):
        db.add(Subtask(
            task_id=dst_id,
            title=row.title,
            done_at=row.done_at,
            sort_order=row.sort_order,
        ))


def get_task(db: Session, task_id: str) -> Task | None:
    """单条（router 口径）：软删视为不存在（业务规则在 service，不下沉 router），
    重复系列附带聚合墓碑日期。"""
    result = _fetch_active(db, task_id)
    if result is None:
        logger.info("读取单条任务：id=%s 未命中（不存在或已软删）", task_id)
    else:
        logger.info("读取单条任务：id=%s 命中「%s」", task_id, result.title)
        _attach_cancelled_dates(db, [result])
        _attach_subtasks(db, [result])
    return result


def create_task(db: Session, body: TaskCreate) -> Task:
    """新建任务（快捷录入框）：sort_order 排全表末位，start_time 非空即 ever_scheduled。

    校验与不变式同 update_task 口径：project_id 活性校验、end ⇒ start、end > start、
    start/end 齐备时 duration_mins 强制重算（排期计算优先于显式携带值）。
    业务校验失败抛 ValueError（router 翻 422）。
    """
    if body.project_id is not None:
        project = db.get(Project, body.project_id)
        if project is None or project.is_deleted:
            raise ValueError("所属项目不存在")  # FK 真约束下脏 id 会打 IntegrityError 500
    if body.end_time is not None:
        if body.start_time is None:
            raise ValueError("结束时间需要开始时间")
        if body.end_time <= body.start_time:
            raise ValueError("结束时间必须晚于开始时间")
    all_day = bool(body.all_day)
    if all_day and body.start_time is None:
        raise ValueError("全天日程需要开始日期")
    start_time, end_time = body.start_time, body.end_time
    duration_mins = body.duration_mins
    if all_day:
        # 全天/跨天（2026-11-06 批 B）：时刻归一为日期区间，占天不占时段——
        # duration 恒空（不变式豁免），负载口径在 _task_dur 短路为 0
        start_time, end_time = _normalize_all_day_times(start_time, end_time)
        duration_mins = None
    elif start_time is not None and end_time is not None:
        # 耗时三字段一致性不变式（同 update_task）：排期齐备时 duration ≡ end-start
        duration_mins = round((end_time - start_time).total_seconds() / 60)
    sort_order = db.scalar(select(func.coalesce(func.max(Task.sort_order), -1))) + 1
    task = Task(
        title=body.title,
        duration_mins=duration_mins,
        start_time=start_time,
        end_time=end_time,
        all_day=all_day,
        recurrence_rule=body.recurrence_rule,
        project_id=body.project_id,
        notes=body.notes,  # 2026-10-14 增：inbox 建议卡收录时携带来源摘要（≤500，schema 已校验）
        # 采集溯源三列（2026-10-23 增）：采集建议落库时从 source_ref 透传——
        # 手建/快录任务恒 None，无行为分叉
        source_key=body.source_key,
        source_fingerprint=body.source_fingerprint,
        source_evidence=body.source_evidence,
        sort_order=sort_order,
        ever_scheduled=body.start_time is not None,
        priority=body.priority if body.priority is not None else 2,
    )
    db.add(task)
    db.commit()
    db.refresh(task)  # created_at 是 Python 侧 default 已有值，updated_at 是 server_default 需 refresh
    logger.info(
        "新建任务 id=%s「%s」：%s",
        task.id, task.title,
        f"排期 {body.start_time}" if body.start_time is not None else "未排期",
    )
    return task


# —— 行为遥测（2026-11-03 记忆重构批 5）：update_task 单点写，前端零改动 ——————

# 前后会议邻近判定窗口（分钟）：busy 最近邻 gap ≤30min 记入环境快照
_EVENT_MEETING_GAP_MIN = 30
# 时段桶（分钟）：环境快照的 period 值
_PERIODS = ((300, "清晨"), (720, "上午"), (1080, "下午"), (1440, "晚上"))


def _period_of(minute: int) -> str:
    for cap, name in _PERIODS:
        if minute < cap:
            return name
    return "晚上"


def _event_context(db: Session, anchor: datetime | None) -> dict | None:
    """环境快照（纯读）：周几 / 时段取 anchor 时刻；前后会议 = 当日 busy 最近邻
    （gap ≤30min 才记标题，None = 无邻近会议）。anchor 为空（未排期勾完成等）
    返回 None——无锚的快照无聚类价值。"""
    if anchor is None:
        return None
    try:
        busy = busy_intervals(db, days_back=0, days_fwd=1)
    except Exception:  # 遥测防御：读库异常不带崩主流程
        busy = []
    day0 = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    before = after = None
    for b0, b1 in busy:
        if b1 <= day0 or b0 > day0 + timedelta(days=2):
            continue
        if b1 <= anchor:
            gap = (anchor - b1).total_seconds() / 60
            if gap <= _EVENT_MEETING_GAP_MIN and (before is None or gap < before[0]):
                before = (gap, b0)
        if b0 > anchor:
            gap = (b0 - anchor).total_seconds() / 60
            if gap <= _EVENT_MEETING_GAP_MIN and (after is None or gap < after[0]):
                after = (gap, b0)
    return {
        "weekday": anchor.weekday(),
        "period": _period_of(anchor.hour * 60 + anchor.minute),
        "before_meeting": before is not None,
        "after_meeting": after is not None,
    }


def _record_event(db: Session, task: Task, kind: str, *,
                  prev_start: datetime | None = None,
                  new_start: datetime | None = None,
                  delta_min: int | None = None,
                  planned_min: int | None = None) -> None:
    """追加一条行为遥测事件（夜间反思偏差聚类的原料）。整体 try/except 吞掉打
    warning——遥测绝不让改期/完成 500（防御纪律同 weather）。commit 与调用方
    共事务末尾合并：只 db.add + flush，不单独 commit（调用点均在 db.commit 前）。"""
    try:
        from models import BehaviorEvent

        anchor = prev_start or new_start
        db.add(BehaviorEvent(
            task_id=task.id, kind=kind,
            prev_start=prev_start, new_start=new_start,
            delta_min=delta_min, planned_min=planned_min,
            context=_event_context(db, anchor),
        ))
        db.flush()
    except Exception as exc:  # noqa: BLE001——遥测防御边界
        logger.warning("行为遥测写入失败（%s %s）：%s", kind, task.id, exc)


def update_task(
    db: Session,
    task_id: str,
    patch: TaskUpdate,
    mode: str = "all",
    date: str | None = None,
) -> Task | None:
    """部分更新：patch.exclude_unset 取数，键不出现不动、显式 null 清空该字段。

    mode 分流（编辑的 Clone & Override，见模块头）：all（默认）直接更新任务本体；
    this&date 实体化单次例外（主记录不动）；following&date 截断老系列 + 新建主记录。
    时间/优先级/重复规则在入库前归一（时区已在 schemas 契约层转 naive 本地）。
    业务校验与不变式对 setattr 后的"合并态"做——覆盖"本次只发 end_time、start_time
    是库中旧值"的交叉场景。返回 None = 不存在（router 404）。
    """
    task = _fetch_active(db, task_id)
    if task is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    # 完成态开关（2026-10-22 增，同日修订系列口径）：入参键 done 不是列名，先弹出映射
    # 为 done_at。非系列行 mode=all 直接落；重复系列仅支持 mode=this（按单次实例实体化
    # 例外行标完成，主行与其余实例不动）——mode=all 整系列 done 语义错仍拦 422
    done_flag = data.pop("done", None)
    if done_flag is not None and task.recurrence_rule is not None and mode != "this":
        raise ValueError("重复系列暂不支持整系列标记完成，请对单次日程操作")
    if data.get("all_day") is None:
        data.pop("all_day", None)  # bool 开关无 null 清空语义（False 即关闭），null 不动
    # 子任务清单（批 C）：单独弹出走全量替换（不进 setattr——它不是 Task 列）
    subtask_items = data.pop("subtasks", None)
    # mode 校验先于空 patch 短路：this/following 携带空 body 属调用方错误（前端乐观
    # 更新把响应当"新行"消费，空请求返回主行会形成"本地已挖空、后端无实体"的洞）
    if mode in ("this", "following"):
        _validate_series_mode(task, mode, date)
        if not data and done_flag is None and subtask_items is None:
            raise ValueError("该模式要求至少一个修改字段")
        if mode == "this" and "recurrence_rule" in data:
            raise ValueError("单次例外不支持修改重复规则")
    if not data and done_flag is None and subtask_items is None:
        return task  # 空 patch 短路：不发 UPDATE、不误抬 updated_at
    # Optional 入参会放行显式 null，但 title/priority 是 NOT NULL 列——拦为 422（否则 IntegrityError 500）
    for key, label in (("title", "标题"), ("priority", "优先级")):
        if data.get(key) is None and key in data:
            raise ValueError(f"{label}不允许置空")
    if "priority" in data:
        data["priority"] = PRIORITY_INT[data.pop("priority")]  # 字符串 → 库内 INT
    if data.get("project_id") is not None:
        project = db.get(Project, data["project_id"])
        if project is None or project.is_deleted:
            raise ValueError("所属项目不存在")  # FK 真约束下脏 id 会打 IntegrityError 500
    if mode == "this":  # 场景 A：虚拟实例实体化为单次例外，主记录不动
        exception = _materialize_exception(db, task, date, data)
        if subtask_items is not None:  # 三问路径的清单编辑落在例外行上（构造已复制主行清单，此处覆盖）
            _replace_subtasks(db, exception, subtask_items)
            db.commit()
            db.refresh(exception)
            _attach_subtasks(db, [exception])  # 重装配（覆盖构造时复制的旧清单态）
        if done_flag is not None:
            # 系列单实例完成：落在实体化的例外行上（上面已 commit，此处增量落值再提交）
            exception.done_at = datetime.now() if done_flag else None
            db.commit()
            db.refresh(exception)
            if done_flag:  # 遥测（批 5）：系列实例完成同样记 done（改期不记，同 reschedule_count 口径）
                _record_event(db, exception, "done",
                              prev_start=exception.start_time,
                              planned_min=exception.duration_mins,
                              delta_min=(round((datetime.now() - exception.start_time).total_seconds() / 60)
                                         - exception.duration_mins
                                         if exception.start_time else None))
                db.commit()
        return exception
    if mode == "following":  # 场景 B：老系列 UNTIL 截断 + 新建主记录
        new = _split_series(db, task, date, data)
        if subtask_items is not None:  # 新系列承接清单（构造已复制，此处覆盖为提交态）
            _replace_subtasks(db, new, subtask_items)
            db.commit()
        return new
    # mode == all：直接更新任务本体
    if task.parent_id is not None and data.get("recurrence_rule") is not None:
        # 活体例外行（parent_id 锚定主系列）原地转系列会同时具备两重身份——既被
        # 当独立系列展开、又被聚合为主系列的挖空日期，拒绝（先删例外关系再转系列）
        raise ValueError("例外行不支持设置重复规则")
    # 取消排期联动清结束时刻：库内不变式 end ⇒ start，不留"有 end 无 start"的脏数据
    #（本期前端不发 start_time=null，schema 开放给将来的"日程拖回收件箱"）
    if data.get("start_time") is None and "start_time" in data:
        data["end_time"] = None
    prev_start = task.start_time  # 改期计数锚：setattr 前留存旧时刻
    prev_end = task.end_time      # 遥测锚（批 5）：resize 偏差 = 新时长 − 旧时长
    if done_flag is not None:
        task.done_at = datetime.now() if done_flag else None
    for key, value in data.items():
        setattr(task, key, value)
    # weekly 系列跨天重锚的 BYDAY 跟随（2026-11-12）：日期变化（非纯调钟点）时
    # 平移星期集合，防「基准周五、规则周三」的脏状态让实例渲染回原星期
    if (
        task.recurrence_rule is not None
        and "start_time" in data
        and data["start_time"] is not None
        and prev_start is not None
        and data["start_time"].date() != prev_start.date()
    ):
        task.recurrence_rule = _shift_weekly_byday(
            task.recurrence_rule, prev_start, data["start_time"]
        )
    if (
        "start_time" in data
        and prev_start is not None
        and data["start_time"] is not None
        and data["start_time"] != prev_start
    ):
        # 改期计数（夜间复盘"改期痕迹"信号源，2026-10-22 补写点）：仅"已排期任务被
        # 挪到新时刻"计数——首次排期（None→值）与取消排期（值→None）不算改期
        task.reschedule_count = (task.reschedule_count or 0) + 1
        # 遥测（批 5）：改期事件——带符号分钟差（新−旧）+ 环境快照
        _record_event(db, task, "reschedule",
                      prev_start=prev_start, new_start=data["start_time"],
                      delta_min=round((data["start_time"] - prev_start).total_seconds() / 60))
    if ("start_time" in data and data["start_time"] is None
            and prev_start is not None):
        # 遥测（批 5）：取消排期（放弃这段安排的近似口径——真删走 DELETE 记 abandon）
        _record_event(db, task, "unschedule", prev_start=prev_start)
    if ("end_time" in data and data["end_time"] is not None
            and "start_time" not in data
            and prev_end is not None and data["end_time"] != prev_end
            and task.start_time is not None):
        # 遥测（批 5）：改时长（估时偏差 / 超长耗时信号源）——start 不变时
        # 新旧时长差 = end 差；不变式重算前 data 里就是新 end
        _record_event(db, task, "resize", prev_start=task.start_time,
                      delta_min=round((data["end_time"] - prev_end).total_seconds() / 60))
    if done_flag:
        # 遥测（批 5）：完成事件——delta = 实际-计划（未排期勾选完成两值 None）
        _record_event(db, task, "done", prev_start=task.start_time,
                      planned_min=task.duration_mins,
                      delta_min=(round((datetime.now() - task.start_time).total_seconds() / 60)
                                 - task.duration_mins
                                 if task.start_time and task.duration_mins else None))
    if task.end_time is not None:
        if task.start_time is None:
            raise ValueError("结束时间需要开始时间")
        if task.end_time <= task.start_time:
            raise ValueError("结束时间必须晚于开始时间")
    if task.start_time is not None:
        # 曾经排期过的单向开关：合并态有 start_time 即置 True、置过不清回——
        # 将来"日程拖回收件箱"清 start_time 后，场景 3（排期取消）判定仍成立
        task.ever_scheduled = True
    if task.all_day:
        # 全天/跨天合并态（2026-11-06 批 B）：开启（本帧或库中旧值）即归一日期
        # 区间并豁免 duration 重算——占天不占时段
        if task.start_time is None:
            raise ValueError("全天日程需要开始日期")
        task.start_time, task.end_time = _normalize_all_day_times(
            task.start_time, task.end_time
        )
        task.duration_mins = None
    elif task.start_time is not None and task.end_time is not None:
        # 耗时三字段一致性不变式：start/end 齐备时 duration_mins ≡ end-start——与显式
        # 携带的 duration_mins 冲突时以排期计算为准强制重算（含拉伸调时长的路径），
        # 未排期任务（start/end 空）的 duration_mins 不受影响、可自由设置
        task.duration_mins = round((task.end_time - task.start_time).total_seconds() / 60)
    if subtask_items is not None:  # 全量替换清单（随主行事务一起提交）
        _replace_subtasks(db, task, subtask_items)
    db.commit()
    db.refresh(task)  # updated_at 已是 Python 侧 onupdate（本地 naive），refresh 仅防御性同步
    if subtask_items is not None:
        _attach_subtasks(db, [task])  # 响应回填需真实清单（防前端本地态被空 [] 抹掉）
    logger.info(
        "更新任务 id=%s「%s」：字段 %s",
        task_id, task.title,
        sorted(data) + (["done"] if done_flag is not None else []),
    )
    return task


def _validate_series_mode(task: Task, mode: str, date: str | None) -> None:
    """this/following 模式的前置校验（update/delete 共用）：仅重复系列有实例语义，
    date 必须是 YYYY-MM-DD 且该日真实存在实例。不合法抛 ValueError（router 翻 422）。

    实例存在性校验与前端 occursOn 同口径——前端 UI 只允许选真实实例日，此校验
    防直接 API 调用（阶段二 agent 复用 service 层）：date 超出已有 UNTIL 会让
    "删及后续"的反向截断变成延长（被砍实例复活）、非 BYDAY 日期产出永不生效的
    垃圾墓碑。"""
    if task.recurrence_rule is None:
        raise ValueError("非重复任务不支持该模式")
    if date is None or not _DATE_PATTERN.match(date):
        raise ValueError("date 参数须为 YYYY-MM-DD")
    y, m, d = (int(x) for x in date.split("-"))
    day = datetime(y, m, d)
    if task.start_time is not None and day.date() < task.start_time.date():
        raise ValueError("该日期早于系列基准日，无实例")
    parts = task.recurrence_rule.split(";")
    until = next((p[len("UNTIL="):] for p in parts if p.startswith("UNTIL=")), None)
    if until is not None and day.date() > datetime.strptime(until[:8], "%Y%m%d").date():
        raise ValueError("该日期超出系列 UNTIL 截断日，无实例")
    freq = parts[0]
    if freq == "FREQ=WEEKLY":
        # 契约层 _RRULE_PATTERN 强制 weekly 带 BYDAY（schemas.py），缺段视为非法放行拒绝
        byday = next((p[len("BYDAY="):] for p in parts if p.startswith("BYDAY=")), "")
        weekdays = {_BYDAY_TO_ISO[x] for x in byday.split(",") if x in _BYDAY_TO_ISO}
        if weekdays and day.isoweekday() not in weekdays:
            raise ValueError("该日期不在每周重复的星期集合内")
    if freq == "FREQ=MONTHLY" and task.start_time is not None and day.day != task.start_time.day:
        raise ValueError("该日期与每月重复的日号不符")


def _instance_time(task: Task, date: str) -> datetime:
    """date（YYYY-MM-DD）+ 主任务开始钟点 → 该实例的锚点时刻（无排期基准则当日 0 点，
    与墓碑口径一致）。"""
    y, m, d = (int(x) for x in date.split("-"))
    clock = task.start_time.time() if task.start_time is not None else None
    return datetime(y, m, d, clock.hour, clock.minute) if clock else datetime(y, m, d)


def _find_exception(db: Session, parent_id: str, day: datetime) -> Task | None:
    """查重复系列在指定日期的活体例外行（实体化的单次实例；墓碑 is_deleted=True 不算）。"""
    return db.scalar(
        select(Task).where(
            Task.parent_id == parent_id,
            Task.is_deleted.is_(False),
            func.date(Task.original_time) == day.date(),
        )
    )


def _materialize_exception(db: Session, task: Task, date: str, data: dict) -> Task:
    """Clone & Override 场景 A（仅修改此日程）：把重复系列的某个虚拟实例实体化为独立的
    单次任务行——字段继承主记录、patch 覆盖、start_time = 实例时刻、parent_id /
    original_time 锚定派生关系（读路径聚合挖空该日虚拟实例，本行自身按普通任务渲染），
    recurrence_rule=NULL（例外是单次）。同日已有活体例外则复用该行（二次编辑幂等不叠加）。"""
    original = _instance_time(task, date)
    exception = _find_exception(db, task.id, original)
    if exception is None:
        duration = (
            task.end_time - task.start_time
            if task.start_time is not None and task.end_time is not None
            else None
        )
        exception = Task(
            title=task.title,
            priority=task.priority,
            project_id=task.project_id,
            duration_mins=task.duration_mins,
            notes=task.notes,
            start_time=original,
            end_time=original + duration if duration is not None else None,
            all_day=task.all_day,  # 全天系列实例继承占天语义（跨距随 duration 复制）
            parent_id=task.id,
            original_time=original,
            sort_order=task.sort_order,  # 继承主记录（口径同 _split_series），防列表序跳最前
            ever_scheduled=True,
        )
        db.add(exception)
        db.flush()  # 拿 id（uuid default 在 flush 生成）——清单复制需要（批 C）
        _copy_subtasks(db, task.id, exception.id)
    for key, value in data.items():
        setattr(exception, key, value)
    # 合并态校验与耗时重算（口径同 all 分支：例外行是自带 start/end 的已排期单次任务）
    if exception.end_time is not None:
        if exception.start_time is None:
            raise ValueError("结束时间需要开始时间")
        if exception.end_time <= exception.start_time:
            raise ValueError("结束时间必须晚于开始时间")
    if exception.all_day:
        # 全天例外（2026-11-06 批 B）：归一日期区间 + duration 豁免
        exception.start_time, exception.end_time = _normalize_all_day_times(
            exception.start_time, exception.end_time
        )
        exception.duration_mins = None
    elif exception.start_time is not None and exception.end_time is not None:
        exception.duration_mins = round(
            (exception.end_time - exception.start_time).total_seconds() / 60
        )
    db.commit()
    db.refresh(exception)
    _attach_subtasks(db, [exception])  # 响应回填需真实清单（缺装配会回 [] 抹掉前端本地态）
    logger.info(
        "实体化单次例外：系列 id=%s「%s」%s → 例外 id=%s（字段 %s）",
        task.id, task.title, date, exception.id, sorted(data),
    )
    return exception


def _split_series(db: Session, task: Task, date: str, data: dict) -> Task:
    """Clone & Override 场景 B（修改此日程及后续）：老系列 UNTIL 截断到被点实例日的
    前一晚，同时新建一条主记录承接被点日起新循环——字段继承老记录、patch 覆盖、
    基准重锚到被点实例日（钟点不变）、规则复制老规则但去掉 UNTIL 尾（新系列从被点
    日重新开放）。返回新主记录。"""
    y, m, d = (int(x) for x in date.split("-"))
    until_day = datetime(y, m, d) - timedelta(days=1)
    old_rule = task.recurrence_rule
    task.recurrence_rule = _with_until(old_rule, until_day)
    start = _instance_time(task, date)
    duration = (
        task.end_time - task.start_time
        if task.start_time is not None and task.end_time is not None
        else None
    )
    new = Task(
        title=task.title,
        priority=task.priority,
        project_id=task.project_id,
        duration_mins=task.duration_mins,
        notes=task.notes,
        start_time=start,
        end_time=start + duration if duration is not None else None,
        all_day=task.all_day,  # 全天系列拆分继承占天语义（2026-11-06 批 B）
        recurrence_rule=old_rule.split(";UNTIL=")[0],
        sort_order=task.sort_order,
        ever_scheduled=True,
    )
    db.add(new)
    db.flush()  # 先落新行拿 id（uuid default 在 flush 时生成），子行迁移要用
    _copy_subtasks(db, task.id, new.id)  # 清单随系列拆分继承（批 C）
    # 被点日（含）之后的墓碑与活体例外改挂新系列：拆分后老系列已被 UNTIL 截断，
    # 子行留在老系列名下则聚合挖空对新系列无效——已删实例复活（墓碑）、例外行与
    # 新系列虚拟实例同日双渲染（活体例外）。改挂后聚合挖空随新系列生效（活体例外
    # 日期同样进 cancelled_dates 挖空虚拟实例、例外行自身照常渲染）；早于被点日的
    # 子行仍属老系列前段，继续生效。
    db.execute(
        sa_update(Task).where(
            Task.parent_id == task.id,
            func.date(Task.original_time) >= date,
        ),
        {"parent_id": new.id},
    )
    for key, value in data.items():
        setattr(new, key, value)
    # 「及后续」跨天改期（2026-11-12 三问批）：新系列日期相对原实例日变化时
    # BYDAY 跟随平移（口径同 mode=all 重锚）
    if (
        new.recurrence_rule is not None
        and "start_time" in data
        and data["start_time"] is not None
        and data["start_time"].date() != start.date()
    ):
        new.recurrence_rule = _shift_weekly_byday(
            new.recurrence_rule, start, data["start_time"]
        )
    # 合并态校验与耗时重算（口径同 all 分支）
    if new.end_time is not None:
        if new.start_time is None:
            raise ValueError("结束时间需要开始时间")
        if new.end_time <= new.start_time:
            raise ValueError("结束时间必须晚于开始时间")
    if new.all_day:
        new.start_time, new.end_time = _normalize_all_day_times(
            new.start_time, new.end_time
        )
        new.duration_mins = None
    elif new.start_time is not None and new.end_time is not None:
        new.duration_mins = round((new.end_time - new.start_time).total_seconds() / 60)
    db.commit()
    db.refresh(new)
    _attach_subtasks(db, [new])  # 响应回填需真实清单（口径同实体化分支）
    logger.info(
        "系列拆分：老系列 id=%s「%s」UNTIL=%s → 新系列 id=%s（字段 %s）",
        task.id, task.title, until_day.date(), new.id, sorted(data),
    )
    return new


def _create_tombstone(db: Session, task: Task, date: str) -> bool:
    """为重复系列建墓碑行（场景 4"仅删除本次"）：parent_id 指向主任务、
    original_time = 实例日 + 主任务开始钟点（无排期基准则当日 0 点）、title 复制、
    is_deleted=True——主任务规则不动，读路径聚合日期挖空该次实例。同日已有墓碑
    则跳过（API 重复调用幂等不叠加）。返回是否新建。"""
    original = _instance_time(task, date)  # 实例日 + 主任务钟点（与例外实体化同锚点）
    existed = db.scalar(
        select(func.count()).select_from(Task).where(
            Task.parent_id == task.id,
            Task.is_deleted.is_(True),
            func.date(Task.original_time) == original.date(),
        )
    )
    if existed:
        return False
    db.add(Task(title=task.title, parent_id=task.id, original_time=original, is_deleted=True))
    return True


def _with_until(rule: str, until_day: datetime) -> str:
    """规则串重拼 UNTIL 截断：去掉已有 UNTIL 尾巴后拼新值（被点实例日的前一晚
    23:59:59——前一日的实例保留、被点日起砍掉）。新 UNTIL 恒早于或等于旧值
    （被点实例日必不晚于旧截断日，否则该实例不存在），覆盖安全。"""
    base = rule.split(";UNTIL=")[0]
    return base + ";UNTIL=" + until_day.strftime("%Y%m%d") + "T235959"


def delete_task(db: Session, task_id: str, *, mode: str = "auto", date: str | None = None) -> bool:
    """删除任务，按模块头的 6 场景判定矩阵执行（生命周期由 ever_scheduled 与
    created_at 判定）。返回 False = 不存在（router 404）；参数不合法抛 ValueError
    （router 422）。"""
    task = _fetch_active(db, task_id)
    if task is None:
        return False
    if mode in ("this", "following"):
        _validate_series_mode(task, mode, date)

    if mode == "this":  # 场景 4：挖空单次实例，主任务与规则不动。
        # 该日已实体化为活体例外（Clone & Override 场景 A 产物）→ 软删该实体行
        # （is_deleted=True 后即墓碑形态，聚合挖空与 purge 保护口径自洽）；无则建墓碑
        exception = _find_exception(db, task.id, _instance_time(task, date))
        if exception is not None:
            exception.is_deleted = True
            exception.deleted_at = datetime.now()
            db.commit()
            logger.info(
                "删除单次实例（已实体化，软删实体行）：任务 id=%s「%s」%s → 例外 id=%s",
                task_id, task.title, date, exception.id,
            )
            return True
        created = _create_tombstone(db, task, date)
        logger.info(
            "删除单次实例：任务 id=%s「%s」%s（%s）",
            task_id, task.title, date, "建墓碑" if created else "已有墓碑，跳过",
        )
        db.commit()
        return True
    if mode == "following":  # 场景 5：UNTIL 截断，被点日之前的实例保留
        y, m, d = (int(x) for x in date.split("-"))
        until_day = datetime(y, m, d) - timedelta(days=1)  # 前一晚（该日实例保留）
        task.recurrence_rule = _with_until(task.recurrence_rule, until_day)
        logger.info("删除此日程及后续：任务 id=%s「%s」UNTIL=%s", task_id, task.title, until_day.date())
        db.commit()
        return True

    # mode == auto / all：整任务删除（非重复场景 1-3 与重复场景 6 共用此段）。
    # 场景 6 按创建时长二分（用户规则："刚建好物理抹除，建久了永久软删"）；
    # 场景 1 叠加"从未排期"约束——刚排期就删属"排期取消"（场景 3），非噪音
    age = (datetime.now() - task.created_at).total_seconds()
    flash = age < FLASH_DELETE_SECONDS and (
        task.recurrence_rule is not None or not task.ever_scheduled
    )
    # 遥测（批 5）：整任务删除记 abandon（this/following 是系列局部操作不记；
    # 闪建闪删是输入噪音不记——只记曾认真存在过的任务被放弃）
    if not flash:
        _record_event(db, task, "abandon", prev_start=task.start_time,
                      planned_min=task.duration_mins)
    if flash:
        db.execute(sa_delete(Task).where(Task.parent_id == task.id))  # 墓碑随主行清
        db.execute(sa_delete(Subtask).where(Subtask.task_id == task.id))  # 清单随主行清（批 C）
        db.delete(task)
        db.commit()
        logger.info("物理删除任务（闪建闪删）：id=%s「%s」", task_id, task.title)
        return True
    # 场景 2（草稿流产，TTL 后清理）与场景 3/6（永久软删）同为软删——是否会被
    # purge 清理由行状态（非重复且从未排期）决定，无需在此分叉
    task.is_deleted = True
    task.deleted_at = datetime.now()
    # 子行随主行连带软删（与闪删分支的物理清子行对齐）：活体例外行是独立已排期
    # 任务，主系列"删除所有"后若不动它会永久留在日历成孤儿；墓碑本就 is_deleted=True，
    # 仅补 deleted_at 无语义变化。purge 的 parent_id IS NULL 条件保证这批子行永不误清。
    db.execute(
        sa_update(Task).where(Task.parent_id == task.id),
        {"is_deleted": True, "deleted_at": datetime.now()},
    )
    db.commit()
    logger.info("软删任务：id=%s「%s」", task_id, task.title)
    return True


# —— 排期智能配套（2026-10-17 增：chat 快录自动落库 + 私人秘书级全盘视野） ——————


def _rule_occurs(rule: str, base: datetime, day: datetime) -> bool:
    """重复规则在 day 当日是否有实例（负荷/忙区展开的近似口径，纯函数）。

    支持 DAILY / WEEKLY(BYDAY) / MONTHLY(日号) 三频 + UNTIL 截断 + 早于基准日
    无实例。后端不做完整 RRULE 展开（那是前端 rrule.ts 的职责），这里只需要
    "该日是否被占用"的布尔近似——口径宁多算占用不漏算（防撞车方向保守）。
    """
    parts = rule.split(";")
    freq = parts[0]
    if freq == "FREQ=DAILY":
        match = True
    elif freq == "FREQ=WEEKLY":
        byday = next((p[len("BYDAY="):] for p in parts if p.startswith("BYDAY=")), "")
        weekdays = {_BYDAY_TO_ISO[x] for x in byday.split(",") if x in _BYDAY_TO_ISO}
        match = not weekdays or day.isoweekday() in weekdays
    elif freq == "FREQ=MONTHLY":
        match = base.day == day.day
    else:
        return False
    if day.date() < base.date():
        return False
    until = next((p[len("UNTIL="):] for p in parts if p.startswith("UNTIL=")), None)
    if until is not None and day.date() > datetime.strptime(until[:8], "%Y%m%d").date():
        return False
    return match


def expand_occurrences(task, lo: datetime, hi: datetime) -> list[datetime]:
    """任务在 [lo, hi) 窗口内的实例起点（纯函数不读库）。

    单次任务：start_time 落窗即一个实例；重复系列：逐日过 _rule_occurs，钟点取
    基准 start_time，cancelled_dates（list_tasks 聚合的墓碑与活体例外日期）当日
    挖空。测试替身 SimpleNamespace（无 recurrence_rule / cancelled_dates 属性）
    经 getattr 自然走单次分支，零适配。
    """
    start = task.start_time
    if start is None:
        return []
    if getattr(task, "recurrence_rule", None) is None:
        return [start] if lo <= start < hi else []
    base = start
    cancelled = set(getattr(task, "cancelled_dates", None) or ())
    occs: list[datetime] = []
    day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < hi:
        if day.date().isoformat() not in cancelled and _rule_occurs(task.recurrence_rule, base, day):
            occs.append(day.replace(hour=base.hour, minute=base.minute))
        day += timedelta(days=1)
    return occs


def _task_dur(task) -> int:
    """任务占用分钟数：duration_mins 优先，回落 end-start，再回落 0（纯函数）。
    全天/跨天行恒 0（2026-11-06 批 B）：占天不占时段——负载/时长统计（load_summary、
    execution_rhythm 均值）不因 24h 跨距被灌满；「占满整天」语义在 busy_intervals
    单独处理。"""
    if getattr(task, "all_day", False):
        return 0
    if task.duration_mins and task.duration_mins > 0:
        return task.duration_mins
    if task.start_time and task.end_time and task.end_time > task.start_time:
        return int((task.end_time - task.start_time).total_seconds() // 60)
    return 0


def _day0(dt: datetime) -> datetime:
    """时刻 → 当日 0 点（窗口计算的公共小件）。"""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def calendar_load_summary(db: Session, *, days_back: int = 1, days_fwd: int = 14) -> list[dict]:
    """按日负荷摘要（chat planner 与晨间 Reduce 共用的"全盘视野"）。

    [今天-days_back, 今天+days_fwd) 每日一行 {date, scheduled_count 条数,
    total_mins 已排分钟, high_count 高强度条数}，无安排的日期也出零行——LLM
    看得见"哪天空"。high 口径同 nodes.task_intensity（时长 ≥90 或高优
    priority≤1；service 层不 import agents，本地同口径实现防环）。重复系列按
    expand_occurrences 展开实例计入。纯读。
    """
    lo = _day0(datetime.now() - timedelta(days=days_back))
    hi = _day0(datetime.now() + timedelta(days=days_fwd))
    days: list[dict] = []
    index: dict[str, dict] = {}
    day = lo
    while day < hi:
        row = {"date": day.date().isoformat(), "scheduled_count": 0,
               "total_mins": 0, "high_count": 0}
        days.append(row)
        index[row["date"]] = row
        day += timedelta(days=1)
    for t in list_tasks(db, start=lo, end=hi):
        dur = _task_dur(t)
        high = dur >= 90 or (t.priority or 3) <= 1
        for occ in expand_occurrences(t, lo, hi):
            row = index[occ.date().isoformat()]
            row["scheduled_count"] += 1
            row["total_mins"] += dur
            if high:
                row["high_count"] += 1
    return days


def execution_rhythm(db: Session, *, window_days: int = 14) -> dict:
    """计划 vs 完成节奏聚合（2026-11-01 优化批 5，纯读零写）：近 window_days 天
    （不含今天——还没过完不计）已排期任务的「实际执行风格」量化——
    recent_done 只是原始清单，这里给出对照口径：

    - done_rate = 勾选完成数 / 已排期数（夜间铁律「已过≠已完成」：只认勾选）；
    - avg_done_mins = 完成任务均时长（样本 <3 给 None 不硬凑——真实产能口径）；
    - top_deferred = reschedule_count 降序 top3（「这件事一直被挤」的清单），
      条目带 priority（统计域「要事霸占拖延榜」行为推断的证据源）。
    planned_count=0 时 rate/avg 均 None，注入端提示词层据实降级。"""
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    lo = day0 - timedelta(days=window_days)
    rows = list(db.scalars(select(Task).where(
        Task.is_deleted.is_(False),
        Task.start_time.is_not(None),
        Task.start_time >= lo, Task.start_time < day0,
    )))
    planned = len(rows)
    done = [t for t in rows if t.done_at is not None]
    durs = [d for t in done if (d := _task_dur(t)) > 0]
    deferred = sorted((t for t in rows if (t.reschedule_count or 0) > 0),
                      key=lambda t: -(t.reschedule_count or 0))[:3]
    return {
        "window_days": window_days,
        "planned_count": planned,
        "done_count": len(done),
        "done_rate": round(len(done) / planned, 2) if planned else None,
        "avg_done_mins": int(sum(durs) / len(durs)) if len(durs) >= 3 else None,
        "top_deferred": [{"title": t.title, "count": t.reschedule_count,
                          "priority": t.priority}
                         for t in deferred],
    }


def busy_intervals(db: Session, *, days_back: int = 1, days_fwd: int = 14) -> list[tuple[datetime, datetime]]:
    """窗口内真实占用区间（单次 + 系列展开实例）——冲突后验 / 推荐空档兜底的
    统一口径。零时长实例（无 duration 无 end）产出零长区间，半开区间比较恒不
    重叠，无害。纯读。"""
    lo = _day0(datetime.now() - timedelta(days=days_back))
    hi = _day0(datetime.now() + timedelta(days=days_fwd))
    out: list[tuple[datetime, datetime]] = []
    for t in list_tasks(db, start=lo, end=hi):
        if getattr(t, "all_day", False):
            # 全天/跨天（2026-11-06 批 B）：实例占满整天（跨距随主行 end-start，
            # 单日 end 空 = 24h）——出差日不宜再排时段任务，排期闸按重叠拒
            span = (
                t.end_time - t.start_time
                if t.start_time is not None and t.end_time is not None
                else timedelta(days=1)
            )
            for occ in expand_occurrences(t, lo, hi):
                out.append((occ, occ + span))
            continue
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            out.append((occ, occ + timedelta(minutes=dur)))
    return out


# 相似标题比对阈值：中文短标题（"体检" vs "年度体检"）0.6 是"同类事项"与
# "碰巧像"的经验分界——与 agents/nodes 查重同口径；提升到 service 层供历史
# 频率查询复用（2026-10-21 排期子图批1）
SIMILAR_TITLE_RATIO = 0.6


def normalize_title(title: str) -> str:
    """标题归一化（相似比对前置）：去首尾空白、剔除标点、转小写——"年度体检！"
    与"年度体检"归一后一致，标点噪声不剔除会把 difflib 相似度拖到阈值下。"""
    return re.sub(r"[\W_]+", "", title or "", flags=re.UNICODE).lower()


def similar_title(title: str,
                  existing: list[tuple[str, datetime | None]]) -> tuple[str, float] | None:
    """在既有条目里找相似标题（返回 (命中标题, 相似度) 或 None）。

    difflib.SequenceMatcher 对归一化串做规则层近似比对——同类事项标题高度
    重合已是强信号，不需要嵌入模型。existing 为 (标题, 时刻) 二元组列表，
    窗口过滤由调用方负责；多命中取相似度最高者。"""
    norm = normalize_title(title)
    if not norm:
        return None
    best: tuple[str, float] | None = None
    for other, _dt in existing:
        ratio = difflib.SequenceMatcher(None, norm, normalize_title(other)).ratio()
        if ratio >= SIMILAR_TITLE_RATIO and (best is None or ratio > best[1]):
            best = (other, ratio)
    return best


def last_similar_occurrence(db: Session, title: str, *,
                            lookback_days: int = 365) -> datetime | None:
    """历史同类任务最近一次执行时刻（频率防呆取数件，2026-10-21 排期子图批1：
    洗牙建议半年一次、体检一年一次——间隔不足时排期子图 Node3 产 frequency
    问询让用户确认，"距上次洗牙仅 2 个月"的拦截依据）。

    纯过去窗口（未来已排的不算"做过"）；重复系列按实例展开，多命中取最近。
    相似判定复用 similar_title 同口径（0.6）。纯读。"""
    now = datetime.now()
    if not normalize_title(title):
        return None
    lo = _day0(now - timedelta(days=lookback_days))
    best: datetime | None = None
    for t in list_tasks(db, start=lo, end=now):
        if similar_title(title, [(t.title, t.start_time)]) is None:
            continue
        for occ in expand_occurrences(t, lo, now):
            if best is None or occ > best:
                best = occ
    return best


def apply_plan_entries(db: Session, entries: list[dict]) -> list[Task]:
    """chat 快录自动落库（壳 _finalize_turn 调用——节点零写库红线不破，写库
    收敛 service 层）。

    逐条：task_id 非空跳过（第一版不自动改排期——系列 this/following 语义
    不可逆，防御性兜底，正常链路不会出现）；start 缺失建未排期待办（问询
    "加待办"的落点）；否则走 create_task 全套校验（TaskCreate：start +
    duration → end，耗时一致性由 create_task 强制重算）。两段式保证原子性：
    先整批校验构体（含 TaskCreate 构造——pydantic ValidationError 也是
    ValueError 子类），任一条非法在零落库时上抛，由调用方原子降级为 draft
    确认卡（create_task 内部逐条 commit，单循环做不到无半批）。条目形状由
    节点防御层保证，这里的异常路径全是纵深防御。priority 可选透传
    （2026-10-22：profiler 按用户明示/记忆画像直判 1，缺省走库默认 2）。
    """
    bodies: list[TaskCreate] = []
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("title"), str) and e["title"].strip()):
            raise ValueError("自动落库条目缺标题")
        if e.get("task_id"):
            continue
        start = None
        raw = e.get("start")
        if isinstance(raw, str) and raw:
            try:
                start = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(f"自动落库条目时间无法解析：{raw}") from exc
            if start.tzinfo is not None:
                raise ValueError("自动落库条目带时区后缀")
        duration = e.get("duration")
        if start is not None and (not isinstance(duration, int) or duration <= 0):
            raise ValueError("自动落库条目 duration 非法")
        priority = e.get("priority")
        priority = priority if isinstance(priority, int) and priority in (1, 2, 3) else None
        # 采集溯源透传（2026-10-23 增）：ingest chat 分支条目如带 source_ref/evidence
        # 自然落列；排期链自产条目无此二键 → None（create_task 同一口径）
        ref = e.get("source_ref")
        ref = ref if isinstance(ref, dict) else {}
        fp = ref.get("fingerprint")
        ev = e.get("evidence")
        bodies.append(TaskCreate(
            title=e["title"].strip(),
            start_time=start,
            end_time=start + timedelta(minutes=duration) if start is not None else None,
            duration_mins=duration if isinstance(duration, int) and duration > 0 else None,
            priority=priority,
            source_key=ref.get("source_key"),
            source_fingerprint=fp if isinstance(fp, str) and fp else None,
            source_evidence=ev if isinstance(ev, str) and ev else None,
        ))
    return [create_task(db, body) for body in bodies]


def purge_expired_soft_deletes(db: Session) -> int:
    """物理清理场景 2（草稿流产）过期软删行：软删超 TTL 且非墓碑、非重复系列、
    从未排期——其余软删行是 AI 学习资产永久保留。返回清理条数。"""
    cutoff = datetime.now() - timedelta(days=SOFT_DELETE_TTL_DAYS)
    result = db.execute(
        sa_delete(Task).where(
            Task.is_deleted.is_(True),
            Task.parent_id.is_(None),  # 墓碑行永生
            Task.recurrence_rule.is_(None),  # 重复系列永久软删
            Task.ever_scheduled.is_(False),  # 排期取消永久软删
            Task.deleted_at < cutoff,
        )
    )
    # 孤儿子任务清理（批 C）：父行已物理删的清单行一并清（软删父任务的子任务
    # 不可见但父行仍在，不受影响）——task_id NOT IN tasks 一条 SQL 幂等
    db.execute(
        sa_delete(Subtask).where(Subtask.task_id.not_in(select(Task.id)))
    )
    db.commit()
    n = result.rowcount or 0
    if n:
        logger.info("清理过期软删任务（草稿流产超 %d 天）：%d 条", SOFT_DELETE_TTL_DAYS, n)
    return n


# —— 排期空档引擎·规则层（2026-10-20 增：时间决策的确定性内核，单点实现。
#    自 agents/nodes.py 提升消除双实现——收件箱一键安排、chat planner、AI 微调
#    三类调用方共用同一套网格/午休/就医闸口径，效果统一） ————————


# 推荐空档的扫描步长（分钟）：对齐到一刻钟，读起来自然
SLOT_STEP_MIN = 15
# 推荐空档最多往后找几天（超出交回用户决定，问询让用户给时间）
SLOT_MAX_DAYS = 7
# 就医类关键词（标题子串命中即"只能上午"）：体检/抽血/验血/空腹常需空腹血糖
# （只有上午做），复诊/挂号随就诊时段——提示词软约束之外的规则层硬闸
MORNING_KEYWORDS = ("体检", "抽血", "验血", "空腹", "复诊", "挂号")
# "上午"的硬边界（分钟）：结束时刻不晚于 12:00
MORNING_END_MIN = 720


class AlreadyScheduledError(ValueError):
    """任务已有排期（start_time 非空）——一键安排端点翻译为 409。"""


class NoSlotError(ValueError):
    """未来 SLOT_MAX_DAYS 天无可排空档——一键安排端点翻译为 422（文案可直显）。"""


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    """两个 [start, end) 时间区间是否重叠（半开区间，首尾相接不算冲突）。"""
    return a_start < b_end and b_start < a_end


def ceil_step(t: datetime, step_min: int) -> datetime:
    """时刻向上对齐到步长整数倍（秒/微秒清零）——推荐空档网格扫描的起点规整。"""
    base = t.replace(second=0, microsecond=0)
    offset = base.hour * 60 + base.minute
    return base.replace(hour=0, minute=0) + timedelta(
        minutes=((offset + step_min - 1) // step_min) * step_min)


def title_morning_only(title: str) -> bool:
    """标题命中就医类关键词 → 该条目的推荐/主荐时间必须落在上午（常识硬闸）。

    关键词子串匹配够用：排期标题是用户原话提炼（"体检"、"年度体检抽血"），
    不需要语义模型；误伤面可控（"复诊"类词几乎不出现在普通工作事项里）。
    """
    return any(k in (title or "") for k in MORNING_KEYWORDS)


def day_busy_minutes(busy: list[tuple[datetime, datetime]]) -> dict[date, int]:
    """busy 区间按天拆分的每日占用分钟数（跨午夜区间拆到两天）——每日工时
    预算闸（suggest_slot / is_valid_slot 的 day_load 参数）的初始负荷口径，
    引擎决策层与落库重验层共用，保证两处判定同源。"""
    loads: dict[date, int] = {}
    for b0, b1 in busy:
        cur = b0
        while cur < b1:
            nxt = cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            seg_end = min(b1, nxt)
            key = cur.date()
            loads[key] = loads.get(key, 0) + int((seg_end - cur).total_seconds() // 60)
            cur = seg_end
    return loads


def _gates_ok(t: datetime, duration: int, busy: list[tuple[datetime, datetime]], *,
              day_start_min: int, cap_min: int, lunch: tuple[int, int] | None,
              day_load: dict[date, int] | None, day_budget: int | None,
              avoid_windows: list[dict] | None = None) -> bool:
    """候选时刻的五道闸（suggest_slot / is_valid_slot / candidate_slots 单点实现，
    2026-10-21 随多候选泛化提取）：落在 [day_start, cap) 可排时段、不进午休、
    日预算（当日已占用非零且加本条超预算即拒——空日豁免防超大任务无处可排）、
    不与 busy 重叠。morning_only 语义由调用方收紧 cap_min（上界 12:00）实现，
    闸本身无感知。

    avoid_windows（2026-10-26 批 5 偏好硬闸）：记忆偏好解析出的避让窗
    （services.preference_slots.parse_avoid_windows 产出——「周五下午别排事」
    「工作日上午不排会」「每月末留白」下沉为引擎硬约束，槽区间与禁窗重叠即
    拒）。2026-11-01 优化批 6 起判定单源调用 preference_slots.hits_window
    （复杂窗形态与跨午夜顺修不再两处复制漂移）。None = 无偏好规则，行为不变。"""
    end = t + timedelta(minutes=duration)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if t < day + timedelta(minutes=day_start_min) or end > day + timedelta(minutes=cap_min):
        return False
    if lunch is not None and overlaps(t, end, day + timedelta(minutes=lunch[0]),
                                      day + timedelta(minutes=lunch[1])):
        return False
    if avoid_windows and hits_window(t, duration, avoid_windows) is not None:
        return False
    if day_budget is not None and day_load is not None:
        used = day_load.get(t.date(), 0)
        if used > 0 and used + duration > day_budget:
            return False
    return not any(overlaps(t, end, b0, b1) for b0, b1 in busy)


def suggest_slot(start: datetime, duration: int, busy: list[tuple[datetime, datetime]],
                 *, day_start_min: int, day_end_min: int,
                 lunch: tuple[int, int] | None,
                 morning_only: bool = False,
                 same_clock_first: bool = True,
                 day_load: dict[date, int] | None = None,
                 day_budget: int | None = None,
                 avoid_windows: list[dict] | None = None) -> datetime | None:
    """确定性找推荐空档（返回候选起点或 None）——统一引擎的规则层。

    候选顺序：①（same_clock_first=True 时）起始日起后续各天同一钟点——"改到
    后天上午"的语感，撞期问询的建议保持这个口径；② 15 分钟网格顺延（当日从
    起始时刻起，之后每天从可排时段 day_start 起）——一键安排语义是"最早优先"，
    传 same_clock_first=False 跳过 ① 直接网格。候选必须落在 [day_start,
    day_end] 内、不与 busy 重叠、不进午休；最多往后找 SLOT_MAX_DAYS 天，找
    不到返回 None。morning_only=True（就医类标题，title_morning_only 判定）时
    上界额外收紧到 12:00 前——体检没有下午做的（2026-10-18 真机教训）。

    每日工时预算（2026-10-20 增，day_budget=work_minutes_per_day 传入）：
    当日已有占用（day_load，含既有 busy 与同批已分配条目）非零且加上本条
    超出当日工时 → 该日不可再堆，网格自然顺延到后续天——批量排期不再全堆
    同一天（真机教训：最早优先会把十条任务全塞进今天）。空日豁免：当日尚无
    占用则放行（超大任务不至于因预算无处可排）。纯函数不读库，AI 微调层
    （engine.plan_slots）与校验层（is_valid_slot）共用同一套时间轴参数。
    """
    cap_min = MORNING_END_MIN if morning_only else day_end_min

    def fits(t: datetime) -> bool:
        return _gates_ok(t, duration, busy, day_start_min=day_start_min, cap_min=cap_min,
                         lunch=lunch, day_load=day_load, day_budget=day_budget,
                         avoid_windows=avoid_windows)

    if same_clock_first:
        for i in range(1, SLOT_MAX_DAYS + 1):  # ① 同钟点跨日优先
            cand = ceil_step(start + timedelta(days=i), SLOT_STEP_MIN)
            if fits(cand):
                return cand
    for i in range(SLOT_MAX_DAYS + 1):  # ② 网格顺延兜底
        day = start + timedelta(days=i)
        day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
        lo = day0 + timedelta(minutes=day_start_min)
        hi = day0 + timedelta(minutes=cap_min)
        t = ceil_step(max(start, lo) if i == 0 else lo, SLOT_STEP_MIN)
        while t + timedelta(minutes=duration) <= hi:
            if fits(t):
                return t
            t += timedelta(minutes=SLOT_STEP_MIN)
    return None


def is_valid_slot(start: datetime, duration: int, busy: list[tuple[datetime, datetime]],
                  *, now: datetime, day_start_min: int, day_end_min: int,
                  lunch: tuple[int, int] | None,
                  morning_only: bool = False,
                  day_load: dict[date, int] | None = None,
                  day_budget: int | None = None,
                  avoid_windows: list[dict] | None = None) -> bool:
    """候选时刻的统一后验闸（AI 微调结果的唯一合法性口径）：naive、不早于
    now、落在可排时段、不进午休、不撞 busy；morning_only（就医类）时结束
    不得晚于 12:00；day_budget（每日工时预算，与 suggest_slot 同口径）下
    当日占用非零且加上本条超预算也非法；avoid_windows（偏好避让窗）重叠
    即拒（同 suggest_slot 口径，批 5）。任一不过返回 False——调用方回落
    规则槽（AI 贴习惯但不可信，规则不聪明但可靠）。"""
    if start.tzinfo is not None or start < now:
        return False
    cap_min = MORNING_END_MIN if morning_only else day_end_min
    return _gates_ok(start, duration, busy, day_start_min=day_start_min, cap_min=cap_min,
                     lunch=lunch, day_load=day_load, day_budget=day_budget,
                     avoid_windows=avoid_windows)


def candidate_slots(start: datetime, duration: int, busy: list[tuple[datetime, datetime]],
                    *, day_start_min: int, day_end_min: int,
                    lunch: tuple[int, int] | None,
                    morning_only: bool = False, same_clock_first: bool = True,
                    day_load: dict[date, int] | None = None,
                    day_budget: int | None = None,
                    allowed_dates: set[date] | None = None,
                    anchor_mins: list[int] | None = None,
                    avoid_windows: list[dict] | None = None,
                    k: int = 5) -> list[datetime]:
    """suggest_slot 的多候选泛化（排期子图 Node3 规则初筛核心件，2026-10-21）：
    同一套五道闸网格扫出至多 k 个可行起点，按扫描序返回即偏好序——不带新筛
    维度时首选恒与 suggest_slot 同值（对拍用例锁定，主荐口径不漂移）。

    两个新筛维度（都由 Node3 按任务画像决定后传入）：
    allowed_dates  日期白名单（政务限工作日 / 户外剔雨天——Node3 先算好日期集），
                   None 不限；
    anchor_mins    垃圾时间锚点（分钟制，低能耗短任务"别占黄金脑力时段"）——
                   每天先从各锚点起网格扫，锚点扫完不足 k 再从时段起点兜底补齐，
                   None 则与 suggest_slot 完全同序。

    候选互为备选（同一任务的替代起点），相互可重叠；批量互斥由调用方逐条定
    槽后回填 busy 实现（引擎层同款口径）。纯函数不读库。"""
    cap_min = MORNING_END_MIN if morning_only else day_end_min
    out: list[datetime] = []

    def push(t: datetime) -> None:
        if (len(out) < k and t not in out
                and (allowed_dates is None or t.date() in allowed_dates)
                and _gates_ok(t, duration, busy, day_start_min=day_start_min,
                              cap_min=cap_min, lunch=lunch, day_load=day_load,
                              day_budget=day_budget, avoid_windows=avoid_windows)):
            out.append(t)

    if same_clock_first:
        for i in range(1, SLOT_MAX_DAYS + 1):  # ① 同钟点跨日优先（与 suggest 同序）
            push(ceil_step(start + timedelta(days=i), SLOT_STEP_MIN))
            if len(out) >= k:
                return out
    for i in range(SLOT_MAX_DAYS + 1):  # ② 网格顺延（锚点优先，时段起点兜底）
        day0 = (start + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        hi = day0 + timedelta(minutes=cap_min)
        lo = day0 + timedelta(minutes=day_start_min)
        floor = ceil_step(max(start, lo) if i == 0 else lo, SLOT_STEP_MIN)
        offsets = [max(a, day_start_min) for a in (anchor_mins or [])] + [day_start_min]
        for off in dict.fromkeys(offsets):  # 去重保序：锚点 clamp 后可能重合
            t = max(ceil_step(day0 + timedelta(minutes=off), SLOT_STEP_MIN), floor)
            while t + timedelta(minutes=duration) <= hi:
                push(t)
                if len(out) >= k:
                    return out
                t += timedelta(minutes=SLOT_STEP_MIN)
    return out


def apply_slot_decisions(db: Session, decisions: list[dict]) -> tuple[list[Task], list[dict]]:
    """引擎时间决策 → 顺序落库（收件箱一键安排的写路径，agents/engine.py 调用；
    节点零写库红线不破，写库收敛 service 层——与 apply_plan_entries 同位）。

    decisions 条目：{"task_id", "title", "slot"}（engine.auto_schedule_run 产出；
    slot 已过 plan_slots 校验层，此处 fresh busy + 每日工时预算重验与决策层
    同口径，只防决策与落库之间的并发写入——非法记 failed 不中断整批）。每落
    一条 busy 即更新，后续条目天然避让已落库占用（整批不重叠的最后一道闸）。
    落库复用 update_task 单一写路径（ever_scheduled 置位 + duration_mins 重算
    不变量）。
    返回 (成功 Task 列表, failed [{"id", "title", "reason"}])。
    """
    from services.settings import get_settings  # 函数内 import 防环（house style）
    s = get_settings(db)
    day_start_min, day_end_min = s.calendar_start_min, s.calendar_end_min
    lunch = (s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None
    # 偏好避让窗（2026-11-03 记忆重构批 4）：落库重验与决策层同口径——旧版
    # 重验不带 avoid_windows，决策与落库之间无并发时口径一致，但决策层
    # （engine.plan_slots 规则层）已消费偏好，此处补齐防微调/直落绕窗
    from services.ai_data import list_memory_rules, rule_label
    from services.preference_slots import parse_avoid_windows

    avoid = parse_avoid_windows([rule_label(r) for r in list_memory_rules(db)])
    # now 秒/微秒清零与决策层（auto_schedule_run / plan_slots）同口径：suggest
    # 可产出恰等于网格点 now 的槽，重验若带秒则 start < now 误拒（每刻钟后
    # 约 1 秒的竞态窗口，2026-10-21 test_auto_schedule 间歇 422 的根因）
    now = datetime.now().replace(second=0, microsecond=0)
    scheduled: list[Task] = []
    failed: list[dict] = []
    for d in decisions:
        task = get_task(db, d["task_id"])
        if task is None or task.start_time is not None:
            failed.append({"id": d["task_id"], "title": d.get("title") or "",
                           "reason": "任务不存在或已排期"})
            continue
        dur = task.duration_mins if task.duration_mins and task.duration_mins > 0 else 60
        slot: datetime | None = d.get("slot")
        fresh = busy_intervals(db, days_back=0, days_fwd=SLOT_MAX_DAYS + 1)
        if slot is not None and not is_valid_slot(
                slot, dur, fresh,
                now=now, day_start_min=day_start_min, day_end_min=day_end_min,
                lunch=lunch, morning_only=title_morning_only(task.title),
                day_load=day_busy_minutes(fresh), day_budget=s.work_minutes_per_day,
                avoid_windows=avoid):
            slot = None  # 决策后被并发写入占掉（正常链路不会出现）→ failed 隔离
        if slot is None:
            failed.append({"id": task.id, "title": task.title,
                           "reason": "未来 7 天没有可用空档"})
            continue
        updated = update_task(db, task.id, TaskUpdate(
            start_time=slot, end_time=slot + timedelta(minutes=dur)))
        if updated is None:  # 纵深防御：并发删除等（正常链路不会出现）
            failed.append({"id": task.id, "title": task.title, "reason": "更新失败"})
            continue
        scheduled.append(updated)
    return scheduled, failed
