"""记忆重构批 1 测试（2026-11-03）：数据层地基——slot 槽位 / 纠偏账 /
episodic FTS5 虚表 / PATCH 升级语义 / conflict-check / MemorySource 补 chat。

批 1 范围：models 三新表两新列、database 迁移与 FTS、schemas 契约、
config_backup 补表、ai_data slot_of/CRUD 改造、ai_coach conflict-check 端点。
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.pool import StaticPool

from models import BehaviorEvent, EpisodicFact, MemoryDismissal, MemoryRule
from services import ai_data, config_backup
from services.ai_data import (
    SLOT_VALUES,
    find_rule_conflicts,
    prune_stale_dismissals,
    rule_label,
    slot_of,
    upsert_memory_rule,
)
from conftest import TestingSessionLocal


# —— slot_of 纯函数矩阵 ————————————————————————————————————————


def test_slot_of_category_mapping():
    """category 基础映射：作息规律→rhythm；会议偏好/工作习惯→meeting；其余→other。"""
    assert slot_of("作息规律", "晚上十一点前睡觉") == "rhythm"
    assert slot_of("会议偏好", "会议控制在半小时内") == "meeting"
    assert slot_of("工作习惯", "上午先处理最难的任务") == "meeting"
    assert slot_of("优先级倾向", "健康类事务优先安排") == "other"
    assert slot_of("排期偏好", "重要事项安排在上午") == "other"
    assert slot_of("不存在的分类", "随便什么") == "other"


def test_slot_of_boundary_keyword_override():
    """content 关键词二次修正：红线语义从正文识别（category 是排期偏好但语义是红线）。"""
    assert slot_of("排期偏好", "周五下午别排事") == "boundary"
    assert slot_of("其他", "周末不安排工作") == "boundary"
    assert slot_of("作息规律", "晚间免打扰") == "boundary"
    assert slot_of("其他", "绝不安排背靠背会议") == "boundary"
    # 宁缺勿误：普通作息描述不误归 boundary
    assert slot_of("作息规律", "每天七点起床") == "rhythm"


def test_slot_values_tuple_order():
    """槽位元组序 = 双段注入分组序（rhythm → meeting → boundary → other）。"""
    assert SLOT_VALUES == ("rhythm", "meeting", "boundary", "other")


# —— CRUD 落 slot / PATCH 升级 / DELETE 记账 ————————————————————


def _make_rule(**kw) -> MemoryRule:
    defaults = dict(category="作息规律", content="晚上十一点前睡觉",
                    source="ai_reflect", confidence=0.55,
                    evidence="近 3 周婉拒 5 个晚间安排")
    defaults.update(kw)
    row = MemoryRule(**defaults)
    with TestingSessionLocal() as db:
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def test_create_memory_rule_writes_slot():
    """手写入口落 slot（由后端单源计算，前端不传）。"""
    from schemas import MemoryRuleCreate

    with TestingSessionLocal() as db:
        row = ai_data.create_memory_rule(db, MemoryRuleCreate(
            category="其他", content="周六上午绝不安排会议"))
        assert row.slot == "boundary"  # content 关键词修正


def test_upsert_new_row_slot_and_backfill():
    """upsert 新建落 slot；印证路径对旧行 NULL slot 顺带回填。"""
    with TestingSessionLocal() as db:
        row = upsert_memory_rule(db, category="会议偏好", content="会前留十分钟缓冲",
                                 confidence=0.5, evidence="行为归纳")
        assert row.slot == "meeting"
        # 手动抹掉 slot 模拟旧行，再 upsert 同条印证
        row.slot = None
        db.commit()
        again = upsert_memory_rule(db, category="会议偏好", content="会前留十分钟缓冲",
                                   confidence=0.6, evidence="行为归纳（+1 次）")
        assert again.id == row.id
        assert again.slot == "meeting"


def test_patch_ai_rule_promotes_to_user_manual():
    """用户手动编辑 AI 推导行 → 自动升 user_manual / 1.0（亲笔即最高裁决）。"""
    from schemas import MemoryRuleUpdate

    row = _make_rule(source="ai_reflect", confidence=0.5)
    with TestingSessionLocal() as db:
        updated = ai_data.update_memory_rule(
            db, row.id, MemoryRuleUpdate(content="晚上十一点半前睡觉"))
        assert updated.source == "user_manual"
        assert updated.confidence == 1.0
        assert updated.last_confirmed_at is not None
        assert updated.slot == "rhythm"


def test_patch_manual_rule_stays_user_manual():
    """手写行编辑不重复升级（本就是 user_manual），字段照常改。"""
    from schemas import MemoryRuleUpdate

    row = _make_rule(source="user_manual", confidence=1.0)
    with TestingSessionLocal() as db:
        updated = ai_data.update_memory_rule(
            db, row.id, MemoryRuleUpdate(evidence="亲笔补充"))
        assert updated.source == "user_manual"
        assert updated.evidence == "亲笔补充"


def test_delete_dismiss_writes_ledger():
    """dismiss=True 落纠偏账（原文 + 归一化指纹）；默认 False（合并先删）不落。"""
    row = _make_rule(content="下午不排会！")
    with TestingSessionLocal() as db:
        assert ai_data.delete_memory_rule(db, row.id, dismiss=True)
        led = db.scalars(select(MemoryDismissal)).all()
        assert len(led) == 1
        assert led[0].fingerprint == "下午不排会"  # normalize_title 剥标点
        assert led[0].content == "下午不排会！"
        assert led[0].source == "ai_reflect"

    row2 = _make_rule(content="上午先做最难的事")
    with TestingSessionLocal() as db:
        assert ai_data.delete_memory_rule(db, row2.id)  # 默认不记账
        # 仍只有第一段落的那条（合并先删不新增纠偏账）
        led = db.scalars(select(MemoryDismissal)).all()
        assert len(led) == 1 and led[0].fingerprint == "下午不排会"


def test_prune_stale_dismissals():
    """超 90 天的纠偏记录夜间清理，新鲜记录保留。"""
    with TestingSessionLocal() as db:
        db.add(MemoryDismissal(fingerprint="旧规则", content="旧规则原文",
                               source="ai_reflect",
                               dismissed_at=datetime.now() - timedelta(days=91)))
        db.add(MemoryDismissal(fingerprint="新规则", content="新规则原文",
                               source="user_manual",
                               dismissed_at=datetime.now() - timedelta(days=10)))
        db.commit()
        removed = prune_stale_dismissals(db)
        assert removed == 1
        rest = db.scalars(select(MemoryDismissal)).all()
        assert len(rest) == 1 and rest[0].fingerprint == "新规则"


# —— rule_label 分级格式单源 ———————————————————————————————————


def test_rule_label_grading():
    """用户设定（user_manual/chat）铁律标签；ai_reflect 带置信标签。"""
    assert rule_label(_make_rule_no_save(source="user_manual", category="作息规律",
                                         content="晚十一点睡")) == "[作息规律·用户设定] 晚十一点睡"
    assert rule_label(_make_rule_no_save(source="chat", category="作息规律",
                                         content="晚十一点睡")) == "[作息规律·用户设定] 晚十一点睡"
    assert rule_label(_make_rule_no_save(source="ai_reflect", category="作息规律",
                                         content="晚十一点睡",
                                         confidence=0.55)) == "[作息规律·AI学习·置信0.55] 晚十一点睡"


def _make_rule_no_save(**kw) -> MemoryRule:
    """rule_label 纯格式函数测试：不落库直接构造 ORM 实例。"""
    defaults = dict(category="作息规律", content="", source="ai_reflect", confidence=1.0)
    defaults.update(kw)
    return MemoryRule(**defaults)


# —— conflict-check（service + 端点）——————————————————————————


def test_find_rule_conflicts_hit_exclude_and_miss():
    """≥0.6 相似命中；exclude_id 排除自身；低相似不误报。"""
    row = _make_rule(category="其他", content="下午两点到四点不安排会议")
    with TestingSessionLocal() as db:
        # 高相似（同语义改写）
        hits = find_rule_conflicts(db, category="其他", content="下午2点到4点不排会议")
        assert len(hits) == 1 and hits[0]["id"] == row.id
        assert hits[0]["similarity"] >= 0.6
        # 排除自身（编辑场景：新值 vs 旧行自身不算冲突）
        hits_self = find_rule_conflicts(db, category="其他",
                                        content="下午两点到四点不安排会议",
                                        exclude_id=row.id)
        assert hits_self == []
        # 低相似不误报
        assert find_rule_conflicts(db, category="其他", content="每天喝八杯水") == []


def test_conflict_check_endpoint(client: TestClient):
    """POST /api/ai/memory-rules/conflict-check 端点契约（零 LLM 秒回）。"""
    _make_rule(category="其他", content="周五下午别排事")
    resp = client.post("/api/ai/memory-rules/conflict-check",
                       json={"category": "其他", "content": "周五下午不要排任何事"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["conflicts"]) == 1
    assert {"id", "category", "content", "source", "similarity"} <= set(body["conflicts"][0])
    # 无冲突空数组
    resp2 = client.post("/api/ai/memory-rules/conflict-check",
                        json={"category": "其他", "content": "每天跑步半小时"})
    assert resp2.status_code == 200 and resp2.json()["conflicts"] == []


def test_delete_endpoint_writes_dismissal(client: TestClient):
    """前端 DELETE /memory-rules/{id} 落纠偏账（用户主动删除防再推）。"""
    row = _make_rule()
    resp = client.delete(f"/api/ai/memory-rules/{row.id}")
    assert resp.status_code == 204
    with TestingSessionLocal() as db:
        assert db.scalars(select(MemoryDismissal)).all() != []


# —— MemorySource 补 chat（500 同型病回归）————————————————————


def test_memory_rules_get_serializes_chat_source(client: TestClient):
    """含 source=chat 行的 GET /memory-rules 全量 200（Literal 漏登曾致
    同型 ProposalKind 启动 500，此为回归哨兵）。"""
    _make_rule(source="chat", content="周三下午开放讨论", confidence=1.0)
    resp = client.get("/api/ai/memory-rules")
    assert resp.status_code == 200
    rows = resp.json()
    chat_rows = [r for r in rows if r["source"] == "chat"]
    assert len(chat_rows) == 1
    # 新契约字段带出
    assert "slot" in chat_rows[0] and "observation_count" in chat_rows[0]


# —— episodic FTS5 虚表与触发器 ————————————————————————————————


def test_fts_triggers_sync_on_insert_and_delete():
    """ORM 写主表经触发器同步虚表；删除走 'delete' 指令行同步清索引。"""
    with TestingSessionLocal() as db:
        a = EpisodicFact(topic="健康", fact="这周感冒需要多休息",
                         source="chat", expires_at=datetime.now() + timedelta(days=7))
        b = EpisodicFact(topic="出差", fact="下周三到周五去上海出差",
                         source="chat", expires_at=datetime.now() + timedelta(days=14))
        db.add_all([a, b])
        db.commit()
        fts_count = db.execute(text("SELECT count(*) FROM episodic_facts_fts")).scalar()
        assert fts_count == 2
        # trigram 中文子串命中（unicode61 对连续汉字不可检索，trigram 是唯一解）
        hit = db.execute(text(
            "SELECT count(*) FROM episodic_facts_fts WHERE episodic_facts_fts "
            "MATCH '\"去上海出差\"'")).scalar()
        assert hit == 1
        db.delete(a)
        db.commit()
        fts_count = db.execute(text("SELECT count(*) FROM episodic_facts_fts")).scalar()
        assert fts_count == 1


def test_fts_ensure_idempotent():
    """_ensure_episodic_fts 重复执行幂等（IF NOT EXISTS 全套）。"""
    import database

    database._ensure_episodic_fts(TestingSessionLocal().bind)
    database._ensure_episodic_fts(TestingSessionLocal().bind)  # 二次不炸不重复建
    with TestingSessionLocal() as db:
        triggers = db.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'episodic_facts_%'")).scalar()
        assert triggers == 3  # ai / ad / au


def test_behavior_event_table_shape():
    """behavior_events 表可写入并可按 kind+created 索引查询（弱关联无 FK）。"""
    with TestingSessionLocal() as db:
        db.add(BehaviorEvent(task_id=None, kind="reschedule",
                             prev_start=datetime(2026, 11, 3, 9, 0),
                             new_start=datetime(2026, 11, 3, 11, 0),
                             delta_min=120, context={"weekday": 1, "period": "上午"}))
        db.commit()
        rows = db.execute(text(
            "SELECT kind, delta_min FROM behavior_events")).all()
        assert rows == [("reschedule", 120)]


# —— 老库迁移幂等 ————————————————————————————————————————————


def test_migrate_memory_rule_columns_backfills():
    """老库（无 slot / observation_count）跑迁移补列；再跑幂等。"""
    import database

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    with eng.begin() as conn:
        # 造一张 2026-11-03 之前形态的旧表（无 slot / observation_count）
        conn.execute(text(
            "CREATE TABLE memory_rules (id VARCHAR(36) PRIMARY KEY, "
            "category VARCHAR(30) NOT NULL, content VARCHAR(200) NOT NULL, "
            "source VARCHAR(20) NOT NULL, confidence FLOAT DEFAULT 1.0, "
            "evidence VARCHAR(200), created_at DATETIME, updated_at DATETIME)"))
    database._migrate_memory_rule_columns(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("memory_rules")}
    assert {"slot", "observation_count"} <= cols
    database._migrate_memory_rule_columns(eng)  # 二次幂等
    cols2 = {c["name"] for c in inspect(eng).get_columns("memory_rules")}
    assert cols == cols2


# —— config_backup 补记忆表 ————————————————————————————————————


def test_config_snapshot_covers_memory_tables():
    """dump/restore 覆盖 memory_rules 与 memory_dismissals（删库重建记忆不再丢）。"""
    _make_rule(source="user_manual", content="晚十一点前睡觉")
    with TestingSessionLocal() as db:
        db.add(MemoryDismissal(fingerprint="旧偏好", content="旧偏好原文",
                               source="ai_reflect"))
        db.commit()
        snap = config_backup.dump_config(db)
        assert "memory_rules" in snap and len(snap["memory_rules"]) >= 1
        assert "memory_dismissals" in snap and len(snap["memory_dismissals"]) == 1
        # 清库模拟删库重建 → 恢复回全量
        for table in ("memory_rules", "memory_dismissals"):
            db.execute(text(f"DELETE FROM {table}"))
        db.commit()
        restored = config_backup.restore_config(db, snap)
        assert restored >= 2
        back = db.execute(text("SELECT count(*) FROM memory_rules")).scalar()
        assert back >= 1
