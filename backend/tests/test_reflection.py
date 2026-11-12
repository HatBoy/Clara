"""agents.reflection.run_reflection 测试（批 E：习惯学习官四步链）。

替身口径（docs/ai-agents.md §五）：
- LLM：monkeypatch services.llm.call_llm，按 system prompt（REFLECT_PROMPT /
  RESOLVE_PROMPT）身份分发预设应答——两步调用一次注入两个键即可精确各自应答；
- 读库走真表（TestingSessionLocal，conftest 每用例重建），建议卡经
  ai_data.create_proposal + record_proposal_outcome 造行（与生产同管线）。
"""

import json

import pytest
from conftest import TestingSessionLocal
from services import ai_data, llm
from services.ai_data import list_memory_rules

from agents import reflection
from agents.prompts import REFLECT_PROMPT, RESOLVE_PROMPT


class _LLMCapture:
    """call_llm 替身：按 system prompt 子串分发预设应答并记录 (system, user) 调用。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None):
        system = messages[0]["content"]
        self.calls.append((system, messages[1]["content"]))
        for prompt, reply in self.replies.items():
            if prompt in system:
                return reply
        return self.default


def _decide(kind="schedule", payload=None, outcome="rejected", reason=None):
    """造一条当日已决建议（与生产同管线：建卡 → 回写结果）。"""
    with TestingSessionLocal() as db:
        p = ai_data.create_proposal(db, kind=kind, payload=payload or {})
        ai_data.record_proposal_outcome(db, p.id, outcome, reason)
        return p.id


def _inbox_payload(source_key):
    """带源键的 inbox 卡 payload（adoption_stats_by_source 读 items[].source_ref）。"""
    return {"items": [{"title": "来自源的事项", "duration": 30, "notes": None,
                       "source_ref": {"source": "mcp", "source_key": source_key,
                                      "item_id": "i1", "title": "原始条目",
                                      "time": None, "fingerprint": "fp-x"}}]}


# —— 跳过路径：无已决建议零 LLM ————————————————————————————————————————————

def test_reflection_skips_without_decisions(monkeypatch):
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.skipped and report.source_rules == 0
        assert list_memory_rules(db) == []
    assert fake.calls == []  # 零 LLM 零成本


def test_reflection_decays_stale_rules_even_when_skipped(monkeypatch):
    """时间衰减先于"无已决建议"早退——陈年清算不依赖当日裁决（PRD §4.3）。"""
    from datetime import datetime, timedelta

    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        from models import MemoryRule

        db.add(MemoryRule(category="作息规律", content="陈年规则", source="ai_reflect",
                          confidence=0.8, evidence="旧证据",
                          last_confirmed_at=datetime.now() - timedelta(days=95)))
        db.commit()
        report = reflection.run_reflection(db)
        assert report.skipped  # 无已决建议仍跳过归纳
        assert (report.decayed, report.removed) == (1, 0)
        assert list_memory_rules(db)[0].confidence == 0.7
    assert fake.calls == []  # 衰减纯规则零 LLM


# —— 步骤⓪：源价值学习（纯规则） ———————————————————————————————————————————

def test_source_value_learning_upserts_low_value_rule(monkeypatch):
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(
        {REFLECT_PROMPT: '{"candidates": []}'}))  # 归纳空手而归即可
    for _ in range(6):  # 低价值源：6 条全拒（total 6 ≥ 5，rate 0 < 0.3）
        _decide(kind="inbox", payload=_inbox_payload("mcp:notes"))
    for _ in range(5):  # 健康源：5 条全采纳（rate 1.0 不立规则）
        _decide(kind="inbox", payload=_inbox_payload("cli:lark"), outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.source_rules == 1
        rules = list_memory_rules(db)
        low = [r for r in rules if r.category == "源价值"]
        assert len(low) == 1 and "mcp:notes" in low[0].content
        assert low[0].evidence == "近14天 6 条建议仅 0 条采纳"
        assert not any("cli:lark" in r.content for r in rules)
        report2 = reflection.run_reflection(db)  # 再跑一次 = 印证更新不堆行
        assert report2.source_rules == 1
        assert len([r for r in list_memory_rules(db) if r.category == "源价值"]) == 1


def test_source_value_needs_min_sample(monkeypatch):
    # 4 条全拒：样本不足 5 不判定（偶发不是模式）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(
        {REFLECT_PROMPT: '{"candidates": []}'}))
    for _ in range(4):
        _decide(kind="inbox", payload=_inbox_payload("mcp:notes"))
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.source_rules == 0


# —— 步骤①②③：归纳 → 消解 → 落库主链 ———————————————————————————————————————

def _candidate(content="上午10点前不排会"):
    return {"category": "作息规律", "content": content,
            "evidence": "三次拒绝上午时段建议", "confidence": 0.8}


def test_reflection_new_rule_from_rejects(monkeypatch):
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None, "category": "作息规律",
             "content": "上午10点前不排会", "evidence": "三次拒绝上午时段建议",
             "confidence": 0.8}]}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(payload={"draft_plan": [{"task_id": None, "title": "上午评审",
                                     "start": "2026-10-17T09:00:00", "duration": 60}]},
            outcome="rejected", reason="上午要写代码")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert (report.created, report.merged, report.replaced, report.kept) == (1, 0, 0, 0)
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].content == "上午10点前不排会"
        assert rules[0].evidence == "三次拒绝上午时段建议"
    # 步骤①输入：已决行带 titles + reject_reason（最强信号）+ modified_diff
    # （优化批 4 回流键，无偏离为 None）；existing 为空表
    reflect_user = json.loads(fake.calls[0][1])
    assert reflect_user["decisions"][0] == {
        "kind": "schedule", "titles": ["上午评审"], "outcome": "rejected",
        "reject_reason": "上午要写代码", "modified_diff": None}
    assert reflect_user["existing_rules"] == []


def test_reflection_replaces_old_rule(monkeypatch):
    with TestingSessionLocal() as db:
        old = ai_data.upsert_memory_rule(db, category="工作习惯", content="上午不排会",
                                         confidence=0.5, evidence="旧证据")
        old_id = old.id
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_candidate("仅周一上午不排会")]},
                                   ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "replace", "rule_id": old_id, "category": "工作习惯",
             "content": "仅周一上午不排会", "evidence": "新证据更准",
             "confidence": 0.9}]}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.replaced == 1 and report.created == 0
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].content == "仅周一上午不排会"
        assert all(r.id != old_id for r in rules)  # 旧行已删


def test_reflection_merges_and_keeps(monkeypatch):
    with TestingSessionLocal() as db:
        old = ai_data.upsert_memory_rule(db, category="工作习惯", content="上午不排会",
                                         confidence=0.5, evidence="旧")
        old_id = old.id
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps(
            {"candidates": [_candidate("上午不排会，周二例外"), _candidate("杂项")],
             }, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "merge", "rule_id": old_id, "category": "工作习惯",
             "content": "上午不排会，周二例外", "evidence": "合并证据", "confidence": 0.8},
            {"candidate_idx": 1, "action": "keep", "rule_id": None, "category": "其他",
             "content": None, "evidence": "无增量", "confidence": 0.5}]},
            ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="modified")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert (report.merged, report.kept, report.created) == (1, 1, 0)
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].content == "上午不排会，周二例外"
        assert all(r.id != old_id for r in rules)


def test_reflection_drops_candidates_without_evidence(monkeypatch):
    # 无证据 / 零置信候选逐条丢弃（无出处的画像不落库）；候选清零 → 消解步零调用
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "作息规律", "content": "有内容没证据", "evidence": "",
             "confidence": 0.9},
            {"category": "作息规律", "content": "零置信", "evidence": "有证据",
             "confidence": 0.0}]}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.discarded == 2 and report.created == 0
        assert list_memory_rules(db) == []
    assert len(fake.calls) == 1  # 候选空 → ②零调用


def test_reflection_step1_garbage_zero_rules(monkeypatch):
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(default="抱歉我不会"))
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.created == 0 and list_memory_rules(db) == []


def test_reflection_step2_garbage_discards_batch(monkeypatch):
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
    })  # RESOLVE 落 default 垃圾
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="rejected", reason="太早")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.discarded == 1 and report.created == 0
        assert list_memory_rules(db) == []


def test_reflection_hallucinated_rule_id_drops_action(monkeypatch):
    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="工作习惯", content="上午不排会",
                                   confidence=0.5, evidence="旧")
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "replace", "rule_id": "ghost-id", "category": "工作习惯",
             "content": "幻觉替换", "evidence": "e", "confidence": 0.9}]},
            ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.replaced == 0 and report.created == 0  # 动作被丢，零落库
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].content == "上午不排会"  # 旧规则无恙


def test_reflection_llm_exception_propagates(monkeypatch):
    # LLM 调用失败是故障 → 原样上抛（_run_nightly 外层兜底），不静默吞
    def boom(messages, *, json_mode=False, max_tokens=None):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(llm, "call_llm", boom)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        with pytest.raises(RuntimeError, match="网络炸了"):
            reflection.run_reflection(db)


# —— 对话信号学习（批 4，2026-10-20）———————————————————————————————————

def _say(text):
    """当日一条用户原话（与生产同管线 append_message）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content=text)


