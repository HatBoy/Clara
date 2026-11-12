"""对话滚动上下文（2026-10-20）：chat_context / compaction_due / pending_compaction_slice /
save_chat_summary / compact_chat_history 直测——三层有界上下文（近期原文 + 滚动摘要 +
长期画像）。2026-10-26 增：双阈值触发 + force 强制压缩 + 切块循环 + CompactPartialError。

游标语义：messages append-only，summarized_count 位置稳定；压缩窗口 = [cursor : n-K]。
"""

import json

import pytest

from agents import graph
from conftest import TestingSessionLocal
from services import ai_data


def _msgs(n: int) -> None:
    """落 n 条消息（user/assistant 交替，正文可区分）。"""
    with TestingSessionLocal() as db:
        for i in range(n):
            ai_data.append_message(
                db, role="user" if i % 2 == 0 else "assistant", content=f"msg-{i}")
        ai_data.append_message(db, role="user", content="本轮原话")


def test_chat_context_empty_db():
    """空库：摘要 None + 零历史（消息数组退化 [system, user]，旧形状零漂移）。"""
    with TestingSessionLocal() as db:
        summary, history = ai_data.chat_context(db, exclude="本轮原话")
        assert summary is None
        assert history == []


def test_chat_context_returns_recent_k_in_order():
    """正序最近 K 条：11 条（含本轮）→ 剔本轮后取最近 10 条正序。"""
    _msgs(11)
    with TestingSessionLocal() as db:
        summary, history = ai_data.chat_context(db, exclude="本轮原话")
        assert summary is None
        assert [m.content for m in history] == [f"msg-{i}" for i in range(1, 11)]


def test_chat_context_exclude_drops_this_turn():
    """exclude 只剔一条：历史里恰好有同文句时只删尾部匹配的那条（不误伤更早同文句）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="记一下")
        ai_data.append_message(db, role="assistant", content="好")
        ai_data.append_message(db, role="user", content="记一下")  # 尾部同文句
        summary, history = ai_data.chat_context(db, exclude="记一下")
        assert [m.content for m in history] == ["记一下", "好"]


def test_chat_context_with_summary():
    """摘要非空时随历史一并返回（节点把摘要追加到 system 尾部）。"""
    _msgs(2)
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="此前聊过搬家与体检", count=2)
        summary, history = ai_data.chat_context(db, exclude="本轮原话")
        assert summary == "此前聊过搬家与体检"
        assert [m.content for m in history] == ["msg-0", "msg-1"]


def test_clear_chat_session_resets_cursor():
    """/clear（2026-10-21）：消息全删 + 滚动摘要与游标归零——上下文注入从零
    起步（ChatState docstring 预留的语义落地）。"""
    _msgs(4)
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="旧摘要", count=3)
        ai_data.clear_chat_session(db)
        assert ai_data.list_messages(db) == []
        state = ai_data.get_chat_state(db)
        assert state.summary is None and state.summarized_count == 0
        summary, history = ai_data.chat_context(db)
        assert summary is None and history == []


def test_compaction_due_below_threshold_noop():
    """未到阈值：未摘要数 ≤ K+缓冲 → None（零 LLM 前提）。"""
    _msgs(15)
    with TestingSessionLocal() as db:
        assert ai_data.compaction_due(db) is None


def test_compaction_due_returns_slice():
    """超阈值：16 条原文 + 1 条本轮 → 17 条，片段 = 前 7 条（保留最近 K=10 不动）。"""
    _msgs(16)
    with TestingSessionLocal() as db:
        fragment = ai_data.compaction_due(db)
        assert fragment is not None
        assert [m.content for m in fragment] == [f"msg-{i}" for i in range(7)]


def test_compaction_due_cursor_no_replay():
    """游标推进后不重放：save(count=7) → n-cursor=10 ≤ 阈值不再触发；
    再来 10 条 → 新片段从 msg-7 起到 msg-16（不含已摘要与保留窗）。"""
    _msgs(16)
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="s", count=7)
        assert ai_data.compaction_due(db) is None
        for i in range(10):
            ai_data.append_message(db, role="user", content=f"later-{i}")
        fragment = ai_data.compaction_due(db)
        contents = [m.content for m in fragment]
        assert contents == [f"msg-{i}" for i in range(7, 16)] + ["本轮原话"]


def test_compaction_due_char_threshold():
    """双阈值之字符阈值（2026-10-26）：条数不达（切片 2 条 ≤ 缓冲 6）但积压字符
    超限也触发——长消息被后续对话挤出保留窗后靠字符口径压（条数口径漏掉的形态；
    短消息两阈皆不达的负例见 test_compaction_due_below_threshold_noop）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="长" * 3500)
        ai_data.append_message(db, role="user", content="长" * 3500)
        for i in range(9):
            ai_data.append_message(db, role="user", content=f"短-{i}")
        ai_data.append_message(db, role="user", content="本轮原话")
        fragment = ai_data.compaction_due(db)  # n=12 → 切片 [0:2] = 两条长消息
        assert fragment is not None
        assert len(fragment) == 2 and len(fragment[0].content) == 3500


