"""QA 边界补强（轮 2 深度项）：时钟边界 / 空库 / 超长输入 / Unicode / 采集限额。
"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm
from services.ai_data import list_memory_rules

from agents.graph import run_preflight
from agents.scheduler import scheduler_app
from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    MultiKeyLLMCapture,
    patch_scheduler_inputs,
    patch_strategist_reads,
)


def _prof(entries):
    return json.dumps({"entries": entries, "reply": "好的。"}, ensure_ascii=False)


def _invoke(monkeypatch, user_input, entries, *, anchor=None):
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof(entries),
        KEY_ADJUDICATOR: json.dumps({"selections": [], "explanation": ""}),
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch, anchor=anchor)
    out = scheduler_app.invoke({"user_input": user_input, "memory_rules": []})
    return out, cap


def test_edge_day_end_rolls_to_tomorrow(monkeypatch):
    """日末边界：23:30 请求 60min 任务 → 当日窗口（22:00 截止）放不下 →
    次日 08:00 起草案（绝不排到 22:00 后）。"""
    anchor = datetime(2026, 11, 25, 23, 30)
    out, _ = _invoke(monkeypatch, "安排写周报", [
        {"task_id": None, "origin": "agent", "title": "写周报", "duration": 60}],
        anchor=anchor)
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.date() == (anchor + timedelta(days=1)).date()
    assert start.hour == 8


def test_edge_quiet_day_preflight(monkeypatch):
    """空库晨报：静好分支零 LLM 早退（「没有已排期日程」文案 + 空清单）。"""
    cap = MultiKeyLLMCapture()
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    with TestingSessionLocal() as db:
        out = run_preflight(db, datetime.now().date())
    assert cap.calls == []  # 零 LLM
    assert "没有已排期日程" in out["preflight"]["headline"]
    assert out["preflight"]["checklist"] == []


def test_edge_max_input_length_rejected(client):
    """ChatIn 1~2000 字契约：2001 字 → 422（输入闸拒绝，不进管线）。"""
    resp = client.post("/api/ai/chat", json={"message": "排" * 2001})
    assert resp.status_code == 422


def test_edge_unicode_emoji_title(monkeypatch):
    """Unicode：emoji 标题排期正常出草案（30 字截断契约内）。"""
    out, _ = _invoke(monkeypatch, "安排🚀火箭发射复盘会", [
        {"task_id": None, "origin": "agent", "title": "🚀火箭发射复盘会🛰️",
         "duration": 45}])
    drafts = out.get("draft_plan") or []
    assert drafts and "复盘" in drafts[0]["title"]


def test_edge_zero_budget_blocks_day(monkeypatch):
    """预算边界：当日负荷 480 分钟恰好满额 → 后续条目顺延次日（预算闸语义）。"""
    today = datetime(2026, 11, 25)
    with TestingSessionLocal() as db:
        db.add(Task(title="满额", start_time=today.replace(hour=8),
                    end_time=today.replace(hour=16), duration_mins=480))
        db.commit()
    out, _ = _invoke(monkeypatch, "再加一个短会", [
        {"task_id": None, "origin": "agent", "title": "短会", "duration": 30}],
        anchor=today.replace(hour=16, minute=30))
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.date() == (today + timedelta(days=1)).date()


def test_edge_max_budget_one_more_min(monkeypatch):
    """预算边界 +1：479 分钟负荷 → 当日仍有 1 分钟预算但 30min 条目放不下 →
    顺延次日（预算闸按条目时长判定）。"""
    today = datetime(2026, 11, 25)
    with TestingSessionLocal() as db:
        db.add(Task(title="近满额", start_time=today.replace(hour=8),
                    end_time=today.replace(hour=15, minute=59), duration_mins=479))
        db.commit()
    out, _ = _invoke(monkeypatch, "加个短会", [
        {"task_id": None, "origin": "agent", "title": "短会", "duration": 30}],
        anchor=today.replace(hour=16))
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]).date() == \
        (today + timedelta(days=1)).date()


def test_edge_ingest_max_items_per_source():
    """采集单源 ≤20 条上限：消费点单源在 fetch 层（_MAX_ITEMS_PER_SOURCE），
    装配/Map 层不二次截断——观察项登记：31 条结构化条目若绕过 fetch 层
    （如未来新源接入路径）会全量进卡，建议 Map 层补同款切片（低优先）。"""
    from agents.graph import _assemble_structured
    from services.data_ingest import _MAX_ITEMS_PER_SOURCE

    assert _MAX_ITEMS_PER_SOURCE == 20
    items = [{"id": f"i{i}", "title": f"条目{i}", "body": "{}", "time": None,
              "fingerprint": f"fp{i}".ljust(40, "0"), "sched_hint": False,
              "domain": "todo", "priority": None} for i in range(31)]
    struct, doc = split_structured_items({"source": "滴答", "kind": "mcp",
                                          "ok": True, "key": "k", "fail_streak": 0,
                                          "items": items, "deduped": 0,
                                          "error": None, "note": None})
    drafts, inbox, evidence, note = _assemble_structured(
        [{"title": it["title"], "item_id": it["id"], "when_hint": None,
          "evidence": None, "confidence": None, "source": "滴答", "source_key": "k",
          "item_title": it["title"], "time": None, "fingerprint": it["fingerprint"],
          "structured": True, "raw_priority": None} for it in struct],
        existing_titles=[])
    # 现状口径锁定：31 条全量装配（fetch 层截断是唯一防线——已登记观察项）
    assert len(drafts) + len(inbox) == 31


def split_structured_items(src):
    from services.data_ingest import split_structured_items as _f

    return _f(src)


def test_edge_memory_rule_max_content(client):
    """记忆规则 200 字内容上限：201 字 POST → 422（契约层执法）。

    禁止自建 TestClient 覆盖 dependency_overrides——全局单例 app 的 override
    是共享态，坏 lambda 会连坐同进程后续全部 HTTP 用例（本轮实测 187 例连坐，
    QA 基建坑登记 docs/qa-bug-checklist.md §三）。"""
    resp = client.post("/api/ai/memory-rules", json={
        "category": "排期偏好", "content": "长" * 201})
    assert resp.status_code == 422