def _chat_candidate(content, confidence=0.5):
    return {"category": "排期偏好", "content": content,
            "evidence": "用户原话：" + content, "confidence": confidence,
            "signal": "chat"}


def test_reflection_chat_only_two_signals_still_skips(monkeypatch):
    # 无裁决 + 2 条对话（<3）→ 早退零 LLM；3 条同向才够进归纳
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    _say("周五下午别排事，我要接孩子")
    _say("上午十点前别给我排会")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.skipped
    assert fake.calls == []


def test_reflection_ack_only_day_skips(monkeypatch):
    # 纯应答/寒暄日：四道闸滤光信号 → 早退（确认语零画像价值）
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    for text in ["好的。", "嗯嗯", "ok！", "收到", "谢谢"]:
        _say(text)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.skipped
    assert fake.calls == []


def test_reflection_learns_from_chat_excerpts(monkeypatch):
    # 无裁决 + 4 条同向原话 → 归纳→消解→落库端到端；chat 候选 confidence ≤0.55
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_chat_candidate("周五下午不排事")]},
                                   ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None,
             "category": "排期偏好", "content": "周五下午不排事",
             "evidence": "用户原话：周五下午不排事", "confidence": 0.5}]},
            ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    for text in ["周五下午别排事，我要接孩子", "这周五下午也别安排会",
                 "上周五下午的会挪一下", "以后周五下午留给我"]:
        _say(text)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.created == 1 and not report.skipped
        rules = list_memory_rules(db)
        assert rules[0].content == "周五下午不排事"
        assert rules[0].confidence <= 0.55  # chat 钳制在解析层生效
    reflect_user = json.loads(fake.calls[0][1])
    assert len(reflect_user["chat_excerpts"]) == 4  # 四道闸全过后进提示词


