"""agents.graph 采集编排测试（run_ingest + chat 图，docs/ai-ingest.md §五 验收清单可自动化子集）。

替身模式：graph.py 的 services 一律函数内 import（调用时点解析模块属性），
故 monkeypatch 源模块即可拦截——data_ingest.fetch_recent / claim_llm_budget、
ai_data.dedup_context / list_memory_rules、llm.call_llm 四层替身。chat 图用例
另 patch agents.graph.SessionLocal → TestingSessionLocal（漏 patch 会真开 dev 库）。
"""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from sqlalchemy import select

import agents.graph as graph_mod
from agents.prompts import MERGE_PROMPT, ROUTER_PROMPT, SOURCE_DIGEST_PROMPT
from conftest import TestingSessionLocal
from schemas import TaskCreate
from services import ai_data, data_ingest, llm
from services.settings import get_settings
from services.tasks import create_task


# —— 替身小件 ————————————————————————————————————————————————————————————————


def _src(name="测试源", key="k1", ok=True, items=None, error=None, deduped=0):
    """fetch_recent 单源条目（九键契约形态）。"""
    return {"source": name, "kind": "mcp", "ok": ok, "key": key, "fail_streak": 0,
            "items": items if items is not None else [], "deduped": deduped,
            "error": error, "note": None}


def _item(id_="i1", title="条目", fingerprint="a" * 40, time=None, domain=None,
          priority=None):
    return {"id": id_, "title": title, "body": "{}", "time": time,
            "fingerprint": fingerprint, "sched_hint": False, "domain": domain,
            "priority": priority}


class _FakeLLM:
    """call_llm 替身：按 system prompt 身份分发 Router/Map/Reduce 三侧应答，记录调用。"""

    def __init__(self, map_reply=None, reduce_reply=None, router_reply=None):
        self.map_reply = map_reply if map_reply is not None else json.dumps(
            {"source_note": "正常", "candidates": []}, ensure_ascii=False)
        self.reduce_reply = reduce_reply if reduce_reply is not None else json.dumps(
            {"draft_plan": [], "inbox_items": [], "evidence": [], "summary": "综述"},
            ensure_ascii=False)
        # 图路由化后 center_router 是第一跳；默认指 ingest 保持本文件哨兵语义
        self.router_reply = router_reply if router_reply is not None else json.dumps(
            {"intent": "ingest", "reason": "测试默认"}, ensure_ascii=False)
        self.map_calls: list[str] = []
        self.reduce_calls: list[str] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None):
        system = messages[0]["content"]
        if system is ROUTER_PROMPT:
            return self.router_reply
        if system is SOURCE_DIGEST_PROMPT:
            self.map_calls.append(messages[1]["content"])
            return self.map_reply
        if system is MERGE_PROMPT:
            self.reduce_calls.append(messages[1]["content"])
            return self.reduce_reply
        raise AssertionError("未知 system prompt")


def _reduce_payload(fake: _FakeLLM) -> dict:
    """Reduce 收到的 user payload（json 字符串 → dict）。"""
    assert fake.reduce_calls, "Reduce 未被调用"
    return json.loads(fake.reduce_calls[0])


def _patch_services(monkeypatch, *, sources=None, truncated=False, error=None,
                    existing=None, rules=None, llm_fake=None, budget=True):
    """四层替身一次挂好；返回 _FakeLLM（budget=False 时不替 claim_llm_budget，
    供真预算用例）。"""
    envelope = {"sources": sources or [], "truncated": truncated, "error": error}
    monkeypatch.setattr(data_ingest, "fetch_recent", lambda db, *, hours=72: envelope)
    monkeypatch.setattr(ai_data, "dedup_context", lambda db, **kw: existing or [])
    monkeypatch.setattr(
        ai_data, "list_memory_rules",
        lambda db: rules if rules is not None
        # 2026-11-03 批 3 起走 format_memory_lines 分级标签（替身行补 source/
        # confidence 让分流与格式贴近真实 ai_reflect 形态）
        else [SimpleNamespace(category="作息", content="早睡",
                              source="ai_reflect", confidence=0.55)])
    if budget:
        monkeypatch.setattr(data_ingest, "claim_llm_budget", lambda db, n=1: True)
    fake = llm_fake or _FakeLLM()
    monkeypatch.setattr(llm, "call_llm", fake)
    return fake


# —— run_ingest 直测（验收清单 ①②③④⑦ 及编排细节） ————————————————————————


def test_run_ingest_happy_path_provenance_attached(monkeypatch):
    # 溯源字段编排侧附加：LLM 只给 item_id，fingerprint 等从 by_id 照抄
    src = _src(items=[_item(id_="i1", title="周五评审会", fingerprint="a" * 40)])
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "1 条候选", "candidates": [
            {"title": "周五评审会", "item_id": "i1", "when_hint": "周五 10:00",
             "evidence": "原文摘录", "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [{"task_id": None, "title": "周五评审会",
                            "start": "2026-10-17T10:00:00", "duration": 60}],
            "inbox_items": [],
            "evidence": [{"idx": 0, "source": "测试源", "source_key": "k1",
                          "item_id": "i1", "title": "周五评审会", "time": None,
                          "fingerprint": "a" * 40}],
            "summary": "已提炼 1 条"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)

    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))

    assert set(result) == {"draft_plan", "inbox_items", "evidence", "final_reply"}
    assert result["draft_plan"][0]["title"] == "周五评审会"
    assert result["evidence"][0]["fingerprint"] == "a" * 40  # 白名单命中保留
    assert result["final_reply"] == "已提炼 1 条"
    payload = _reduce_payload(llm_fake)
    cand = payload["candidates"][0]
    assert cand["source"] == "测试源" and cand["source_key"] == "k1"
    assert cand["fingerprint"] == "a" * 40
    assert cand["item_title"] == "周五评审会"
    assert payload["existing_titles"] == []
    assert payload["memory_rules"] == ["[作息·AI学习·置信0.55] 早睡"]  # 批 3 双段单源分级格式
    assert payload["today"] == "2026-10-16"
    assert payload["source_notes"] == ["测试源: 1 条候选"]


