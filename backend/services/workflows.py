"""阶段二定时工作流调度（2026-09-20 增，AI 配套区）：asyncio 事件化三工作流 + 启动补账。

架构口径同 services/reminders.reminder_loop（定时任务收敛边车，不引 APScheduler——
桌面应用重启频繁，asyncio 循环足够）。触发语义 = "不漏"而非"准时"：
- 晨间对齐：now ≥ morning_brief_min 且当日未认领——错过时刻的启动首 tick 自然补账；
- 数据采集提炼：ingest_enabled 开且 now ≥ ingest_brief_min 且当日未认领（补账同晨间；
  默认关——采集打外网 API，用户显式开启）——手写区 run_ingest 从数据接入源拉数据
  提炼任务/日程建议（services/data_ingest.py 采集管道，2026-10-14 增；过载熔断闸
  2026-11-04 移除——采集是数据同步不是排期，见 _run_ingest docstring）；
- 夜间碎片整理：20:00 后且空闲（无进行中日程 + N 分钟无 HTTP 活动），启动宽限期内
  豁免空闲判定（= 补账语义：昨晚没开应用，今早启动即补跑）；
- 脉搏监控：运行期每 3h tick 一次纯规则层负荷检查（无补账语义——不运行时无所谓监控）。

"当日未跑过"的持久化载体 = workflow_runs 表（(name, run_date) 唯一，INSERT ON
CONFLICT 认领，原子防 tick 重叠双发）。谓词与 runner 分离：谓词可纯函数直测，
测试不需要跑 asyncio 循环。

线程隔离铁律：一切 LLM 调用（run_preflight / run_reflection）一律 asyncio.to_thread
（litellm 同步客户端，卡事件循环会拖迟同进程的提醒调度）。手写区入口延迟 import +
容错：未就绪（ImportError / 桩 NotImplementedError）是过渡期常态，日志跳过不崩。
规则层（负荷计算 / 碎片聚合 / 熔断判定）零 LLM，不受 ai_enabled 与零激活影响。
"""

import asyncio
import importlib
import json
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Task, WorkflowRun
from services.ai_data import append_message, create_proposal, expire_stale_proposals
from services.model_providers import get_active_model
from services.reminders import _cancelled_dates, _parse_rule
from services import stats as stats_service
from services.settings import get_settings

logger = logging.getLogger(__name__)

# 主循环节拍（秒）：晨间/夜间为分钟级判定，60s 足够；LLM runner 经 create_task 并出
AGENT_TICK_SECONDS = 60
# 脉搏监控间隔（秒）= 3 小时（PRD §7.3）
HEARTBEAT_INTERVAL_S = 3 * 3600
# 夜间整理的最早触发时刻已设置化（2026-10-20）：settings.nightly_start_min（默认
# 1200=20:00），夜间整理/会前准备/高难协助三流共用此门槛，原硬编码常量退役
# 空闲阈值（秒）= 10 分钟无 HTTP 活动 + 无进行中日程
IDLE_THRESHOLD_S = 10 * 60
# 启动宽限（秒）：窗口内触发夜间整理 = 补账语义（豁免空闲判定，应用刚启动必"空闲"）
BOOT_GRACE_S = 120
# 负荷熔断阈值（负荷率 > 0.8 触发过载提醒；分档 0-50 健康 / 51-80 警戒 / >80 过载）
OVERLOAD_RATIO = 0.8
# 晨间补账截止窗（分钟，2026-10-25 启动收敛）：now ≥ 报点+180 即当日整跳——
# 时间敏感简报过窗即失效，过点补发只剩打扰（口径同 reminders 的过期宽限）
MORNING_CATCHUP_WINDOW_MIN = 180
# 补账静默容差（分钟）：距报点超过此值视为补账轮——准点触发才通知（广播 +
# 渠道推送），补账轮只留痕（卡/消息/认领行照落，无 toast 无提示音无推送）
CATCHUP_SILENCE_TOL_MIN = 10
# 采集多槽顺延偏移（分钟，基于 ingest_brief_min；2026-10-23 频率档位）：2 次→+5h；
# 3 次→+4h/+8h；4 次→+3h/+6h/+9h——末槽 ≈ 首次+9h（默认 09:00 末槽 18:00，先于夜间报点）
_INGEST_SLOT_OFFSETS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (300,),
    3: (240, 480),
    4: (180, 360, 540),
}
# 夜间重要整理（2026-10-26 重写，替代旧碎片块聚合）：单轮预排待办条数上限
NIGHT_PICK_MAX = 5


# —— 空闲检测（main.py 的 HTTP middleware 调 note_activity 刷新活动信号） ——————————


# 最近一次用户活动（任意 HTTP 请求完成）时刻；None = 启动后从未有请求
_last_activity: datetime | None = None
_booted_at: datetime = datetime.now()


def note_activity() -> None:
    """刷新活动时刻（main.py middleware 每请求调用；夜间空闲判定的代理信号）。"""
    global _last_activity
    _last_activity = datetime.now()


def _is_idle(db: Session, now: datetime) -> bool:
    """空闲 = 距最近活动 ≥ 阈值 且 无进行中的单次日程（start ≤ now < coalesce(end,
    start+1h)，与前端卡片几何同口径）。重复系列实例不判——夜间常开系列（如睡眠）
    会让"空闲"永不成立，登记为取舍（docs/tech-debt.md）。"""
    if _last_activity is not None and (now - _last_activity).total_seconds() < IDLE_THRESHOLD_S:
        return False
    busy = db.scalar(
        select(Task.id)
        .where(
            Task.is_deleted.is_(False),
            Task.recurrence_rule.is_(None),
            Task.start_time.is_not(None),
            Task.start_time <= now,
            or_(
                and_(Task.end_time.is_(None), Task.start_time > now - timedelta(hours=1)),
                Task.end_time > now,
            ),
        )
        .limit(1)
    )
    return busy is None


# —— 规则层负荷计算（零 LLM；PRD §7.2 / §8.1） ————————————————————————


