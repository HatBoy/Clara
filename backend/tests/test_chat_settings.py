"""对话自然语言设置测试（2026-10-22 批 3，docs/ai-pipeline.md「对话设置通路」）。

service 层（chat_config 三动作 + 撤销反演）直测为主；settings_handler 节点走
替身 call_llm（打 services.llm 层——handler 函数内 import 该符号，monkeypatch
源模块即可拦截）；router 七分类与让位硬闸各一例结构断言。撤销跨重启语义 =
proposal 落库（内存库同会话内即"重启后仍可查"的等价验证，不做真重启）。
"""

import json

from conftest import TestingSessionLocal
from models import MemoryRule, Proposal
from services import chat_config
from services.ai_data import list_memory_rules
from services.settings import get_settings


def _rules() -> list[MemoryRule]:
    with TestingSessionLocal() as db:
        return list_memory_rules(db)


def _proposals() -> list[Proposal]:
    with TestingSessionLocal() as db:
        return list(db.query(Proposal).filter_by(kind="settings_change"))


def _run_handler(monkeypatch, raw: str, llm_out: str | None = None) -> str:
    """直调 settings_handler；llm_out=None 时断言零 LLM（撤销短路路径）。"""
    import agents.nodes as nodes

    monkeypatch.setattr(nodes, "SessionLocal", TestingSessionLocal)
    if llm_out is None:
        def boom(*a, **kw):  # 撤销短路不应碰 LLM——碰了即炸
            raise AssertionError("撤销短路不应调用 LLM")
        monkeypatch.setattr("services.llm.call_llm", boom)
    else:
        monkeypatch.setattr("services.llm.call_llm", lambda *a, **kw: llm_out)
    return nodes.settings_handler({"user_input": raw})["final_reply"]


def test_prefer_writes_chat_rule_and_proposal(monkeypatch):
    reply = _run_handler(
        monkeypatch, "以后周五下午都别排事",
        json.dumps({"action": "prefer", "category": "排期偏好",
                    "content": "周五下午不安排任务"}, ensure_ascii=False))
    rules = _rules()
    assert len(rules) == 1 and rules[0].source == "chat"
    assert rules[0].confidence == 1.0
    assert "以后周五下午都别排事" in rules[0].evidence
    assert "周五下午不安排任务" in reply
    props = _proposals()
    assert len(props) == 1 and props[0].outcome == "accepted"
    assert props[0].payload["actions"][0]["type"] == "prefer"


def test_prefer_idempotent_same_content(monkeypatch):
    llm = json.dumps({"action": "prefer", "category": "排期偏好",
                      "content": "周五下午不安排任务"}, ensure_ascii=False)
    _run_handler(monkeypatch, "以后周五下午都别排事", llm)
    _run_handler(monkeypatch, "以后周五下午别安排事", llm)
    assert len(_rules()) == 1  # 同 category+content 走印证更新不堆行


def test_unprefer_single_hit_deletes(monkeypatch):
    _run_handler(
        monkeypatch, "以后周五下午都别排事",
        json.dumps({"action": "prefer", "content": "周五下午不安排任务",
                    "category": "排期偏好"}, ensure_ascii=False))
    reply = _run_handler(
        monkeypatch, "周五下午不用留了",
        json.dumps({"action": "unprefer", "rule_ref": "周五下午不安排任务"},
                   ensure_ascii=False))
    assert _rules() == []
    assert "已撤销偏好" in reply
    assert _proposals()[-1].payload["actions"][0]["type"] == "unprefer"


def test_unprefer_exact_reference_beats_ambiguity(monkeypatch):
    """精确指认短路（2026-11-05 真机批）：两条近孪生规则（差 3 字）按原文
    逐字指认仍被 difflib 判双命中——归一化全等直取，绝不进歧义分支。"""
    _run_handler(monkeypatch, "以后每周四上午都别排事",
                 json.dumps({"action": "prefer", "content": "每周四上午不安排任何任务",
                             "category": "排期偏好"}, ensure_ascii=False))
    _run_handler(monkeypatch, "以后每周五下午都别排事",
                 json.dumps({"action": "prefer", "content": "每周五下午不安排任何任务",
                             "category": "排期偏好"}, ensure_ascii=False))
    reply = _run_handler(
        monkeypatch, "撤掉这条：每周四上午不安排任何任务",
        json.dumps({"action": "unprefer", "rule_ref": "每周四上午不安排任何任务"},
                   ensure_ascii=False))
    assert [r.content for r in _rules()] == ["每周五下午不安排任何任务"]
    assert "已撤销偏好" in reply