def test_run_ingest_no_sources_silent_round(monkeypatch):
    # 验收①改版（2026-10-23 静默轮）：无源启用 → 零候选零异常 → 跳过 Reduce
    # 保底调用、返回 silent 键（壳不落消息不广播）——提频后无源日不再每天刷综述
    llm_fake = _patch_services(monkeypatch, sources=[])
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []
    assert result["silent"] is True
    assert result["draft_plan"] == [] and result["inbox_items"] == []
    assert result["final_reply"] == ""


def test_run_ingest_all_sources_empty_silent_round(monkeypatch):
    # 各源 ok 但零新条目（Map 写死「无新条目」尾串）→ 同样静默
    llm_fake = _patch_services(monkeypatch, sources=[
        _src(name="空源A", key="ka", ok=True, items=[]),
        _src(name="空源B", key="kb", ok=True, items=[]),
    ])
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []
    assert result["silent"] is True


def test_run_ingest_garbage_reduce_zero_cards(monkeypatch):
    # 验收②：Reduce 回非 JSON → 零卡零落库、降级原文；Map 输出带围栏证明剥离
    src = _src(items=[_item()])
    llm_fake = _FakeLLM(
        map_reply="```json\n" + json.dumps({"source_note": "ok", "candidates": [
            {"title": "候选", "item_id": "i1", "when_hint": None,
             "evidence": None, "confidence": 0.9}]}, ensure_ascii=False) + "\n```",
        reduce_reply="抱歉，我无法从这个输入中提炼内容")
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["draft_plan"] == [] and result["inbox_items"] == []
    assert result["evidence"] == []
    assert result["final_reply"] == "抱歉，我无法从这个输入中提炼内容"
    assert _reduce_payload(llm_fake)["candidates"][0]["title"] == "候选"


def test_run_ingest_map_garbage_note_and_reduce_keeps(monkeypatch):
    # Map 单源回垃圾 → note 如实、候选空、Reduce 仍跑
    src = _src(items=[_item()])
    llm_fake = _FakeLLM(map_reply="这不是 JSON")
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["draft_plan"] == []
    assert _reduce_payload(llm_fake)["source_notes"] == ["测试源: （源摘要解析失败）"]


