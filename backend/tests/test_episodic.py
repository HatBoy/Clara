"""记忆重构批 6 测试（2026-11-03）：Tier 2 事实链——episodic 服务（BM25 trigram
中文检索 / 短词 LIKE 回落 / 过期过滤 / 时间加权）+ 端点 + qa/smalltalk 注入 +
settings 同跳 facts。
"""

import json
from datetime import datetime, timedelta

from conftest import TestingSessionLocal
from sqlalchemy import select
from models import EpisodicFact
from services.episodic import (
    delete_fact,
    expire_episodic_facts,
    fact_lines,
    list_episodic_facts,
    search_facts,
    upsert_fact,
)


def _fact(topic="健康", fact="这周感冒需要多休息", **kw):
    with TestingSessionLocal() as db:
        return upsert_fact(db, topic=topic, fact=fact, **kw)


# —— 服务层 ————————————————————————————————————————————————————————


def test_upsert_fact_dedup_and_expiry_clamp():
    """同 topic+fact 印证刷新过期钟不堆行；expires_days 钳 [1,90]。"""
    r1 = _fact(expires_days=3)
    assert r1.expires_at is not None
    r2 = _fact(expires_days=999)  # 越界钳 90
    assert r2.id == r1.id
    assert (r2.expires_at - datetime.now()).days <= 90
    assert r2.expires_at > r1.expires_at  # 过期钟刷新
    with TestingSessionLocal() as db:
        assert len(list_episodic_facts(db)) == 1


def test_upsert_fact_rejects_empty():
    assert _fact(topic=" ", fact="x") is None
    assert _fact(topic="x", fact="") is None


def test_search_facts_chinese_bm25_hit():
    """trigram 中文 BM25 检索命中；不相关查询零命中。"""
    _fact(topic="健康", fact="这周感冒需要多休息")
    _fact(topic="出差", fact="下周三到周五去上海出差")
    with TestingSessionLocal() as db:
        hits = search_facts(db, "感冒了怎么办")
        assert len(hits) == 1 and "感冒" in hits[0].fact
        assert search_facts(db, "完全无关的查询词") == []


def test_search_facts_short_query_like_fallback():
    """短词（<3 字符）回落 LIKE 命中。"""
    _fact(topic="健康", fact="胃不舒服，吃得清淡")
    with TestingSessionLocal() as db:
        hits = search_facts(db, "胃不")
        assert len(hits) == 1


def test_search_facts_filters_expired():
    """过期事实检索不可见（自动过滤杜绝上下文污染）。"""
    with TestingSessionLocal() as db:
        upsert_fact(db, topic="健康", fact="上周牙疼已看过医生", expires_days=1)
        # 手动把过期时刻改到过去模拟到期
        row = db.scalar(select(EpisodicFact))
        row.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
        assert search_facts(db, "牙疼") == []


def test_expire_episodic_facts_cleans():
    """夜间清理物理删除过期行（触发器同步 FTS 索引）。"""
    with TestingSessionLocal() as db:
        r = upsert_fact(db, topic="健康", fact="临时嗓子疼", expires_days=1)
        r.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
        assert expire_episodic_facts(db) == 1
        assert list_episodic_facts(db) == []


def test_fact_lines_expiry_suffix():
    _fact(expires_days=5)
    perm = _fact(topic="习惯", fact="每天早上喝温水")  # 永久
    with TestingSessionLocal() as db:
        rows = list_episodic_facts(db)
    lines = fact_lines(rows)
    assert any("前有效）" in x for x in lines)
    assert any("每天早上喝温水" in x and "前有效" not in x for x in lines)


def test_delete_fact_via_trigger_sync():
    _fact()
    with TestingSessionLocal() as db:
        rows = list_episodic_facts(db)
        assert delete_fact(db, rows[0].id)
        from sqlalchemy import text

        assert db.execute(text("SELECT count(*) FROM episodic_facts_fts")).scalar() == 0


# —— 端点 ————————————————————————————————————————————————————————


def test_episodic_facts_endpoints(client):
    from fastapi.testclient import TestClient

    r = _fact()
    resp = client.get("/api/ai/episodic-facts")
    assert resp.status_code == 200 and len(resp.json()) == 1
    body = resp.json()[0]
    assert {"id", "topic", "fact", "source", "confidence",
            "expires_at", "created_at"} <= set(body)
    assert client.delete(f"/api/ai/episodic-facts/{r.id}").status_code == 204
    assert client.delete(f"/api/ai/episodic-facts/{r.id}").status_code == 404


# —— settings 同跳 facts（用户拍板：对话提取为主）——————————————————————————


def test_settings_handler_extracts_facts(monkeypatch):
    """settings 轮 LLM 输出带 facts → 落库 + 回执附注（零新增 LLM 调用）。"""
    from agents import nodes
    from services import llm

    monkeypatch.setattr(llm, "call_llm_aux", lambda *a, **kw: json.dumps({
        "action": "unclear", "question": None,
        "facts": [{"topic": "出差", "fact": "下周三到周五去上海出差",
                   "expires_days": 10}]}, ensure_ascii=False))
    monkeypatch.setattr(nodes, "SessionLocal", TestingSessionLocal)  # 模块级绑定绕过 conftest 替换
    state = {"user_input": "我下周要出差，顺便问下天气怎么样"}
    out = nodes.settings_handler(state)
    assert "已记下" in out["final_reply"]
    with TestingSessionLocal() as db:
        rows = list_episodic_facts(db)
        assert len(rows) == 1 and rows[0].topic == "出差"
        assert rows[0].source == "chat"
        assert (rows[0].expires_at - datetime.now()).days <= 10


def test_settings_handler_no_facts_no_note(monkeypatch):
    from agents import nodes
    from services import llm

    monkeypatch.setattr(llm, "call_llm_aux", lambda *a, **kw: json.dumps({
        "action": "unclear", "question": "想改哪一项？", "facts": []},
        ensure_ascii=False))
    out = nodes.settings_handler({"user_input": "嗯嗯"})
    assert "已记下" not in out["final_reply"]


# —— qa 注入 ————————————————————————————————————————————————————————


def test_qa_answerer_carries_related_facts(monkeypatch):
    """qa 载荷带 related_facts 键（命中注入行；零命中空数组）。"""
    from agents import nodes
    from services import llm

    _fact(topic="健康", fact="这周感冒需要多休息")
    captured: dict = {}

    def _fake_stream(messages, **kw):
        captured["payload"] = json.loads(messages[1]["content"])
        return "多喝热水早日康复"

    monkeypatch.setattr(llm, "call_llm_stream", _fake_stream)
    monkeypatch.setattr(nodes, "SessionLocal", TestingSessionLocal)
    nodes.qa_answerer({"user_input": "我感冒了明天能爬山吗", "memory_rules": []})
    facts = captured["payload"]["related_facts"]
    assert facts and "感冒" in facts[0]
