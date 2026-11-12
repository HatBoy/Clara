"""agents.arbiter 仲裁者测试（2026-11-03 记忆重构批 2）+ chat_config 仲裁接线。

替身口径同 test_reflection：monkeypatch services.llm.call_llm，按 system prompt
（RESOLVE_PROMPT）身份分发预设应答（call_llm_aux 未配置 aux 时字面委托
call_llm，同入口命中）。
"""

import json

from conftest import TestingSessionLocal
from sqlalchemy import select
from models import MemoryDismissal, MemoryRule
from services import ai_data, llm
from services.ai_data import list_memory_rules, upsert_memory_rule
from services.chat_config import apply_preference, remove_preference

from agents.arbiter import arbitrate_rules, filter_dismissed, slice_existing_rules
from agents.prompts import RESOLVE_PROMPT


class _LLMCapture:
    """call_llm 替身：按 system prompt 子串分发预设应答并记录调用。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, **kw):
        system = messages[0]["content"]
        self.calls.append((system, messages[1]["content"]))
        for prompt, reply in self.replies.items():
            if prompt in system:
                return reply
        return self.default


def _cand(content="周五下午别排事", signal="adjudication", confidence=0.6):
    return {"category": "排期偏好", "content": content, "evidence": "三次行为",
            "confidence": confidence, "signal": signal}


def _old_rule(content="下午不排会", **kw):
    kw.setdefault("confidence", 0.5)
    with TestingSessionLocal() as db:
        return upsert_memory_rule(db, category="排期偏好", content=content,
                                  evidence="旧证据", **kw)


# —— arbitrate_rules 四防线 ——————————————————————————————————————


def test_arbitrate_new_action(monkeypatch):
    fake = _LLMCapture({RESOLVE_PROMPT: json.dumps({"actions": [
        {"candidate_idx": 0, "action": "new", "rule_id": None,
         "category": "排期偏好", "content": "周五下午别排事",
         "evidence": "三次行为", "confidence": 0.9}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        actions = arbitrate_rules(db, candidates=[_cand()])
        assert actions and actions[0]["action"] == "new"
        assert actions[0]["confidence"] == 0.6  # 继承候选值，不信回抄 0.9


def test_arbitrate_rejects_broken_idx_and_ghost_id(monkeypatch):
    """idx 越界 / rule_id 幻觉 → 动作整条丢（宁可不学不可学错）。"""
    old = _old_rule()
    fake = _LLMCapture({RESOLVE_PROMPT: json.dumps({"actions": [
        {"candidate_idx": 7, "action": "replace", "rule_id": old.id,
         "category": "c", "content": "x", "evidence": "e", "confidence": 0.5},
        {"candidate_idx": 0, "action": "replace", "rule_id": "ghost-id",
         "category": "c", "content": "x", "evidence": "e", "confidence": 0.5}]},
        ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        actions = arbitrate_rules(db, candidates=[_cand()])
        assert actions == []


def test_arbitrate_yields_none_on_garbage(monkeypatch):
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(default="not-json"))
    with TestingSessionLocal() as db:
        assert arbitrate_rules(db, candidates=[_cand()]) is None


def test_arbitrate_yield_gate_blocks_non_user_signals(monkeypatch):
    """让位闸批 2 收紧：adjudication / chat / behavior 信号都不得动
    user_manual·chat 源规则；user 信号放行。"""
    manual = _old_rule(content="下午不排会", source="user_manual", confidence=1.0)

    def _reply(action, rule_id, idx=0):
        return json.dumps({"actions": [
            {"candidate_idx": idx, "action": action, "rule_id": rule_id,
             "category": "排期偏好", "content": "合成终态", "evidence": "e",
             "confidence": 0.6}]}, ensure_ascii=False)

    monkeypatch.setattr(llm, "call_llm", _LLMCapture({RESOLVE_PROMPT: _reply("replace", manual.id)}))
    with TestingSessionLocal() as db:
        for signal in ("adjudication", "chat", "behavior"):
            assert arbitrate_rules(db, candidates=[_cand(signal=signal)]) == []
    # user 信号可动（用户此刻明示）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture({RESOLVE_PROMPT: _reply("replace", manual.id)}))
    with TestingSessionLocal() as db:
        actions = arbitrate_rules(db, candidates=[_cand(signal="user", confidence=1.0)])
        assert actions and actions[0]["action"] == "replace"


def test_arbitrate_dismissal_filter_and_user_exemption(monkeypatch):
    """纠偏账命中丢弃 AI 候选；user 信号豁免（用户旧删除不拦自己的新明示）。"""
    with TestingSessionLocal() as db:
        db.add(MemoryDismissal(fingerprint="周五下午别排事", content="周五下午别排事",
                               source="ai_reflect"))
        db.commit()
        kept, dropped = filter_dismissed(db, [_cand(), _cand("每天喝水八杯")])
        assert dropped == 1 and len(kept) == 1
        kept_u, dropped_u = filter_dismissed(
            db, [_cand(signal="user", confidence=1.0)])
        assert dropped_u == 0 and len(kept_u) == 1
    # 全部命中 → 空列表（不调 LLM）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture())
    with TestingSessionLocal() as db:
        db.add(MemoryDismissal(fingerprint="周五下午别排事", content="周五下午别排事",
                               source="ai_reflect"))
        db.commit()
        assert arbitrate_rules(db, candidates=[_cand()]) == []
        assert not hasattr(llm, "_never")  # sanity


def test_slice_existing_rules_double_gate():
    """40 条 / 3000 字符双闸头部切片。"""
    with TestingSessionLocal() as db:
        for i in range(45):
            db.add(MemoryRule(category="其他", content=f"规则{i}：" + "长" * 30,
                              source="ai_reflect", confidence=0.3, evidence="e"))
        db.commit()
        capped = slice_existing_rules(db)
        assert len(capped) <= 40
        total = sum(len(f"[{r.category}] {r.content}") for r in capped)
        assert total <= 3000 + 80  # 最后一条整条计入（超预算即停，允许单条溢出）


# —— chat_config 仲裁接线 ————————————————————————————————————————


def test_apply_preference_merges_via_arbiter(monkeypatch):
    """对话偏好经仲裁 merge 既有规则（用户痛点：矛盾规则不共存）。"""
    old = _old_rule(content="下午不排会")
    fake = _LLMCapture({RESOLVE_PROMPT: json.dumps({"actions": [
        {"candidate_idx": 0, "action": "merge", "rule_id": old.id,
         "category": "排期偏好", "content": "下午不排会，周三下午开放讨论",
         "evidence": "对话明示", "confidence": 1.0}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        result = apply_preference(db, raw="以后周三下午都开放讨论",
                                  category="排期偏好", content="周三下午开放讨论")
        assert result["ok"]
        rules = list_memory_rules(db)
        assert len(rules) == 1
        assert rules[0].content == "下午不排会，周三下午开放讨论"
        assert rules[0].source == "chat" and rules[0].confidence == 1.0
        assert all(r.id != old.id for r in rules)  # 旧行已删（先删后写）
        assert "合并" in result["reply"]


def test_apply_preference_keep_upgrades_existing(monkeypatch):
    """keep = 用户亲口印证既有行 → 升 source=chat / 1.0。"""
    old = _old_rule(content="周五下午别排事")
    fake = _LLMCapture({RESOLVE_PROMPT: json.dumps({"actions": [
        {"candidate_idx": 0, "action": "keep", "rule_id": old.id,
         "category": "排期偏好", "content": None, "evidence": "无增量",
         "confidence": 1.0}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        result = apply_preference(db, raw="周五下午别排事",
                                  category="排期偏好", content="周五下午不排事")
        assert result["ok"]
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].id == old.id
        assert rules[0].source == "chat" and rules[0].confidence == 1.0


def test_apply_preference_falls_back_to_direct_write(monkeypatch):
    """仲裁异常（LLM 不可用）→ 降级直写 source=chat（用户意图必须落）。"""
    def _boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm, "call_llm", _boom)
    with TestingSessionLocal() as db:
        result = apply_preference(db, raw="以后中午不排会",
                                  category="排期偏好", content="中午不排会")
        assert result["ok"]
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].source == "chat"


def test_remove_preference_writes_dismissal():
    """对话撤销偏好落纠偏账（删了就别再推）。"""
    _old_rule(content="下午不排会")
    with TestingSessionLocal() as db:
        result = remove_preference(db, rule_ref="下午不排会")
        assert result["ok"]
        led = db.scalars(select(MemoryDismissal)).all()
        assert len(led) == 1


# —— classifier 纠偏闸（strategist 问询确认制）—————————————————————


def test_consume_memory_blocked_by_dismissal(monkeypatch):
    """agree 落库前查纠偏账：命中不写库、回执引导手动添加。"""
    from agents.strategist import classifier

    with TestingSessionLocal() as db:
        db.add(MemoryDismissal(fingerprint="周五下午别排事", content="周五下午别排事",
                               source="ai_reflect"))
        db.commit()
    monkeypatch.setattr(classifier, "_judge", lambda ask, reply: "agree")
    pending = {"proposal_id": "p1", "ask_text": "要记下吗",
               "rule": {"content": "周五下午别排事", "evidence": "三次对话",
                        "category": "排期偏好"}}
    result = classifier._consume_memory(pending, "好的")
    assert result["resolved_proposal"]["outcome"] == "accepted"
    assert "删除过" in result["final_reply"]
    with TestingSessionLocal() as db:
        assert list_memory_rules(db) == []


def test_consume_memory_writes_when_not_dismissed(monkeypatch):
    from agents.strategist import classifier

    monkeypatch.setattr(classifier, "_judge", lambda ask, reply: "agree")
    pending = {"proposal_id": "p1", "ask_text": "要记下吗",
               "rule": {"content": "每天十点前处理最难任务", "evidence": "三次复盘",
                        "category": "工作习惯"}}
    result = classifier._consume_memory(pending, "好的")
    assert "已写入长期记忆" in result["final_reply"]
    with TestingSessionLocal() as db:
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].slot == "meeting"


# —— undo 恢复行带 slot ————————————————————————————————————————————


def test_undo_restores_rule_with_slot():
    """unprefer → undo 恢复的规则行带上 slot 计算（非 NULL）。"""
    from services.chat_config import undo_last_change

    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="排期偏好",
                                   content="周五下午别排事",
                                   confidence=1.0, evidence="对话明示",
                                   source="chat")
        removed = remove_preference(db, rule_ref="周五下午别排事")
        assert removed["ok"]
        # 手动落一条 journal proposal（正常链路由 settings_handler 落，这里直造）
        ai_data.create_proposal(db, kind="settings_change",
                                payload={"actions": [removed["journal"]]})
        result = undo_last_change(db)
        assert result["ok"]
        rules = list_memory_rules(db)
        assert len(rules) == 1
        assert rules[0].slot == "boundary"
        assert rules[0].content == "周五下午别排事"