def test_run_ingest_draft_entry_validation(monkeypatch):
    # 验收③：draft 四键严格——时区后缀 / 零时长 / 超长标题被丢，task_id 幻觉强制 None
    # （evidence 须指向形状校验后留存的序 0/1 条——无来源条目丢弃是 2026-10-26 闸）
    src = _src(items=[_item()])
    _ok_ev = {"source": "测试源", "source_key": "k1", "item_id": "i1",
              "title": "条目", "time": None, "fingerprint": "a" * 40}
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "n", "candidates": [
            {"title": "候选", "item_id": "i1", "source_key": "k1",
             "fingerprint": "a" * 40, "when_hint": None,
             "evidence": None, "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({"draft_plan": [
        {"task_id": None, "title": "合法", "start": "2026-10-17T09:00:00", "duration": 30},
        {"task_id": None, "title": "带时区", "start": "2026-10-17T09:00:00+08:00", "duration": 30},
        {"task_id": None, "title": "Z 后缀", "start": "2026-10-17T09:00:00Z", "duration": 30},
        {"task_id": None, "title": "零时长", "start": "2026-10-17T09:00:00", "duration": 0},
        {"task_id": None, "title": "超" * 31, "start": "2026-10-17T09:00:00", "duration": 30},
        {"task_id": "t-9", "title": "幻觉任务号", "start": "2026-10-17T09:00:00", "duration": 30},
    ], "inbox_items": [], "evidence": [{**_ok_ev, "idx": 0}, {**_ok_ev, "idx": 1}],
        "summary": "s"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert [d["title"] for d in result["draft_plan"]] == ["合法", "幻觉任务号"]
    assert all(d["task_id"] is None for d in result["draft_plan"])
    assert set(result["draft_plan"][0]) == {"task_id", "title", "start", "duration"}


def test_run_ingest_evidence_missing_fingerprint_and_bad_idx_dropped(monkeypatch):
    # 验收④相关：evidence 缺指纹整条丢；idx 指向被过滤草案下标 → 丢（对齐哨兵）
    src = _src(items=[_item(fingerprint="a" * 40)])
    ok_ev = {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i1",
             "title": "x", "time": None, "fingerprint": "a" * 40}
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "n", "candidates": [
            {"title": "候选", "item_id": "i1", "when_hint": None,
             "evidence": None, "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
        "draft_plan": [{"task_id": None, "title": "合法", "start": "2026-10-17T09:00:00",
                        "duration": 30},
                       {"task_id": None, "title": "零时长", "start": "2026-10-17T09:00:00",
                        "duration": 0}],
        "inbox_items": [],
        "evidence": [ok_ev,
                     {k: v for k, v in ok_ev.items() if k != "fingerprint"},  # 缺指纹
                     {**ok_ev, "idx": 1}],  # 指向被过滤草案的下标
        "summary": "s"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["evidence"] == [ok_ev]


def test_run_ingest_failed_or_empty_source_no_llm(monkeypatch):
    # 失败/空源不调 LLM，note 如实进 Reduce
    llm_fake = _patch_services(monkeypatch, sources=[
        _src(name="断连源", key="bad", ok=False, error="[认证] 凭据已失效"),
        _src(name="空源", key="empty", ok=True, items=[]),
    ])
    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == []
    assert _reduce_payload(llm_fake)["source_notes"] == [
        "断连源: [认证] 凭据已失效", "空源: 无新条目"]


def test_run_ingest_map_exception_isolated(monkeypatch):
    # 单源 Map 调用炸 → note 记录，其余源候选照常进 Reduce

    class _RaisingOnBad:
        def __init__(self):
            self.reduce_calls = []

        def __call__(self, messages, *, json_mode=False, max_tokens=None):
            if messages[0]["content"] is SOURCE_DIGEST_PROMPT:
                if '"key": "bad"' in messages[1]["content"]:
                    raise RuntimeError("炸了")
                return json.dumps({"source_note": "n", "candidates": [
                    {"title": "好源候选", "item_id": "i1", "when_hint": None,
                     "evidence": None, "confidence": 0.9}]}, ensure_ascii=False)
            self.reduce_calls.append(messages[1]["content"])
            return json.dumps({"draft_plan": [], "inbox_items": [],
                               "evidence": [], "summary": "s"}, ensure_ascii=False)

    fake = _RaisingOnBad()
    _patch_services(monkeypatch, sources=[
        _src(name="坏源", key="bad", items=[_item()]),
        _src(name="好源", key="k1", items=[_item(id_="i1")]),
    ], llm_fake=fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["draft_plan"] == []
    payload = json.loads(fake.reduce_calls[0])
    assert payload["source_notes"][0] == "坏源: 提炼调用失败，已跳过"
    assert payload["candidates"][0]["item_title"] == "条目"


def test_run_ingest_budget_stops_map_reduce_keeps(monkeypatch):
    # 验收⑦：预算设 1 → Map 只跑 1 源即停、第二源如实声明，Reduce 仍出综述
    monkeypatch.setattr(data_ingest, "_LLM_BUDGET_STATE", None)  # 模块态防跨用例串台
    with TestingSessionLocal() as db:
        s = get_settings(db)
        s.ingest_llm_budget = 1
        db.commit()
    llm_fake = _FakeLLM(map_reply=json.dumps(
        {"source_note": "n", "candidates": []}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[
        _src(name="源零", key="k0", items=[_item()]),
        _src(name="源一", key="k1", items=[_item()]),
    ], llm_fake=llm_fake, budget=False)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert len(llm_fake.map_calls) == 1
    assert "源一: 当日预算用尽未提炼" in _reduce_payload(llm_fake)["source_notes"]
    assert result["final_reply"] == "综述"  # Reduce 保底


def test_run_ingest_reduce_exception_propagates(monkeypatch):
    # Reduce 异常按模板上抛（chat 壳译 agent_error、workflows logger 兜底）

    class _ReduceRaises(_FakeLLM):
        def __call__(self, messages, *, json_mode=False, max_tokens=None):
            if messages[0]["content"] is MERGE_PROMPT:
                raise RuntimeError("网络炸了")
            return super().__call__(messages, json_mode=json_mode, max_tokens=max_tokens)

    _patch_services(monkeypatch, sources=[_src(items=[_item()])],
                    llm_fake=_ReduceRaises())
    with TestingSessionLocal() as db:
        with pytest.raises(RuntimeError, match="网络炸了"):
            graph_mod.run_ingest(db, date(2026, 10, 16))


def test_run_ingest_caps_sources_and_candidates(monkeypatch):
    # 源 ≤8（第 9 个只进诚实注记）、每源候选 ≤10
    twelve = [{"title": f"候选{i}", "item_id": "i1", "when_hint": None,
               "evidence": None, "confidence": 0.9} for i in range(12)]
    llm_fake = _FakeLLM(map_reply=json.dumps(
        {"source_note": "n", "candidates": twelve}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[
        _src(name=f"源{i}", key=f"k{i}", items=[_item()]) for i in range(9)
    ], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))
    assert len(llm_fake.map_calls) == 8
    payload = _reduce_payload(llm_fake)
    assert "另有 1 个源超出本轮上限未提炼" in payload["source_notes"]
    assert len(payload["candidates"]) == 80  # 8 源 × 每源截断 10


def test_run_ingest_map_all_fail_reduce_still_runs(monkeypatch):
    # Map 全败 → Reduce 仍跑（输入只有 source_notes）产综述零卡
    llm_fake = _FakeLLM(map_reply="垃圾输出")
    _patch_services(monkeypatch, sources=[
        _src(name="源A", key="ka", items=[_item()]),
        _src(name="源B", key="kb", items=[_item()]),
    ], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["draft_plan"] == [] and result["inbox_items"] == []
    payload = _reduce_payload(llm_fake)
    assert payload["candidates"] == []
    assert all("解析失败" in n for n in payload["source_notes"])


def test_run_ingest_inbox_evidence_backfill(monkeypatch):
    # 溯源素材回填（2026-10-23）：inbox 条目按指纹对上候选 → 候选 evidence 摘录
    # 写进条目（采纳落库三列透传的地基）；候选无 evidence 不写键
    src = _src(items=[_item(id_="i1", title="羽毛球", fingerprint="b" * 40),
                      _item(id_="i2", title="无素材", fingerprint="c" * 40)])
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "n", "candidates": [
            {"title": "羽毛球", "item_id": "i1", "when_hint": None,
             "evidence": "邮件原文摘录", "confidence": 0.9},
            {"title": "无素材", "item_id": "i2", "when_hint": None,
             "evidence": None, "confidence": 0.8}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [],
            "inbox_items": [
                {"title": "订羽毛球场地", "duration": 20, "notes": None,
                 "source_ref": {"source": "测试源", "source_key": "k1", "item_id": "i1",
                                "title": "羽毛球", "time": None, "fingerprint": "b" * 40}},
                {"title": "无素材条目", "duration": 10, "notes": None,
                 "source_ref": {"source": "测试源", "source_key": "k1", "item_id": "i2",
                                "title": "无素材", "time": None, "fingerprint": "c" * 40}}],
            "evidence": [], "summary": "s"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    items = {it["title"]: it for it in result["inbox_items"]}
    assert items["订羽毛球场地"]["evidence"] == "邮件原文摘录"
    assert "evidence" not in items["无素材条目"]  # 候选无素材 → 不写键


def test_run_ingest_provenance_whitelist_drops_fabricated(monkeypatch):
    # LLM 编造溯源：evidence 指纹不在候选集 → 整条丢；无有效来源的条目同弃
    # （2026-10-26 修订，曾置 None 条目保留——溯源被证伪 = 条目本身可疑，且
    # 数据源页每条建议必有明确来源）；无 evidence 覆盖的 draft 条目一并丢弃
    src = _src(items=[_item(fingerprint="a" * 40)])
    fabricated = {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "hack",
                  "title": "编造", "time": None, "fingerprint": "c" * 40}
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "n", "candidates": [
            {"title": "候选", "item_id": "i1", "when_hint": None,
             "evidence": None, "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [{"task_id": None, "title": "合法",
                            "start": "2026-10-17T09:00:00", "duration": 30}],
            "inbox_items": [{"title": "保留条目", "duration": 15, "notes": None,
                             "source_ref": {"source": "测试源", "source_key": "k1",
                                            "item_id": "hack", "title": "编造",
                                            "time": None, "fingerprint": "c" * 40}}],
            "evidence": [fabricated],
            "summary": "s"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)
    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert result["evidence"] == []  # 编造指纹整条丢
    assert result["inbox_items"] == []  # 编造溯源条目整条丢弃，不再置 None 保留
    assert result["draft_plan"] == []  # evidence 全滤后无覆盖 → draft 条目无来源同弃


def test_run_ingest_honest_notes_for_envelope(monkeypatch):
    # 诚实注记：catastrophic 信封 error 与 truncated 如实进综述，不静默
    llm_fake = _patch_services(monkeypatch, sources=[], truncated=True,
                               error="采集异常：TimeoutError（本次无数据）")
    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))
    notes = _reduce_payload(llm_fake)["source_notes"]
    assert "采集层异常：采集异常：TimeoutError（本次无数据）" in notes
    assert "部分源数据被预算截断" in notes


# —— chat 图全链（build_graph 真图 + 四层替身，不联网） —————————————————————


def test_chat_graph_ingest_branch_no_thread_id(monkeypatch):
    # 回归哨兵：langgraph 1.2.11 实证挂 MemorySaver 的图零 config invoke 必抛
    # ValueError——build_graph 默认裸编译，本用例不传 config 直接 invoke 真图
    # （图已路由化：router 默认替身答 ingest，链路 = router → collect_data → refine）
    monkeypatch.setattr(graph_mod, "SessionLocal", TestingSessionLocal)
    src = _src(items=[_item(id_="i1", title="周五评审会", fingerprint="a" * 40)])
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "1 条候选", "candidates": [
            {"title": "周五评审会", "item_id": "i1", "when_hint": "周五 10:00",
             "evidence": None, "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [{"task_id": None, "title": "周五评审会",
                            "start": "2026-10-17T10:00:00", "duration": 60}],
            "inbox_items": [],
            "evidence": [{"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i1",
                          "title": "周五评审会", "time": None, "fingerprint": "a" * 40}],
            "summary": "chat 提炼完成"},
            ensure_ascii=False))
    rules_calls: list[int] = []
    monkeypatch.setattr(ai_data, "list_memory_rules",
                        lambda db: rules_calls.append(1) or [])
    _patch_services(monkeypatch, sources=[src], llm_fake=llm_fake)

    final = graph_mod.build_graph().invoke(
        {"user_input": "看看接入源里有什么要做的", "memory_rules": ["[作息] 早睡"]})

    assert final["draft_plan"][0]["title"] == "周五评审会"
    assert final["final_reply"] == "chat 提炼完成"
    # evidence 随草案保留（每条建议带真实来源，2026-10-26 无来源批）
    assert final["inbox_items"] == [] and len(final["evidence"]) == 1
    # 中间键落终态：collect_data 写入、refine 消费
    assert final["schedule_context"]["sources"][0]["key"] == "k1"
    assert final["dedup_titles"] == []
    # memory_rules 用壳注入值不重查（chat 节点不自开 rules 查询）
    assert rules_calls == []
    assert _reduce_payload(llm_fake)["memory_rules"] == ["[作息] 早睡"]


# —— 秘书视野注入与冲突后验（2026-10-17 增） ————————————————————————————————


def test_run_ingest_reduce_payload_secretary_context(monkeypatch):
    # Reduce payload 携带 load_summary（空库也出 15 天零行）+ settings 四键——
    # 晨间批量推荐首次看得见周围日程密度与作息约束（须带 items 源：零源已改静默轮）
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[_item()])])
    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))
    payload = _reduce_payload(llm_fake)
    assert len(payload["load_summary"]) == 15
    assert all(r["scheduled_count"] == 0 for r in payload["load_summary"])
    assert set(payload["settings"]) == {
        "work_minutes_per_day", "day_start", "day_end", "lunch", "reminder_lead_min"}
    # weather 注入：内存库默认无城市 → None（零外呼，2026-10-18 增）
    assert payload["weather"] is None


