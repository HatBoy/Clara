"""Node1 场景嗅探矩阵测试（Track B 批 2，2026-10-21）：短路栈五步逐个覆盖 +
origin 派生（ai_data.active_pending）+ 壳幕僚问询落库（空 entries 放行 +
scenario_type/rule 随行）。classifier 直调不走全图；LLM 替身按 system 子串
分发（「幕僚长·场景嗅探官」/「幕僚长·问询裁决官」）。
"""

from agents.strategist.classifier import classifier_node
import database
from routers.ai_coach import _finalize_turn
from services import ai_data
from services.ai_data import active_pending
from conftest import TestingSessionLocal


class _LLMCapture:
    """按 system 子串分发的 call_llm 替身（scheduler 测试同款口径）。"""

    def __init__(self, replies=None, default="{}"):
        self.replies = replies or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None,
                 on_chunk=None):
        system = messages[0]["content"]
        self.calls.append(system)
        for key, reply in self.replies.items():
            if key in system:
                return reply
        return self.default


def _pending(kind="memory", **extra):
    p = {"proposal_id": "p1", "kind": kind, "entries": [],
         "original_input": "复盘一下今天", "ask_text": "同意写入长期记忆吗？",
         "origin": "strategist"}
    p.update(extra)
    return p


# —— ① cron 直打（零 LLM）———————————————————————————————————————————————


def test_cron_trigger_skips_llm(monkeypatch):
    cap = _LLMCapture()
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = classifier_node({"trigger": "morning", "user_input": ""})
    assert out["scenario_type"] == "morning"
    assert out["trigger_input"] == "morning"
    assert cap.calls == []


# —— ② memory pending 消费 ———————————————————————————————————————————————


def test_memory_agree_writes_rule_and_settles(monkeypatch, fresh_db):
    # classifier 函数内 from database import SessionLocal——每次执行从模块
    # 取属性，patch database.SessionLocal 即可替到测试库（test_tasks.py 先例）
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "agree"}'}))
    pending = _pending(rule={"category": "精力节律", "content": "下午3点不宜深度创作",
                             "evidence": "近两周下午3点任务三次被推迟"})
    out = classifier_node({"user_input": "同意", "pending": pending})
    assert out["final_reply"].startswith("已写入长期记忆")
    assert out["resolved_proposal"] == {"proposal_id": "p1", "outcome": "accepted"}
    with TestingSessionLocal() as db:
        rules = ai_data.list_memory_rules(db)
        hit = [r for r in rules if "深度创作" in r.content]
        assert hit and hit[0].source == "ai_reflect"
        assert hit[0].confidence == 0.5
        assert hit[0].evidence  # 无出处不落库纪律


def test_memory_agree_without_evidence_skips_write(monkeypatch, fresh_db):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "agree"}'}))
    out = classifier_node({"user_input": "写入吧", "pending": _pending(
        rule={"category": "作息", "content": "早睡", "evidence": ""})})
    assert "先不写入" in out["final_reply"]
    assert out["resolved_proposal"]["outcome"] == "accepted"


def test_memory_refuse_settles_ignored(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "refuse"}'}))
    out = classifier_node({"user_input": "不用", "pending": _pending()})
    assert out["resolved_proposal"] == {"proposal_id": "p1", "outcome": "ignored"}
    assert "不写入" in out["final_reply"]


def test_memory_unrelated_falls_through_to_classify(monkeypatch):
    # 答非所问：不结算（pending 生命周期对齐 scheduler），走分类
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "unrelated"}',
         "幕僚长·场景嗅探官": '{"scenario": "crisis", "ask": null}'}))
    out = classifier_node({"user_input": "救命，下午全乱了", "pending": _pending()})
    assert "resolved_proposal" not in out
    assert out["scenario_type"] == "crisis"


# —— ③ clarify pending 消费 ——————————————————————————————————————————————


def test_clarify_agree_merges_and_resumes(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "agree"}'}))
    pending = _pending(kind="strategist_clarify", scenario_type="meeting_prep",
                       ask_text="想先补一个关键信息：是哪场会？")
    out = classifier_node({"user_input": "下周三跟投资人的路演", "pending": pending})
    assert out["trigger_input"] == "复盘一下今天\n（补充）下周三跟投资人的路演"
    assert out["scenario_type"] == "meeting_prep"  # 恢复轮零分类 LLM
    assert out["resolved_proposal"]["outcome"] == "accepted"