def test_unprefer_multiple_hits_lists(monkeypatch):
    _run_handler(monkeypatch, "以后周五下午都别排事",
                 json.dumps({"action": "prefer", "category": "排期偏好",
                             "content": "周五下午不安排任务"}, ensure_ascii=False))
    _run_handler(monkeypatch, "以后周五上午都别排事",
                 json.dumps({"action": "prefer", "category": "排期偏好",
                             "content": "周五下午也不排任务"}, ensure_ascii=False))
    reply = _run_handler(
        monkeypatch, "把周五那条撤了",
        json.dumps({"action": "unprefer", "rule_ref": "周五下午不排"}, ensure_ascii=False))
    assert len(_rules()) == 2  # 多命中不删
    assert "请说出更完整" in reply


def test_unprefer_zero_hit_honest(monkeypatch):
    reply = _run_handler(
        monkeypatch, "撤掉那条偏好",
        json.dumps({"action": "unprefer", "rule_ref": "不存在的规则"}, ensure_ascii=False))
    assert "没找到" in reply and _proposals() == []


def test_setting_applies_and_journals(monkeypatch):
    reply = _run_handler(
        monkeypatch, "每天工作 6 小时",
        json.dumps({"action": "setting", "key": "work_minutes_per_day", "value": 360},
                   ensure_ascii=False))
    with TestingSessionLocal() as db:
        assert get_settings(db).work_minutes_per_day == 360
    assert "已生效" in reply
    act = _proposals()[-1].payload["actions"][0]
    assert act == {"type": "setting", "key": "work_minutes_per_day", "old": 480, "new": 360}


def test_setting_invalid_value_becomes_question(monkeypatch):
    reply = _run_handler(
        monkeypatch, "提醒提前 45 分钟",
        json.dumps({"action": "setting", "key": "reminder_lead_min", "value": 45},
                   ensure_ascii=False))
    assert "没改成" in reply  # ValueError 文案转追问，不 500
    assert _proposals() == []


def test_setting_hour_misparse_corrected_by_raw_text(monkeypatch):
    # 换算后验（2026-11-01 真机批）：「每天排25小时」被 LLM 错算 150 分钟
    # （2.5h 挪小数点）且 150 落在合法区间拦不住——原话数字纠偏到 25*60=1500，
    # 再由超上限 ValueError 原样回执（改不成、不落库）
    reply = _run_handler(
        monkeypatch, "每天给我排25小时",
        json.dumps({"action": "setting", "key": "work_minutes_per_day", "value": 150},
                   ensure_ascii=False))
    assert "没改成" in reply  # 1500 > 720 上限 → ValueError 回执
    with TestingSessionLocal() as db:
        assert get_settings(db).work_minutes_per_day == 480  # 未被 150 污染
    assert _proposals() == []


def test_setting_hour_correct_conversion_untouched(monkeypatch):
    # LLM 换算正确（6 小时 = 360）时后验不动值——正常路径零影响
    reply = _run_handler(
        monkeypatch, "每天工作 6 小时",
        json.dumps({"action": "setting", "key": "work_minutes_per_day", "value": 360},
                   ensure_ascii=False))
    assert "已生效" in reply


def test_settings_change_proposal_serializes(monkeypatch):
    # ProposalOut.kind 补登 settings_change（2026-11-02 真机修复）：2026-10-22
    # 批 3 落地时 chat_config 已写该 kind 行而 Literal 漏登——真机库首现该行后
    # GET /api/ai/proposals 整体 500（ResponseValidationError 7 处 literal_error）
    from schemas import ProposalOut

    _run_handler(  # 本用例自建行，不依赖前序用例顺序
        monkeypatch, "每天工作 6 小时",
        json.dumps({"action": "setting", "key": "work_minutes_per_day", "value": 360},
                   ensure_ascii=False))
    row = _proposals()[-1]
    out = ProposalOut.model_validate(row)  # from_attributes 序列化不再炸
    assert out.kind == "settings_change" and out.outcome == "accepted"
    ProposalOut.model_dump(out)  # dump 全程可序列化（response_model 链路）


def test_setting_pairs_applies_both_lunch_keys(monkeypatch):
    # 成对设置（2026-11-01 真机批）：「午休改12点到1点」一句话两键——旧单
    # key schema 下被判 unclear；pairs 数组两键齐改、journal 两动作齐记
    reply = _run_handler(
        monkeypatch, "把午休时间改成12点到1点",
        json.dumps({"action": "setting", "key": None, "value": None,
                    "pairs": [{"key": "lunch_start_min", "value": 720},
                              {"key": "lunch_end_min", "value": 780}]},
                   ensure_ascii=False))
    with TestingSessionLocal() as db:
        s = get_settings(db)
        assert s.lunch_start_min == 720 and s.lunch_end_min == 780
    assert "已生效" in reply
    acts = _proposals()[-1].payload["actions"]
    assert [a["key"] for a in acts] == ["lunch_start_min", "lunch_end_min"]