def test_run_ingest_draft_clash_postcheck_drops_and_remaps(monkeypatch):
    # Reduce 草案撞真实日历（busy_intervals 系列展开口径）→ 丢弃 + reply 附注 +
    # evidence idx 重排对齐；空档草案照留
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = day0 + timedelta(days=1)
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "2 条候选", "candidates": [
            {"title": "撞车草案", "item_id": "i1", "when_hint": "明天 10:30",
             "evidence": "原文", "confidence": 0.9},
            {"title": "空档草案", "item_id": "i2", "when_hint": "明天 15:00",
             "evidence": "原文", "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [
                {"task_id": None, "title": "撞车草案",
                 "start": t0.replace(hour=10, minute=30).isoformat(), "duration": 60},
                {"task_id": None, "title": "空档草案",
                 "start": t0.replace(hour=15).isoformat(), "duration": 60}],
            "inbox_items": [],
            "evidence": [
                {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i1",
                 "title": "撞车草案", "time": None, "fingerprint": "a" * 40},
                {"idx": 1, "source": "测试源", "source_key": "k1", "item_id": "i2",
                 "title": "空档草案", "time": None, "fingerprint": "b" * 40}],
            "summary": "已提炼 2 条"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="撞车草案", fingerprint="a" * 40),
        _item(id_="i2", title="空档草案", fingerprint="b" * 40)])], llm_fake=llm_fake)

    with TestingSessionLocal() as db:
        create_task(db, TaskCreate(  # 明天 10:00-11:00 既有安排
            title="既有会议", start_time=t0.replace(hour=10),
            end_time=t0.replace(hour=11), duration_mins=60))
        result = graph_mod.run_ingest(db, date(2026, 10, 16))

    assert [d["title"] for d in result["draft_plan"]] == ["空档草案"]
    assert [e["idx"] for e in result["evidence"]] == [0]  # 留存条目 idx 重排对齐
    assert result["evidence"][0]["item_id"] == "i2"
    assert "撞车草案" in result["final_reply"] and "冲突" in result["final_reply"]


