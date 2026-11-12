"""services/ai_data.py 测试（基建见 conftest.py）：service 层直测（路由全链路在
test_ai_coach.py）。覆盖记忆规则注入序/上限淘汰/upsert 去重、建议 outcome 回写与
过期、消息排序、信任统计口径、冷启动种子推导与幂等。
"""

from datetime import datetime, timedelta

import pytest

from conftest import TestingSessionLocal
from schemas import MemoryRuleCreate, MemoryRuleUpdate
from services import ai_data
from services.settings import get_settings


def _add_rule(category="作息规律", content="规则", source="ai_reflect",
              confidence=0.5, evidence="证据", **overrides):
    with TestingSessionLocal() as db:
        from models import MemoryRule

        row = MemoryRule(category=category, content=content, source=source,
                         confidence=confidence, evidence=evidence, **overrides)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


# —— 记忆规则：注入序 / CRUD / 上限 ——


def test_list_memory_rules_injection_order():
    _add_rule(source="ai_reflect", confidence=0.9)
    _add_rule(source="user_manual", confidence=0.3)
    _add_rule(source="ai_reflect", confidence=0.7)
    with TestingSessionLocal() as db:
        rules = ai_data.list_memory_rules(db)
    # user_manual 恒最前；ai_reflect 按 confidence 降序
    assert [r.source for r in rules] == ["user_manual", "ai_reflect", "ai_reflect"]
    assert rules[1].confidence >= rules[2].confidence


def test_create_memory_rule_is_user_manual_and_confident():
    with TestingSessionLocal() as db:
        row = ai_data.create_memory_rule(
            db, MemoryRuleCreate(category="开会习惯", content="不排晚 8 点后的会")
        )
        assert row.source == "user_manual" and row.confidence == 1.0
        assert row.last_confirmed_at is not None


def test_create_memory_rule_rejects_when_full():
    for i in range(ai_data.MEMORY_RULES_MAX):
        _add_rule(content=f"规则{i}")
    with TestingSessionLocal() as db:
        try:
            ai_data.create_memory_rule(db, MemoryRuleCreate(category="作息规律", content="溢出"))
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass


def test_update_and_delete_memory_rule():
    rid = _add_rule(content="旧内容")
    with TestingSessionLocal() as db:
        row = ai_data.update_memory_rule(db, rid, MemoryRuleUpdate(content="新内容"))
        assert row is not None and row.content == "新内容"
        assert ai_data.update_memory_rule(db, "不存在", MemoryRuleUpdate(content="x")) is None
        assert ai_data.delete_memory_rule(db, rid) is True
        assert ai_data.delete_memory_rule(db, rid) is False


def test_upsert_memory_rule_updates_existing():
    _add_rule(category="开会习惯", content="不爱开会", confidence=0.4)
    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="开会习惯", content="不爱开会",
                                   confidence=0.9, evidence="新证据")
        rules = ai_data.list_memory_rules(db)
        assert len(rules) == 1
        assert rules[0].confidence == 0.9 and rules[0].evidence == "新证据"


def test_upsert_memory_rule_evicts_lowest_confidence_when_full():
    for i in range(ai_data.MEMORY_RULES_MAX):
        _add_rule(category="作息规律", content=f"规则{i}", confidence=0.3 if i == 0 else 0.9)
    lowest_id = None
    with TestingSessionLocal() as db:
        lowest = [r for r in ai_data.list_memory_rules(db) if r.confidence == 0.3][0]
        lowest_id = lowest.id
    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="开会习惯", content="新规则",
                                   confidence=0.8, evidence="证据")
        rules = ai_data.list_memory_rules(db)
        assert len(rules) == ai_data.MEMORY_RULES_MAX  # 换一条不涨
        assert all(r.id != lowest_id for r in rules)  # 最低置信被淘汰
        assert any(r.content == "新规则" for r in rules)


def test_decay_stale_rules_lowers_old_ai_reflect():
    stale = datetime.now() - timedelta(days=91)
    _add_rule(content="陈年规则", confidence=0.7, last_confirmed_at=stale)
    _add_rule(content="新近规则", confidence=0.7,
              last_confirmed_at=datetime.now() - timedelta(days=10))
    with TestingSessionLocal() as db:
        decayed, removed = ai_data.decay_stale_rules(db)
        assert (decayed, removed) == (1, 0)
        rules = {r.content: r for r in ai_data.list_memory_rules(db)}
    assert rules["陈年规则"].confidence == 0.6  # 降一档
    assert rules["新近规则"].confidence == 0.7  # 界内不动


