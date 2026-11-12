"""Node2 深度上下文与知识检索（批 3 正式化，2026-10-21）。

纯代码条件取数：按 scenario_type 从七场景矩阵拉弹药组装 deep_context——
键集随场景条件变化（缺键=该维度无数据，Node3 组段时自然降级）。读库走
模块级 SessionLocal 直连（scheduler/intake 同款先例），services 一律函数内
import；ORM 在会话内转 dict，state 只过纯数据。零 LLM。

夜间数据边界：entries 带 done 标（2026-10-22 起 Task 有 done_at 完成列）——
done 只是用户勾选的标记，未勾选的已过日程不能断言未完成（可能只是忘了勾），
「已过≠已完成」纪律由提示词明令；「计划 vs 实际」四源（已过日程含 done +
rescheduled 改期痕迹 + 当日建议结局 + day_user_excerpts）中 rescheduled 自
2026-10-22 起有真实写点（update_task 改期 +1，此前恒空）。干系人场景无
联系人模型——靠任务标题提及 + 对话历史（recent_chat k=10）。

相关检索本地实现：agents/nodes._find_similar 是排期查重口径（阈值 0.6 只认
「同一件事」），教练场景要「话题相关召回」（阈值 0.4 宽召回前 8，LLM 拿
清单自行取舍）——不跨包复用，本地 difflib。池 = 收件箱 ∪ 今日起 7 天窗
已排任务。

教练溯源（2026-10-23 批 3）：task_breakdown/execution/stakeholder 三场景增
source_context 键——相关任务命中里带 Task.source_evidence 的前 3 条静态摘录
（每条截 300）+ 受控单跳探测（仅前两场景：首个带 source_key 的命中任务回源
搜索更多素材，data_ingest.probe_source 复用补采硬闸，每轮至多 1 次）。探测
失败/超帽/无工具一律静默跳过——静态摘录兜底不缺席，教练链路永不因探测降级。
"""

import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from database import SessionLocal

from .state import StrategistState

logger = logging.getLogger(__name__)

# 七场景键表（deep_context 键集单源：代码组段、测试断言、docstring 三方共用）
# chat_summary 随 recent_chat 成对出现（2026-10-26）：有近期对话的场景一并带
# 更早对话滚动摘要——教练链此前丢摘要，跨天语境接不住；晨/夜/危机三场景同批
# 补（morning/nightly 此前"晨报失忆"——用户昨晚聊的决心晨报接不住，crisis
# 重组方案不知危机从何而来）
# related_facts（2026-11-03 记忆重构批 6）为全场景统一键（Tier 2 事实软参考，
# 不入本表——场景特有键才列）
_SCENARIO_KEYS = {
    "task_breakdown": ("today", "tomorrow", "inbox", "load_summary", "related",
                       "source_context", "recent_chat", "chat_summary", "weather"),
    "meeting_prep":   ("today", "tomorrow", "recent_chat", "chat_summary",
                       "related", "weather"),
    "execution":      ("today", "inbox", "load_summary", "recent_chat", "chat_summary",
                       "related", "weather", "source_context"),
    "morning":        ("today", "yesterday_outcomes", "inbox_high", "load_summary",
                       "weather", "recent_chat", "chat_summary", "rhythm",
                       "stalled_items"),
    "nightly":        ("today_past", "tomorrow", "inbox", "load_summary",
                       "today_outcomes", "rescheduled", "day_excerpts",
                       "recent_chat", "chat_summary", "rhythm", "holidays",
                       "tomorrow_pressure", "week_review", "week_rhythm"),
    "crisis":         ("today_rest", "tomorrow", "inbox", "load_summary",
                       "recent_chat", "chat_summary"),
    "stakeholder":    ("inbox", "recent_chat", "chat_summary", "related", "source_context"),
}

_MAX_ENTRIES = 24       # 日历清单上限（系列实例化后）
_MAX_INBOX = 15         # 收件箱快照上限
_MAX_INBOX_HIGH = 5     # 晨间高优收件箱上限（挑硬骨头原料）
_MAX_RELATED = 8        # 相关检索命中上限
_MAX_PROPOSALS = 20     # 建议结局快照上限
_RELATED_FLOOR = 0.4    # 相关召回阈值（宽于排期查重 0.6——召回目标不是判重）
_RELATED_QUERY_CHARS = 40
_CHAT_CHARS = 80        # recent_chat 单条截断（planner 历史同款口径）
_MAX_SOURCE_ITEMS = 3   # source_context 静态摘录条数上限（3×300 自然封顶总量）
_SOURCE_EVIDENCE_CHARS = 300  # 单条素材摘录注入截断（落库 500，注入再收紧）