def test_run_ingest_draft_gates_drop_lunch_and_window(monkeypatch):
    # 全闸后验（2026-11-01 优化批 1）：busy 撞期之外，午休内（默认 11:30-13:30）
    # 与可排时段外（默认止 22:00）的草案同样丢弃——MERGE 让 LLM 自排 start，
    # 合法性口径与引擎硬闸一致（is_valid_slot 同款五闸）
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = day0 + timedelta(days=1)
    llm_fake = _FakeLLM(
        map_reply=json.dumps({"source_note": "3 条候选", "candidates": [
            {"title": "午休草案", "item_id": "i1", "when_hint": "明天 12:00",
             "evidence": "原文", "confidence": 0.9},
            {"title": "深夜草案", "item_id": "i2", "when_hint": "明天 23:00",
             "evidence": "原文", "confidence": 0.9},
            {"title": "合法草案", "item_id": "i3", "when_hint": "明天 15:00",
             "evidence": "原文", "confidence": 0.9}]}, ensure_ascii=False),
        reduce_reply=json.dumps({
            "draft_plan": [
                {"task_id": None, "title": "午休草案",
                 "start": t0.replace(hour=12).isoformat(), "duration": 60},
                {"task_id": None, "title": "深夜草案",
                 "start": t0.replace(hour=23).isoformat(), "duration": 60},
                {"task_id": None, "title": "合法草案",
                 "start": t0.replace(hour=15).isoformat(), "duration": 60}],
            "inbox_items": [],
            "evidence": [
                {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i1",
                 "title": "午休草案", "time": None, "fingerprint": "a" * 40},
                {"idx": 1, "source": "测试源", "source_key": "k1", "item_id": "i2",
                 "title": "深夜草案", "time": None, "fingerprint": "b" * 40},
                {"idx": 2, "source": "测试源", "source_key": "k1", "item_id": "i3",
                 "title": "合法草案", "time": None, "fingerprint": "c" * 40}],
            "summary": "已提炼 3 条"}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="午休草案", fingerprint="a" * 40),
        _item(id_="i2", title="深夜草案", fingerprint="b" * 40),
        _item(id_="i3", title="合法草案", fingerprint="c" * 40)])], llm_fake=llm_fake)

    with TestingSessionLocal() as db:
        result = graph_mod.run_ingest(db, date(2026, 10, 16))

    assert [d["title"] for d in result["draft_plan"]] == ["合法草案"]
    assert [e["idx"] for e in result["evidence"]] == [0]
    assert result["evidence"][0]["item_id"] == "i3"
    assert "作息" in result["final_reply"]


def test_refine_chat_draft_postcheck_matches_run_ingest(monkeypatch):
    # chat 采集轮补后验（2026-11-01 优化批 1）：refine 与 run_ingest 同体防御
    # ——此前 chat 轮草案零后验，LLM 自排的时间可撞真实日历（同体管道不同
    # 防御的缺口）。_map_phase 替身直供候选，只验 refine 出口的撞期丢弃
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = day0 + timedelta(days=1)

    def fake_map(_db, _raw):
        cands = []
        for title, item_id, fp in (("撞车草案", "i1", "a" * 40),
                                   ("空档草案", "i2", "b" * 40)):
            cands.append({"title": title, "item_id": item_id, "when_hint": "明天",
                          "evidence": "原文", "confidence": 0.9, "source": "测试源",
                          "source_key": "k1", "item_title": title, "time": None,
                          "fingerprint": fp})
        return cands, ["测试源: 2 条候选"]

    monkeypatch.setattr(graph_mod, "_map_phase", fake_map)
    llm_fake = _FakeLLM(reduce_reply=json.dumps({
        "draft_plan": [
            {"task_id": None, "title": "撞车草案",
             "start": t0.replace(hour=10, minute=30).isoformat(), "duration": 60},
            {"task_id": None, "title": "空档草案",
             "start": t0.replace(hour=15).isoformat(), "duration": 60}],
        "inbox_items": [],
        "evidence": [
            {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i1",
             "title": "撞车草案", "time": None, "fingerprint": "a" * 40},
            {"idx": 1, "source": "测试源", "source_key": "k1", "item_id": "i2",
             "title": "空档草案", "time": None, "fingerprint": "b" * 40}],
        "summary": "chat 提炼完成"}, ensure_ascii=False))
    monkeypatch.setattr(llm, "call_llm", llm_fake)
    monkeypatch.setattr(graph_mod, "SessionLocal", TestingSessionLocal)
    with TestingSessionLocal() as db:
        create_task(db, TaskCreate(  # 明天 10:00-11:00 既有安排
            title="既有会议", start_time=t0.replace(hour=10),
            end_time=t0.replace(hour=11), duration_mins=60))

    out = graph_mod.refine({"schedule_context": {}, "dedup_titles": [],
                            "memory_rules": []})

    assert [d["title"] for d in out["draft_plan"]] == ["空档草案"]
    assert [e["idx"] for e in out["evidence"]] == [0]
    assert "撞车草案" in out["final_reply"] and "冲突" in out["final_reply"]


# —— 高可信直通（2026-10-24）：workflows 与 chat 壳两处分流落库 ———————————————

_FP_TODO = "a" * 40
_FP_DOC = "c" * 40
_FP_CAL = "b" * 40


def _seed_trusted_ledger():
    """预置 trusted 源行 + 三域台账行（todo/calendar 直落、doc 建卡）。"""
    from models import IngestSource, IngestionItem

    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="k1", trusted=True))
        for fp, dom, title in ((_FP_TODO, "todo", "评审材料"),
                               (_FP_CAL, "calendar", "周五评审会"),
                               (_FP_DOC, "doc", "笔记待办")):
            db.add(IngestionItem(source_key="k1", fingerprint=fp,
                                 item_title=title, domain=dom))
        db.commit()


def _direct_result():
    """run_ingest 假产物：1 条 todo 待办 + 1 条 calendar 草案 + 1 条 doc 待办。"""
    return {
        "draft_plan": [{"task_id": None, "title": "周五评审会",
                        "start": "2026-10-25T10:00:00", "duration": 60}],
        "inbox_items": [
            {"title": "评审材料", "duration": 30, "notes": None,
             "source_ref": {"source": "测试源", "source_key": "k1", "item_id": "i1",
                            "title": "评审材料", "time": None, "fingerprint": _FP_TODO}},
            {"title": "笔记提炼的待办", "duration": None, "notes": None,
             "source_ref": {"source": "测试源", "source_key": "k1", "item_id": "i3",
                            "title": "笔记提炼的待办", "time": None, "fingerprint": _FP_DOC}},
        ],
        "evidence": [
            {"idx": 0, "source": "测试源", "source_key": "k1", "item_id": "i2",
             "title": "周五评审会", "time": None, "fingerprint": _FP_CAL}],
        "final_reply": "已提炼 3 条",
    }