def day_load_minutes(db: Session, day: date) -> int:
    """某日已排期总分钟：单次任务（时长 = end−start，无 end 按 1h，与前端卡片几何
    同口径）+ 重复系列当日实例（freq/weekdays/until 判定复用 services.reminders 的
    解析器防口径分叉；取消实例经墓碑集合排除）。"""
    lo = datetime.combine(day, time.min)
    hi = lo + timedelta(days=1)
    total = 0
    for start, end in db.execute(
        select(Task.start_time, Task.end_time).where(
            Task.is_deleted.is_(False),
            Task.start_time.is_not(None),
            Task.recurrence_rule.is_(None),
            Task.start_time >= lo,
            Task.start_time < hi,
        )
    ):
        total += int(((end or start + timedelta(hours=1)) - start).total_seconds() // 60)
    for task in db.scalars(
        select(Task).where(
            Task.is_deleted.is_(False),
            Task.recurrence_rule.is_not(None),
            Task.parent_id.is_(None),
            Task.start_time.is_not(None),
        )
    ):
        freq, weekdays, until_day = _parse_rule(task.recurrence_rule)
        base_date = task.start_time.date()
        if day < base_date or (until_day is not None and day > until_day):
            continue
        if freq == "WEEKLY" and weekdays and day.isoweekday() not in weekdays:
            continue
        if freq == "MONTHLY" and day.day != base_date.day:
            continue
        if day in _cancelled_dates(db, task.id):
            continue
        duration = (task.end_time or task.start_time + timedelta(hours=1)) - task.start_time
        total += int(duration.total_seconds() // 60)
    return total


def load_ratio(db: Session, day: date) -> float:
    """负荷率 = 当日已排期分钟 ÷ work_minutes_per_day（分母 0 防御返回 0.0）。"""
    minutes_per_day = get_settings(db).work_minutes_per_day
    if minutes_per_day <= 0:
        return 0.0
    return day_load_minutes(db, day) / minutes_per_day


# —— 手写区入口容错装载 —————————————————————————————————————


def _load_agent(module_name: str, attr: str):
    """延迟 import 手写区入口（agents.graph.run_preflight / agents.reflection.
    run_reflection）。ImportError / AttributeError → None + 日志（未就绪是过渡期
    常态）；桩函数的 NotImplementedError 由调用点分类处理。"""
    try:
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    except (ImportError, AttributeError):
        logger.info("手写区入口未就绪：%s.%s（跳过对应 AI 步骤）", module_name, attr)
        return None


# —— 触发谓词（判定即认领，副作用落 workflow_runs；纯直测） ————————————————


def _claim_run(db: Session, name: str, day: date) -> bool:
    """认领当日运行权：INSERT ON CONFLICT DO NOTHING（(name, run_date) 唯一键），
    rowcount=1 才真跑——原子防 tick 重叠双发；成功失败都不重试当日。"""
    result = db.execute(
        sqlite_insert(WorkflowRun)
        .values(name=name, run_date=day)
        .on_conflict_do_nothing(index_elements=["name", "run_date"])
    )
    db.commit()
    return result.rowcount == 1


def _claim_morning(db: Session, now: datetime) -> bool:
    """晨间对齐：now ≥ morning_brief_min（设置化，默认 08:30）且当日未认领。
    morning_enabled 关 = 永不认领（2026-10-21 复查批 D1，同 ingest_enabled 语义：
    不落 workflow_runs 行，开关只管这一个流）。启动补账走同一谓词：错过时刻的
    应用启动首 tick 即命中。补账截止窗（2026-10-25 启动收敛）：now ≥ 报点 +
    MORNING_CATCHUP_WINDOW_MIN（默认 +3h）当日整跳不认领不落行——下午开机收到
    「晨间」简报只剩打扰，次日照常跑。"""
    settings = get_settings(db)
    if not settings.morning_enabled:
        return False
    brief_at = datetime.combine(
        now.date(), time(settings.morning_brief_min // 60, settings.morning_brief_min % 60)
    )
    if now < brief_at or now >= brief_at + timedelta(minutes=MORNING_CATCHUP_WINDOW_MIN):
        return False
    return _claim_run(db, "morning", now.date())


def _claim_ingest(db: Session, now: datetime) -> bool:
    """数据采集提炼：开关开 且已到当日某槽位时刻 且该槽未认领。频率档位
    （ingest_runs_per_day，2026-10-23）：首轮 ingest_brief_min（设置化，默认
    09:00），后续槽按 _INGEST_SLOT_OFFSETS 当天顺延；槽位认领键用 name 后缀
    （首轮 "ingest"、后续 "ingest@2/3/4"——零表迁移复用 (name, run_date) 唯一键）。
    开关关 = 永不认领（不落 workflow_runs 行，与 ai_enabled 正交——开关只管这一个流）。
    错过补账语义保留：错过的槽在后续 tick 逐个补领（在途闸串行，后补轮零新
    条目即静默，成本 ≈ 0）。"""
    settings = get_settings(db)
    if not settings.ingest_enabled:
        return False
    offsets = _INGEST_SLOT_OFFSETS.get(settings.ingest_runs_per_day, ())
    for slot, offset in enumerate((0, *offsets)):
        at = datetime.combine(
            now.date(), time(settings.ingest_brief_min // 60, settings.ingest_brief_min % 60)
        ) + timedelta(minutes=offset)
        if now < at:
            continue
        name = "ingest" if slot == 0 else f"ingest@{slot + 1}"
        if _claim_run(db, name, now.date()):
            return True
    return False


def _claim_nightly(db: Session, now: datetime) -> bool:
    """夜间碎片整理：now ≥ nightly_start_min（设置化，默认 20:00）且当日未认领
    且（空闲 或 启动宽限内=补账豁免空闲）。nightly_enabled 关 = 整流停（碎片
    聚合/学习/综述/清理全部随停，2026-10-21 复查批 D1——注册表 desc 同口径）。
    会前准备/高难协助共用此报点门槛但无空闲条件（静默建卡不打扰，各自独立谓词）。"""
    settings = get_settings(db)
    if not settings.nightly_enabled:
        return False
    if now.hour * 60 + now.minute < settings.nightly_start_min:
        return False
    in_boot_grace = (now - _booted_at).total_seconds() <= BOOT_GRACE_S
    if not (in_boot_grace or _is_idle(db, now)):
        return False
    return _claim_run(db, "nightly", now.date())


def _claim_prep(db: Session, now: datetime) -> bool:
    """会前准备：now ≥ nightly_start_min（与夜间整理共用报点门槛）且当日未认领。
    无空闲条件——静默建卡不打扰，用户整晚在用电脑也照跑（不挂 _run_nightly
    尾部的根因：避免「不空闲 → nightly 不跑 → prep 连带不跑」的耦合）。
    prep_enabled 关 = 永不认领（2026-10-21 复查批 D1）。"""
    settings = get_settings(db)
    if not settings.prep_enabled:
        return False
    if now.hour * 60 + now.minute < settings.nightly_start_min:
        return False
    return _claim_run(db, "prep", now.date())


def _claim_hardtask(db: Session, now: datetime) -> bool:
    """高难协助：同会前准备——共用报点门槛、无空闲条件、当日独立认领；
    hardtask_enabled 关 = 永不认领（2026-10-21 复查批 D1）。"""
    settings = get_settings(db)
    if not settings.hardtask_enabled:
        return False
    if now.hour * 60 + now.minute < settings.nightly_start_min:
        return False
    return _claim_run(db, "hardtask", now.date())


def _claim_weekly_report(db: Session, now: datetime) -> bool:
    """周报自动生成（2026-11-07 统计 120 分档）：weekly_report_enabled 开 且
    今日为周日（Python weekday：周一=0 … 周日=6）且 now ≥ nightly_start_min
    （报点复用夜间整理，不新增时刻列）。认领键 (name="weekly_report",
    run_date=当日)——周一谓词不满足即错过作废（用户定案「错过即丢」：周报时间
    不敏感但隔天补发无行动价值，且跨零点 period_range 已锚新一周，次周日照常）。
    补账窗 = weekday 门本身（报点到 24:00，天然宽于晨间 180min）；补账静默由
    runner 内 _catchup_silent 判定。与 nightly_enabled 正交（报告生成不依赖
    夜间整理跑不跑）。"""
    settings = get_settings(db)
    if not settings.weekly_report_enabled:
        return False
    if now.weekday() != 6:
        return False
    if now.hour * 60 + now.minute < settings.nightly_start_min:
        return False
    return _claim_run(db, "weekly_report", now.date())


def _claim_custom_briefs(db: Session, now: datetime) -> list[str]:
    """自定义简报认领：enabled 行逐条判时刻（now ≥ brief_min 且当日未认领），
    各自独立认领互不挤占；错过时刻当日首个到达 tick 补跑一次（同晨间
    "不漏"语义）。返回本轮命中的 brief_id 列表。"""
    from services.jobs import list_briefs

    hit: list[str] = []
    cur = now.hour * 60 + now.minute
    for b in list_briefs(db):
        if not b.enabled or cur < b.brief_min:
            continue
        if _claim_run(db, f"brief:{b.id}", now.date()):
            hit.append(b.id)
    return hit


# —— 会后纪要邀请（2026-11-10 批 3）：事件窗驱动（非报点），每日至多一次 ——————

# 邀请窗口（分钟）：会议结束后 15 分钟起（散会缓冲）至 120 分钟（隔太久再问
# 是翻旧账，宁少勿多）；无时长会议按默认一小时卡口径计 60 分钟
MINUTES_INVITE_DELAY_MIN = 15
MINUTES_INVITE_WINDOW_MIN = 120


def _has_minutes_trace(db: Session, task_id: str) -> bool:
    """该会议已有纪要痕迹 → 不再邀请：minutes 卡（未决或已决，任何状态）或
    已决的邀请卡（未决 invite = 刚邀请过天然互斥，双保险一并算）。近 30 天
    范围（更早的会是陈年旧账）。"""
    from models import Proposal
    from sqlalchemy import select as _sel

    rows = db.scalars(_sel(Proposal).where(
        Proposal.kind.in_(("minutes", "minutes_invite")),
        Proposal.created_at >= datetime.now() - timedelta(days=30)).limit(50))
    for p in rows:
        if (p.payload or {}).get("meeting_task_id") == task_id:
            return True
    return False


def _pick_minutes_candidate(db: Session, now: datetime):
    """今日已结束 15~120 分钟的重要会议候选（识别口径复用 job_rules，含系列
    展开）；多个候选取最近结束的一场（最贴「刚结束」语境）。返回
    (task, ended_at) 或 None。全天日程无结束语义不参与（is_important_meeting
    覆盖不到 all_day 维度，这里显式排掉）。"""
    from services.job_rules import pick_important_meetings
    from services.tasks import _task_dur

    best = None
    for t, occ in pick_important_meetings(db, now.date()):
        if t.all_day:
            continue
        ended = occ + timedelta(minutes=max(_task_dur(t), 60))
        mins_ago = (now - ended).total_seconds() / 60
        if not (MINUTES_INVITE_DELAY_MIN <= mins_ago <= MINUTES_INVITE_WINDOW_MIN):
            continue
        if _has_minutes_trace(db, t.id):
            continue
        if best is None or ended > best[1]:
            best = (t, ended)
    return best


def _claim_minutes_invite(db: Session, now: datetime) -> bool:
    """会后纪要邀请认领（事件窗驱动，非报点）：开关开 且今日有窗口内候选
    （无纪要痕迹）且当日未认领。认领键 minutes_invite（(name, run_date)
    唯一 = 每日至多邀请一次，宁少勿多）。候选在认领前查（无候选不落行，
    同 ingest 槽位先查语义）。"""
    settings = get_settings(db)
    if not settings.minutes_enabled:
        return False
    if _pick_minutes_candidate(db, now) is None:
        return False
    return _claim_run(db, "minutes_invite", now.date())


# 脉搏上次 tick 时刻（模块态计时；无持久化——运行期才监控，重启重新起算）
_last_heartbeat: datetime | None = None


def _claim_heartbeat(now: datetime) -> bool:
    """脉搏监控：距上次 tick ≥ 3h（判定即认领，模块态更新）。

    首跑吃启动宽限（2026-11-04 真机批）：_last_heartbeat 无持久化重启即 None，
    旧版首轮 tick 必认领——应用刚启动页面未稳就弹过载提醒，打扰且无行动价值。
    宽限内（BOOT_GRACE_S，_booted_at 锚）不认领，启动约 2 分钟后首跑——用户
    已在使用中，过载提醒此时出现有时效价值（复用晨间补账「准点通知」哲学）。"""
    global _last_heartbeat
    if (now - _booted_at).total_seconds() <= BOOT_GRACE_S:
        return False
    if _last_heartbeat is not None and (now - _last_heartbeat).total_seconds() < HEARTBEAT_INTERVAL_S:
        return False
    _last_heartbeat = now
    return True


def heartbeat_last_run() -> datetime | None:
    """负荷监控最近一次检查时刻（内存口径，重启重新起算）——jobs 注册表展示用，
    让用户确认"负荷监控真的在跑"（纯规则不落 workflow_runs，日 8 行会撑表）。"""
    return _last_heartbeat


def _catchup_silent(report_min: int, now: datetime | None = None) -> bool:
    """补账轮判定（2026-10-25 启动收敛）：距报点（当日分钟数）> 容差即补账轮。
    原则「准点触发才通知，补账只留痕」——各 run_* 在 broadcast / _fire_push 前判
    本函数，补账轮跳过通知（toast/提示音/渠道推送），建议卡与面板消息照落。
    调用方保证 now ≥ report_min（认领谓词已确保报点已过，无跨天回绕）。"""
    now = now or datetime.now()
    return (now.hour * 60 + now.minute) - report_min > CATCHUP_SILENCE_TOL_MIN


# —— 工作流 runner（LLM 部分一律 to_thread；各自开 SessionLocal） ———————————————

# 渠道推送后台任务池（弱引用防 GC——chat_turn._bg_compact_tasks 先例；done 即自清）
_push_tasks: set[asyncio.Task] = set()


def _fire_push(text: str) -> None:
    """定时任务消息推送渠道（2026-10-23 二批批 4 挂点）：fire-and-forget——
    to_thread 后台跑 push_to_channels，绝不阻塞工作流主流程。空文本丢弃；
    无事件循环（同步直调场景）静默丢弃。chat_channels 惰性导入防环（mcp_session
    先例）。"""
    text = (text or "").strip()
    if not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from services import chat_channels

    task = loop.create_task(
        asyncio.to_thread(chat_channels.push_to_channels, text))
    _push_tasks.add(task)
    task.add_done_callback(_push_tasks.discard)


async def _run_morning() -> None:
    """晨间对齐：任务教练（run_preflight 手写区入口）产出今日预警与防坑指南 →
    preflight 建议卡 + 面板消息 + morning 广播事件。ai_enabled 关 / 零激活 /
    手写区未就绪 → 静默跳过（降级矩阵，PRD §9.1），认领行已落不重试当日。
    补账轮静默（2026-10-25 启动收敛）：跳过广播与渠道推送，卡/消息照落。"""
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("晨间对齐：零激活模型，静默跳过（降级矩阵）")
                return
            report_min = settings.morning_brief_min  # 补账静默判定的报点
            preflight_fn = _load_agent("agents.graph", "run_preflight")
            if preflight_fn is None:
                return
            try:
                result = await asyncio.to_thread(preflight_fn, db, date.today())
            except NotImplementedError:
                logger.info("run_preflight 未实现（手写区），晨间对齐跳过")
                return
        payload = (result or {}).get("preflight") or {}
        reply = (result or {}).get("final_reply") or ""
        with SessionLocal() as db:
            proposal = create_proposal(db, kind="preflight", payload=payload)
            # 问询锚点优先（Track B 批 6）：简报若带 memory 问询，assistant 消息
            # 必须挂问询行——挂 preflight 行会把 active_pending 锚点顶替，问询失活
            anchor = (result or {}).get("question_proposal_id") or proposal.id
            append_message(db, role="assistant", content=reply, proposal_id=anchor)
            if _catchup_silent(report_min):
                logger.info("晨间对齐完成（补账轮静默）：建议卡 %s 不广播", proposal.id)
            else:
                broadcast(
                    {
                        "type": "morning",
                        "headline": payload.get("headline") or reply[:60],
                        "proposal_id": proposal.id,
                    }
                )
                logger.info("晨间对齐完成：建议卡 %s", proposal.id)
                _fire_push(reply)  # 渠道推送（fire-and-forget，reply 即简报全文）
    except Exception:
        logger.exception("晨间对齐工作流异常")


async def _run_prep() -> None:
    """会前准备（2026-10-20 增，「设置-定时任务」配套）：报点后扫描明日重要会议
    （job_rules 纯规则识别，零 LLM），一次 LLM 产出各会议准备清单 → kind="prep"
    建议卡 + 面板消息 + prep 广播；会前提醒经既有 Reminder 通道重推（claim_due
    载荷附 prep_proposal_id）。明日无重要会议 / LLM 全滤空 → 静默零卡零消息。
    ai_enabled 关 / 零激活 → 静默跳过，认领行已落不重试当日（同晨间降级矩阵）。
    补账轮静默（2026-10-25 启动收敛）：跳过广播与渠道推送，卡/消息照落。"""
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("会前准备：零激活模型，静默跳过（降级矩阵）")
                return
            # 低介入档跳过（优化批 5 信任闸）：近 14 天采纳率 <30% 时主动清单
            # 不再生成——明日会议提醒照常走 Reminder 通道（低介入≠静默）
            from services.ai_data import trust_tier

            if trust_tier(db) == "low":
                logger.info("会前准备：低介入档（近14天采纳率<30%），主动清单跳过")
                return
            report_min = settings.nightly_start_min  # 报点（与夜间整理共用门槛）
            prep_fn = _load_agent("agents.graph", "run_meeting_prep")
            if prep_fn is None:
                return
            tomorrow = date.today() + timedelta(days=1)
            result = await asyncio.to_thread(prep_fn, db, tomorrow)
        meetings = (result or {}).get("meetings") or []
        if not meetings:
            logger.info("会前准备：明日无重要会议或全部免备，静默收场")
            return
        reply = (result or {}).get("final_reply") or ""
        with SessionLocal() as db:
            proposal = create_proposal(db, kind="prep", payload={"meetings": meetings})
            append_message(db, role="assistant", content=reply or "明日重要会议准备清单已生成",
                           proposal_id=proposal.id)
            if _catchup_silent(report_min):
                logger.info("会前准备完成（补账轮静默）：%d 场会议，建议卡 %s",
                            len(meetings), proposal.id)
            else:
                broadcast({
                    "type": "prep",
                    "headline": f"明日 {len(meetings)} 场重要会议已备好准备清单",
                    "proposal_id": proposal.id,
                })
                logger.info("会前准备完成：%d 场会议，建议卡 %s", len(meetings), proposal.id)
                _fire_push(reply or "明日重要会议准备清单已生成")  # 与面板消息同文
    except Exception:
        logger.exception("会前准备工作流异常")


async def _run_minutes_invite() -> None:
    """会后纪要邀请（2026-11-10 批 3）：重要会议结束 15~120 分钟窗口内出
    kind=minutes_invite 邀请卡 + 面板消息 + minutes 广播（收件箱轨）。邀请是
    纯规则零 LLM，但补记动作要模型——ai_enabled 关 / 零激活 / 低介入档
    静默跳过（对齐 _run_prep 降级矩阵，防「邀请后补记 503」的断层体验）。
    认领后候选消失（会议被删/改期/已补纪要）静默收场，当日不再试（错过
    即丢，同 prep 语义）。不推渠道——邀请卡的补记是桌面粘贴场景。"""
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("会后纪要邀请：零激活模型，静默跳过（降级矩阵）")
                return
            from services.ai_data import trust_tier

            if trust_tier(db) == "low":
                logger.info("会后纪要邀请：低介入档（近14天采纳率<30%），跳过")
                return
            cand = _pick_minutes_candidate(db, datetime.now())
            if cand is None:
                logger.info("会后纪要邀请：窗口内无候选会议，静默收场")
                return
            t, ended = cand
            proposal = create_proposal(db, kind="minutes_invite", payload={
                "meeting_task_id": t.id,
                "meeting_title": t.title,
                "meeting_start": t.start_time.isoformat() if t.start_time else None,
                "ended_at": ended.isoformat(),
            })
            append_message(db, role="assistant",
                           content=f"《{t.title}》刚结束，要补记纪要吗？粘贴纪要原文即可提炼行动项。",
                           proposal_id=proposal.id)
            broadcast({
                "type": "minutes",
                "headline": f"《{t.title}》刚结束，补记纪要？",
                "proposal_id": proposal.id,
            })
            logger.info("会后纪要邀请已出：会议 %s，邀请卡 %s", t.id, proposal.id)
    except Exception:
        logger.exception("会后纪要邀请工作流异常")


async def _run_hardtask() -> None:
    """高难协助（2026-10-20 增）：报点后规则层挑一件滞留最久的未排期难任务
    （pick_hard_task，零候选零 LLM 静默）→ run_hard_task 产分解步骤 →
    kind="hardtask" 建议卡 + 面板消息 + hardtask 广播；采纳逐条入收件箱
    （前端既有管线）。降级矩阵同会前准备。"""
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("高难协助：零激活模型，静默跳过（降级矩阵）")
                return
            report_min = settings.nightly_start_min  # 报点（与夜间整理共用门槛）
            fn = _load_agent("agents.graph", "run_hard_task")
            if fn is None:
                return
            from services.job_rules import pick_hard_task

            candidate = pick_hard_task(db, datetime.now())
            if candidate is None:
                logger.info("高难协助：无滞留难任务候选，静默收场")
                return
            result = await asyncio.to_thread(fn, db, candidate.id)
        if not result:
            logger.info("高难协助：LLM skip 或解析失败，静默收场")
            return
        with SessionLocal() as db:
            proposal = create_proposal(db, kind="hardtask", payload={
                "task_id": result["task_id"], "title": result["title"],
                "headline": result["headline"], "steps": result["steps"],
                "advice": result["advice"]})
            append_message(db, role="assistant",
                           content=result["headline"], proposal_id=proposal.id)
            if _catchup_silent(report_min):
                logger.info("高难协助完成（补账轮静默）：任务 %s，建议卡 %s",
                            result["task_id"], proposal.id)
            else:
                broadcast({
                    "type": "hardtask",
                    "headline": result["headline"],
                    "proposal_id": proposal.id,
                })
                logger.info("高难协助完成：任务 %s，建议卡 %s", result["task_id"], proposal.id)
                # 渠道推送拼全文（headline + 步骤 + 建议——卡片细节渠道端看不到）
                push_text = "\n".join(
                    [result["headline"]]
                    + [f"{i}. {s}" for i, s in enumerate(result["steps"], 1)]
                    + ([f"💡 {result['advice']}"] if result.get("advice") else [])
                )
                _fire_push(push_text)
    except Exception:
        logger.exception("高难协助工作流异常")


def _report_headline(content: str) -> str:
    """周报广播 headline：取首行【总评】句（约定锚点），失约剥前缀截 60 字兜底。"""
    first = content.splitlines()[0].strip() if content else ""
    if first.startswith("【总评】"):
        first = first[len("【总评】"):].strip()
    return first[:60] or "本周 AI 周报已生成"


async def _run_weekly_report() -> None:
    """周报自动生成（2026-11-07 统计 120 分档）：generate_report("week") →
    StatsReport 留档 + 面板消息全文（morning 先例）+ weekly_report 广播 + 渠道
    推送全文（4000 字截断在 push_to_channels 入口）。不建建议卡——周报是
    Markdown 信息产物无采纳/拒绝语义（overload 直接消息先例）。

    降级矩阵同六流：ai_enabled 关 / 零激活静默跳过——generate_report 自带回落
    模板，但模板周报自动落留档价值弱且污染 stats_reports 列表（同周模板行与
    手动重生成行叠加）；手动端点 POST /api/stats/reports 不查激活照旧回落模板，
    两入口口径有意分叉。补账轮静默：只留痕（留档+消息）不广播不推送。"""
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("周报自动生成：零激活模型，静默跳过（降级矩阵）")
                return
            report_min = settings.nightly_start_min  # 补账静默判定的报点
            # LLM + 全量聚合在 stats_service.generate_report（同步秒级）——铁律 to_thread
            report = await asyncio.to_thread(stats_service.generate_report, db, "week")
            report_id, content = report.id, report.content
            headline = _report_headline(content)
            append_message(db, role="assistant", content=content)
        if _catchup_silent(report_min):
            logger.info("周报自动生成完成（补账轮静默）：报告 %s", report_id)
        else:
            broadcast({
                "type": "weekly_report",
                "headline": headline,
                "report_id": report_id,
            })
            logger.info("周报自动生成完成：报告 %s", report_id)
            _fire_push(content)
    except Exception:
        logger.exception("周报自动生成工作流异常")


async def _run_custom_brief(brief_id: str) -> None:
    """自定义简报（2026-10-20 增）：到点跑用户自设指令的简报 → kind="brief"
    建议卡 + 面板消息 + brief 广播。行已删/停用由 graph 入口静默（停用意图
    优先于补账）；降级矩阵同会前准备。补账轮静默（2026-10-25 启动收敛）。"""
    try:
        with SessionLocal() as db:
            if not get_settings(db).ai_enabled:
                return
            if get_active_model(db) is None:
                logger.info("定时简报 %s：零激活模型，静默跳过", brief_id)
                return
            from services.jobs import list_briefs

            report_min = next(
                (b.brief_min for b in list_briefs(db) if b.id == brief_id), None)
            fn = _load_agent("agents.graph", "run_custom_brief")
            if fn is None:
                return
            result = await asyncio.to_thread(fn, db, brief_id)
        if not result:
            logger.info("定时简报 %s：数据空或解析失败，静默收场", brief_id)
            return
        with SessionLocal() as db:
            proposal = create_proposal(db, kind="brief", payload={
                "brief_id": brief_id,
                "headline": result["headline"], "body": result["body"]})
            append_message(db, role="assistant",
                           content=result["headline"], proposal_id=proposal.id)
            # 行已删（report_min None）走不到这里（graph 入口已静默），判 False 兜底
            if report_min is not None and _catchup_silent(report_min):
                logger.info("定时简报 %s 完成（补账轮静默），建议卡 %s", brief_id, proposal.id)
            else:
                broadcast({
                    "type": "brief",
                    "headline": result["headline"],
                    "proposal_id": proposal.id,
                })
                logger.info("定时简报 %s 完成，建议卡 %s", brief_id, proposal.id)
                _fire_push(f"{result['headline']}\n\n{result['body']}")  # 简报正文全文
    except Exception:
        logger.exception("定时简报 %s 工作流异常", brief_id)


# 数据采集在途闸（M1-⑤a）：手动立即采集与调度 tick 撞车防护。路由与 agent_loop 同在
# 事件循环线程，检查与置位之间无 await，无竞态窗口；try/finally 兜清。
_ingest_in_flight = False


def try_begin_ingest() -> bool:
    """占用在途闸：True = 占用成功可开跑；False = 已有一轮在途（手动触发端点翻 409）。"""
    global _ingest_in_flight
    if _ingest_in_flight:
        return False
    _ingest_in_flight = True
    return True


def _end_ingest() -> None:
    global _ingest_in_flight
    _ingest_in_flight = False


def ingest_in_flight() -> bool:
    """只读窥视在途闸（手动触发端点 409 判定用）。真防重入由 _run_ingest 内部
    try_begin 原子兜底——窥视与 create_task 间的极端窗口最坏=本轮 202 但静默跳过。"""
    return _ingest_in_flight


async def _run_ingest(manual: bool = False) -> None:
    """数据采集提炼：手写区 run_ingest 独立入口产出排期草案 / 收件箱清单 / 综述 →
    schedule 卡（evidence 溯源并入 payload，M1-①）+ inbox 卡（M1-②）+ ingest 广播。
    ai_enabled 关 / 零激活 / 手写区未就绪 → 静默跳过（降级矩阵），认领行
    已落不重试当日。草案与清单都为空不建卡（只广播 headline）。采集建议的
    用户可见视图只有「数据源」页——综述不再落聊天消息（2026-10-25 功能边界：
    原始数据内容物止步数据源页，对话框只留 AI 沟通产物；headline 前 60 字随
    广播作通知文案）。

    manual=True（2026-11-04 真机批）：手动触发（等待型刷新 / 202 立即采集）——
    用户主动点的轮次不套补账静默（_catchup_silent 是给定时补账轮的免打扰语义，
    手动轮距报点再远也照常广播——真机实测 15:30 手动刷新被判静默轮跳过广播，
    直落收件箱的任务前端收不到 ingest 事件不可见，用户以为采集丢了）。

    过载熔断闸已于 2026-11-04 移除（用户拍板，PRD §7.3 偏差修订）：采集是数据
    同步不是排期——汇总到本程序 ≠ 马上排期；trusted 直通的也是用户自己在别处
    创建的日程/待办（同步天经地义），建卡部分纯待审零负担。过载的引导职责归
    heartbeat 提醒（「建议推迟新增安排」），数据同步永不拦（旧版 2026-10-16 按
    PRD §7.3「拦截建议 slot」误把熔断闸挂在了采集入口）。

    自动收录（M2-③）已于 2026-10-24 整体退役（用户决策）：高可信直通（trusted
    源 todo/calendar 域免确认直落）+ 数据源页单条确认已覆盖其价值——inbox 清单恒
    建卡（rest 组）走审阅；schedule 草案恒不自动落库。建卡后统一 mark_items_proposed
    回写台账。

    返回本轮摘要（2026-11-04 手动刷新批）：{skipped, silent, drafts, inbox,
    direct_calendar, direct_inbox}——skipped 携跳过原因键（in_flight/ai_off/
    no_model/not_ready/error；overload 已随熔断闸移除退役），等待型端点
    （POST /run?wait=true）透传给前端做完成反馈；异步调用方（定时 tick /
    202 立即采集）忽略返回值。"""
    def _summary(**kw) -> dict:
        base = {"skipped": None, "silent": False, "drafts": 0, "inbox": 0,
                "direct_calendar": 0, "direct_inbox": 0}
        base.update(kw)
        return base

    if not try_begin_ingest():
        logger.info("数据采集：上一轮仍在途，本次跳过（防重入闸）")
        return _summary(skipped="in_flight")
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if not settings.ai_enabled:
                return _summary(skipped="ai_off")
            if get_active_model(db) is None:
                logger.info("数据采集：零激活模型，静默跳过（降级矩阵）")
                return _summary(skipped="no_model")
            # 补账静默报点 = 最近一个已过槽的时刻（认领谓词保证至少首槽已过；
            # cur 与槽分钟同当日口径无跨天回绕）。default 回落首槽：午夜至首槽
            # 之间直调（测试环境/边界时钟绕过认领谓词）时过滤集为空，旧版
            # max() 空序列直接炸（2026-11-04 跨午夜跑测试实测复现）
            cur = datetime.now().hour * 60 + datetime.now().minute
            slots = [settings.ingest_brief_min + off
                     for off in (0, *_INGEST_SLOT_OFFSETS.get(settings.ingest_runs_per_day, ()))]
            report_min = max((m for m in slots if m <= cur), default=slots[0])
            ingest_fn = _load_agent("agents.graph", "run_ingest")
            if ingest_fn is None:
                return _summary(skipped="not_ready")
            try:
                result = await asyncio.to_thread(ingest_fn, db, date.today())
            except NotImplementedError:
                logger.info("run_ingest 未实现（手写区），数据采集跳过")
                return _summary(skipped="not_ready")
        result = result or {}
        if result.get("silent"):
            # 静默轮（2026-10-23 频率档位配套）：零候选零异常——Reduce 已在入口侧
            # 跳过，这里不落消息不广播（workflow_runs 认领行已落，last_run 可见）
            logger.info("数据采集完成：无新数据静默轮，不落消息不广播")
            return _summary(silent=True)
        draft_plan = result.get("draft_plan") or []
        inbox_items = [it for it in (result.get("inbox_items") or [])
                       if isinstance(it, dict) and it.get("title")]
        evidence = result.get("evidence") or []
        reply = result.get("final_reply") or ""
        if not reply:
            reply = "数据采集完成（无新建议）。"
        with SessionLocal() as db:
            # 局部导入防循环（ai_data/tasks 均已 import settings 链上的模块）
            from schemas import TaskCreate  # 直通 inbox 段 create_task 用
            from services.ai_data import record_proposal_outcome
            from services.data_ingest import (
                mark_items_proposed, payload_refs, split_trusted_direct)
            from services.tasks import apply_plan_entries, create_task

            # —— 高可信直通拆分（2026-10-24）——trusted 源 todo/calendar 域条目
            # 免确认直落（用户定案：结构性数据可信、笔记提炼须人工确认）。
            # inbox 逐条 create_task（auto_apply 同款形态）；draft 走 apply_plan_
            # entries 附 source_ref（溯源三列落地 + 原子性），失败整批并回建卡
            # （fallback 形态已预计算 idx 重排）。直落条目按 accepted 建卡结算——
            # 数据源页不显示（outcome 已决）、台账/采纳率闭环。
            split = split_trusted_direct(db, draft_plan, inbox_items, evidence)
            draft_plan, inbox_items = split["draft_rest"], split["inbox_rest"]
            evidence = split["evidence_rest"]
            direct_parts: list[str] = []
            direct_calendar = direct_inbox = 0
            if split["draft_direct"]:
                try:
                    apply_plan_entries(db, split["draft_direct_entries"])
                except ValueError:
                    # 原子降级：直落整批并回建卡组走确认流程（无部分应用）
                    logger.warning("高可信直通草案落库失败，整批转建卡")
                    draft_plan = split["draft_fallback"]
                    evidence = split["evidence_fallback"]
                else:
                    payload = {"draft_plan": split["draft_direct"], "from_ingest": True,
                               "evidence": split["evidence_direct"]}
                    prop = create_proposal(db, kind="schedule", payload=payload)
                    record_proposal_outcome(db, prop.id, "accepted")
                    mark_items_proposed(db, payload_refs("schedule", payload))
                    direct_parts.append(f"{len(split['draft_direct'])} 条日程直落日历")
                    direct_calendar = len(split["draft_direct"])
            if split["inbox_direct"]:
                clean: list[dict] = []
                for it in split["inbox_direct"]:
                    try:
                        ref = it.get("source_ref")
                        ref = ref if isinstance(ref, dict) else {}
                        fp = ref.get("fingerprint")
                        create_task(db, TaskCreate(
                            title=str(it["title"])[:30],
                            duration_mins=it.get("duration"),
                            notes=it.get("notes") or None,
                            priority=it.get("priority"),
                            source_key=ref.get("source_key"),
                            source_fingerprint=fp if isinstance(fp, str) and fp else None,
                        ))
                        clean.append(it)
                    except Exception:
                        # 单条失败丢直落转建卡（口径同下方 auto_apply 的丢条不弃批反向形态）
                        logger.warning("高可信直通单条失败，转建卡：%r", it.get("title"))
                        inbox_items = inbox_items + [it]
                if clean:
                    prop = create_proposal(db, kind="inbox",
                                           payload={"items": clean, "from_ingest": True})
                    record_proposal_outcome(db, prop.id, "accepted")
                    mark_items_proposed(db, payload_refs("inbox", {"items": clean}))
                    direct_parts.append(f"{len(clean)} 条待办直落收件箱")
                    direct_inbox = len(clean)
            if direct_parts:
                reply += f" 高可信源直通：{'、'.join(direct_parts)}。"

            proposal_id = None
            if draft_plan:
                payload = {"draft_plan": draft_plan, "from_ingest": True}
                if evidence:
                    payload["evidence"] = evidence
                proposal = create_proposal(db, kind="schedule", payload=payload)
                proposal_id = proposal.id
                mark_items_proposed(db, payload_refs("schedule", payload))
            if inbox_items:
                proposal = create_proposal(db, kind="inbox",
                                           payload={"items": inbox_items, "from_ingest": True})
                mark_items_proposed(db, payload_refs("inbox", {"items": inbox_items}))
                if proposal_id is None:  # 双卡并存时消息关联首卡（schedule 优先）
                    proposal_id = proposal.id
            # 不落聊天消息（2026-10-25 功能边界）：综述全文无用户可见处，卡在
            # 数据源页——headline 前 60 字随广播作通知文案即够；补账轮连通知也
            # 静默（徽标计数照常刷新——用户切到数据源页自然看到）；手动轮豁免
            # 静默判定（manual=True，用户主动点的必须给完整反馈）
            if not manual and _catchup_silent(report_min):
                logger.info("数据采集完成（补账轮静默）：建议卡 %s（草案 %d 条，收件箱 %d 条）",
                            proposal_id, len(draft_plan), len(inbox_items))
            else:
                broadcast(
                    {
                        "type": "ingest",
                        "headline": reply[:60],
                        "proposal_id": proposal_id,
                    }
                )
                logger.info("数据采集完成：建议卡 %s（草案 %d 条，收件箱 %d 条）",
                            proposal_id, len(draft_plan), len(inbox_items))
            return _summary(drafts=len(draft_plan), inbox=len(inbox_items),
                            direct_calendar=direct_calendar, direct_inbox=direct_inbox)
    except Exception:
        logger.exception("数据采集工作流异常")
        return _summary(skipped="error")
    finally:
        _end_ingest()


def _collect_priority_plan(db: Session, now: datetime) -> dict | None:
    """夜间重要待办预排（2026-10-26 重写，替代旧碎片块聚合——用户定案：零碎
    小事打包意义不大，夜间该整理的是重要事）。收件箱口径取数（未排期 + 未完成
    + 未删除 + 非子任务；done_at 过滤是旧碎片块漏掉的——已完成任务曾被捞出
    排期），按优先级 1→3（同优先级内 sort_order）挑至多 NIGHT_PICK_MAX 件，
    交统一排期引擎在 7 天窗内找槽（规则硬闸 + 偏好避让 + 日预算；激活模型时
    叠 AI 微调层，未激活纯规则保底——功能不依赖 ai_enabled）。≥2 件有槽才
    出卡（单条不弹、全满不出）；只产草案不写库——点「采纳」前任务保持未排期，
    继续显示在收件箱。"""
    from agents.engine import plan_slots
    from services.ai_data import list_memory_rules
    from services.tasks import (
        SLOT_MAX_DAYS,
        _task_dur,
        busy_intervals,
        calendar_load_summary,
        similar_done_durations,
    )
    from services.weather import forecast_for_city

    tasks = list(db.scalars(
        select(Task).where(
            Task.is_deleted.is_(False),
            Task.start_time.is_(None),
            Task.done_at.is_(None),
            Task.parent_id.is_(None),
        ).order_by(Task.priority, Task.sort_order)
    ))[:NIGHT_PICK_MAX]
    if len(tasks) < 2:
        return None
    s = get_settings(db)
    # 双段注入单源（2026-11-03 批 3 收敛自此处内联格式）——偏好硬闸
    # parse_avoid_windows 消费带分级标签文本；人格不入（草案口吻由引擎承载）
    from services.ai_data import format_memory_lines

    _core, _ref = format_memory_lines(db)
    rules = _core + _ref
    ai_on = bool(s.ai_enabled and get_active_model(db) is not None)
    entries = [{
        "key": t.id, "title": t.title, "priority": t.priority or 2,
        # 未估时先查同类历史均值（优化批 5——旧版一刀切 30 分钟），无记录才落
        # 保守默认 30（重要事项起步）；nightly 预排时长从此贴实际
        "duration": (_task_dur(t) or similar_done_durations(db, t.title) or 30),
    } for t in tasks]
    slots = plan_slots(
        entries=entries,
        busy=busy_intervals(db, days_back=0, days_fwd=SLOT_MAX_DAYS + 1),
        now=now,
        day_start_min=s.calendar_start_min,
        day_end_min=s.calendar_end_min,
        lunch=(s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None,
        load_summary=calendar_load_summary(db),
        weather=forecast_for_city(s.weather_city),
        rules=rules,
        use_ai=ai_on,
        day_budget=s.work_minutes_per_day,
    )
    items = [
        {"task_id": t.id, "title": t.title,
         "start": slots[t.id].isoformat(),
         "duration": entries[i]["duration"],
         "priority": t.priority or 2}
        for i, t in enumerate(tasks) if slots.get(t.id) is not None
    ]
    if len(items) < 2:
        return None
    return {"draft_plan": items}


async def _run_nightly() -> None:
    """夜间重要整理：①优先级预排草案（激活模型时含 AI 微调，未激活纯规则保底
    ——不依赖 ai_enabled）②ai_enabled 时 to_thread 跑习惯学习 + 前夜综述
    （run_preflight 传明日，习惯学习零 LLM 预检从简、综述段零激活静默跳过）
    ③过期未决建议批量置 expired ④nightly 广播。"""
    try:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        proposal_id = None
        pieces = 0
        with SessionLocal() as db:
            settings = get_settings(db)
            # 补账轮静默（2026-10-25 启动收敛）：20 点后启动的应用首 tick 即命中，
            # 广播与渠道推送全跳（三流连发 toast 是启动噪音），卡/消息照落
            silent = _catchup_silent(settings.nightly_start_min, now)
            # 预排可能含 LLM 微调（激活模型时）——to_thread 防阻塞事件循环
            # （agent tick 与 SSE 共用循环）；db 传参先例同下方 reflect_fn
            payload = await asyncio.to_thread(_collect_priority_plan, db, now)
            if payload is not None:
                pieces = len(payload["draft_plan"])
                proposal = create_proposal(db, kind="schedule", payload=payload)
                proposal_id = proposal.id
                append_message(
                    db,
                    role="assistant",
                    content=f"🌙 已按优先级挑出 {pieces} 件未排期待办并预排了时间，点卡片确认后写入日历。",
                    proposal_id=proposal.id,
                )
            if settings.ai_enabled:
                reflect_fn = _load_agent("agents.reflection", "run_reflection")
                if reflect_fn is not None:
                    try:
                        report = await asyncio.to_thread(reflect_fn, db)
                        logger.info("夜间学习完成：%s", report)
                    except NotImplementedError:
                        logger.info("run_reflection 未实现（手写区），本次学习跳过")
                    except Exception:
                        # 学习段异常单独吞（2026-10-21 复查批 C，与下方综述段同口径）：
                        # 反思 LLM 网络/解析故障不得连坐同夜的前夜综述与清理/信任结算
                        logger.exception("夜间学习异常（夜间主流程继续）")
                # 前夜综述：run_preflight 传明日切前夜口吻（与晨间简报同一入口）。
                # 零激活预检在 ai_enabled 之内再加一道——学习段无 LLM 前置需求，
                # 综述段必须真有激活模型才调；异常单独吞，不炸夜间主流程。
                if get_active_model(db) is not None:
                    eve_fn = _load_agent("agents.graph", "run_preflight")
                    if eve_fn is not None:
                        try:
                            eve = await asyncio.to_thread(eve_fn, db, tomorrow)
                            eve_payload = (eve or {}).get("preflight") or {}
                            eve_reply = (eve or {}).get("final_reply") or ""
                            eve_proposal = create_proposal(db, kind="preflight", payload=eve_payload)
                            # 问询锚点优先（Track B 批 6，同晨间）
                            eve_anchor = (eve or {}).get("question_proposal_id") or eve_proposal.id
                            append_message(db, role="assistant",
                                           content=f"🌙 前夜综述：{eve_reply}",
                                           proposal_id=eve_anchor)
                            if not silent:
                                _fire_push(f"🌙 前夜综述：{eve_reply}")  # 与消息同文
                        except NotImplementedError:
                            logger.info("run_preflight 未实现（手写区），前夜综述跳过")
                        except Exception:
                            logger.exception("前夜综述异常（夜间主流程继续）")
            # 数据源页审阅队列（from_ingest 采集卡）留 7 天慢慢整理，其余维持隔夜
            # 过期；采纳率统计双侧剔除 expired，分母口径不受影响
            expired = expire_stale_proposals(db, before=now - timedelta(days=1), ingest=False)
            expired += expire_stale_proposals(db, before=now - timedelta(days=7), ingest=True)
            # M2-①/③ 留存清理与信任降频（纯规则层零 LLM，局部导入防循环同上）
            from services.data_ingest import apply_trust_policy, purge_stale_items

            purge_stale_items(db)
            trust = apply_trust_policy(db)
            if trust["paused"]:
                logger.info("信任降频：%s 采纳率过低，暂停采集至明日 09:00", "、".join(trust["paused"]))
        if silent:
            logger.info("夜间重要整理完成（补账轮静默）：预排 %d 件，过期建议 %d 条", pieces, expired)
        else:
            broadcast(
                {
                    "type": "nightly",
                    "headline": f"夜间重要整理完成：预排 {pieces} 件待办、清理 {expired} 条过期建议",
                    "proposal_id": proposal_id,
                }
            )
            logger.info("夜间重要整理完成：预排 %d 件，过期建议 %d 条", pieces, expired)
    except Exception:
        logger.exception("夜间整理工作流异常")


async def _run_heartbeat() -> None:
    """脉搏监控：今日负荷率 > 80% 熔断阈值 → overload 广播 + AI 推迟建议
    （2026-11-01 优化批 1——PRD §7.3 补全，固定模板升级为秘书口吻建议）。
    headline 事实仍确定性拼装（谎报纪律），AI 只出 advice 字段；零激活/失败
    静默回落纯模板——广播与推送不被 LLM 故障连坐。LLM 调用数：仅过载触发时
    至多 1 次（后台 to_thread 用户零等待；负荷>80% 罕见天然限频）。
    日去重走持久认领（2026-11-04 同日二次修订）：初版模块态重启即失忆——用户
    一天多次启动、当天负荷持续 >80% 时每次开机都再弹再落一条消息（真机实测
    违背「每日一次」预期）；现广播动作经 _claim_run("overload_notify", 当日)
    持久认领（每天至多一行不撑表，六流同机制），3h 检查节拍照旧内存态；只闸
    提醒（2026-11-04 采集熔断闸移除后，heartbeat 是过载引导的唯一载体）。"""
    try:
        with SessionLocal() as db:
            ratio = load_ratio(db, date.today())
        if ratio <= OVERLOAD_RATIO:
            return
        with SessionLocal() as db:
            if not _claim_run(db, "overload_notify", date.today()):
                logger.info("脉搏监控：负荷率 %.2f 仍过载，今日已提醒过跳过（持久日去重）", ratio)
                return
        headline = f"今日负荷 {ratio:.0%}，已越过 80% 熔断线，建议推迟新增安排"
        advice = await _overload_advice(ratio)
        event = {"type": "overload", "headline": headline, "ratio": round(ratio, 2)}
        if advice:
            event["advice"] = advice
        broadcast(event)
        _fire_push(f"⚠️ {headline}" + (f"\n💡 {advice}" if advice else ""))  # 即时预警推渠道
        # AI 建议补落对话框（2026-11-04 真机批，用户拍板）：toast/提醒窗几秒即逝
        # 无留存面——与 10-25「AI 沟通产物留对话框」定案对齐（当时 overload 尚无
        # AI 内容归通知没问题，11-01 加建议后成边界遗漏）；不建卡（纯信息通知，
        # 无采纳/拒绝语义），消息锚点空挂（overload 无 pending 生命周期）
        with SessionLocal() as db:
            append_message(db, role="assistant",
                           content=f"⚠️ {headline}" + (f"\n💡 {advice}" if advice else ""))
        logger.info("脉搏监控：负荷率 %.2f 触发熔断提醒（AI 建议 %s）",
                    ratio, "有" if advice else "无")
    except Exception:
        logger.exception("脉搏监控工作流异常")


async def _overload_advice(ratio: float) -> str | None:
    """过载推迟建议（后台异步单次小调用，优化批 1）：今日清单挑一件可挪/可砍
    的给 ≤80 字秘书建议；零激活或任何异常返回 None 回落纯模板。"""
    from agents.parsing import loads_obj  # 函数内 import（agents 层懒加载先例）

    try:
        with SessionLocal() as db:
            from services.tasks import list_tasks

            day0 = datetime.combine(date.today(), datetime.min.time())
            lines = [f"{t.start_time:%H:%M} {t.title}" if t.start_time else t.title
                     for t in list_tasks(db, start=day0,
                                         end=day0 + timedelta(days=1))][:15]

        def _call() -> str:
            from agents.prompts import HEARTBEAT_ADVICE_PROMPT
            from services.llm import call_llm

            return call_llm(
                [{"role": "system", "content": HEARTBEAT_ADVICE_PROMPT},
                 {"role": "user", "content": json.dumps(
                     {"ratio": round(ratio, 2), "tasks": lines},
                     ensure_ascii=False)}],
                json_mode=True, max_tokens=200)

        out = await asyncio.to_thread(_call)
        data = loads_obj(out)
        text = (data.get("advice") or "").strip() if isinstance(data, dict) else ""
        return text[:80] or None
    except Exception:
        logger.warning("脉搏监控：AI 推迟建议生成失败，回落固定模板", exc_info=True)
        return None


# —— SSE 订阅广播（进程内队列；镜像 services/reminders 同款三件套） —————————————


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


_subscribers: list[asyncio.Queue] = []


def broadcast(event: dict) -> None:
    """把工作流事件投给所有在线订阅者（无人在线则丢弃——呈现归前端）。"""
    for q in list(_subscribers):
        q.put_nowait(event)


# —— 主循环（main.lifespan 启动；异常不退出自愈） ————————————————————————


async def agent_loop() -> None:
    """调度主循环：每 tick 判定认领各工作流，命中即 create_task 并出执行。"""
    while True:
        try:
            now = datetime.now()
            with SessionLocal() as db:
                morning = _claim_morning(db, now)
                ingest = _claim_ingest(db, now)
                nightly = _claim_nightly(db, now)
                prep = _claim_prep(db, now)
                hardtask = _claim_hardtask(db, now)
                weekly = _claim_weekly_report(db, now)
                minutes_invite = _claim_minutes_invite(db, now)
                briefs = _claim_custom_briefs(db, now)
            if morning:
                asyncio.create_task(_run_morning())
            if ingest:
                asyncio.create_task(_run_ingest())
            if nightly:
                asyncio.create_task(_run_nightly())
            if prep:
                asyncio.create_task(_run_prep())
            if hardtask:
                asyncio.create_task(_run_hardtask())
            if minutes_invite:
                asyncio.create_task(_run_minutes_invite())
            if weekly:
                asyncio.create_task(_run_weekly_report())
            for brief_id in briefs:
                asyncio.create_task(_run_custom_brief(brief_id))
            if _claim_heartbeat(now):
                asyncio.create_task(_run_heartbeat())
        except Exception:
            logger.exception("工作流调度循环异常")
        await asyncio.sleep(AGENT_TICK_SECONDS)