def test_setting_unknown_key_rejected(monkeypatch):
    reply = _run_handler(
        monkeypatch, "把 API key 换掉",
        json.dumps({"action": "setting", "key": "api_key", "value": "x"},
                   ensure_ascii=False))
    assert "设置页" in reply and _proposals() == []


def test_undo_shortcircuit_reverts_setting_zero_llm(monkeypatch):
    _run_handler(
        monkeypatch, "每天工作 6 小时",
        json.dumps({"action": "setting", "key": "work_minutes_per_day", "value": 360},
                   ensure_ascii=False))
    reply = _run_handler(monkeypatch, "撤销刚才的设置")  # llm_out=None → 碰 LLM 即炸
    with TestingSessionLocal() as db:
        assert get_settings(db).work_minutes_per_day == 480
    assert "已撤销" in reply
    assert _proposals()[-1].outcome == "rejected"


def test_undo_reverts_prefer_and_unprefer(monkeypatch):
    _run_handler(
        monkeypatch, "以后周五下午都别排事",
        json.dumps({"action": "prefer", "category": "排期偏好",
                    "content": "周五下午不安排任务"}, ensure_ascii=False))
    _run_handler(
        monkeypatch, "周五下午不用留了",
        json.dumps({"action": "unprefer", "rule_ref": "周五下午不安排任务"},
                   ensure_ascii=False))
    assert _rules() == []
    _run_handler(monkeypatch, "反悔")
    rules = _rules()  # unprefer 被反演 → 偏好按快照恢复
    assert len(rules) == 1 and rules[0].content == "周五下午不安排任务"
    assert rules[0].source == "chat" and rules[0].confidence == 1.0
    _run_handler(monkeypatch, "反悔")  # 再撤一轮 → prefer 也被反演（规则删除）
    assert _rules() == []
    reply = _run_handler(monkeypatch, "反悔")  # 无可撤销
    assert "没有可撤销" in reply


def test_unclear_returns_question(monkeypatch):
    reply = _run_handler(
        monkeypatch, "帮我配置一下",
        json.dumps({"action": "unclear", "question": "想改哪项设置？"},
                   ensure_ascii=False))
    assert reply == "想改哪项设置？"


def test_parse_failure_falls_back_to_clarify(monkeypatch):
    reply = _run_handler(monkeypatch, "设置一下", "不是 JSON")
    assert "没完全听懂" in reply  # 兜底追问不炸链


def test_chat_source_survives_nightly_resolve(monkeypatch):
    """让位硬闸白名单扩 chat：消解的 chat 候选不得动 source=chat 规则。
    （2026-11-03 记忆重构批 2：_resolve_actions 迁 agents.arbiter.arbitrate_rules，
    用例等价迁移——同一防线新家；批 2 起非 user 信号全拦）"""
    from agents.arbiter import arbitrate_rules

    _run_handler(
        monkeypatch, "以后周五下午都别排事",
        json.dumps({"action": "prefer", "category": "排期偏好",
                    "content": "周五下午不安排任务"}, ensure_ascii=False))
    target = _rules()[0]
    # 替身 LLM 试图 replace 该 chat 规则（模型违规输出）——硬闸应整条丢弃
    monkeypatch.setattr(
        "services.llm.call_llm",
        lambda *a, **kw: json.dumps(
            {"actions": [{"candidate_idx": 0, "action": "replace",
                          "rule_id": target.id, "content": "新说法",
                          "category": "排期偏好", "evidence": "e", "confidence": 0.7}]},
            ensure_ascii=False))
    from conftest import TestingSessionLocal as _TSL

    with _TSL() as db:
        actions = arbitrate_rules(
            db, candidates=[{"category": "排期偏好", "content": "候选",
                             "evidence": "e", "confidence": 0.5, "signal": "chat"}])
        assert actions == []  # 唯一动作被让位硬闸吃掉
    assert _rules()[0].content == "周五下午不安排任务"  # 原规则未动


def test_list_memory_rules_order_manual_chat_reflect():
    with TestingSessionLocal() as db:
        db.add_all([
            MemoryRule(category="a", content="reflect", source="ai_reflect", confidence=0.9),
            MemoryRule(category="a", content="chat", source="chat", confidence=1.0),
            MemoryRule(category="a", content="manual", source="user_manual", confidence=1.0),
        ])
        db.commit()
    assert [r.content for r in _rules()] == ["manual", "chat", "reflect"]


def test_router_and_graph_wire_settings_intent():
    import agents.graph as graph
    import agents.nodes as nodes

    assert "settings" in nodes._INTENTS
    assert graph._INTENT_EDGES["settings"] == "settings_handler"