def test_decay_stale_rules_exempts_user_manual_and_null_anchor():
    stale = datetime.now() - timedelta(days=200)
    _add_rule(content="手写规则", source="user_manual", confidence=1.0,
              last_confirmed_at=stale)
    _add_rule(content="无锚规则", confidence=0.5, last_confirmed_at=None)
    with TestingSessionLocal() as db:
        decayed, removed = ai_data.decay_stale_rules(db)
        assert (decayed, removed) == (0, 0)
        rules = {r.content: r for r in ai_data.list_memory_rules(db)}
    assert rules["手写规则"].confidence == 1.0
    assert rules["无锚规则"].confidence == 0.5


def test_decay_stale_rules_removes_when_confidence_hits_floor():
    stale = datetime.now() - timedelta(days=91)
    _add_rule(content="低置信陈年", confidence=0.1, last_confirmed_at=stale)
    _add_rule(content="正常陈年", confidence=0.3, last_confirmed_at=stale)
    with TestingSessionLocal() as db:
        decayed, removed = ai_data.decay_stale_rules(db)
        assert (decayed, removed) == (1, 1)
        rules = {r.content: r for r in ai_data.list_memory_rules(db)}
    assert "低置信陈年" not in rules  # 0.1-0.1=0.0 ≤ 0.05 归零清退
    assert rules["正常陈年"].confidence == 0.2


# —— 建议日志：outcome 回写 / 过期 / 查询 ——


def _add_proposal(kind="schedule", payload=None, outcome=None, created_at=None):
    with TestingSessionLocal() as db:
        from models import Proposal

        row = Proposal(kind=kind, payload=payload or {}, outcome=outcome)
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def test_record_proposal_outcome_and_reason():
    pid = _add_proposal(payload={"draft_plan": []})
    with TestingSessionLocal() as db:
        row = ai_data.record_proposal_outcome(db, pid, "rejected", reason="时间不合适")
        assert row.outcome == "rejected" and row.resolved_at is not None
        assert row.payload["reject_reason"] == "时间不合适"
        # 重复回写幂等覆盖
        row = ai_data.record_proposal_outcome(db, pid, "accepted")
        assert row.outcome == "accepted" and "reject_reason" not in row.payload
        assert ai_data.record_proposal_outcome(db, "不存在", "accepted") is None


def test_expire_stale_proposals_only_touches_old_unresolved():
    old = datetime.now() - timedelta(days=2)
    pid_old = _add_proposal(created_at=old)
    pid_recent = _add_proposal()
    pid_done = _add_proposal(outcome="accepted", created_at=old)
    with TestingSessionLocal() as db:
        n = ai_data.expire_stale_proposals(db, before=datetime.now() - timedelta(days=1))
        assert n == 1
    with TestingSessionLocal() as db:
        from models import Proposal

        assert db.get(Proposal, pid_old).outcome == "expired"
        assert db.get(Proposal, pid_recent).outcome is None
        assert db.get(Proposal, pid_done).outcome == "accepted"


def test_list_proposals_day_and_unresolved_filters():
    _add_proposal()
    pid_b = _add_proposal(outcome="accepted")  # 最新创建的今日建议
    yesterday = datetime.now() - timedelta(days=1)
    _add_proposal(created_at=yesterday)
    today = datetime.now().date()
    with TestingSessionLocal() as db:
        assert len(ai_data.list_proposals(db, day=today)) == 2
        assert len(ai_data.list_proposals(db, unresolved_only=True)) == 2
        assert ai_data.list_proposals(db, day=today)[0].id == pid_b  # 倒序最新在前


# —— 会话消息 ——


def test_append_and_list_messages_chronological():
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="第一条")
        ai_data.append_message(db, role="assistant", content="第二条", proposal_id=None)
        rows = ai_data.list_messages(db)
        assert [r.content for r in rows] == ["第一条", "第二条"]  # 正序返回
        assert rows[-1].role == "assistant"