def test_clarify_refuse_terminates(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·问询裁决官": '{"action": "refuse"}'}))
    out = classifier_node({"user_input": "算了", "pending": _pending(
        kind="strategist_clarify", scenario_type="task_breakdown")})
    assert out["resolved_proposal"]["outcome"] == "ignored"
    assert "final_reply" in out


def test_clarify_judge_sees_original_request(monkeypatch):
    """恢复轮裁决看见当时诉求（2026-11-01 优化批 1）：挂起 original_input 并入
    裁决输入——补充是否真在回应当时的诉求判定更准（tech-debt 登记的轻量
    改善，提示词层不动状态键）。"""
    cap = _LLMCapture(replies={
        "问询裁决官": '{"action": "unrelated"}',   # 判无关 → 回落分类
        "场景嗅探官": '{"scenario": "task_breakdown", "ask": null}',
    })
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = classifier_node({
        "user_input": "先不管这个，帮我把发布会物料拆一下",
        "pending": _pending(kind="strategist_clarify", scenario_type="meeting_prep",
                            original_input="帮我准备下周的融资路演",
                            ask_text="想先补一个关键信息：是哪一天的会？")})
    assert "当时诉求：帮我准备下周的融资路演" in cap.calls[0]
    assert out["scenario_type"] == "task_breakdown"  # unrelated 后按新诉求分类


# —— ④ 危机正则（零 LLM）⑤ 分类 + 挂起 ————————————————————————————————


def test_crisis_regex_short_circuits(monkeypatch):
    cap = _LLMCapture()
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = classifier_node({"user_input": "救命，老板临时塞了个急活"})
    assert out["scenario_type"] == "crisis"
    assert out["trigger_input"].startswith("救命")
    assert cap.calls == []


def test_classify_routes_scenario(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·场景嗅探官": '{"scenario": "stakeholder", "ask": null}'}))
    out = classifier_node({"user_input": "下午绩效面谈怎么开口"})
    assert out["scenario_type"] == "stakeholder"
    assert out["trigger_input"] == "下午绩效面谈怎么开口"


def test_classify_ask_raises_clarify_question(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·场景嗅探官":
         '{"scenario": "meeting_prep", "ask": "是哪场会？大概什么形式？"}'}))
    out = classifier_node({"user_input": "帮我准备下周的会"})
    qp = out["question_plan"]
    assert qp["kind"] == "strategist_clarify"
    assert qp["scenario_type"] == "meeting_prep"  # 挂起时必存
    assert qp["original_input"] == "帮我准备下周的会"
    assert out["final_reply"].startswith("想先补一个关键信息")


def test_classify_string_null_ask_means_no_question(monkeypatch):
    # 「无追问」的字符串字面量形态（2026-11-01 真机批）：LLM 常把 JSON null
    # 写成 "null"/"none"——真机曾字面输出「想先补一个关键信息：null」挂起假问询
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(
        {"幕僚长·场景嗅探官": '{"scenario": "execution", "ask": "null"}'}))
    out = classifier_node({"user_input": "什么都不想干"})
    assert out["scenario_type"] == "execution"
    assert "question_plan" not in out  # 假问询不挂起


def test_classify_garbage_falls_back_execution(monkeypatch):
    monkeypatch.setattr("services.llm.call_llm", _LLMCapture(default="not-json"))
    out = classifier_node({"user_input": "随便聊聊任务"})
    assert out["scenario_type"] == "execution"


# —— origin 派生 + 壳幕僚问询落库 ————————————————————————————————————————


def _anchor_question(db, payload):
    row = ai_data.create_proposal(db, kind="question", payload=payload)
    ai_data.append_message(db, role="assistant", content="问句",
                           proposal_id=row.id)
    return row


def test_active_pending_origin_dispatch(fresh_db):
    with TestingSessionLocal() as db:
        _anchor_question(db, {"kind": "conflict", "entries": [{"title": "t"}],
                               "ask_text": "撞期了改到明天？"})
        snap = active_pending(db)
        assert snap["origin"] == "scheduler"

        _anchor_question(db, {"kind": "memory", "entries": [],
                               "rule": {"category": "c", "content": "x",
                                        "evidence": "e"},
                               "ask_text": "同意写入？"})
        snap = active_pending(db)
        assert snap["origin"] == "strategist"
        assert snap["scenario_type"] is None
        assert snap["rule"]["content"] == "x"

        _anchor_question(db, {"kind": "strategist_clarify", "entries": [],
                               "scenario_type": "task_breakdown",
                               "original_input": "拆解发布会", "ask_text": "几号？"})
        snap = active_pending(db)
        assert snap["origin"] == "strategist"
        assert snap["scenario_type"] == "task_breakdown"


def test_shell_persists_strategist_question_without_entries(fresh_db):
    # 壳落库：幕僚问询空 entries 放行 + scenario_type/rule 随 payload 落库
    final = {"question_plan": {
        "kind": "strategist_clarify", "entries": [],
        "scenario_type": "meeting_prep", "original_input": "帮我准备下周的会",
        "ask_text": "想先补一个关键信息：是哪场会？"},
        "final_reply": "想先补一个关键信息：是哪场会？"}
    with TestingSessionLocal() as db:
        events = _finalize_turn(db, final)
        assert [e["type"] for e in events] == ["reply"]  # 零卡零事件只留文字
        snap = active_pending(db)
        assert snap is not None and snap["origin"] == "strategist"
        assert snap["ask_text"].startswith("想先补一个关键信息")


def test_clarify_unrelated_short_reply_reasks(monkeypatch):
    """clarify 二次引导（优化批 6）：judge 判 unrelated 且本轮是短对话式回复
    （≤12 字无动作词）——大概率没答到点上，换角度再问一次（挂起保留）；
    带明确动作的长输入照旧放行走分类。"""
    cap = _LLMCapture(replies={
        "问询裁决官": '{"action": "unrelated"}',
        "场景嗅探官": '{"scenario": "task_breakdown", "ask": null}',
    })
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = classifier_node({
        "user_input": "没听懂你问啥",
        "pending": _pending(kind="strategist_clarify", scenario_type="meeting_prep",
                            original_input="帮我准备下周的融资路演",
                            ask_text="是哪一天的会？")})
    assert "可能没说清楚" in out["final_reply"]
    assert "融资路演" in out["final_reply"]
    assert "question_plan" not in out  # 引导非新问询（挂起原样保留）

    # 长输入（>12 字）照旧放行走分类
    out2 = classifier_node({
        "user_input": "先不管这个，帮我把发布会物料拆一下",
        "pending": _pending(kind="strategist_clarify", scenario_type="meeting_prep",
                            original_input="帮我准备下周的融资路演")})
    assert out2.get("scenario_type") == "task_breakdown"
