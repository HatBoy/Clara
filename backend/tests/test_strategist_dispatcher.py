"""Node4 dispatcher + planner 与主图翻转测试（Track B 批 5，2026-10-21）：
planner 三流向纯函数矩阵（找槽命中/同批占用/reschedule 明日窗+原时长/inbox
降级）+ dispatcher 出口（memory first-wins 与活跃抑制 / preflight 三形态 /
final_reply 附注）+ center_router origin 确定性分支（确认拒绝词零 LLM 直达
coach / 新话题走 LLM 幕僚前缀）+ 图级危机词全链。
"""

import json
from datetime import datetime, timedelta

import agents.strategist.context_fetcher as cf
import agents.strategist.dispatcher as disp
from agents.nodes import center_router
from agents.prompts import ROUTER_PROMPT
from agents.strategist.dispatcher import dispatcher_node
from agents.strategist.planner import plan_actions
from conftest import TestingSessionLocal
from services import ai_data

_NOW = datetime(2026, 10, 21, 20, 0)  # 晚 8 点：今日时段已尽，schedule 首槽必落明日


def _patch_db(monkeypatch):
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)


class _LLMCapture:
    def __init__(self, replies=None, default="{}"):
        self.replies = replies or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        self.calls.append(system)
        for key, reply in self.replies.items():
            if key in system:
                return reply
        return self.default


# —— planner 三流向（纯函数直调）————————————————————————————————————————


def _args(busy=None, known=None):
    return dict(now=_NOW, busy=busy or [], day_start_min=480, day_end_min=1320,
                lunch=(690, 810), day_budget=480, known=known or {})


def test_planner_schedule_hits_and_batch_occupies():
    drafts, inbox = plan_actions([
        {"title": "拆场地", "action": "schedule", "duration": 60},
        {"title": "约供应商", "action": "schedule", "duration": 60},
    ], **_args())
    assert len(drafts) == 2 and not inbox
    assert all(d["task_id"] is None for d in drafts)
    # 晚 8 点 + day_end 22:00：今日余量够 60 分钟，首槽即 20:00（最早优先）
    assert drafts[0]["start"] == "2026-10-21T20:00"
    # 同批占用：第二条顺延 21:00，不与第一条重叠
    assert drafts[1]["start"] == "2026-10-21T21:00"


def test_planner_reschedule_uses_known_duration_from_tomorrow():
    known = {"t1": {"title": "评审会", "duration": 90}}
    drafts, inbox = plan_actions([
        {"title": "LLM 起的别名", "action": "reschedule", "task_id": "t1",
         "duration": 15},  # LLM 估时 15 分钟——不可信，被 known 原时长覆盖
    ], **_args(known=known))
    assert len(drafts) == 1 and not inbox
    d = drafts[0]
    assert d == {"task_id": "t1", "title": "评审会", "start": d["start"],
                 "duration": 90}
    assert d["start"] >= "2026-10-22"  # not_before 明日语义


def test_planner_reschedule_without_snapshot_degrades():
    drafts, inbox = plan_actions([
        {"title": "重排评审", "action": "reschedule"},          # 无 task_id
        {"title": "重排幽灵", "action": "reschedule", "task_id": "ghost"},
    ], **_args(known={"t1": {"title": "有任务", "duration": None}}))
    assert drafts == []
    assert all("未能自动改期" in it["notes"] for it in inbox)


def test_planner_inbox_flow_and_missing_duration():
    drafts, inbox = plan_actions([
        {"title": "想想创意", "action": "inbox"},
        {"title": "没估时的排期", "action": "schedule"},        # 缺 duration → inbox
    ], **_args())
    assert drafts == []
    assert [it["title"] for it in inbox] == ["想想创意", "没估时的排期"]
    assert inbox[0]["notes"] is None and inbox[0]["source_ref"] is None


# —— dispatcher：memory first-wins 与抑制 + preflight 三形态 ———————————————


def test_dispatcher_memory_question_first_wins(monkeypatch, fresh_db):
    _patch_db(monkeypatch)
    out = dispatcher_node({
        "trigger": "chat", "scenario_type": "nightly",
        "strategy_output": "复盘完成。", "checklist": ["晚安"],
        "action_items": [], "deep_context": {}, "now": "2026-10-21T20:00",
        "memory_updates": [
            {"category": "精力节律", "content": "下午3点不宜深度创作",
             "evidence": "近两周三次推迟"},
            {"category": "多提的", "content": "第二条不该出现", "evidence": "x"},
        ]})
    qp = out["question_plan"]
    assert qp["kind"] == "memory" and qp["entries"] == []
    assert qp["rule"]["content"] == "下午3点不宜深度创作"
    assert "同意写入长期记忆吗" in qp["ask_text"]
    assert "第二条不该出现" not in out["final_reply"]  # first-wins 只取第一条
    assert "还有 1 条想确认的习惯" in out["final_reply"]  # 第二条尾注透明化（优化批 1）
    assert "同意写入长期记忆吗" in out["final_reply"]  # 问句拼进回复（cron 承接口径）


def test_dispatcher_memory_suppressed_by_active_pending(monkeypatch, fresh_db):
    _patch_db(monkeypatch)
    with TestingSessionLocal() as db:  # 已有活跃问询 → 静默跳过
        row = ai_data.create_proposal(db, kind="question",
                                      payload={"kind": "memory", "entries": []})
        ai_data.append_message(db, role="assistant", content="问句",
                               proposal_id=row.id)
    out = dispatcher_node({
        "trigger": "chat", "strategy_output": "复盘完成。", "checklist": [],
        "memory_updates": [{"category": "c", "content": "x", "evidence": "e"}]})
    assert "question_plan" not in out
    assert "同意写入长期记忆" not in out["final_reply"]