def test_reflection_chat_candidates_clamped_to_two(monkeypatch):
    # 模型超发 3 条 chat 候选 → 只留前 2 条，第 3 条丢弃计 discarded
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            _chat_candidate("周五下午不排事"),
            _chat_candidate("上午十点前不排会"),
            _chat_candidate("周报固定周日晚上写")]}, ensure_ascii=False),
        RESOLVE_PROMPT: '{"actions": []}',
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")  # 有裁决照常进归纳（decisions 非空不受对话闸）
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.discarded == 1
    resolve_user = json.loads(fake.calls[1][1])
    assert len(resolve_user["candidates"]) == 2
    assert all(c["signal"] == "chat" for c in resolve_user["candidates"])


def test_reflection_resolve_confidence_inherits_candidate(monkeypatch):
    # 消解步 confidence 强制继承候选已钳值（2026-10-21 复查批 B 修）：chat 候选
    # 在归纳步钳到 ≤0.55，模型动作行回抄 0.9 也不得绕过——旧版批 4 双闸被拆一半
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_chat_candidate("周五下午不排事")]},
                                   ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None,
             "category": "排期偏好", "content": "周五下午不排事",
             "evidence": "用户原话：周五下午不排事", "confidence": 0.9}]},
            ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    for text in ["周五下午别排事", "这周五下午也别安排会",
                 "上周五下午的会挪一下", "以后周五下午留给我"]:
        _say(text)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.created == 1
        assert list_memory_rules(db)[0].confidence <= 0.55  # 回抄 0.9 被继承钳回


def test_reflection_resolve_drops_action_without_valid_idx(monkeypatch):
    # 动作行 candidate_idx 缺失/越界 → 整条丢（对应关系断裂宁可不学，
    # 2026-10-21 复查批 B 收紧——keep 也不例外）
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"action": "new", "rule_id": None, "category": "作息规律",
             "content": "无 idx 的新增", "evidence": "e", "confidence": 0.8},
            {"candidate_idx": 9, "action": "keep", "rule_id": None,
             "category": "其他", "content": None, "evidence": "e", "confidence": 0.5}]},
            ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert (report.created, report.kept) == (0, 0)
        assert list_memory_rules(db) == []


