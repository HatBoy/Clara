"""记忆重构批 7 测试（2026-11-03）：夜间反思增强——偏差聚合 / behavior 钳制 /
facts 兜底落库 / 事实蒸馏 / keep 置信演化 / ⓪.5 维护步 / slot 回填。
run_reflection 签名冻结（全在函数体内演进）。
"""

import json
from datetime import datetime, timedelta

from conftest import TestingSessionLocal
from models import BehaviorEvent, MemoryRule
from services import ai_data, llm
from services.ai_data import aggregate_deviations, upsert_memory_rule
from services.episodic import distill_fact_candidates

from agents import reflection
from agents.prompts import REFLECT_PROMPT, RESOLVE_PROMPT


class _LLMCapture:
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


def _decide(outcome="rejected"):
    with TestingSessionLocal() as db:
        p = ai_data.create_proposal(db, kind="schedule", payload={})
        ai_data.record_proposal_outcome(db, p.id, outcome)
        return p.id


def _deviation(kind="reschedule", weekday=4, period="下午", delta=90,
               created=None):
    with TestingSessionLocal() as db:
        db.add(BehaviorEvent(task_id="t", kind=kind, delta_min=delta,
                             context={"weekday": weekday, "period": period},
                             created_at=created or datetime.now()))
        db.commit()


# —— 偏差聚合（纯规则）—————————————————————————————————————————————


def test_aggregate_deviations_buckets_and_threshold():
    """同簇 count≥3 出簇（kind/weekday/period/avg_delta）；不足 3 不出。"""
    for _ in range(3):
        _deviation(kind="reschedule", weekday=4, period="下午", delta=90)
    _deviation(kind="reschedule", weekday=4, period="下午", delta=30)
    _deviation(kind="done", weekday=0, period="上午", delta=10)  # 不足 3
    with TestingSessionLocal() as db:
        clusters = aggregate_deviations(db)
    assert len(clusters) == 1
    c = clusters[0]
    assert (c["kind"], c["weekday"], c["period"]) == ("reschedule", 4, "下午")
    assert c["count"] == 4 and c["avg_delta"] == 75  # (90*3+30)/4


def test_reflection_deviations_flow_into_reflect(monkeypatch):
    """偏差聚类进 REFLECT 输入；behavior 候选钳 [0.6, 0.8]；早退闸扩（无裁决
    无原话但有偏差不跳过）。"""
    for _ in range(3):
        _deviation(kind="reschedule", weekday=4, period="下午", delta=90)
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "排期偏好", "content": "周五下午的安排尽量避开",
             "evidence": "近 14 天周五下午 3 次改期", "confidence": 0.95,
             "signal": "behavior"}], "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None,
             "category": "排期偏好", "content": "周五下午的安排尽量避开",
             "evidence": "e", "confidence": 0.9}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
    assert not report.skipped  # 有偏差不早退
    payload = json.loads(fake.calls[0][1])
    assert payload["deviations"][0]["count"] == 3
    rules = ai_data.list_memory_rules(db)
    assert len(rules) == 1
    assert 0.6 <= rules[0].confidence <= 0.8  # 0.95 被钳到 0.8


def test_reflection_facts_fallback_lands(monkeypatch):
    """REFLECT 输出 facts → episodic 落库（source=reflect，兜底通道）。"""
    _decide()
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [], "facts": [
            {"topic": "健康", "fact": "这周感冒需要多休息", "expires_days": 5}]},
            ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
    assert report.facts_created == 1
    with TestingSessionLocal() as db:
        from services.episodic import list_episodic_facts

        rows = list_episodic_facts(db)
        assert rows and rows[0].source == "reflect"


def test_distill_fact_candidates_threshold():
    """同 topic ≥3 蒸馏为 Tier 1 候选（signal=behavior / 0.6）；不足不蒸馏。"""
    from services.episodic import upsert_fact

    with TestingSessionLocal() as db:
        upsert_fact(db, topic="项目", fact="冲刺期每晚加班")
        upsert_fact(db, topic="项目", fact="冲刺期周末也在赶工")
        cands = distill_fact_candidates(db)
        assert cands == []
        upsert_fact(db, topic="项目", fact="冲刺期下周结束")
        cands = distill_fact_candidates(db)
        assert len(cands) == 1
        assert cands[0]["signal"] == "behavior" and cands[0]["confidence"] == 0.6
        assert "反复出现" in cands[0]["content"]


def test_keep_evolution_reinforces_confidence(monkeypatch):
    """keep 印证：observation_count+1；ai_reflect 行 count≥3 且 <0.8 升档
    min(0.8, +0.15)；user_manual 行只计数不升（已是 1.0）。"""
    with TestingSessionLocal() as db:
        old = upsert_memory_rule(db, category="排期偏好",
                                 content="周五下午别排事",
                                 confidence=0.55, evidence="e")
        old.observation_count = 2  # 再印证一次即达 3
        db.commit()
        old_id = old.id
    fake = _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "排期偏好", "content": "周五下午不安排任务",
             "evidence": "三次印证", "confidence": 0.55, "signal": "adjudication"}],
            "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "keep", "rule_id": old_id,
             "category": "排期偏好", "content": None, "evidence": "无增量",
             "confidence": 0.55}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _decide(outcome="accepted")
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.kept == 1 and report.reinforced == 1
        row = db.get(MemoryRule, old_id)
        assert row.observation_count == 3
        assert row.confidence == 0.7  # 0.55 + 0.15
        assert row.last_confirmed_at is not None


def test_maintenance_step_counts_in_report(monkeypatch):
    """⓪.5 维护步：过期事实/遥测清理计数进 report。"""
    from services.episodic import upsert_fact

    with TestingSessionLocal() as db:
        r = upsert_fact(db, topic="健康", fact="临时嗓子疼", expires_days=1)
        r.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
    monkeypatch.setattr(llm, "call_llm", _LLMCapture())
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
    assert report.facts_expired == 1
    assert report.skipped is True  # 无信号零 LLM


def test_anchor_step_backfills_slot(monkeypatch):
    """打锚步顺带回填旧行 slot（NULL → slot_of 计算）。"""
    monkeypatch.setattr(llm, "call_llm", _LLMCapture())
    with TestingSessionLocal() as db:
        row = MemoryRule(category="排期偏好", content="周五下午别排事",
                         source="user_manual", confidence=1.0, slot=None)
        db.add(row)
        db.commit()
        report = reflection.run_reflection(db)
        db.refresh(row)
        assert row.slot == "boundary"
        assert report.anchored >= 1