def test_run_ingest_workflow_trusted_direct(monkeypatch):
    # 定时/手动采集链路：trusted 源 todo/calendar 域直落（Task 落库 + accepted
    # 结算 + reply 注记），doc 域待办照旧建卡等确认
    from models import IngestSource, Proposal, Task

    import services.workflows as wf

    monkeypatch.setattr(wf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(wf, "_load_agent", lambda m, a: (lambda db, day: _direct_result()))
    messages: list[tuple] = []
    monkeypatch.setattr(wf, "append_message", lambda db, **kw: messages.append(kw) or None)
    broadcasts: list[dict] = []
    monkeypatch.setattr(wf, "broadcast", lambda ev: broadcasts.append(ev))
    _seed_trusted_ledger()
    import uuid as _uuid

    from models import ModelProvider

    with TestingSessionLocal() as db:
        db.add(ModelProvider(name=f"m-{_uuid.uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()

    import asyncio

    asyncio.run(wf._run_ingest())

    with TestingSessionLocal() as db:
        tasks = {t.title: t for t in db.scalars(select(Task)).all()}
        assert tasks["周五评审会"].start_time is not None        # calendar 域直落日历
        assert tasks["周五评审会"].source_fingerprint == _FP_CAL  # 溯源三列落地
        assert tasks["评审材料"].start_time is None               # todo 域直落收件箱
        assert tasks["评审材料"].source_key == "k1"
        assert "笔记提炼的待办" not in tasks                        # doc 域不直落
        props = db.scalars(select(Proposal)).all()
        settled = {p.kind: p for p in props if p.outcome == "accepted"}
        pending = [p for p in props if p.outcome is None]
        assert set(settled) == {"inbox", "schedule"}               # 两组直落各一张结算卡
        assert settled["schedule"].payload["draft_plan"][0]["title"] == "周五评审会"
        assert len(pending) == 1 and pending[0].kind == "inbox"     # doc 待办建卡待确认
        assert pending[0].payload["items"][0]["title"] == "笔记提炼的待办"
    # 综述不落聊天消息（2026-10-25 功能边界）：直通注记随 broadcast headline 可见
    assert not messages
    assert broadcasts[0]["type"] == "ingest"
    assert "高可信源直通：1 条日程直落日历、1 条待办直落收件箱" in broadcasts[0]["headline"]


def test_finalize_turn_trusted_direct_events(monkeypatch):
    # chat 壳链路：直落草案出 applied 事件（前端刷新画布）、直落待办 accepted 结算
    # 无卡片事件、doc 待办照旧 proposal 事件、reply 注记
    from models import Proposal, Task
    from services.chat_turn import finalize_turn

    _seed_trusted_ledger()
    with TestingSessionLocal() as db:
        events = finalize_turn(db, _direct_result())

        tasks = {t.title: t for t in db.scalars(select(Task)).all()}
        assert tasks["周五评审会"].start_time is not None
        assert tasks["评审材料"].start_time is None
        assert "笔记提炼的待办" not in tasks
        applied = [e for e in events if e["type"] == "applied"]
        assert len(applied) == 1 and applied[0]["tasks"][0]["title"] == "周五评审会"
        proposals = [e for e in events if e["type"] == "proposal"]
        assert len(proposals) == 1 and proposals[0]["kind"] == "inbox"  # doc 待办建卡
        reply = next(e for e in events if e["type"] == "reply")
        assert "高可信源直通：1 条日程直落日历、1 条待办直落收件箱" in reply["content"]
        props = db.scalars(select(Proposal)).all()
        assert {p.kind for p in props if p.outcome == "accepted"} == {"inbox", "schedule"}


# —— Map 并发提炼（2026-10-26 提速批 3）————————————————————————————————


def _map_direct(monkeypatch, sources, map_impl):
    """_map_phase 直调壳：call_llm 只拦 Map 侧（其他 system prompt 到达即断言炸），
    返回 (candidates, notes)——绕 run_ingest 聚焦 Map 并发语义。"""
    monkeypatch.setattr(llm, "call_llm", map_impl)
    with TestingSessionLocal() as db:
        return graph_mod._map_phase(db, {"sources": sources})


def test_map_phase_concurrent_and_order_preserved(monkeypatch):
    # ≤8 源 LLM 曾串行 for——改池限 3 并发：barrier 三方齐到才放行（串行则
    # 超时炸 BrokenBarrier → 源记「失败」被断言逮住），结果按源序归位零漂移
    import threading

    barrier = threading.Barrier(3, timeout=5)
    threads: set[int] = set()

    def slow_map(messages, *, json_mode=False, max_tokens=None):
        threads.add(threading.current_thread().ident)
        barrier.wait()  # 三源并发全到位才继续——串行实现必超时
        return json.dumps({"source_note": "ok", "candidates": []}, ensure_ascii=False)

    cands, notes = _map_direct(monkeypatch, [
        _src(name="源零", key="k0", items=[_item()]),
        _src(name="源一", key="k1", items=[_item()]),
        _src(name="源二", key="k2", items=[_item()]),
    ], slow_map)
    assert len(threads) == 3  # 真三线程并发（单源路径才零池）
    assert cands == []
    assert notes == ["源零: ok", "源一: ok", "源二: ok"]  # 源序归位


def test_map_phase_budget_grants_remainder_in_order(monkeypatch):
    # 预算主线程预扣：cap=2、3 源 → 授 2 停 1（旧逐源语义保持——剩 1 提 1，
    # 不整批拒绝）；「预算用尽」如实注记在第 3 源
    monkeypatch.setattr(data_ingest, "_LLM_BUDGET_STATE", None)
    with TestingSessionLocal() as db:
        s = get_settings(db)
        s.ingest_llm_budget = 2
        db.commit()
    calls: list[str] = []

    def map_impl(messages, *, json_mode=False, max_tokens=None):
        calls.append(messages[1]["content"])
        return json.dumps({"source_note": "ok", "candidates": []}, ensure_ascii=False)

    cands, notes = _map_direct(monkeypatch, [
        _src(name="源零", key="k0", items=[_item()]),
        _src(name="源一", key="k1", items=[_item()]),
        _src(name="源二", key="k2", items=[_item()]),
    ], map_impl)
    assert len(calls) == 2
    assert "源零: ok" in notes and "源一: ok" in notes
    assert "源二: 当日预算用尽未提炼" in notes


def test_map_phase_refunds_failed_source(monkeypatch):
    # 失败源额度返还：cap=3、3 源全预扣、源一 LLM 抛异常 → 计数 3-1=2
    #（预算按实际消耗计，失败重试不白扣）
    monkeypatch.setattr(data_ingest, "_LLM_BUDGET_STATE", None)
    with TestingSessionLocal() as db:
        s = get_settings(db)
        s.ingest_llm_budget = 3
        db.commit()

    def flaky_map(messages, *, json_mode=False, max_tokens=None):
        if "k1" in messages[1]["content"]:
            raise RuntimeError("提炼失败（测试注入）")
        return json.dumps({"source_note": "ok", "candidates": []}, ensure_ascii=False)

    cands, notes = _map_direct(monkeypatch, [
        _src(name="源零", key="k0", items=[_item()]),
        _src(name="源一", key="k1", items=[_item()]),
        _src(name="源二", key="k2", items=[_item()]),
    ], flaky_map)
    assert "源一: 提炼调用失败，已跳过" in notes
    assert data_ingest._LLM_BUDGET_STATE[1] == 2  # 3 预扣 − 1 返还


def test_run_ingest_reduce_carries_chat_summary(monkeypatch):
    # Reduce 注入滚动摘要（优化批 4）：采集提炼知道用户最近在忙什么——
    # 「最近在准备 X」的相关素材自然优先；_prep_aux 并行取数（批 3）一并覆盖
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="用户在准备发布会", count=2)
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[_item()])])
    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))
    assert _reduce_payload(llm_fake)["chat_summary"] == "用户在准备发布会"


