"""QA 维度三扩展：会前准备辅导专项与干系人 SBI 结构（发版前验收批）。

补 d3 缺口（2026-11-23 批 meeting_prep 仅路由分发测过一次、stakeholder 仅断
三档话术管道）。断言口径经读码核实：
- meeting_prep 场景取数键集（strategist/context_fetcher.py:116-121）：today/
  tomorrow/chat_summary/recent_chat/related/weather——日历中的目标会议行经
  related 进大脑视野，找不到会议时场景段方法论要求「如实说明 + 通用框架 +
  指出还差哪个信息」（SCENARIO_SECTIONS.meeting_prep 第四/五条）；
- 会前准备块跨轨委派：action=schedule → dispatcher → planner.plan_actions
  （strategist/planner.py:26，统一走 engine.plan_slots(use_ai=False) 规则直排）
  → draft_plan 草案卡（子图不直写日历）；
- settle 回执链（发版前验收批新增基建对账）：Proposal.settled_count /
  item_ids 属性（models.py:603-609，proposal_decisions 单源 visible_ids）——
  采纳回执数与稳定条目 id 数对账；
- SBI 结构是 stakeholder 场景段的硬性方法论（SCENARIO_SECTIONS.stakeholder
  第一条：Situation/Behavior/Impact + 三档话术 温和提醒/正式提出/升级施压）
  ——段文本被裁剪即结构指导失效，deterministic 层锁方法论在文 + system 段
  实达；话术质量（真模型是否按结构产出）归真实层。
"""

import json
from datetime import datetime

from conftest import TestingSessionLocal
from models import Task
from services import llm

from agents.strategist import strategist_app
from agents.strategist.prompts import SCENARIO_SECTIONS
from qa_helpers import (
    KEY_CLASSIFIER,
    KEY_ROUTER,
    KEY_SCENARIO,
    MultiKeyLLMCapture,
    activate_model,
    chat_events,
    first_of,
    next_weekday,
    patch_strategist_reads,
    settle_draft,
)


def _brain(scenario, strategy="方案如下。", checklist=None, actions=None):
    return json.dumps({"strategy": strategy, "checklist": checklist or [],
                       "action_items": actions or [],
                       "memory_updates": []}, ensure_ascii=False)


