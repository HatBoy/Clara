"""日程提醒调度（2026-09-15 增）：周期扫描 → 幂等入列 → 精确到点触发 → SSE 广播。

架构口径：定时任务收敛边车（与软删清理同例不引 APScheduler——桌面应用重启频繁，
asyncio 循环足够）。时序三段（reminder_loop 每 tick 执行）：
  1) scan_due：扫未来窗口内将触发的提醒，按 (task_id, fire_at) 唯一键幂等入列
  2) claim_due：认领已到点的待发行（DELETE 原子防双发），组事件载荷——启动补账
     走这里；过期超宽限或任务已删/改期的放弃（幽灵提醒只有打扰价值）
  3) 窗口内未到点的行按 fire_at 精确 call_later（"准时"档误差 <1s；_pending
     集合防重叠窗口重复挂钟）

扫描窗口 = 2×tick 重叠防漏。通知形式（悬浮框/系统通知）与提示音的呈现归前端
（SSE 事件驱动），本层只负责"何时发、发什么"。"不提醒"档（lead=None）提示音
仍独立生效：按"准时"（0 提前量）入列，载荷 lead_min=None 表仅声音事件（前端
只播提示音、不弹任何通知）；提示音也关闭时才真正零调度。SSE 断线期间的提醒
即错过（本地回环，断线 ≈ 后端不在，无可提醒），不做回放。
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Project, Reminder, Task
from services.job_rules import is_important_meeting
from services.settings import get_settings

logger = logging.getLogger(__name__)

# 扫描周期 / 扫描窗口（2×tick 重叠防漏）/ 过期宽限（超过即放弃，防启动补发风暴）
REMINDER_TICK_SECONDS = 20
REMINDER_LOOKAHEAD_SECONDS = 40
REMINDER_GRACE_SECONDS = 120

# 任务级提前量覆盖档（2026-10-22 批 2 情境感知）：重要会议 60 / 差旅出行 40 /
# 其余走用户全局偏好档（REMINDER_LEAD_CHOICES 0/5/10/15/30 不动——那是用户
# 的全局默认，这两档是引擎识别"要准备的事"后的抬升覆盖）。层级红线：本模块在
# services 层不 import agents（finder.COMMUTE_MINUTES 在 agents 层），差旅判定
# 用标题正则自持（提醒链路只有标题，画像在裁决器出口被剥离不落库）；会议识别
# 复用 job_rules.is_important_meeting 单源（吃鸭子类型）。取 max：用户全局档
# 更大时尊重用户（更保守的偏好不降级）
LEAD_IMPORTANT_MEETING = 60
LEAD_TRAVEL = 40
_TRAVEL_RE = re.compile(r"高铁|机场|航班|出差|跨城|火车|机票")


def _effective_lead_min(task, base_lead: int | None) -> int | None:
    """任务级有效提前量：base None（"不提醒"仅声音档）恒 None——声音是轻提示
    不做抬升；否则差旅/重要会议逐档取 max 抬升（"高铁上的复盘会"双命中取
    更高的会议档），其余全局档。"""
    if base_lead is None:
        return None
    eff = base_lead
    if _TRAVEL_RE.search(task.title or ""):
        eff = max(eff, LEAD_TRAVEL)
    if is_important_meeting(task):
        eff = max(eff, LEAD_IMPORTANT_MEETING)
    return eff

# RRULE 星期码 → ISO 星期序号（与 tasks._BYDAY_TO_ISO、前端 weekdays 同口径）
_BYDAY_TO_ISO = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}


def _parse_rule(rule: str) -> tuple[str, set[int], date | None]:
    """RRULE → (freq, weekdays, until_day)。只取调度所需三段，与 _validate_series_mode
    / 前端 parseRrule 同口径（其余段忽略）。"""
    parts = rule.split(";")
    freq = parts[0].removeprefix("FREQ=")
    byday = next((p.removeprefix("BYDAY=") for p in parts if p.startswith("BYDAY=")), "")
    weekdays = {_BYDAY_TO_ISO[x] for x in byday.split(",") if x in _BYDAY_TO_ISO}
    until = next((p.removeprefix("UNTIL=") for p in parts if p.startswith("UNTIL=")), None)
    until_day = datetime.strptime(until[:8], "%Y%m%d").date() if until else None
    return freq, weekdays, until_day


def _cancelled_dates(db: Session, task_id: str) -> set[date]:
    """系列的例外日期集合（墓碑 + 活体例外一并，聚合挖空同口径）——这些日的
    虚拟实例不提醒。"""
    rows = db.scalars(select(Task.original_time).where(Task.parent_id == task_id))
    return {r.date() for r in rows if r is not None}


def _series_fire_times(
    task: Task, eff: int, now: datetime, lookahead: float, cancelled: set[date]
) -> list[datetime]:
    """重复系列在自身实例窗口 (now+eff, now+eff+lookahead] 内的提醒时刻。
    窗口宽 ≤ 60s、有效提前量 ≤ 60min（分级上限）→ 实例日期只可能是今天或
    明天，逐日判定足够。2026-10-22 分级重构：窗口由本函数按任务自身 eff 自算
    （旧版共用外层单一 lead 窗口，逐任务分级后各窗不同）。"""
    freq, weekdays, until_day = _parse_rule(task.recurrence_rule)
    base_date = task.start_time.date()
    clock = task.start_time.time()
    out = []
    lo = now + timedelta(minutes=eff)
    hi = lo + timedelta(seconds=lookahead)
    for offset in (0, 1):
        day = (now + timedelta(days=offset)).date()
        if day < base_date or (until_day is not None and day > until_day):
            continue
        if freq == "WEEKLY" and weekdays and day.isoweekday() not in weekdays:
            continue
        if freq == "MONTHLY" and day.day != base_date.day:
            continue
        if day in cancelled:
            continue
        inst = datetime.combine(day, clock)
        if lo < inst <= hi:
            out.append(inst - timedelta(minutes=eff))
    return out


def scan_due(db: Session, now: datetime, lookahead: float = REMINDER_LOOKAHEAD_SECONDS) -> int:
    """扫描提醒窗口（fire_at ∈ (now, now+lookahead]）并幂等入列。2026-10-22 分级
    重构：各任务有效提前量不同（60/40/全局档）→ 查询窗取全档并集（最小档 =
    全局档，任务级只抬升不降；最大档 = max(全局档, 60)）一次捞全候选，逐任务再
    按自身档窄窗判定——外扩窗捞回的小档任务落不进自身窄窗自然跳过。
    "不提醒"（lead=None）且提示音关闭时短路；提示音开启时按 0 提前量入列
    （claim_due 组载荷时标记 lead_min=None 表仅声音，分级不作用于仅声音档）。
    返回入列尝试数。"""
    settings = get_settings(db)
    lead = settings.reminder_lead_min
    if lead is None:
        if settings.sound == "off":
            return 0
        lead = 0
    scan_lo = now + timedelta(minutes=lead)  # 并集下界：最小有效档 = 全局档
    scan_hi = now + timedelta(minutes=max(lead, LEAD_IMPORTANT_MEETING), seconds=lookahead)
    rows: list[tuple[str, datetime]] = []
    # 单次任务（含活体例外行——自带 start_time 被当普通已排期行；软删/未排期/已完成排除）
    for task in db.scalars(
        select(Task).where(
            Task.is_deleted.is_(False),
            Task.done_at.is_(None),
            Task.start_time.is_not(None),
            Task.recurrence_rule.is_(None),
            Task.start_time > scan_lo,
            Task.start_time <= scan_hi,
        )
    ):
        eff = _effective_lead_min(task, lead)
        assert eff is not None  # lead 已归一非 None，纯类型收窄
        lo = now + timedelta(minutes=eff)
        if lo < task.start_time <= lo + timedelta(seconds=lookahead):
            rows.append((task.id, task.start_time - timedelta(minutes=eff)))
    # 重复系列：逐任务枚举（规则只在主行；墓碑行 recurrence 为 NULL 不会进来）。
    # done_at 谓词属防御（V1 系列不支持完成，update_task 拦 422）
    for task in db.scalars(
        select(Task).where(
            Task.is_deleted.is_(False),
            Task.done_at.is_(None),
            Task.recurrence_rule.is_not(None),
            Task.parent_id.is_(None),
            Task.start_time.is_not(None),
        )
    ):
        eff = _effective_lead_min(task, lead)
        assert eff is not None
        cancelled = _cancelled_dates(db, task.id)
        rows.extend((task.id, f) for f in _series_fire_times(task, eff, now, lookahead, cancelled))
    for task_id, fire_at in rows:
        db.execute(
            sqlite_insert(Reminder)
            .values(task_id=task_id, fire_at=fire_at)
            .on_conflict_do_nothing(index_elements=["task_id", "fire_at"])
        )
    db.commit()
    if rows:
        logger.info("提醒扫描：窗口 %s 内 %d 项入列", scan_lo, len(rows))
    return len(rows)


def claim_due(db: Session, now: datetime, grace: float = REMINDER_GRACE_SECONDS) -> list[dict]:
    """认领已到点（fire_at <= now）的待发提醒：DELETE 认领（rowcount 原子防双发），
    返回有效事件载荷列表。放弃路径同样删行：过期超宽限（启动补账防风暴）、任务
    已删/未排期/已完成、单次任务已改期（fire_at 与当前 start−有效提前量 不匹配——
    与 scan_due 分级口径同源，改期后的新时刻由后续扫描重新入列）。载荷 lead_min
    报任务级有效提前量（非全局档）；end_at / recurrence 供前端完成/推迟按钮
    （2026-10-22 批 2）。"""
    events: list[dict] = []
    rows = db.scalars(select(Reminder).where(Reminder.fire_at <= now)).all()
    if not rows:
        return events
    lead = get_settings(db).reminder_lead_min
    # 会前重推标记（2026-10-20 增）：rows 非空才查一次（函数内导入防环——ai_data
    # 反向 import reminders 通道）。有未决 prep 卡的会议，事件附 prep_proposal_id，
    # 前端提醒文案分叉「准备清单已生成」；卡已决/无卡 → 无标记，普通提醒。
    from services.ai_data import pending_prep_by_task
    prep_map = pending_prep_by_task(db)
    for r in rows:
        db.execute(sa_delete(Reminder).where(Reminder.id == r.id))
        if now - r.fire_at > timedelta(seconds=grace):
            continue  # 过期放弃
        task = db.get(Task, r.task_id)
        if task is None or task.is_deleted or task.done_at is not None or task.start_time is None:
            continue  # 幽灵提醒（任务已删/已完成/取消排期）
        eff = _effective_lead_min(task, lead)
        effective_lead = 0 if eff is None else eff  # "不提醒"档队列按准时入列
        if task.recurrence_rule is None and r.fire_at != task.start_time - timedelta(
            minutes=effective_lead
        ):
            continue  # 单次任务已改期或档位漂移（系列重锚属边缘场景，不校验放行）
        project_name = None
        if task.project_id is not None:
            project_name = db.scalar(
                select(Project.name).where(
                    Project.id == task.project_id, Project.is_deleted.is_(False)
                )
            )
        event = {
            "task_id": task.id,
            "title": task.title,
            "start_at": (r.fire_at + timedelta(minutes=effective_lead)).isoformat(),
            "end_at": task.end_time.isoformat() if task.end_time else None,
            "lead_min": eff,  # 任务级有效提前量；None = "不提醒"档仅提示音（前端只播声音）
            "recurrence": bool(task.recurrence_rule),  # 前端据此隐藏"完成"按钮（系列 V1 不支持）
            "project_name": project_name,
        }
        pid = prep_map.get(task.id)
        if pid is not None:
            event["prep_proposal_id"] = pid
        events.append(event)
    db.commit()
    return events


def clear_pending(db: Session) -> int:
    """清空待发队列（提醒提前量变更时调用：fire_at 口径已变，旧队列整体失效，
    下轮扫描按新口径重算）。返回清除行数。表里只有待发行（发完即删），全删即清。"""
    result = db.execute(sa_delete(Reminder))
    db.commit()
    return result.rowcount or 0


# —— SSE 订阅广播（进程内队列；单前端客户端，无持久化无回放） ——


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


_subscribers: list[asyncio.Queue] = []


def broadcast(event: dict) -> None:
    """把提醒事件投给所有在线订阅者（无人在线则丢弃——呈现归前端）。"""
    for q in list(_subscribers):
        q.put_nowait(event)


# —— 调度循环（main.lifespan 启动；测试不启 lifespan 不受影响） ——

# 已挂 call_later 的 reminder id（防重叠窗口重复挂钟；触发/异常即移除）
_pending: set[str] = set()


def _spawn_fire(reminder_id: str) -> None:
    """call_later 的同步壳：清 pending 标记后转协程执行认领 + 广播（claim_due
    按时刻全量认领，同刻其他到点行顺带发出，更稳）。"""
    _pending.discard(reminder_id)
    asyncio.get_running_loop().create_task(_fire())


async def _fire() -> None:
    try:
        with SessionLocal() as db:
            events = claim_due(db, datetime.now())
        for ev in events:
            broadcast(ev)
            logger.info(
                "日程提醒已触发：「%s」开始于 %s（%s）",
                ev["title"], ev["start_at"],
                f"提前 {ev['lead_min']} 分钟" if ev["lead_min"] is not None else "仅提示音",
            )
    except Exception:
        logger.exception("提醒触发失败")


async def reminder_loop() -> None:
    """调度主循环：每 tick 扫描入列 + 补账已过点 + 窗口内精确挂钟。异常不退出
    （下一轮自愈）。"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            now = datetime.now()
            with SessionLocal() as db:
                scan_due(db, now)
                events = claim_due(db, now)
                upcoming = db.execute(
                    select(Reminder.id, Reminder.fire_at).where(
                        Reminder.fire_at > now,
                        Reminder.fire_at <= now + timedelta(seconds=REMINDER_LOOKAHEAD_SECONDS),
                    )
                ).all()
            for ev in events:
                broadcast(ev)
                logger.info(
                    "日程提醒已触发：「%s」开始于 %s（%s）",
                    ev["title"], ev["start_at"],
                    f"提前 {ev['lead_min']} 分钟" if ev["lead_min"] is not None else "仅提示音",
                )
            for rid, fire_at in upcoming:
                if rid in _pending:
                    continue
                _pending.add(rid)
                delay = (fire_at - datetime.now()).total_seconds()
                loop.call_later(delay, _spawn_fire, rid)
        except Exception:
            logger.exception("提醒扫描循环异常")
        await asyncio.sleep(REMINDER_TICK_SECONDS)
