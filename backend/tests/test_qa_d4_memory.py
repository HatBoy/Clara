"""QA 维度四：零向量双层记忆与记忆画像闭环（七维度端到端测试批）。

断言口径经读码核实：
- 冲突自愈入口 = chat settings 轮（settings_handler → chat_config.apply_preference
  → arbitrate_rules signal=user 可 merge/replace 既有规则；库空直写省一次仲裁；
  仲裁故障降级直写——已知兜底锁定）；
- 让位硬闸（arbiter.py:138-141）：非 user 信号不得 replace/merge user_manual/chat
  源规则；手动编辑 AI 行自动升 user_manual/confidence=1.0（ai_data.update_memory_rule）；
- 删除落纠偏账（dismiss=True）→ 同义候选 difflib≥0.6 预过滤，AI 不再重推；
- 行为遥测聚类（ai_data.aggregate_deviations）：键 (kind, weekday, period)，
  同簇 count≥3 才出；period 桶 14:30/17:00 同属下午（tasks._period_of）；
  behavior 候选 confidence 钳 [0.6,0.8]；keep×3 印证升档 min(0.8,+0.15)；
- 低置信 AI 规则不作硬闸（preference_slots ·AI学习·置信X <0.8 只作文本参考）。
"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm
from services.ai_data import aggregate_deviations, list_memory_rules, upsert_memory_rule
from services.preference_slots import parse_avoid_windows

from agents import reflection
from agents.prompts import REFLECT_PROMPT, RESOLVE_PROMPT
from qa_helpers import (
    KEY_ROUTER,
    KEY_SETTINGS,
    MultiKeyLLMCapture,
    activate_model,
    chat_events,
    first_of,
    inject_rule,
    patch_nodes_session,
)


def _cap(replies=None, default=""):
    cap = MultiKeyLLMCapture(replies, default=default)
    monkeypatch_target = None
    return cap


def _chat(monkeypatch, client, message, *, router="settings", settings_reply=None):
    """settings 轮 chat 全链（router → 设置官 LLM → chat_config 落库）。"""
    activate_model()
    replies = {KEY_ROUTER: json.dumps({"intent": router, "reason": "qa"})}
    if settings_reply is not None:
        replies[KEY_SETTINGS] = json.dumps(settings_reply, ensure_ascii=False)
    cap = MultiKeyLLMCapture(replies, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_nodes_session(monkeypatch)  # settings 轮写库经 nodes 模块级绑定
    events = chat_events(client, message)
    return events, cap


# —— 记忆冲突自愈 ———————————————————————————————————————————————————————————


def test_conflict_heal_chat_e2e(monkeypatch, client):
    """对话冲突自愈 E2E：先立「周三下午不排会」→ 再说「周三下午15-16点开放
    技术对齐」→ 仲裁 merge/replace → 规则库无裸矛盾共存（单条含例外语义）。"""
    events1, _ = _chat(monkeypatch, client, "以后周三下午都别排会", settings_reply={
        "action": "prefer", "category": "会议偏好",
        "content": "周三下午不排会", "rule_ref": None, "key": None, "value": None,
        "pairs": None, "question": None, "facts": []})
    reply1 = first_of(events1, "reply")
    assert reply1 and "记住了" in reply1["content"]
    with TestingSessionLocal() as db:
        rules = list_memory_rules(db)
        assert len(rules) == 1 and rules[0].source == "chat"

    # 第二轮：库非空 → 走仲裁 → merge（旧删新落）
    def resolve(user_text):
        old_id = _rule_id()
        return json.dumps({"actions": [
            {"candidate_idx": 0, "action": "merge", "rule_id": old_id,
             "category": "会议偏好",
             "content": "周三下午原则上不排会，15-16 点可排内部技术对齐",
             "evidence": "对话明示：周三下午开放技术对齐", "confidence": 1.0}]},
            ensure_ascii=False)

    holder = {}

    def resolve_fn(user_text):
        return resolve(user_text)

    # 先取出旧规则 id（合并动作需要真实 rule_id，幻觉 id 会被整条丢）
    with TestingSessionLocal() as db:
        old_id = list_memory_rules(db)[0].id
    cap2 = MultiKeyLLMCapture({
        KEY_ROUTER: '{"intent": "settings", "reason": "qa"}',
        KEY_SETTINGS: json.dumps({
            "action": "prefer", "category": "会议偏好",
            "content": "周三下午15-16点开放技术对齐", "rule_ref": None,
            "key": None, "value": None, "pairs": None, "question": None,
            "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "merge", "rule_id": old_id,
             "category": "会议偏好",
             "content": "周三下午原则上不排会，15-16 点可排内部技术对齐",
             "evidence": "对话明示：周三下午开放技术对齐", "confidence": 1.0}]},
            ensure_ascii=False),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", cap2)
    events2 = chat_events(client, "以后每周三下午15点到16点开放技术对齐")
    reply2 = first_of(events2, "reply")
    assert reply2 and "合并" in reply2["content"]
    with TestingSessionLocal() as db:
        rules = list_memory_rules(db)
    assert len(rules) == 1  # 无裸矛盾共存
    assert "对齐" in rules[0].content


def test_conflict_heal_arbiter_failure_degrades_direct_write(monkeypatch, client):
    """仲裁故障（LLM 垃圾）→ 降级直写不挡用户意图（已知兜底：两条并存，
    锁定现状——修复方向「降级前本地 difflib 窄域消解」登记 checklist）。"""
    _chat(monkeypatch, client, "以后周三下午都别排会", settings_reply={
        "action": "prefer", "category": "会议偏好", "content": "周三下午不排会",
        "facts": []})
    cap2 = MultiKeyLLMCapture({
        KEY_ROUTER: '{"intent": "settings", "reason": "qa"}',
        KEY_SETTINGS: json.dumps({
            "action": "prefer", "category": "会议偏好",
            "content": "周三下午15-16点开放技术对齐", "facts": []},
            ensure_ascii=False),
    }, default="这不是 JSON")  # 仲裁 LLM 恒垃圾 → None → 降级直写
    monkeypatch.setattr(llm, "call_llm", cap2)
    chat_events(client, "以后每周三下午15点到16点开放技术对齐")
    with TestingSessionLocal() as db:
        rules = list_memory_rules(db)
    assert len(rules) == 2  # 降级直写：意图落库（已知取舍）


def test_yield_hardgate_protects_manual_rules(monkeypatch):
    """让位硬闸：behavior 信号候选试图 replace user_manual 规则 → 动作整条丢，
    手写规则原样保留（人类最高裁决权）。"""
    with TestingSessionLocal() as db:
        manual = inject_rule(db, "下午不排会", source="user_manual",
                             confidence=1.0, category="会议偏好")
    from agents.arbiter import arbitrate_rules

    fake = MultiKeyLLMCapture({RESOLVE_PROMPT: json.dumps({"actions": [
        {"candidate_idx": 0, "action": "replace", "rule_id": manual.id,
         "category": "会议偏好", "content": "AI 合成终态", "evidence": "e",
         "confidence": 0.7}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        actions = arbitrate_rules(db, candidates=[{
            "category": "会议偏好", "content": "午后低谷少排会", "evidence": "聚类",
            "confidence": 0.7, "signal": "behavior"}])
        assert actions == []
        assert db.get(type(manual), manual.id).content == "下午不排会"


# —— Human-in-the-Loop —————————————————————————————————————————————————————




def _decided_proposal():
    """造一条当日已决建议（反思链非 skip 前提：decisions 非空）。"""
    from services.ai_data import create_proposal, record_proposal_outcome

    with TestingSessionLocal() as db:
        p = create_proposal(db, kind="inbox", payload={"items": [
            {"title": "来自源的事项", "duration": 30}]})
        record_proposal_outcome(db, p.id, "rejected", reason="不需要")


def test_hitl_patch_promotes_and_protects(monkeypatch, client):
    """手动编辑 AI 推导行 → 自动升 user_manual/confidence=1.0 → 夜间反思
    （仲裁替身试图 replace）不再覆盖手改结果。"""
    with TestingSessionLocal() as db:
        row = inject_rule(db, "周三下午不排会", source="ai_reflect",
                          confidence=0.6, category="会议偏好", evidence="聚类")
        rid = row.id
    resp = client.patch(f"/api/ai/memory-rules/{rid}",
                        json={"content": "周三下午 15 点后不排会"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "user_manual"
    assert resp.json()["confidence"] == 1.0

    # 夜间反思：REFLECT 给同语义候选 + 仲裁替身 replace 该行 → 让位闸拦截
    fake = MultiKeyLLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "会议偏好", "content": "周三下午别排会",
             "evidence": "近14天周三下午3次改期", "confidence": 0.7,
             "signal": "behavior"}], "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "replace", "rule_id": rid,
             "category": "会议偏好", "content": "AI 终态", "evidence": "e",
             "confidence": 0.7}]}, ensure_ascii=False),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", fake)
    _decided_proposal()
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert not report.skipped and fake.count(RESOLVE_PROMPT) == 1
        rules = {r.id: r for r in list_memory_rules(db)}
    assert rules[rid].content == "周三下午 15 点后不排会"  # 手改原文原样保留
    assert rules[rid].source == "user_manual" and rules[rid].confidence == 1.0


def test_dismissal_blocks_relearn(monkeypatch, client):
    """删除 AI 规则（dismiss=True 落纠偏账）→ 夜间反思同义候选被预过滤，
    绝不重复推导已删除的规则。"""
    with TestingSessionLocal() as db:
        row = inject_rule(db, "周三下午不排会", source="ai_reflect",
                          confidence=0.6, category="会议偏好", evidence="聚类")
        rid = row.id
    resp = client.delete(f"/api/ai/memory-rules/{rid}?dismiss=true")
    assert resp.status_code in (200, 204)
    _decided_proposal()
    from models import MemoryDismissal

    with TestingSessionLocal() as db:
        assert list_memory_rules(db) == []
        assert len(list(db.scalars(__import__("sqlalchemy").select(MemoryDismissal)))) == 1

    fake = MultiKeyLLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "会议偏好", "content": "周三下午别安排会议",
             "evidence": "近14天周三下午3次改期", "confidence": 0.7,
             "signal": "behavior"}], "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None,
             "category": "会议偏好", "content": "周三下午别安排会议",
             "evidence": "e", "confidence": 0.7}]}, ensure_ascii=False),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", fake)
    from datetime import date
    from models import Proposal

    for offset in (0, 1, 2):
        night = datetime.now() + timedelta(days=offset)
        class Night(date):
            @classmethod
            def today(cls):
                return night.date()

        monkeypatch.setattr(reflection, 'date', Night)
        with TestingSessionLocal() as db:
            if offset:
                db.add(Proposal(kind='schedule', payload={}, outcome='rejected', created_at=night))
                db.commit()
            report = reflection.run_reflection(db)
            assert not report.skipped and report.discarded >= 1
            assert list_memory_rules(db) == []
    assert fake.count(REFLECT_PROMPT) == 3


def test_decay_exempts_user_sources():
    """90 天未印证：user_manual/chat 源永不衰减，ai_reflect −0.1/晚。"""
    from datetime import datetime as _dt

    with TestingSessionLocal() as db:
        old = _dt.now() - timedelta(days=95)
        m1 = inject_rule(db, "手写规则", source="user_manual", category="作息规律")
        m2 = inject_rule(db, "对话规则", source="chat", category="作息规律")
        m3 = inject_rule(db, "AI 规则", source="ai_reflect", confidence=0.8,
                         category="作息规律", evidence="e")
        from models import MemoryRule

        for r in (m1, m2, m3):
            row = db.get(MemoryRule, r.id)
            row.last_confirmed_at = old
        db.commit()
        report = reflection.run_reflection(db)
        rules = {r.content: r for r in list_memory_rules(db)}
    assert rules["手写规则"].confidence == 1.0
    assert rules["对话规则"].confidence == 1.0
    assert rules["AI 规则"].confidence == 0.7


# —— 行为遥测与自学习演化 ———————————————————————————————————————————————————


def _make_task_at(weekday_hour=None):
    """建一个排在「下周三 14:30」的任务（同 weekday 下午桶，聚类成立前提）。"""
    from qa_helpers import next_weekday

    wed = next_weekday(datetime.now().date(), 2)  # 0=周一 → 2=周三
    start = datetime(wed.year, wed.month, wed.day, 14, 30)
    with TestingSessionLocal() as db:
        t = Task(title="周报评审", start_time=start,
                 end_time=start + timedelta(minutes=60), duration_mins=60)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


def test_telemetry_cluster_forms_via_patch_chain(client):
    """遥测真链路：同一周三下午任务三次 PATCH 改期 → (reschedule, 周X, 下午)
    簇 count=3（14:30→下午、15:00/16:00/17:00 仍下午——_period_of 同桶）。"""
    tid = _make_task_at()
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start0 = None
    with TestingSessionLocal() as db:
        start0 = db.get(Task, tid).start_time
    for new_h in (15, 16, 17):
        resp = client.patch(f"/api/tasks/{tid}", json={
            "start_time": start0.replace(hour=new_h).isoformat(),
            "end_time": start0.replace(hour=new_h + 1).isoformat()})
        assert resp.status_code == 200
        start0 = start0.replace(hour=new_h)
    with TestingSessionLocal() as db:
        clusters = aggregate_deviations(db)
    hit = [c for c in clusters if c["kind"] == "reschedule"
           and c["count"] >= 3 and c["period"] == "下午"]
    assert hit, clusters
    assert hit[0]["avg_delta"] > 0


@pytest.mark.parametrize("independent", [False, True], ids=["同一改期重投", "三项独立改期"])
def test_reschedule_replay_is_not_three_observations(client, independent):
    first = _make_task_at()
    with TestingSessionLocal() as db:
        start = db.get(Task, first).start_time.replace(hour=15)
    ids = [first, _make_task_at(), _make_task_at()] if independent else [first] * 3
    for task_id in ids:
        result = client.patch(f"/api/tasks/{task_id}", json={
            "start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat()})
        assert result.status_code == 200
    with TestingSessionLocal() as db:
        clusters = aggregate_deviations(db)
    assert [c['count'] for c in clusters] == ([3] if independent else [])


def test_telemetry_low_sample_no_cluster(client):
    """样本不足（2 次）→ 无簇、无规则（门槛制宁缺毋滥）。"""
    tid = _make_task_at()
    with TestingSessionLocal() as db:
        start0 = db.get(Task, tid).start_time
    for new_h in (15, 16):
        client.patch(f"/api/tasks/{tid}", json={
            "start_time": start0.replace(hour=new_h).isoformat(),
            "end_time": start0.replace(hour=new_h + 1).isoformat()})
        start0 = start0.replace(hour=new_h)
    with TestingSessionLocal() as db:
        clusters = [c for c in aggregate_deviations(db)
                    if c["kind"] == "reschedule" and c["count"] >= 3]
        assert clusters == []


def test_telemetry_reflection_to_rule_e2e(client, monkeypatch):
    """自学习全链：3 次拖拽 → 夜间反思收 deviations 簇（payload 含
    count/avg_delta）→ behavior 候选落库 source=ai_reflect confidence∈[0.6,0.8]
    content 提及午后 → 低置信注入行不作硬闸。"""
    tid = _make_task_at()
    with TestingSessionLocal() as db:
        start0 = db.get(Task, tid).start_time
    for new_h in (15, 16, 17):
        client.patch(f"/api/tasks/{tid}", json={
            "start_time": start0.replace(hour=new_h).isoformat(),
            "end_time": start0.replace(hour=new_h + 1).isoformat()})
        start0 = start0.replace(hour=new_h)

    fake = MultiKeyLLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [
            {"category": "精力节律", "content": "午后 14-17 点是低谷，少排硬骨头",
             "evidence": "近14天周三下午3次改期，平均推迟约150分钟", "confidence": 0.7,
             "signal": "behavior"}], "facts": []}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [
            {"candidate_idx": 0, "action": "new", "rule_id": None,
             "category": "精力节律", "content": "午后 14-17 点是低谷，少排硬骨头",
             "evidence": "近14天周三下午3次改期", "confidence": 0.7}]},
            ensure_ascii=False),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        reflection.run_reflection(db)
        rules = list_memory_rules(db)
    assert len(rules) == 1
    r = rules[0]
    assert r.source == "ai_reflect" and 0.6 <= r.confidence <= 0.8
    assert "午后" in r.content or "低谷" in r.content
    payload = fake.users(REFLECT_PROMPT)[0]
    assert '"count": 3' in payload or '"count":3' in payload
    # 低置信注入行不作硬闸
    from services.ai_data import injected_rules

    with TestingSessionLocal() as db:
        inj = injected_rules(db).all
    gates = parse_avoid_windows([ln for ln in inj if "午后" in ln])
    assert gates == []  # 0.7 < 0.8 硬闸阈值


def test_keep_reinforce_upgrades_to_hardgate(monkeypatch):
    """keep×3 印证 → confidence 0.6→0.75→0.8 升档 → 注入行过 0.8 阈 →
    偏好窗硬闸生效。"""
    with TestingSessionLocal() as db:
        row = inject_rule(db, "周三下午别排会", source="ai_reflect",
                          confidence=0.6, category="会议偏好", evidence="初始")
    for _ in range(4):
        _decided_proposal()
        fake = MultiKeyLLMCapture({
            REFLECT_PROMPT: json.dumps({"candidates": [
                {"category": "会议偏好", "content": "周三下午别排会",
                 "evidence": "对话原话", "confidence": 0.6,
                 "signal": "adjudication", "source_idx": 0}], "facts": []}, ensure_ascii=False),
            RESOLVE_PROMPT: json.dumps({"actions": [
                {"candidate_idx": 0, "action": "keep",
                 "rule_id": row.id, "category": "会议偏好",
                 "content": "周三下午别排会", "evidence": "e",
                 "confidence": 0.6}]}, ensure_ascii=False),
        }, default="")
        monkeypatch.setattr(llm, "call_llm", fake)
        with TestingSessionLocal() as db:
            reflection.run_reflection(db)
            db.expire_all()
            count = db.get(type(row), row.id).observation_count
            reflection.run_reflection(db)
            db.expire_all()
            assert db.get(type(row), row.id).observation_count == count
            fresh = [r for r in list_memory_rules(db) if r.id == row.id][0]
    assert fresh.confidence >= 0.8, fresh.confidence
    from services.ai_data import injected_rules

    with TestingSessionLocal() as db:
        inj = [ln for ln in injected_rules(db).all if "周三下午" in ln]
    gates = parse_avoid_windows(inj)
    assert gates and gates[0]["weekday"] == 2  # 硬闸生效（周三 12-18 禁排）


# —— 记忆画像 API 与边界 ———————————————————————————————————————————————————


def test_memory_rules_api_roundtrip(client):
    """记忆画像 CRUD 端点全链：POST(user_manual/1.0) → GET → conflict-check →
    DELETE(dismiss 落纠偏账)。"""
    resp = client.post("/api/ai/memory-rules", json={
        "category": "排期偏好", "content": "周五下午别排事"})
    assert resp.status_code == 201
    rid = resp.json()["id"]
    assert resp.json()["source"] == "user_manual"
    assert resp.json()["confidence"] == 1.0
    chk = client.post("/api/ai/memory-rules/conflict-check", json={
        "category": "排期偏好", "content": "周五下午不排事"})
    assert chk.status_code == 200 and chk.json()  # 相似规则命中冲突
    dele = client.delete(f"/api/ai/memory-rules/{rid}?dismiss=true")
    assert dele.status_code in (200, 204)
    with TestingSessionLocal() as db:
        assert list_memory_rules(db) == []


def test_rule_cap_120_returns_422(client):
    """总量硬上限 120：满员后手动新增 422（淘汰只挤 AI 行——user_manual 恒保护）。"""
    with TestingSessionLocal() as db:
        for i in range(120):
            inject_rule(db, f"规则{i}", source="ai_reflect", confidence=0.5,
                        category="其他", evidence=f"证据{i}")
    resp = client.post("/api/ai/memory-rules", json={
        "category": "排期偏好", "content": "第121条"})
    assert resp.status_code == 422


def test_episodic_ttl_clamp_and_2gram_fallback():
    """Tier 2 事实：expires_days 钳 [1,90]；FTS trigram 短词零命中时 2-gram
    功能字评分兜底（中文三层检索防线）。"""
    from services.episodic import search_facts, upsert_fact

    with TestingSessionLocal() as db:
        upsert_fact(db, topic="出差", fact="下周三到周五在杭州出差",
                    expires_days=200)  # 越界钳 90
        from models import EpisodicFact

        row = list(db.scalars(__import__("sqlalchemy").select(EpisodicFact)))[0]
        assert (row.expires_at - row.created_at).days <= 90
        # 2-gram 兜底：trigram 对双字查询失效（<3 字符不进 FTS）→ 2-gram 召回
        hits = search_facts(db, "出差")
        assert hits and "杭州" in hits[0].fact