def _invoke(monkeypatch, user_input, *, classify, brain_reply):
    replies = {KEY_CLASSIFIER: json.dumps(
        {"scenario": classify[0], "ask": classify[1]}, ensure_ascii=False)}
    if brain_reply is not None:
        replies[KEY_SCENARIO[classify[0]]] = brain_reply
    cap = MultiKeyLLMCapture(replies, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    state = {"user_input": user_input, "memory_rules": [], "pending": None,
             "trigger": "chat"}
    return strategist_app.invoke(state), cap


def _db_task(title, start, end, *, priority=2):
    with TestingSessionLocal() as db:
        t = Task(title=title[:30], start_time=start, end_time=end,
                 duration_mins=int((end - start).total_seconds() // 60),
                 priority=priority)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


# —— 场景：meeting_prep 会议准备辅导 ————————————————————————————————————————————


def test_meeting_prep_prep_block_delegates_to_track_a(monkeypatch):
    """日历有目标会议 → 相关行进大脑视野 → 推演清单 + 预留准备块（schedule）
    → Track A 规则直排定钟点 → 草案卡（确认制，绝不直写日历）。"""
    meeting_day = next_weekday(datetime.now().date(), 2)  # 下周三
    start = datetime(meeting_day.year, meeting_day.month, meeting_day.day, 14, 0)
    _db_task("投资人融资路演", start, start.replace(hour=15), priority=1)
    out, cap = _invoke(
        monkeypatch, "下周三的投资人融资路演帮我准备一下",
        classify=("meeting_prep", None),
        brain_reply=_brain(
            "meeting_prep",
            strategy="锁定目标：拿到投资意向。对方最刁钻的三个问题预演…开场30秒结论先行…材料清单：日历里只有路演行本身，财务数据待补。",
            checklist=["锁定路演目标与底线", "预演三个最刁钻问题",
                       "开场30秒结论先行", "核对材料与数据缺口"],
            actions=[{"title": "路演材料终稿走查", "duration": 60,
                      "action": "schedule"}]))
    assert cap.count(KEY_SCENARIO["meeting_prep"]) == 1
    payload = cap.users(KEY_SCENARIO["meeting_prep"])[0]
    assert "投资人融资路演" in payload  # 目标会议行进大脑上下文（related）
    pre = out.get("preflight")
    assert pre and 3 <= len(pre["checklist"]) <= 6  # 推演清单形态（铁律 3）
    drafts = out.get("draft_plan") or []
    assert len(drafts) == 1
    prep_start = datetime.fromisoformat(drafts[0]["start"])
    assert prep_start < start, f"准备块未落在会议前：{prep_start} vs {start}"
    assert drafts[0]["title"] == "路演材料终稿走查"


def test_meeting_prep_no_meeting_cold_start(monkeypatch):
    """空日历冷启动：brain 收不到任何会议行 → 策略走通用准备框架 + 明说缺哪个
    信息，零 schedule action → 零草案零编造（不虚构会议行）。"""
    out, cap = _invoke(
        monkeypatch, "帮我准备下周和客户的关键对齐会",
        classify=("meeting_prep", None),
        brain_reply=_brain(
            "meeting_prep",
            strategy="日历里没有找到这场对齐会的行，先给通用准备框架：一锁定目标…还差会议的具体时间与参会人，请补充。",
            checklist=["锁定会议要拿到的结果", "预演对方三种反应"],
            actions=[]))
    assert cap.count(KEY_SCENARIO["meeting_prep"]) == 1
    assert not (out.get("draft_plan") or [])  # 零编造零准备块
    assert out.get("preflight")  # 策略卡照常出（辅导本身即产物）
    assert "还差" in out["final_reply"] or "请补充" in out["final_reply"]


# —— 场景：stakeholder SBI 结构 —————————————————————————————————————————————————


def test_stakeholder_sbi_methodology_in_scenario_section():
    """SBI 方法论在场景段单源（Situation/Behavior/Impact + 三档话术三档名），
    段文本被裁剪即结构指导失效。"""
    section = SCENARIO_SECTIONS["stakeholder"]
    assert "SBI" in section
    for word in ("Situation", "Behavior", "Impact"):
        assert word in section, f"SBI 三要素缺 {word}"
    assert "三档话术" in section
    for tier in ("温和提醒", "正式提出", "升级施压"):
        assert tier in section, f"三档话术缺 {tier}"


def test_stakeholder_system_carries_sbi(monkeypatch):
    """live 路径：stakeholder 大脑调用的 system 实际携带 SBI 场景段
    （section 不是死文案，真进每次大脑调用）。"""
    out, cap = _invoke(
        monkeypatch, "怎么跟老板汇报项目延期",
        classify=("stakeholder", None),
        brain_reply=_brain(
            "stakeholder",
            strategy="SBI 定调：事实是里程碑后移两周…三档话术：温和提醒/正式提出/升级施压…"))
    system = cap.systems(KEY_SCENARIO["stakeholder"])[0]
    assert "SBI" in system and "三档话术" in system
    assert not (out.get("draft_plan") or [])  # 话术即产物零草案（d3 口径延续）


# —— chat 全链：会前准备块 settle 回执链（proposal_decisions 基建对账）————————


def test_chat_meeting_prep_settle_receipt_chain(monkeypatch, client):
    """chat SSE → meeting_prep 草案卡 → 两步结算 → Proposal.settled_count 与
    item_ids 对账（新基建回执单源），任务真落日历。"""
    activate_model()
    meeting_day = next_weekday(datetime.now().date(), 2)
    start = datetime(meeting_day.year, meeting_day.month, meeting_day.day, 14, 0)
    meeting_id = _db_task("投资人融资路演", start, start.replace(hour=15), priority=1)
    pre_ids = {row["id"] for row in client.get("/api/tasks").json()}
    assert pre_ids == {meeting_id}
    cap = MultiKeyLLMCapture({
        KEY_ROUTER: '{"intent": "coach", "reason": "会前准备诉求"}',
        KEY_CLASSIFIER: '{"scenario": "meeting_prep", "ask": null}',
        KEY_SCENARIO["meeting_prep"]: _brain(
            "meeting_prep", strategy="锁定目标…",
            checklist=["预演三个刁钻问题", "开场结论先行"],
            actions=[
                {"title": "路演材料终稿走查", "duration": 60, "action": "schedule"},
                {"title": "预演投资人问答", "duration": 45, "action": "schedule"},
            ]),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    events = chat_events(client, "下周三的投资人融资路演帮我准备一下")
    draft = first_of(events, "draft")
    assert draft and draft["proposal_id"] and len(draft["draft_plan"]) == 2
    assert {row["id"] for row in client.get("/api/tasks").json()} == pre_ids  # 确认前零新增
    created = settle_draft(client, draft["proposal_id"], draft["draft_plan"])
    assert len(created) == len({row["id"] for row in created}) == 2
    from services.ai_data import list_proposals

    with TestingSessionLocal() as db:
        row = next(p for p in list_proposals(db) if p.id == draft["proposal_id"])
        ids, settled = row.item_ids, row.settled_count
    assert row.outcome == "accepted"
    assert settled == len(ids) == 2  # 回执数与稳定条目数对账（新基建单源）