def context_fetcher_node(state: StrategistState) -> dict:
    """Node2：按场景条件取数 → deep_context（键集见 _SCENARIO_KEYS；scenario
    落白名单外走 execution 形兜底——classifier 白名单 + 钳制后的防御位）。"""
    from services.settings import get_settings
    from services.tasks import calendar_load_summary
    from services.weather import forecast_for_city

    scenario = state["scenario_type"] if state.get("scenario_type") in _SCENARIO_KEYS else "execution"
    now = _state_now(state)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow0 = day0 + timedelta(days=1)
    ctx: dict = {"now": now.isoformat(timespec="minutes")}
    query = state.get("trigger_input") or ""

    with SessionLocal() as db:
        s = get_settings(db)  # 天气城市（三个场景消费；一次轻查询无条件取）
        if scenario == "task_breakdown":
            ctx["today"] = _entries(db, day0, tomorrow0)
            ctx["tomorrow"] = _entries(db, tomorrow0, tomorrow0 + timedelta(days=1))
            ctx["inbox"] = _inbox(db)
            ctx["load_summary"] = calendar_load_summary(db)
            ctx["related"] = _related(db, query, day0)
            ctx["source_context"] = _source_context(db, query, day0, probe=True)
            # 对话/天气补键（2026-11-01 优化批 1）：最常用教练场景此前无衔接
            # 语境——"帮我把刚才聊的那个活动组织一下"接不住、拆户外活动看不见
            # 天气；键集与 execution 场景对齐（meeting_prep 先例）
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
            ctx["weather"] = forecast_for_city(s.weather_city)
        elif scenario == "meeting_prep":
            ctx["today"] = _entries(db, day0, tomorrow0)
            ctx["tomorrow"] = _entries(db, tomorrow0, tomorrow0 + timedelta(days=1))
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
            ctx["related"] = _related(db, query, day0)
            ctx["weather"] = forecast_for_city(s.weather_city)
        elif scenario == "execution":
            ctx["today"] = _entries(db, day0, tomorrow0)
            ctx["inbox"] = _inbox(db)
            ctx["load_summary"] = calendar_load_summary(db)
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
            ctx["related"] = _related(db, query, day0)
            ctx["weather"] = forecast_for_city(s.weather_city)
            ctx["source_context"] = _source_context(db, query, day0, probe=True)
        elif scenario == "morning":
            ctx["today"] = _entries(db, day0, tomorrow0)
            ctx["yesterday_outcomes"] = _outcomes(db, (day0 - timedelta(days=1)).date())
            ctx["inbox_high"] = _inbox(db, cap=_MAX_INBOX_HIGH, high_only=True)
            ctx["load_summary"] = calendar_load_summary(db)
            ctx["weather"] = forecast_for_city(s.weather_city)
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
            # 执行节奏（优化批 5）：晨间建议对照「实际完成率/均时长/反复推迟」
            # ——recent_done 只是原始清单，这里给量化对照
            from services.tasks import execution_rhythm
            ctx["rhythm"] = execution_rhythm(db)
            # 滞留点名（优化批 7）：「排了却不做」的清单——与 hardtask 流互补
            # （那边挑未排期难任务）；空表=无滞留（键恒在保形状稳定）
            from services.job_rules import pick_stalled_tasks

            ctx["stalled_items"] = pick_stalled_tasks(db)
        elif scenario == "nightly":
            # 假日前瞻（优化批 6）：明日是假日/临近假日时前夜综述主动提醒
            # （「明天国庆放假，后天返工」——调休以官方为准不编）
            from services.holidays import upcoming_holidays

            ctx["holidays"] = upcoming_holidays(days=3)
            ctx["today_past"] = _entries(db, day0, now)       # 已过 ≠ 已完成（硬边界）
            ctx["tomorrow"] = _entries(db, tomorrow0, tomorrow0 + timedelta(days=1))
            ctx["inbox"] = _inbox(db)
            ctx["load_summary"] = calendar_load_summary(db)
            ctx["today_outcomes"] = _outcomes(db, day0.date())
            ctx["rescheduled"] = _rescheduled(db, day0, tomorrow0)
            ctx["day_excerpts"] = _day_excerpts(db, day0.date())
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
            from services.tasks import execution_rhythm
            ctx["rhythm"] = execution_rhythm(db)
            # 前夜压力信号（优化批 7）：明日 ≥3 场重要会议 → 综述主动加「今晚
            # 提前梳理」建议段（阈值保守防打扰；不足三场空表，键恒在保形状）
            from services.job_rules import pick_important_meetings

            _tm = pick_important_meetings(db, (day0 + timedelta(days=1)).date())
            ctx["tomorrow_pressure"] = (
                [{"title": t.title, "start": occ.strftime("%H:%M")}
                 for t, occ in _tm[:5]] if len(_tm) >= 3 else [])
            # 周回顾（优化批 7，解禁 6 零新 cron）：周日夜综述追加周回顾段，
            # 数据复用 execution_rhythm 的 7 天口径
            ctx["week_review"] = now.isoweekday() == 7
            ctx["week_rhythm"] = execution_rhythm(db, window_days=7)
        elif scenario == "crisis":
            ctx["today_rest"] = _entries(db, now, tomorrow0)  # 剩余余量的全部原料
            ctx["tomorrow"] = _entries(db, tomorrow0, tomorrow0 + timedelta(days=1))
            ctx["inbox"] = _inbox(db)
            ctx["load_summary"] = calendar_load_summary(db)
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=6)
        else:  # stakeholder
            ctx["inbox"] = _inbox(db)
            ctx["chat_summary"], ctx["recent_chat"] = _recent_chat(db, state, k=10)
            ctx["related"] = _related(db, query, day0)
            ctx["source_context"] = _source_context(db, query, day0, probe=False)
        # Tier 2 相关事实（2026-11-03 记忆重构批 6）：全场景统一带——「我在赶
        # 项目/感冒了」教练收敛建议强度、危机重组知道身体状态；零命中空数组
        from services.episodic import fact_lines, search_facts

        ctx["related_facts"] = fact_lines(search_facts(db, query))
    logger.info("[strategist] context：scenario=%s 取数 %d 键（%s）", scenario,
                len(ctx), "、".join(k for k in ctx if k != "now"))
    return {"deep_context": ctx}