# —— 信任渐进统计 ——


def test_adoption_stats_empty():
    with TestingSessionLocal() as db:
        stats = ai_data.adoption_stats(db)
        assert stats == {"total": 0, "accepted_modified": 0, "rate": 0.0}


def test_adoption_stats_excludes_expired_and_old():
    _add_proposal(outcome="accepted")
    _add_proposal(outcome="modified")
    _add_proposal(outcome="rejected")
    _add_proposal(outcome="expired")  # 双侧剔除
    _add_proposal(outcome="accepted", created_at=datetime.now() - timedelta(days=30))  # 窗口外
    with TestingSessionLocal() as db:
        stats = ai_data.adoption_stats(db)
        assert stats == {"total": 3, "accepted_modified": 2, "rate": 2 / 3}


# —— 冷启动种子 ——


def test_seed_memory_rules_if_empty_derives_from_settings():
    with TestingSessionLocal() as db:
        get_settings(db)  # 先落设置行（默认 8h 工时 / 午休开）
        assert ai_data.seed_memory_rules_if_empty(db) is True
        rules = ai_data.list_memory_rules(db)
        assert len(rules) == 3  # 午休 + 工时 + 可视区间
        assert all(r.source == "user_manual" and r.confidence == 1.0 for r in rules)
        assert any("午休" in r.content for r in rules)
        assert any("8 小时" in r.content for r in rules)
        # 幂等：二次调用不再灌入
        assert ai_data.seed_memory_rules_if_empty(db) is False
        assert len(ai_data.list_memory_rules(db)) == 3


def test_seed_memory_rules_skips_lunch_when_disabled():
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.lunch_enabled = False
        db.commit()
        ai_data.seed_memory_rules_if_empty(db)
        rules = ai_data.list_memory_rules(db)
        assert len(rules) == 2  # 无午休条目
        assert all("午休" not in r.content for r in rules)


# —— 跨天重复防线：Reduce 语义去重对照集 ————————————————————————


def test_dedup_context_merges_three_sources():
    from schemas import TaskCreate
    from services.tasks import create_task

    with TestingSessionLocal() as db:
        create_task(db, TaskCreate(title="未排期的待办"))
        create_task(db, TaskCreate(title="已排期的日程", duration_mins=30,
                                   start_time=datetime.now() + timedelta(days=1)))
        accepted = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "昨日已采纳的草案", "start": "2026-10-15T09:00:00",
                            "duration": 30}]})
        accepted.outcome = "accepted"
        rejected = ai_data.create_proposal(db, kind="inbox", payload={
            "items": [{"title": "昨日已拒的收件箱建议"}]})
        rejected.outcome = "rejected"
        stale = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "三十天前的旧草案", "start": "2026-09-15T09:00:00",
                            "duration": 30}]})
        stale.outcome = "accepted"
        stale.created_at = datetime.now() - timedelta(days=30)
        ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "待处理不计入", "start": "2026-10-15T10:00:00",
                            "duration": 30}]})  # outcome 空 = 未决
        db.commit()

        titles = ai_data.dedup_context(db)
    # 三源齐备：未排期待办 + 未来已排期 + 近期已决（accepted 与 rejected 都算——
    # 拒过的更不该再提）；超窗旧建议与未决卡不计入
    assert "未排期的待办" in titles
    assert "已排期的日程" in titles
    assert "昨日已采纳的草案" in titles
    assert "昨日已拒的收件箱建议" in titles
    assert "三十天前的旧草案" not in titles
    assert "待处理不计入" not in titles


def test_expire_ingest_split_windows():
    """数据源页审阅队列（2026-10-23）：from_ingest 采集卡 7 天、其余隔夜——ingest
    分流三态（None=全量原行为 / True=仅采集卡 / False=排除采集卡）。"""
    old = datetime.now() - timedelta(days=2)
    pid_chat = _add_proposal(kind="schedule", created_at=old)
    pid_ingest = _add_proposal(kind="inbox", payload={"items": [], "from_ingest": True},
                               created_at=old)
    with TestingSessionLocal() as db:
        assert ai_data.expire_stale_proposals(
            db, before=datetime.now() - timedelta(days=1), ingest=False) == 1
        assert ai_data.expire_stale_proposals(
            db, before=datetime.now() - timedelta(days=7), ingest=True) == 0
    from models import Proposal

    with TestingSessionLocal() as db:
        assert db.get(Proposal, pid_chat).outcome == "expired"
        assert db.get(Proposal, pid_ingest).outcome is None  # 采集卡 7 天内不死
        db.get(Proposal, pid_ingest).created_at = datetime.now() - timedelta(days=8)
        db.commit()
    with TestingSessionLocal() as db:
        assert ai_data.expire_stale_proposals(
            db, before=datetime.now() - timedelta(days=7), ingest=True) == 1