def test_map_phase_seen_count_low_priority_annotation(monkeypatch):
    """seen_count 消费（优化批 5）：反复出现（≥3 次）且从未进过建议卡的素材带
    低优先标记进 Map 输入（台账从只写不读变闭环）；已进过卡与新条目不带。"""
    from models import IngestionItem

    with TestingSessionLocal() as db:
        db.add(IngestionItem(source_key="k1", fingerprint="a" * 40,
                             item_title="老条目", seen_count=5))
        db.add(IngestionItem(source_key="k1", fingerprint="b" * 40,
                             item_title="已提过", seen_count=5,
                             proposed_at=datetime.now()))
        db.commit()
    llm_fake = _FakeLLM(map_reply=json.dumps(
        {"source_note": "1 条候选", "candidates": []}, ensure_ascii=False))
    _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="老条目", fingerprint="a" * 40),
        _item(id_="i2", title="已提过", fingerprint="b" * 40),
        _item(id_="i3", title="新条目", fingerprint="c" * 40)])], llm_fake=llm_fake)

    with TestingSessionLocal() as db:
        graph_mod.run_ingest(db, date(2026, 10, 16))

    inner = llm_fake.map_calls[0].split("<<<DATA\n", 1)[1].rsplit("\nDATA>>>", 1)[0]
    items = {it["id"]: it for it in json.loads(inner)["items"]}
    assert items["i1"].get("seen_count") is True      # ≥3 次未处理 → 低优先标记
    assert "seen_count" not in items["i2"]            # 已进过建议卡
    assert "seen_count" not in items["i3"]            # 首次出现


# —— 结构化直映射零 LLM（2026-11-05 采集批，用户拍板） ————————————————————————


def test_run_ingest_structured_zero_llm(monkeypatch):
    """todo/calendar 域条目零 LLM 直映射：时间照抄源数据（带未来时间 → 日历
    草案；无时间 → 收件箱待办），Map/Reduce 一次 LLM 都不调。"""
    # 明天 15:00（固定安全时刻：避开午休窗/日边界——now+24h 同时分秒会让
    # 后验随测试运行时段随机丢条，11:30-13:30 跑测试必挂）
    future = ((datetime.now() + timedelta(days=1))
              .replace(hour=15, minute=0, second=0, microsecond=0)
              .isoformat(timespec="seconds"))
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="带时间日程", fingerprint="a" * 40,
              time=future, domain="calendar"),
        _item(id_="i2", title="时间待定的待办", fingerprint="b" * 40,
              time=None, domain="todo")])])
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []  # 零 LLM
    assert [d["title"] for d in out["draft_plan"]] == ["带时间日程"]
    assert out["draft_plan"][0]["start"] == future      # 时间照抄不编造
    assert [i["title"] for i in out["inbox_items"]] == ["时间待定的待办"]
    ref = out["inbox_items"][0].get("source_ref", {})
    assert ref.get("fingerprint") == "b" * 40
    # source_ref 六键齐全（数据源页组名/徽标读 source 显示名——真机显示批回归）
    assert ref.get("source") == "测试源" and ref.get("source_key") == "k1"
    assert ref.get("item_id") == "i2" and ref.get("title") == "时间待定的待办"
    assert out["evidence"][0]["fingerprint"] == "a" * 40
    assert "零 LLM" in out["final_reply"]


def test_run_ingest_structured_tz_and_past_keep_time(monkeypatch):
    """日期丢失回归（2026-11-05 真机批）：滴答 MCP 返回带 +08:00 时区的日程，
    旧版直映射 `tzinfo is None` 判定不成立 → 带时间日程全降级无时间待办。
    修复后：带时区转本地 naive 进日历；过去带时间的日程也照抄进日历（同步
    语义，窗口钳制 ≤168h）。"""
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="带时区日程", fingerprint="a" * 40,
              time="2026-11-05T10:01:35+08:00", domain="todo"),
        _item(id_="i2", title="过去日程", fingerprint="b" * 40,
              time="2026-11-02T14:02:36+08:00", domain="calendar")])])
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []
    starts = {d["title"]: d["start"] for d in out["draft_plan"]}
    assert "T10:01:35" in starts["带时区日程"] and "+" not in starts["带时区日程"]
    assert starts["过去日程"].startswith("2026-11-02T14:02:36")
    assert out["inbox_items"] == []           # 日期不再丢失降级