def _state_now(state: StrategistState) -> datetime:
    """读 classifier 写入的 now（整轮时间一致：挂起恢复轮/分支判定同源）；
    缺失或非法回落 datetime.now()（直调测试防御）。"""
    raw = state.get("now")
    try:
        return datetime.fromisoformat(raw) if raw else datetime.now()
    except (TypeError, ValueError):
        return datetime.now()


def _entries(db, lo: datetime, hi: datetime, cap: int = _MAX_ENTRIES) -> list[dict]:
    """窗口内日程实例清单（系列按 expand_occurrences 展开——补「主行基准日
    在窗外」盲区，intake 同款口径）。[{id, title, start, end, done}] 按 start
    排序；id 供 Node4 的 task_id 白名单，done=用户勾选的完成标记（夜间复盘
    「计划 vs 实际」的完成信号，未勾选不断言未完成）。"""
    from services.tasks import _task_dur, expand_occurrences, list_tasks

    out: list[dict] = []
    for t in list_tasks(db, start=lo, end=hi):
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            # priority/notes 补键（2026-10-26 批 4）：排优先级判断与"这条日历
            # 是干嘛的"的语境（截 60，None 省 token）
            out.append({"id": t.id, "title": t.title,
                        "start": occ.isoformat(timespec="minutes"),
                        "end": (occ + timedelta(minutes=dur)).isoformat(timespec="minutes"),
                        "priority": t.priority or 2,
                        "notes": (t.notes or "").strip()[:60] or None,
                        "done": t.done_at is not None})
    out.sort(key=lambda e: e["start"])
    return out[:cap]


def _inbox(db, cap: int = _MAX_INBOX, high_only: bool = False) -> list[dict]:
    """收件箱快照 [{id, title, duration, priority}]；high_only 只留高优
    （priority ≤ 1——晨间挑硬骨头的原料）并按优先级升序再截 cap——建序在前的
    中优条目不得挤出高优大事（run_preflight 复查批 A 口径，收编时承袭）。"""
    from services.tasks import list_tasks

    rows = list_tasks(db, unscheduled=True)
    if high_only:
        rows = sorted((t for t in rows if (t.priority or 2) <= 1),
                      key=lambda t: (t.priority or 2,
                                     getattr(t, "sort_order", 0) or 0))
    return [{"id": t.id, "title": t.title, "duration": t.duration_mins,
             "priority": t.priority or 2} for t in rows[:cap]]


def _related_pool(db, day0: datetime):
    """相关检索共用池（_related 打分与 _source_context 溯源共用，一次语义一次
    查询）：收件箱 ∪ [今日, +8d) 已排任务的 ORM 行。"""
    from services.tasks import list_tasks

    pool = list(list_tasks(db, unscheduled=True))
    pool += list(list_tasks(db, start=day0, end=day0 + timedelta(days=8)))
    return pool


def _related(db, query: str, day0: datetime) -> list[dict]:
    """话题相关任务检索（本地 difflib）：池（_related_pool）归一标题与归一诉求
    比对，ratio ≥ 0.4 按分排序取前 8。"""
    q = _norm(query)[:_RELATED_QUERY_CHARS]
    if not q:
        return []
    scored = []
    for t in _related_pool(db, day0):
        ratio = SequenceMatcher(None, q, _norm(t.title)).ratio()
        if ratio >= _RELATED_FLOOR:
            scored.append({"id": t.id, "title": t.title, "score": round(ratio, 2)})
    scored.sort(key=lambda r: -r["score"])
    return scored[:_MAX_RELATED]