def test_reflection_existing_rules_capped_for_prompt(monkeypatch):
    # existing_rules 进提示词双闸（2026-10-21 复查批 B）：条数 ≤40 + 累计 ≤3000
    # 字符——旧版全量注入，画像库涨满后小档位模型必溢出，学习链渐进停摆
    with TestingSessionLocal() as db:
        for i in range(50):  # 50 条 × ~60 字 ≈ 3000+ 字符，双闸都得动
            ai_data.upsert_memory_rule(db, category="工作习惯",
                                       content=f"习惯条目 {i} " + "字" * 40,
                                       confidence=0.5, evidence="e")
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": []}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        reflection.run_reflection(db)
    reflect_user = json.loads(fake.calls[0][1])
    rules = reflect_user["existing_rules"]
    assert len(rules) == 40  # 50 条同长行先触条数闸（单行 ~55 字，字符闸不先到）
    assert sum(len(r) for r in rules) <= 3000


def test_reflection_chat_cannot_replace_user_manual(monkeypatch):
    # 让位硬闸：chat 候选 replace user_manual 规则 → 动作整条丢，手写规则无恙
    with TestingSessionLocal() as db:
        manual = ai_data.upsert_memory_rule(
            db, category="作息规律", content="上午不排会",
            confidence=1.0, evidence=None, source="user_manual")
        manual_id = manual.id
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [_chat_candidate("全天不排会")]},
                                   ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "replace", "rule_id": manual_id,
             "category": "作息规律", "content": "全天不排会",
             "evidence": "用户原话", "confidence": 0.5}]}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="rejected", reason="上午不排会")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.replaced == 0 and report.created == 0
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].content == "上午不排会"
        assert rules[0].source == "user_manual"


def test_reflection_rows_carry_modified_diff(monkeypatch):
    """decisions 行带 modified_diff（优化批 4 回流）：用户把建议改成了什么
    （字段级 [原值, 新值]）进归纳输入——改时间=时段偏好、改时长=估时修正的
    最强学习信号，此前反思链只见 outcome 分布。"""
    import json as _json

    captured: dict = {}

    def fake(messages, *, json_mode=False, max_tokens=None):
        if REFLECT_PROMPT in messages[0]["content"]:
            captured["rows"] = _json.loads(messages[1]["content"])["decisions"]
            return _json.dumps({"candidates": []}, ensure_ascii=False)
        return _json.dumps({"actions": []}, ensure_ascii=False)

    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "体检", "start": "2026-11-02T08:00:00",
                            "duration": 60}],
            "modified_diff": [{"idx": 0, "title": "体检",
                               "changes": {"start": ["2026-11-02T08:00:00",
                                                     "2026-11-02T10:00:00"]}}]})
        ai_data.record_proposal_outcome(db, row.id, "modified")
        reflection.run_reflection(db)
    assert captured["rows"][0]["modified_diff"][0]["changes"]["start"] == [
        "2026-11-02T08:00:00", "2026-11-02T10:00:00"]


def test_anchor_high_confidence_rules(monkeypatch):
    """高置信打锚（优化批 8 自进化）：≥0.8 且可解析的规则存 structured_anchor
    （设置页「硬约束」徽标数据源）；低置信/不可解析恒 None；文本改写后重打。"""
    fake = _LLMCapture({
        REFLECT_PROMPT: '{"candidates": []}',
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        high = ai_data.upsert_memory_rule(db, category="排期偏好",
                                          content="周五下午别排事",
                                          confidence=0.85, evidence="三次拒绝周五建议")
        low = ai_data.upsert_memory_rule(db, category="作息规律",
                                         content="周五下午别排事",
                                         confidence=0.5, evidence="一次表达")
        vague = ai_data.upsert_memory_rule(db, category="工作习惯",
                                           content="深度时段不排会议",
                                           confidence=0.9, evidence="哲学文本")
        reflection.run_reflection(db)
        rows = {r.id: r for r in list_memory_rules(db)}
        assert rows[high.id].structured_anchor is not None
        assert rows[high.id].structured_anchor["weekday"] == 4
        assert rows[low.id].structured_anchor is None      # 置信不足
        assert rows[vague.id].structured_anchor is None    # 三闸不解析（哲学文本）
        # 文本改写为复杂形态后重打（工作日窗）
        rows[high.id].content = "工作日上午不排会"
        db.commit()
        reflection.run_reflection(db)
        fresh = {r.id: r for r in list_memory_rules(db)}
        assert fresh[high.id].structured_anchor == {
            "workdays": True, "start_min": 0, "end_min": 720,
            "rule": "[排期偏好] 工作日上午不排会"}
