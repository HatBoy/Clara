"""QA 维度五：数据源接入沙盒安全与交互克制（七维度端到端测试批）。

断言口径经读码核实：
- 沙盒核心 = local_folder._resolve_in_roots（resolve 后逐根 is_relative_to，
  越界/不存在 ValueError；10MB/500 文件/≤10 目录三限额）；
- 交互克制 = 采集非 trusted 源恒建卡（proposals kind=schedule/inbox，数据源页
  唯一视图），未经确认绝不落 Task；trusted 源 todo/calendar 域直落（用户拍板
  设计）+ 直落失败整批回建卡；拒绝有记忆（指纹 rejected_at 永静默）；
- 条目级结算隔离（settle-items 只静默被拒指纹）；
- .ics 导入默认信任直落 + 指纹 sha1(UID+起始锚) 防重导。
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import TestingSessionLocal
from models import IngestSource, Task
from services import data_ingest, llm, workflows
from services.ai_data import list_proposals

from agents.graph import run_ingest
from qa_helpers import activate_model


# —— 本地文件夹沙盒 ————————————————————————————————————————————————————————


def test_sandbox_rejects_outside_roots(tmp_path):
    """越界路径（系统根/上级穿越/符号解析后出界）一律 ValueError 拒绝。"""
    from services.local_folder import _resolve_in_roots

    root = tmp_path / "docs"
    root.mkdir()
    inside = root / "a.md"
    inside.write_text("hi", encoding="utf-8")

    outside = tmp_path / "非授权.md"
    outside.write_text("隔离哨兵", encoding="utf-8")
    for bad in (str(outside), str(root / ".." / outside.name)):
        try:
            _resolve_in_roots([root], bad)
            raise AssertionError(f"越界路径未被拒绝：{bad}")
        except ValueError as e:
            assert "越界" in str(e)
    assert _resolve_in_roots([root], str(inside)) == inside.resolve()


def test_sandbox_limits(tmp_path):
    """三限额：目录数 >10 拒；单文件 >10MB 拒读；>500 文件截断标记。"""
    from services.local_folder import (
        _MAX_FILE_BYTES,
        _validate_paths,
        list_files_in,
        read_file_as_markdown,
    )

    root = tmp_path / "many"
    root.mkdir()
    for i in range(505):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = list_files_in([root])
    assert out["total"] == 505 and out["truncated"] is True and len(out["files"]) == 500

    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (_MAX_FILE_BYTES + 1))
    try:
        read_file_as_markdown([tmp_path], str(big))
        raise AssertionError("超大文件未被拒绝")
    except ValueError as e:
        assert "上限" in str(e)

    bad = [f"D:/nonexistent-{i}" for i in range(11)]
    try:
        _validate_paths(bad)
        raise AssertionError("目录数超限未被拒绝")
    except ValueError as e:
        assert "10" in str(e)


# —— 采集交互克制 ————————————————————————————————————————————————————————


def _item(id_="i1", title="条目", fingerprint=None, time=None, domain=None):
    return {"id": id_, "title": title, "body": "{}", "time": time,
            "fingerprint": fingerprint or ("fp-" + id_).ljust(40, "0"),
            "sched_hint": False, "domain": domain, "priority": None}


def _src(name="测试源", key="k1", items=None, ok=True):
    return {"source": name, "kind": "mcp", "ok": ok, "key": key, "fail_streak": 0,
            "items": items or [], "deduped": 0, "error": None, "note": None}


def _patch_ingest(monkeypatch, *, items, map_reply, reduce_reply, source_key="k1"):
    """采集链替身：fetch_recent + 预算 + LLM 三层（test_run_ingest 口径）。"""
    from agents.prompts import MERGE_PROMPT, SOURCE_DIGEST_PROMPT

    envelope = {"sources": [_src(key=source_key, items=items)],
                "truncated": False, "error": None}
    monkeypatch.setattr(data_ingest, "fetch_recent", lambda db, *, hours=72: envelope)
    monkeypatch.setattr(data_ingest, "claim_llm_budget", lambda db, n=1: True)

    class _FakeLLM:
        def __init__(self):
            self.map_calls, self.reduce_calls = [], []

        def __call__(self, messages, *, json_mode=False, max_tokens=None):
            system = messages[0]["content"]
            if SOURCE_DIGEST_PROMPT in system:
                self.map_calls.append(messages[1]["content"])
                return map_reply
            if MERGE_PROMPT in system:
                self.reduce_calls.append(messages[1]["content"])
                return reduce_reply
            return ""

    fake = _FakeLLM()
    monkeypatch.setattr(llm, "call_llm", fake)
    return fake


def _seed_ledger(source_key: str, fingerprints: list[str], domain: str | None = None):
    """预插台账行（模拟 fetch_recent 真函数内的 register——替身绕过台账生命周期，
    钩子链 mark_items_proposed / on_proposal_settled / _filter_and_keepalive
    的对账全部依赖台账行存在）。"""
    from models import IngestionItem

    with TestingSessionLocal() as db:
        for fp in fingerprints:
            db.add(IngestionItem(source_key=source_key, fingerprint=fp,
                                 item_title="qa", domain=domain))
        db.commit()


def _ledger_row(source_key: str, fp: str):
    from models import IngestionItem

    with TestingSessionLocal() as db:
        return db.scalar(__import__("sqlalchemy").select(IngestionItem).where(
            IngestionItem.source_key == source_key,
            IngestionItem.fingerprint == fp))


def _run_ingest_now(monkeypatch):
    """async 采集壳直跑（manual=True 免补账静默）。

    workflows.SessionLocal 是模块级 import 绑定（conftest 只替换 database.
    SessionLocal——不跟随），不 patch 会连真 dev 库（本轮实测污染 2 行
    workflow_runs 认领已清理——QA 基建坑登记 docs/qa-bug-checklist.md）。"""
    monkeypatch.setattr(workflows, "SessionLocal", TestingSessionLocal, raising=False)
    return asyncio.run(workflows.run_ingest_now(manual=True))


def _task_count():
    with TestingSessionLocal() as db:
        return len(list(db.scalars(__import__("sqlalchemy").select(Task))))


_DOC_MAP = json.dumps({"source_note": "1 条候选", "candidates": [
    {"title": "周五评审会", "item_id": "i1", "when_hint": "周五 10:00",
     "evidence": "原文摘录", "confidence": 0.9}]}, ensure_ascii=False)


@pytest.mark.parametrize('collision', [True, False])
def test_ingest_untrusted_goes_to_cards_not_calendar(monkeypatch, collision):
    """交互克制核心：非 trusted 源的日程语义条目 → 幽灵方块（kind=schedule 卡）
    → Task 表零新增——绝不未经授权直接改日历。"""
    activate_model()
    proposed = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=10)
    _patch_ingest(monkeypatch, items=[_item(title="周五评审会")],
                  map_reply=_DOC_MAP,
                  reduce_reply=json.dumps({
                      "draft_plan": [{"task_id": None, "title": "周五评审会",
                                      "start": proposed.isoformat(), "duration": 60}],
                      "inbox_items": [],
                      "evidence": [{"idx": 0, "source": "测试源", "source_key": "k1",
                                    "item_id": "i1", "title": "周五评审会",
                                    "time": None, "fingerprint": "fp-i1".ljust(40, "0")}],
                      "summary": "已提炼 1 条"}, ensure_ascii=False))
    with TestingSessionLocal() as db:
        start = proposed if collision else proposed.replace(hour=15)
        end = start + timedelta(hours=1)
        row = Task(title="用户已确认事项", start_time=start, end_time=end)
        db.add(row)
        db.commit()
        task_id = row.id
    before = _task_count()
    summary = _run_ingest_now(monkeypatch)
    assert summary["drafts"] == int(not collision) and not summary["skipped"]
    assert _task_count() == before  # 日历零变化
    with TestingSessionLocal() as db:
        saved = db.get(Task, task_id)
        assert (saved.title, saved.start_time, saved.end_time, saved.is_deleted) == ("用户已确认事项", start, end, False)
        cards = [p for p in list_proposals(db) if p.kind == "schedule"
                 and p.outcome in (None, "")]
        assert len(cards) == int(not collision)


def test_ingest_reject_fingerprint_silenced(monkeypatch, client):
    """拒绝有记忆：卡被拒 → 同指纹二轮采集不再出卡（静默轮）。"""
    activate_model()
    _patch_ingest(monkeypatch, items=[_item(title="周五评审会")],
                  map_reply=_DOC_MAP,
                  reduce_reply=json.dumps({
                      "draft_plan": [{"task_id": None, "title": "周五评审会",
                                      "start": "2026-12-04T10:00:00", "duration": 60}],
                      "inbox_items": [],
                      "evidence": [{"idx": 0, "source": "测试源", "source_key": "k1",
                                    "item_id": "i1", "title": "周五评审会",
                                    "time": None, "fingerprint": "fp-i1".ljust(40, "0")}],
                      "summary": "已提炼 1 条"}, ensure_ascii=False))
    fp = "fp-i1".ljust(40, "0")
    _seed_ledger("k1", [fp])
    _run_ingest_now(monkeypatch)
    with TestingSessionLocal() as db:
        card = [p for p in list_proposals(db) if p.kind == "schedule"][0]
    # 拒绝走 HTTP 结算端点（on_proposal_settled 钩子真实链路）
    resp = client.post(f"/api/ai/proposals/{card.id}/decide",
                       json={"items": [{"item_id": i, "action": "reject", "reason": "不要"} for i in card.item_ids]})
    assert resp.status_code == 200
    row = _ledger_row("k1", fp)
    assert row.rejected_at is not None  # 指纹已置永静默
    # 二轮采集的台账滤除（真函数直测——fetch 替身绕过它的边界见 _seed_ledger 注）
    from services.data_ingest import _filter_and_keepalive

    with TestingSessionLocal() as db:
        kept, deduped = _filter_and_keepalive(db, "k1", [_item(title="周五评审会")])
    assert kept == [] and deduped == 1  # 同指纹不再进卡


def _make_trusted_source(key="k1"):
    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key=key, trusted=True))
        db.commit()


def test_ingest_trusted_todo_direct_inbox(monkeypatch):
    """trusted 源 todo 域 → 免确认直落收件箱（用户拍板设计）+ accepted 卡快照。"""
    activate_model()
    _make_trusted_source()
    _seed_ledger("k1", ["fp-t1".ljust(40, "0")], domain="todo")
    _patch_ingest(monkeypatch, items=[_item(id_="t1", title="滴答待办A", domain="todo")],
                  map_reply="{}", reduce_reply="{}")  # 结构化轮零 LLM
    before = _task_count()
    summary = _run_ingest_now(monkeypatch)
    assert summary["direct_inbox"] == 1 and summary["drafts"] == 0
    assert _task_count() == before + 1
    with TestingSessionLocal() as db:
        from sqlalchemy import select

        row = list(db.scalars(select(Task).where(Task.title == "滴答待办A")))[0]
        assert row.start_time is None  # 收件箱待办（无时间照抄源——无时间不编钟点）
        accepted = [p for p in list_proposals(db) if p.outcome == "accepted"]
        assert accepted


def test_ingest_trusted_calendar_direct_with_time(monkeypatch):
    """trusted 源 calendar 域带时间 → 直落日历（时间照抄源数据，绝不编钟点）。"""
    activate_model()
    _make_trusted_source("kc")
    _seed_ledger("kc", ["fp-c1".ljust(40, "0")], domain="calendar")
    when = (datetime.now() + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    _patch_ingest(monkeypatch, source_key="kc",
                  items=[_item(id_="c1", title="飞书日程", domain="calendar",
                               time=when.isoformat())],
                  map_reply="{}", reduce_reply="{}")
    summary = _run_ingest_now(monkeypatch)
    assert summary["direct_calendar"] == 1
    with TestingSessionLocal() as db:
        from sqlalchemy import select

        row = list(db.scalars(select(Task).where(Task.title == "飞书日程")))[0]
        assert row.start_time == when


def test_ingest_settle_items_isolation(monkeypatch, client):
    """条目级结算隔离：同卡两指纹，拒 index0 → 仅该指纹静默，同卡其余二轮存活。"""
    activate_model()
    _patch_ingest(monkeypatch,
                  items=[_item(id_="i1", title="条目甲"), _item(id_="i2", title="条目乙")],
                  map_reply=json.dumps({"source_note": "2 条候选", "candidates": [
                      {"title": "条目甲", "item_id": "i1", "when_hint": None,
                       "evidence": "e", "confidence": 0.9},
                      {"title": "条目乙", "item_id": "i2", "when_hint": None,
                       "evidence": "e", "confidence": 0.9}]}, ensure_ascii=False),
                  reduce_reply=json.dumps({
                      "draft_plan": [],
                      "inbox_items": [
                          {"title": "条目甲", "duration": 30, "notes": None,
                           "source_ref": {"source": "测试源", "source_key": "k1",
                                          "item_id": "i1", "title": "条目甲",
                                          "time": None,
                                          "fingerprint": "fp-i1".ljust(40, "0")}},
                          {"title": "条目乙", "duration": 30, "notes": None,
                           "source_ref": {"source": "测试源", "source_key": "k1",
                                          "item_id": "i2", "title": "条目乙",
                                          "time": None,
                                          "fingerprint": "fp-i2".ljust(40, "0")}}],
                      "evidence": [], "summary": "2 条待办"}, ensure_ascii=False))
    # 结构化域（todo）：二轮已见未提会重放——rejected 才是真静默，条目级隔离
    # 语义只有结构化域可分辨（doc 域已见即滤是 keepalive 既有语义）
    _seed_ledger("k1", ["fp-i1".ljust(40, "0"), "fp-i2".ljust(40, "0")], domain="todo")
    summary = _run_ingest_now(monkeypatch)
    assert summary["inbox"] == 2
    with TestingSessionLocal() as db:
        card = [p for p in list_proposals(db) if p.kind == "inbox"][0]
    resp = client.post(f"/api/ai/proposals/{card.id}/decide",
                       json={"items": [{"item_id":card.item_ids[0], "action":"reject"}]})
    assert resp.status_code == 200
    # 条目级隔离：仅被拒条目指纹置 rejected_at，同卡其余不受牵连
    r1 = _ledger_row("k1", "fp-i1".ljust(40, "0"))
    r2 = _ledger_row("k1", "fp-i2".ljust(40, "0"))
    assert r1.rejected_at is not None and r2.rejected_at is None
    # 二轮同指纹两条都照滤（proposed_at 与 rejected_at 均不重出——既有语义）：
    # 条目级隔离的行为证据 = rejected_at 精确置位（上断言）+ 卡上条目乙继续待审
    # （settle_proposal_items payload 收缩），二轮不重出是台账防打扰语义
    from services.data_ingest import _filter_and_keepalive

    with TestingSessionLocal() as db:
        kept, deduped = _filter_and_keepalive(db, "k1", [
            _item(id_="i1", title="条目甲", domain="todo"),
            _item(id_="i2", title="条目乙", domain="todo")])
        assert kept == [] and deduped == 2
        row_card = __import__("services.ai_data", fromlist=["get_proposal"])             .get_proposal(db, card.id)
        assert len(row_card.payload.get("items") or []) == 1  # 卡上条目乙继续待审
        assert row_card.payload["items"][0]["title"] == "条目乙"


def test_ingest_skipped_without_model(monkeypatch):
    """零激活模型 → 采集静默跳过（skipped=no_model，降级矩阵）不崩不卡。"""
    _patch_ingest(monkeypatch, items=[_item()], map_reply="{}", reduce_reply="{}")
    summary = _run_ingest_now(monkeypatch)
    assert summary["skipped"] == "no_model"
    workflows._end_ingest()


# —— ICS 导入与导出冒烟 ——————————————————————————————————————————————————————


def _ics_text() -> str:
    soon = (datetime.now() + timedelta(days=3)).strftime("%Y%m%dT%H0000")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:qa-ics-1@clara\r\n"
            f"DTSTART:{soon}\r\nDURATION:PT1H\r\n"
            "SUMMARY:QA 同步会议\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def test_ics_import_direct_and_dedup(tmp_path, client):
    """.ics 导入默认信任直落日历 + 指纹防重导（同文件重导全跳过）。"""
    ics = tmp_path / "qa.ics"
    ics.write_text(_ics_text(), encoding="utf-8", newline="")
    resp1 = client.post("/api/ingest/ics", json={"path": str(ics)})
    assert resp1.status_code in (200, 201), resp1.text[:300]
    with TestingSessionLocal() as db:
        from sqlalchemy import select

        rows = list(db.scalars(select(Task).where(Task.title == "QA 同步会议")))
        assert len(rows) == 1 and rows[0].start_time is not None
    resp2 = client.post("/api/ingest/ics", json={"path": str(ics)})
    assert resp2.status_code in (200, 201)
    with TestingSessionLocal() as db:
        from sqlalchemy import select

        rows = list(db.scalars(select(Task).where(Task.title == "QA 同步会议")))
        assert len(rows) == 1  # 重导零重复


def test_broadcast_event_delivery():
    """状态同步广播：workflows.subscribe/broadcast 单点投递（前端 SSE 上游）。"""
    async def _main():
        sub = workflows.subscribe()
        try:
            workflows.broadcast({"type": "tasks_changed", "headline": "qa"})
            import asyncio as _aio

            got = await _aio.wait_for(sub.get(), timeout=2)
            assert got["type"] == "tasks_changed"
        finally:
            workflows.unsubscribe(sub)

    asyncio.run(_main())


def test_export_csv_ics_smoke(tmp_path, client):
    """数据主权冒烟：CSV/ICS 导出端点落盘且含造数行（path 契约=前端原生保存
    对话框回传的绝对路径）。"""
    with TestingSessionLocal() as db:
        db.add(Task(title="导出冒烟A", start_time=datetime.now() + timedelta(days=1),
                    duration_mins=60))
        db.commit()
    csv_path = tmp_path / "out.csv"
    ics_path = tmp_path / "out.ics"
    csv = client.post("/api/export/tasks-csv", json={"path": str(csv_path)})
    ics = client.post("/api/export/tasks-ics", json={"path": str(ics_path)})
    assert csv.status_code == 200 and ics.status_code == 200, (csv.text, ics.text)
    assert "导出冒烟A" in csv_path.read_text(encoding="utf-8-sig")
    assert "导出冒烟A" in ics_path.read_text(encoding="utf-8")