def test_pending_slice_matches_due_slice():
    """切片口径单源：compaction_due 命中时与 pending_compaction_slice 返回全等
    （force 与自动路径共用同一刀切，防止两套切片漂移）。"""
    _msgs(16)
    with TestingSessionLocal() as db:
        due = ai_data.compaction_due(db)
        pending = ai_data.pending_compaction_slice(db)
        assert due is not None
        assert [m.id for m in due] == [m.id for m in pending]


def test_compact_noop_below_threshold(monkeypatch):
    """未到阈值零 LLM：call_llm 替身若被调即炸（哨兵）。"""
    _msgs(15)

    def _boom(*_a, **_k):
        raise AssertionError("未到阈值不应调用 LLM")

    monkeypatch.setattr("services.llm.call_llm", _boom)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db) == 0


def test_compact_runs_and_advances_cursor(monkeypatch):
    """压缩执行：替身 call_llm 返回摘要 → 落库 + 游标 = 片段长（7）。"""
    _msgs(16)
    calls: list = []

    def fake_call_llm(messages, **kwargs):
        calls.append(messages)
        return "合并后的摘要"

    monkeypatch.setattr("services.llm.call_llm", fake_call_llm)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db) == 7
        state = ai_data.get_chat_state(db)
        assert state.summary == "合并后的摘要"
        assert state.summarized_count == 7
    assert len(calls) == 1


def test_compact_empty_output_keeps_cursor(monkeypatch):
    """空输出 = 块级中断（done=0）：不落库不推游标（下轮窗口不变自然重试）。"""
    _msgs(16)
    monkeypatch.setattr("services.llm.call_llm", lambda *a, **k: "  ")
    with TestingSessionLocal() as db:
        with pytest.raises(ai_data.CompactPartialError) as ei:
            graph.compact_chat_history(db)
        assert ei.value.done == 0
        assert ai_data.get_chat_state(db).summarized_count == 0


def test_compact_force_below_threshold(monkeypatch):
    """force 跳过阈值（/compact 语义，2026-10-26）：12 条未达条数阈值，非 force
    零 LLM；force 仍把切片 [0:2] 并入摘要。"""
    _msgs(11)

    def _boom(*_a, **_k):
        raise AssertionError("未到阈值不应调用 LLM")

    monkeypatch.setattr("services.llm.call_llm", _boom)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db) == 0

    calls: list = []

    def fake_call_llm(messages, **kwargs):
        calls.append(messages)
        return "强制摘要"

    monkeypatch.setattr("services.llm.call_llm", fake_call_llm)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db, force=True) == 2
        state = ai_data.get_chat_state(db)
        assert state.summary == "强制摘要" and state.summarized_count == 2
    assert len(calls) == 1


def test_compact_chunking_multi_llm(monkeypatch):
    """切块循环（2026-10-26）：积压字符超单块上限自动分多次 LLM 滚动合并，
    逐块推游标，第二块的 prev_summary 是第一块产物（合并链）。"""
    with TestingSessionLocal() as db:
        for _ in range(20):
            ai_data.append_message(db, role="user", content="字" * 1200)
        ai_data.append_message(db, role="user", content="本轮原话")
    # n=21 → 切片 11 条 × 1200 字：贪心分 10+1 两块（10×1200=12000 不超上限）
    calls: list = []

    def fake_call_llm(messages, **kwargs):
        calls.append(messages)
        return f"摘要-{len(calls)}"

    monkeypatch.setattr("services.llm.call_llm", fake_call_llm)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db, force=True) == 11
        state = ai_data.get_chat_state(db)
        assert state.summary == "摘要-2" and state.summarized_count == 11
    assert len(calls) == 2
    second = json.loads(calls[1][1]["content"])
    assert second["prev_summary"] == "摘要-1" and len(second["messages"]) == 1