# —— modified 偏离回流（2026-11-01 优化批 4）：payload 快照不回写，diff 并列新键 ——


def test_record_outcome_modified_diff_written():
    # 整卡 modified + applied_entries → modified_diff 字段级 [原值, 新值]；
    # 原快照不回写（既有拍板），非 modified 轮不产 diff 键
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "体检", "start": "2026-11-02T08:00:00",
                            "duration": 60, "priority": 2}]})
        ai_data.record_proposal_outcome(db, row.id, "modified", applied_entries=[
            {"title": "体检", "start": "2026-11-02T10:00:00", "duration": 90,
             "priority": 2}])
        out = ai_data.get_proposal(db, row.id)
        diff = out.payload["modified_diff"]
        assert diff[0]["changes"]["start"] == ["2026-11-02T08:00:00",
                                               "2026-11-02T10:00:00"]
        assert diff[0]["changes"]["duration"] == [60, 90]
        assert "title" not in diff[0]["changes"]  # 未变字段不进 diff
        # 原快照不回写（payload 契约：diff 是并列新键）
        assert out.payload["draft_plan"][0]["start"] == "2026-11-02T08:00:00"

        row2 = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "X", "start": "2026-11-02T08:00:00",
                            "duration": 30}]})
        ai_data.record_proposal_outcome(db, row2.id, "accepted", applied_entries=[
            {"title": "X", "start": "2026-11-02T09:00:00", "duration": 30}])
        assert "modified_diff" not in (ai_data.get_proposal(db, row2.id).payload or {})


def test_settle_items_modified_diff_item_level():
    # 条目级 modified：卡未清空 → item_diffs 暂存；末条结清 → 归位 modified_diff
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="inbox", payload={
            "items": [{"title": "A", "duration": 30},
                      {"title": "B", "duration": 30}]})
        out, _mini = ai_data.settle_proposal_items(
            db, row.id, [0], "modified",
            applied_entries=[{"title": "A", "duration": 60}])
        assert out.payload["item_diffs"][0]["changes"]["duration"] == [30, 60]
        assert out.outcome is None  # 卡未清空继续待审

        ai_data.settle_proposal_items(db, row.id, [0], "modified",
                                      applied_entries=[{"title": "B", "duration": 45}])
        out2 = ai_data.get_proposal(db, row.id)
        assert out2.outcome == "modified"
        assert "modified_diff" in out2.payload and "item_diffs" not in out2.payload
        assert len(out2.payload["modified_diff"]) == 2  # 两条偏离都保留


@pytest.mark.parametrize(("accepted", "expected"), [
    (10, "enhanced"),   # 10 样本全采纳 → 增强档（主动性全开）
    (5, "normal"),      # 10 样本 50% 采纳 → 中间档（不奖不惩）
    (2, "low"),         # 10 样本仅 20% 采纳 → 低介入（主动推送物降频）
])
def test_trust_tier_thresholds(accepted, expected):
    """信任渐进档位（优化批 5 全局消费，PRD §10.3——adoption_stats 首个生产
    消费方）：≥10 样本 ∧ ≥60% → enhanced；<30% → low（生日前瞻/会前清单跳过）；
    样本不足 → normal。"""
    with TestingSessionLocal() as db:
        for _ in range(accepted):
            row = ai_data.create_proposal(db, kind="schedule", payload={})
            ai_data.record_proposal_outcome(db, row.id, "accepted")
        for _ in range(10 - accepted):
            row = ai_data.create_proposal(db, kind="schedule", payload={})
            ai_data.record_proposal_outcome(db, row.id, "rejected")
        assert ai_data.trust_tier(db) == expected