def _source_context(db, query: str, day0: datetime, probe: bool) -> list[dict]:
    """教练溯源上下文（2026-10-23 批 3）：相关任务命中里带采集溯源的素材。

    静态段：按 _related 同口径打分排序，带 Task.source_evidence 的前 3 条 →
    [{task, source_key, evidence}]（每条截 300——落库存 500，注入收紧，3 条
    封顶防上下文撑爆）。

    探测段（probe=True 仅 task_breakdown/execution）：首个带 source_key 的命中
    任务回源搜更多素材（data_ingest.probe_source：补采硬闸全在、每源每日帽、
    本函数每轮至多 1 次），命中 → {"task", "source_key", "probe"}；无工具/
    超帽/失败一律静默跳过（静态段兜底不缺席）。"""
    from services.data_ingest import probe_source

    q = _norm(query)[:_RELATED_QUERY_CHARS]
    if not q:
        return []
    scored = []
    for t in _related_pool(db, day0):
        ratio = SequenceMatcher(None, q, _norm(t.title)).ratio()
        if ratio >= _RELATED_FLOOR:
            scored.append((ratio, t))
    scored.sort(key=lambda p: -p[0])
    rows = [t for _, t in scored]
    out = [{"task": t.title, "source_key": t.source_key,
            "evidence": (t.source_evidence or "")[:_SOURCE_EVIDENCE_CHARS]}
           for t in rows if t.source_evidence][:_MAX_SOURCE_ITEMS]
    if probe:
        # 有界工具循环（2026-11-01 优化批 9，解禁 1 的克制形态）：回源探测
        # 默认只试首个带源任务；coach_probe_deep 开时扩到至多 2 个目标（第二
        # 跳——首个空手时换下一个相关任务再探）。每源每日帽与既有补采共享
        # （probe_source 硬闸全在），失败静默降级静态摘录兜底不缺席
        from services.settings import get_settings

        n_targets = 2 if getattr(
            get_settings(db), "coach_probe_deep", False) else 1
        for target in [t for t in rows if t.source_key][:n_targets]:
            text = probe_source(db, target.source_key, target.title)
            if text:
                out.append({"task": target.title, "source_key": target.source_key,
                            "probe": text})
                break
    return out


def _outcomes(db, day) -> list[dict]:
    """某日建议行结局快照（晨间看昨日采纳 / 夜间看当日全貌）。reject_reason
    补键（2026-10-26 批 4）：拒绝理由是习惯学习的强信号（"没时间"≠"不想要"），
    截 40 字——payload 里没有就是 None。"""
    from services.ai_data import list_proposals

    out = []
    for p in list_proposals(db, day=day)[:_MAX_PROPOSALS]:
        reason = ((p.payload or {}).get("reject_reason") or "").strip()[:40]
        out.append({"kind": p.kind, "outcome": p.outcome,
                    "reject_reason": reason or None})
    return out


def _rescheduled(db, lo: datetime, hi: datetime) -> list[dict]:
    """窗口内改期痕迹（reschedule_count > 0 的任务）——夜间「计划 vs 实际」
    四源之一：被反复挪动是执行受阻的强信号。"""
    from services.tasks import list_tasks

    return [{"title": t.title,
             "start": t.start_time.isoformat(timespec="minutes") if t.start_time else None,
             "reschedule_count": t.reschedule_count or 0}
            for t in list_tasks(db, start=lo, end=hi)
            if (t.reschedule_count or 0) > 0][:_MAX_RELATED]


def _recent_chat(db, state: StrategistState, k: int) -> tuple[str | None, list[dict]]:
    """近期对话 + 更早滚动摘要二元组（会前/执行 k=6、干系人 k=10——干系人
    话术高度依赖前文语境）。exclude 本轮原话（chat_context 同款防重复注入）。"""
    from services.ai_data import chat_context

    summary, msgs = chat_context(db, recent_k=k, exclude=state.get("user_input") or None)
    return summary, [{"role": m.role, "content": (m.content or "")[:_CHAT_CHARS]}
                     for m in msgs]


def _day_excerpts(db, day) -> list[str]:
    """当日用户原话节选（夜间四源之一，四道闸在 ai_data 内）。"""
    from services.ai_data import day_user_excerpts

    return day_user_excerpts(db, day=day)


def _norm(text: str) -> str:
    """标题/诉求归一化（去标点小写）——与 nodes._normalize_title 同口径，但
    本地实现：strategist 不依赖排期链函数（批 7 死代码清理的安全边界）。"""
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).lower()