def test_compact_partial_failure_keeps_done(monkeypatch):
    """块级中断（2026-10-26）：第 2 块调用失败 → CompactPartialError.done = 已
    成功块条数，库内游标已推进（断点续压：再执行从游标续）。"""
    with TestingSessionLocal() as db:
        for _ in range(20):
            ai_data.append_message(db, role="user", content="字" * 1200)
        ai_data.append_message(db, role="user", content="本轮原话")
    calls: list = []

    def fake_call_llm(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return "第一块摘要"
        raise RuntimeError("llm down")

    monkeypatch.setattr("services.llm.call_llm", fake_call_llm)
    with TestingSessionLocal() as db:
        with pytest.raises(ai_data.CompactPartialError) as ei:
            graph.compact_chat_history(db, force=True)
        assert ei.value.done == 10
        state = ai_data.get_chat_state(db)
        assert state.summarized_count == 10 and state.summary == "第一块摘要"


def test_compact_force_no_pending_zero(monkeypatch):
    """无可压内容（空库）force → 0，零 LLM（哨兵）。"""

    def _boom(*_a, **_k):
        raise AssertionError("无待摘要不应调用 LLM")

    monkeypatch.setattr("services.llm.call_llm", _boom)
    with TestingSessionLocal() as db:
        assert graph.compact_chat_history(db, force=True) == 0


def test_autocompact_switch_gates_background(monkeypatch):
    """开关 gate（2026-10-26）：chat_autocompact_enabled 关 → compact_history_async
    开 SessionLocal 后即早退（compact_chat_history 零调用）；重开照常进入——
    手动 /compact 端点不经此函数，开关语义 = 只关自动。"""
    from services import chat_turn
    from services.settings import get_settings

    calls: list = []
    monkeypatch.setattr("agents.graph.compact_chat_history",
                        lambda *a, **k: calls.append(a) or 0)
    with TestingSessionLocal() as db:
        get_settings(db).chat_autocompact_enabled = False
        db.commit()
    chat_turn.compact_history_async()
    assert calls == []  # 关：早退零调用（哪怕积压超阈值）
    with TestingSessionLocal() as db:
        get_settings(db).chat_autocompact_enabled = True
        db.commit()
    chat_turn.compact_history_async()
    assert len(calls) == 1  # 开：进入压缩链（打桩替身不落库不炸）


def test_chat_recent_k_adapts_to_context_length():
    """注入窗自适应（2026-10-26）：按激活模型 context_length 映射档位——小窗
    收窄让位摘要（省预算）、大窗拉长活记忆（1M 档 16 条原文）；无激活模型
    回落 CHAT_RECENT_K 现状值（压缩保留窗口径不变）。"""
    from models import ModelProvider

    def _set_active(ctx_len: int) -> None:
        with TestingSessionLocal() as db:
            db.query(ModelProvider).delete()  # 激活行唯一，逐档轮换
            db.add(ModelProvider(name="m", model_name="x/y",
                                 is_active=True, context_length=ctx_len))
            db.commit()

    with TestingSessionLocal() as db:
        assert ai_data.chat_recent_k(db) == 10  # 无激活模型 → 默认窗
    for ctx_len, k in ((8192, 6), (16384, 6), (32768, 10), (65536, 10),
                       (131072, 12), (262144, 12), (1048576, 16)):
        _set_active(ctx_len)
        with TestingSessionLocal() as db:
            assert ai_data.chat_recent_k(db) == k, ctx_len

def test_compaction_due_chars_threshold_countfirst():
    """字符阈值经 count-first 快路径（优化批 3）：条数远未达阈值但窗口内两条
    7000 字长文积压超限 → 触发（快路径的窗口 LENGTH 聚合与切片口径一致）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="长" * 3500)
        ai_data.append_message(db, role="assistant", content="长" * 3500)
        for i in range(10):
            ai_data.append_message(db, role="user", content=f"短{i}")
        fragment = ai_data.compaction_due(db)
        assert fragment is not None
        assert len(fragment) == 2  # 保留窗 K=10 之外的前两条
        assert fragment[0].content.startswith("长")
