"""Node4 动作分发与记忆固化（批 5 正式化，2026-10-21）。

纯代码编排出口四件（铁律同 scheduler adjudicator「LLM 不产卡片结构」：
LLM 只交意图，每个钟点由 planner 引擎调用产生，headline/ask 由代码模板合成）：

- action_items → planner 规则直排 → draft_plan / inbox_items；
- memory_updates 确认制问询（first-wins：至多第 1 条提案；已有活跃问询
  静默跳过——chat/cron 同口径单点抑制）→ question_plan（kind=memory，
  写库由 classifier 消费轮执行，节点不动库）；
- preflight 卡：cron 轮 headline 确定性合成（「N月N日 晨间启航/夜间复盘」）；
  chat 轮 checklist 非空且 strategy 非空才出卡（headline 由 strategy 首行
  剥 md 合成——保「指名任务出简报卡」UX；brain 垃圾兜底 checklist 恒空，
  兜底轮零卡纯文字）；
- final_reply = strategy 长文 + 产物摘要附注（草案/收件箱/问询引导），
  恒非空（坑 ⑦：晨间无条件落消息）。

读库单点：action/memory 一次 SessionLocal 会话内完成（settings/busy/known/
active_pending）。零 LLM。

busy 权威源（2026-10-22 真机修复）：busy_intervals 全窗拉库（days_fwd=
SLOT_MAX_DAYS+1，与 engine.py 同口径含系列展开）——曾用 deep_context 清单
收集，但场景取数最宽只拉今明两天，suggest_slot 扫 7 天窗口，后天起的已有
日程全盲区：草案直接排进既有日程零询问。deep_context 只保留 known（场景
相关性正是 task_id 白名单的语义）。
"""

import logging
import re
from datetime import datetime

from database import SessionLocal

from .context_fetcher import _state_now
from .planner import plan_actions
from .state import StrategistState

logger = logging.getLogger(__name__)


def dispatcher_node(state: StrategistState) -> dict:
    """分发节点：见模块 docstring。输出九键子集（question_plan/draft_plan/
    inbox_items/preflight/final_reply 五键非空才写）。"""
    from services.ai_data import active_pending
    from services.settings import get_settings
    from services.tasks import SLOT_MAX_DAYS, busy_intervals

    trigger = state.get("trigger") or "chat"
    strategy = (state.get("strategy_output") or "").strip()
    checklist = [c for c in (state.get("checklist") or []) if isinstance(c, str)]
    actions = state.get("action_items") or []
    memories = state.get("memory_updates") or []
    deep_context = state.get("deep_context") or {}
    now = _state_now(state)
    out: dict = {}
    notes: list[str] = []

    with SessionLocal() as db:
        if actions:
            s = get_settings(db)
            busy = busy_intervals(db, days_back=0, days_fwd=SLOT_MAX_DAYS + 1)
            logger.info("[strategist] dispatcher：直排前全窗读库 busy %d 区间", len(busy))
            drafts, inbox = plan_actions(
                actions, now=now, busy=busy,
                day_start_min=s.calendar_start_min, day_end_min=s.calendar_end_min,
                lunch=(s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None,
                day_budget=s.work_minutes_per_day, known=_known_from(deep_context),
                rules=state.get("memory_rules"))
            if drafts:
                out["draft_plan"] = drafts
                notes.append(f"已出 {len(drafts)} 条时间草案，确认后写入日历")
            if inbox:
                out["inbox_items"] = inbox
                notes.append(f"{len(inbox)} 条已放收件箱")
        if memories and active_pending(db) is None:  # 活跃问询抑制（first-wins）
            rule = memories[0]
            ask = f"复盘出一条习惯规则：「{rule['content']}」，同意写入长期记忆吗？"
            out["question_plan"] = {"kind": "memory", "entries": [],
                                    "rule": rule, "ask_text": ask}
            notes.append(ask)
            if len(memories) > 1:  # first-wins 抑制的第二条不再静默丢（优化批 1）
                notes.append(f"还有 {len(memories) - 1} 条想确认的习惯，明晚复盘再问")

    if trigger == "morning":
        out["preflight"] = {"headline": f"{now.month}月{now.day}日 晨间启航",
                            "checklist": checklist}
    elif trigger == "nightly":
        out["preflight"] = {"headline": f"{now.month}月{now.day}日 夜间复盘",
                            "checklist": checklist}
    elif checklist and strategy and state.get("scenario_type") in (
            "task_breakdown", "meeting_prep", "crisis"):
        # chat 轮简报卡（2026-11-05 真机批收口）：只有"清单即产物"的场景出卡
        # （拆解/会前准备/危机重组）；execution（方法求助·指导形态）与
        # stakeholder（话术即产物）的 checklist 已在 reply 正文里，再出简报卡
        # 是三件套刷屏（真机「拖延求助」同轮出指导长文 + 草案 + 简报卡——
        # 曾以 known 快照非空当指名信号，但 deep_context 恒带收件箱任务，
        # 判据永真）。教练双模式批 2 的 UX 落地补全
        out["preflight"] = {"headline": _headline_from(strategy),
                            "checklist": checklist}

    reply = strategy
    if notes:
        reply = (reply + "\n\n" + "；".join(notes)).strip()
    out["final_reply"] = reply or "好。"  # 恒非空（防御位：strategy 与附注全空）
    logger.info("[strategist] dispatcher：出口 %s", "、".join(sorted(out)) or "（空）")
    return out


def _headline_from(strategy: str) -> str:
    """strategy 首行剥 Markdown 记号合成 ≤30 字 headline。"""
    first = next((ln for ln in strategy.splitlines() if ln.strip()), "")
    head = re.sub(r"^[#*\d.\-、\s]+", "", first).replace("*", "").strip()
    return (head or "幕僚长简报")[:30]


def _known_from(deep_context: dict) -> dict[str, dict]:
    """deep_context 已知任务快照 {id: {title, duration}}——reschedule 的标题
    与时长唯一可信源。时长：inbox 条目 duration 键直取；日历条目由 end-start
    推；无时长（related）→ None（planner 走附注降级）。"""
    known: dict[str, dict] = {}
    for v in deep_context.values():
        if isinstance(v, list):
            for e in v:
                if not (isinstance(e, dict) and isinstance(e.get("id"), str)):
                    continue
                dur = e.get("duration")
                if not (isinstance(dur, int) and dur > 0):
                    dur = None
                    if e.get("start") and e.get("end"):
                        try:
                            span = int((datetime.fromisoformat(e["end"])
                                        - datetime.fromisoformat(e["start"]))
                                       .total_seconds() // 60)
                            dur = span if span > 0 else None
                        except ValueError:
                            dur = None
                known[e["id"]] = {"title": e.get("title") or "", "duration": dur}
    return known