def test_run_ingest_structured_never_fabricates_time(monkeypatch):
    """回归（真机铁证）：无时间的结构化待办绝不被排期——旧链 Map+Reduce LLM
    把滴答「时间待定」待办编造成次日 09:30 日历事件直通落库。"""
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="项目启动会（时间待定）", fingerprint="a" * 40,
              time=None, domain="todo")])])
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []
    assert out["draft_plan"] == []
    assert len(out["inbox_items"]) == 1  # 恒收件箱待办（无时间）
    assert out["inbox_items"][0]["title"] == "项目启动会（时间待定）"


def test_run_ingest_structured_title_dedup(monkeypatch):
    """直映射确定性查重：与既有任务归一化同名 → 跳过 + reply 计数注记。"""
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="项目启动会（时间待定）", fingerprint="a" * 40,
              time=None, domain="todo")])],
        existing=["项目启动会(时间待定)"])  # 标点差异归一化后同名
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert out["inbox_items"] == [] and out["draft_plan"] == []
    assert "同名跳过" in out["final_reply"]


def test_run_ingest_mixed_source_doc_goes_llm_structured_skips(monkeypatch):
    """同源混合域：doc 条目进 LLM（Map payload 只含 doc 条目），结构化条目
    直映射；Reduce 只喂 doc 候选（structured 不进 LLM 输入）。"""
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="待办", fingerprint="a" * 40, domain="todo"),
        _item(id_="i2", title="笔记素材", fingerprint="b" * 40, domain="doc")])],
        llm_fake=_FakeLLM(
            map_reply=json.dumps({"source_note": "1 条候选", "candidates": [
                {"title": "笔记提炼待办", "item_id": "i2"}]}, ensure_ascii=False),
            reduce_reply=json.dumps({
                "draft_plan": [], "inbox_items": [
                    {"title": "笔记提炼待办", "duration": None, "notes": None,
                     "source_ref": {"source_key": "k1", "fingerprint": "b" * 40}}],
                "evidence": [], "summary": "doc 综述"}, ensure_ascii=False)))
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert len(llm_fake.map_calls) == 1
    assert '"i1"' not in llm_fake.map_calls[0]          # 结构化条目不进 Map
    assert '"i2"' in llm_fake.map_calls[0]
    payload = _reduce_payload(llm_fake)
    assert payload["candidates"] and payload["candidates"][0].get("item_id") == "i2"
    titles = [i["title"] for i in out["inbox_items"]]
    assert titles == ["笔记提炼待办", "待办"]           # LLM 产物 + 直映射并入
    assert "doc 综述" in out["final_reply"] and "零 LLM" in out["final_reply"]


def test_run_ingest_structured_round_failure_note_kept(monkeypatch):
    """全结构化轮跳过 Reduce 后，失败源注记确定性直列（不静默丢信息）。"""
    llm_fake = _patch_services(monkeypatch, sources=[
        _src(items=[_item(id_="i1", title="待办", fingerprint="a" * 40,
                          domain="todo")]),
        _src(name="lark", key="cli:lark", ok=False, error="CLI 执行失败")])
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.reduce_calls == []
    assert "请注意" in out["final_reply"] and "lark" in out["final_reply"]


def test_run_ingest_sync_draft_skips_postvalidate_and_priority(monkeypatch):
    """同步草案跳过后验 + 优先级映射（2026-11-05 真机批）：偏好避让窗把同步
    日程全丢（真机用户快照复现——后验是 LLM 建议的兜底不是同步的约束）；
    滴答优先级 1/3/5 → 高/中/低，无优先级默认低。"""
    # 避让窗规则全天生效（若后验不跳过，任何时段草案都会被拒）
    future = ((datetime.now() + timedelta(days=1))
              .replace(hour=15, minute=0, second=0, microsecond=0)
              .isoformat(timespec="seconds"))
    llm_fake = _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="高优日程", fingerprint="a" * 40,
              time=future, domain="calendar", priority=1),
        _item(id_="i2", title="无优待办", fingerprint="b" * 40,
              time=None, domain="todo", priority=0),
        _item(id_="i3", title="低优待办", fingerprint="c" * 40,
              time=None, domain="todo", priority=5)])],
        rules=[SimpleNamespace(category="排期偏好", content="每天零点到二十三点不要排事",
                               source="user_manual", confidence=1.0)])
    with TestingSessionLocal() as db:
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert llm_fake.map_calls == [] and llm_fake.reduce_calls == []
    assert len(out["draft_plan"]) == 1 and out["draft_plan"][0]["_sync"] is True
    assert out["draft_plan"][0]["priority"] == 1            # 滴答 1=高
    prios = {i["title"]: i["priority"] for i in out["inbox_items"]}
    assert prios["无优待办"] == 3 and prios["低优待办"] == 3  # 0=无→默认低；5=低
    assert "丢弃" not in out["final_reply"]                  # 后验不再吃同步草案


def test_run_ingest_structured_fingerprint_dedup_skips_replay(monkeypatch):
    """指纹溯源防重复（2026-11-05）：条目指纹命中既有 Task.source_fingerprint →
    跳过（曾以其他形态落库的同一源条目不重复同步；时间升级自动化登记后续）。"""
    future = ((datetime.now() + timedelta(days=1))
              .replace(hour=15, minute=0, second=0, microsecond=0)
              .isoformat(timespec="seconds"))
    _patch_services(monkeypatch, sources=[_src(items=[
        _item(id_="i1", title="已有任务的同源日程", fingerprint="a" * 40,
              time=future, domain="calendar"),
        _item(id_="i2", title="全新待办", fingerprint="b" * 40,
              time=None, domain="todo")])])
    with TestingSessionLocal() as db:
        from models import Task
        from services.tasks import create_task as _ct

        _ct(db, TaskCreate(title="已有任务的同源日程", priority=2))
        db.execute(Task.__table__.update().where(Task.title == "已有任务的同源日程")
                   .values(source_fingerprint="a" * 40))
        db.commit()
        out = graph_mod.run_ingest(db, date(2026, 10, 16))
    assert out["draft_plan"] == []                          # 指纹命中跳过
    assert [i["title"] for i in out["inbox_items"]] == ["全新待办"]