def test_dispatcher_preflight_three_forms(monkeypatch, fresh_db):
    _patch_db(monkeypatch)
    base = {"action_items": [], "deep_context": {}, "memory_updates": [],
            "strategy_output": "## 今天最要紧的一件事\n正文", "checklist": ["保住上午"]}
    out = dispatcher_node({**base, "trigger": "chat", "scenario_type": "task_breakdown",
                           "now": "2026-10-21T08:00"})
    assert out["preflight"]["headline"] == "今天最要紧的一件事"  # 剥 md 合成
    # 指导形态零卡（2026-11-05 真机批收口）：execution 方法求助的 checklist
    # 已在 reply 正文里，再出简报卡是三件套刷屏——清单即产物的场景才出卡
    out = dispatcher_node({**base, "trigger": "chat", "scenario_type": "execution",
                           "now": "2026-10-21T08:00"})
    assert "preflight" not in out
    out = dispatcher_node({**base, "trigger": "morning", "scenario_type": "morning",
                           "now": "2026-10-21T08:00"})
    assert out["preflight"]["headline"] == "10月21日 晨间启航"
    out = dispatcher_node({**base, "trigger": "nightly", "scenario_type": "nightly",
                           "now": "2026-10-21T20:00"})
    assert out["preflight"]["headline"] == "10月21日 夜间复盘"
    # checklist 空的指导形态零卡（dispatcher 出卡条件）
    out = dispatcher_node({**base, "trigger": "chat", "checklist": []})
    assert "preflight" not in out


def test_dispatcher_direct_schedule_avoids_db_conflicts(monkeypatch, fresh_db):
    """直排 busy 全窗拉库（2026-10-22 真机修复回归）：库里已有任务占满今起
    4 小时 → 草案必须避开——曾用 deep_context 清单（最宽只拉今明两天），
    suggest_slot 扫 7 天窗，后天起撞既有日程零询问。"""
    _patch_db(monkeypatch)
    from models import Task

    now = datetime.now()
    with TestingSessionLocal() as db:
        db.add(Task(id="c1", title="已排占位", start_time=now,
                    end_time=now + timedelta(hours=4)))
        db.commit()
    out = dispatcher_node({
        "trigger": "chat", "scenario_type": "task_breakdown",
        "strategy_output": "拆解完成。", "checklist": [],
        "action_items": [{"title": "写大纲", "action": "schedule", "duration": 60}],
        "memory_updates": [], "deep_context": {}})
    assert out["draft_plan"]
    start = datetime.fromisoformat(out["draft_plan"][0]["start"])
    assert start >= now + timedelta(hours=4)  # 不落进库内既有占用


# —— center_router：origin 确定性分支 —————————————————————————————————————


def _pending(origin="strategist"):
    return {"kind": "memory", "entries": [], "origin": origin,
            "ask_text": "同意写入长期记忆吗？", "proposal_id": "p1"}


def test_router_strategist_pending_ack_direct(monkeypatch):
    cap = _LLMCapture()
    monkeypatch.setattr("services.llm.call_llm", cap)
    for raw in ("同意", "可以", "好的", "嗯嗯", "不用了", "算了"):
        assert center_router(
            {"user_input": raw, "pending": _pending()}) == {"intent": "coach"}
    assert cap.calls == []  # 确认/拒绝词表直达，零 LLM


def test_router_strategist_pending_new_topic_goes_llm(monkeypatch):
    cap = _LLMCapture(replies={
        ROUTER_PROMPT: json.dumps({"intent": "qa", "reason": "查天气"})})
    monkeypatch.setattr("services.llm.call_llm", cap)
    assert center_router({"user_input": "明天天气怎么样", "pending": _pending()})
    assert "（待幕僚长确认的问询：" in cap.calls[0]  # 幕僚前缀强化


def test_router_scheduler_pending_keeps_old_prefix(monkeypatch):
    cap = _LLMCapture(replies={
        ROUTER_PROMPT: json.dumps({"intent": "schedule", "reason": "给时间"})})
    monkeypatch.setattr("services.llm.call_llm", cap)
    center_router({"user_input": "改到周三下午", "pending": _pending("scheduler")})
    assert "（待确认问询：" in cap.calls[0]  # scheduler 轮前缀原样


# —— 批 5（2026-10-26）：直排接引擎——全套闸继承 ————————————————————————


def test_planner_inherits_morning_gate():
    # 教练直排「复诊」经 engine.plan_slots：morning_only 收紧到 12 点前
    drafts, inbox = plan_actions([
        {"title": "医院复诊", "action": "schedule", "duration": 30},
    ], **_args())
    assert drafts and not inbox
    assert datetime.fromisoformat(drafts[0]["start"]).hour < 12  # 次日 08:00


def test_planner_inherits_avoid_window():
    # 偏好避让窗随 rules 进引擎硬筛：周三晚全禁 → 首槽顺延周四 08:00
    drafts, inbox = plan_actions([
        {"title": "拆场地", "action": "schedule", "duration": 60},
    ], rules=["[排期偏好·用户设定] 周三晚上别排事"], **_args())
    assert drafts and drafts[0]["start"] == "2026-10-22T08:00"
