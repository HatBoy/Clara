"""routers/ai_coach.py 测试（基建见 conftest.py）。

替身模式：monkeypatch ai_coach._load_graph 注入 FakeGraph（不碰 LLM 与手写区）；
SSE 断言经 client.stream 逐帧解析（httpx TestClient 支持）。memory-rules CRUD 走
API 全链路；_finalize_turn 纯函数直测。
"""

import json
import uuid

from conftest import TestingSessionLocal
from models import ModelProvider
from routers import ai_coach
from schemas import MemoryRuleCreate
from services import ai_data
from services import tasks as tasks_svc


class FakeGraph:
    """graph() 的返回替身：stream 恒产出（可选 delta 序列 +）终态 values 帧，
    或在首个 next 抛预设异常；invoke 保留兼容直测惯例。stream 的 (mode, payload)
    二元组形态对齐真图 stream_mode=["custom","values"]（langgraph 1.2.11）。"""

    def __init__(self, final=None, raiser=None, deltas=None):
        self.final = final or {}
        self.raiser = raiser
        self.deltas = list(deltas or [])

    def invoke(self, state):
        if self.raiser is not None:
            raise self.raiser
        return self.final

    def stream(self, state, stream_mode=None, **kwargs):
        for text in self.deltas:
            yield ("custom", {"type": "delta", "text": text})
        if self.raiser is not None:
            raise self.raiser  # deltas 流出后再抛——模拟流中途故障（零 deltas 时等同首 next 抛）
        yield ("values", self.final)


def _patch_graph(monkeypatch, final=None, raiser=None, deltas=None) -> None:
    monkeypatch.setattr(
        ai_coach, "_load_graph", lambda: (lambda: FakeGraph(final, raiser, deltas)))


def _activate_model(**overrides) -> None:
    with TestingSessionLocal() as db:
        db.add(
            ModelProvider(
                name=f"m-{uuid.uuid4().hex[:6]}",
                model_name="deepseek/deepseek-chat",
                is_active=True,
                **overrides,
            )
        )
        db.commit()


def _stream_events(client, message: str = "帮我排一下明天的任务") -> list[dict]:
    """POST /chat 收全 SSE 流，返回 data 帧的 json 列表（注释行跳过）。"""
    events: list[dict] = []
    with client.stream("POST", "/api/ai/chat", json={"message": message}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


# —— 对话链路 ——


def test_chat_full_chain_persists_and_streams(monkeypatch, client):
    _activate_model()
    final = {
        "final_reply": "已生成草案",
        "draft_plan": [{"task_id": None, "title": "写周报", "start": "2026-09-21T09:00:00", "duration": 30}],
    }
    _patch_graph(monkeypatch, final=final)
    events = _stream_events(client)

    types = [e["type"] for e in events]
    assert types == ["message", "reply", "draft", "done"]  # 用户回显 → 回复 → 草案 → 收尾
    draft = next(e for e in events if e["type"] == "draft")
    assert draft["draft_plan"] == final["draft_plan"]
    assert draft["proposal_id"]

    # 持久化：user + assistant 消息（assistant 关联建议行）、kind=schedule 建议行
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].proposal_id == draft["proposal_id"]
        proposals = ai_data.list_proposals(db)
        assert len(proposals) == 1 and proposals[0].kind == "schedule"


def test_chat_preflight_event(monkeypatch, client):
    _activate_model()
    final = {"final_reply": "明早有评审", "preflight": {"headline": "明日预警", "checklist": ["过一遍数据"]}}
    _patch_graph(monkeypatch, final=final)
    events = _stream_events(client)
    proposal_ev = next(e for e in events if e["type"] == "proposal")
    assert proposal_ev["kind"] == "preflight" and proposal_ev["payload"]["headline"] == "明日预警"


def test_chat_reply_only_no_draft(monkeypatch, client):
    _activate_model()
    _patch_graph(monkeypatch, final={"final_reply": "你好呀"})
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "reply", "done"]
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


def test_chat_no_model_sends_guide_event(monkeypatch, client):
    # 零激活：no_model 错误帧（前端引导卡口径），不落消息
    _patch_graph(monkeypatch, final={"final_reply": "x"})
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["error", "done"]
    assert events[0]["code"] == "no_model"
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []


def test_chat_not_ready_on_stub(monkeypatch, client):
    # 手写区桩语义：build_graph 未实现抛 NotImplementedError → not_ready 帧。
    # 2026-10-15 替身化（§6.7 表 1 预案）：真 build_graph 已就绪，不 monkeypatch
    # 会真跑图（联网 + 采集 + 依赖外部源状态），not_ready 分支改经 raiser 覆盖
    _activate_model()
    _patch_graph(monkeypatch, raiser=NotImplementedError("手写区桩未实现"))
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "error", "done"]
    assert events[1]["code"] == "not_ready"


def test_chat_disabled_by_switch(monkeypatch, client):
    _activate_model()
    _patch_graph(monkeypatch, final={"final_reply": "x"})
    assert client.patch("/api/settings", json={"ai_enabled": False}).status_code == 200
    events = _stream_events(client)
    assert events[0]["type"] == "error" and events[0]["code"] == "disabled"


def test_chat_agent_error(monkeypatch, client):
    _activate_model()
    _patch_graph(monkeypatch, raiser=RuntimeError("炸了"))
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "error", "done"]
    assert events[1]["code"] == "agent_error"


# —— token 级流式：delta 帧在 message 后 reply 前到达 ————————————————————————


def test_chat_streams_deltas_before_reply(monkeypatch, client):
    _activate_model()
    _patch_graph(
        monkeypatch,
        final={"final_reply": "今天有 3 件事"},
        deltas=["今天", "有 3 件", "事"],
    )
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "delta", "delta", "delta", "reply", "done"]
    assert [e["text"] for e in events[1:4]] == ["今天", "有 3 件", "事"]
    # reply 是权威整段（非 delta 拼接口径）；落库同样取权威文本
    assert events[4]["content"] == "今天有 3 件事"
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db)[1].content == "今天有 3 件事"


def test_chat_no_values_frame_defensive(monkeypatch, client):
    # 图只推 custom 不产 values 帧（理论不发生）→ final=None 兜底不炸，无 reply
    class _CustomOnly:
        def stream(self, state, stream_mode=None, **kwargs):
            yield ("custom", {"type": "delta", "text": "半截"})

    monkeypatch.setattr(ai_coach, "_load_graph", lambda: (lambda: _CustomOnly()))
    _activate_model()
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "delta", "done"]  # _finalize_turn({}) 零事件
    with TestingSessionLocal() as db:
        assert [m.role for m in ai_data.list_messages(db)] == ["user"]


def test_chat_delta_then_agent_error(monkeypatch, client):
    # 流到一半图炸：delta 先行、error 收尾（前端据此清流式气泡出警示气泡）
    _activate_model()
    _patch_graph(monkeypatch, raiser=RuntimeError("流中途炸"), deltas=["开头"])
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "delta", "error", "done"]
    assert events[2]["code"] == "agent_error"


def test_chat_empty_message_rejected(client):
    assert client.post("/api/ai/chat", json={"message": ""}).status_code == 422


def test_clear_session_endpoint(client):
    """清空会话端点（/clear 命令后端，2026-10-21）：消息全删 + 滚动摘要游标
    归零——上下文注入从零起步；建议行不受影响。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="聊过搬家")
        ai_data.save_chat_summary(db, summary="搬家话题", count=1)
        ai_data.create_proposal(db, kind="hardtask", payload={"steps": []})
    assert client.post("/api/ai/session/clear").status_code == 204
    assert client.get("/api/ai/messages").json() == []
    with TestingSessionLocal() as db:
        state = ai_data.get_chat_state(db)
        assert state.summary is None and state.summarized_count == 0
        assert ai_data.list_proposals(db) != []  # 建议行保留


def test_compact_session_endpoint(client, monkeypatch):
    """手动压缩端点（/compact 命令后端，2026-10-26）：替身 _compact_force → 7，
    200 带条数（AI 开 + 激活模型两道秒查先行通过）。"""
    _activate_model()
    monkeypatch.setattr(ai_coach, "_compact_force", lambda: 7)
    resp = client.post("/api/ai/session/compact")
    assert resp.status_code == 200
    assert resp.json() == {"compacted": 7, "reason": None}


def test_compact_session_prechecks(client, monkeypatch):
    """秒查两态（照 chat 前置快查先例）：零激活模型 → no_model；AI 关 →
    disabled——均不触碰压缩执行体。"""
    def _boom():
        raise AssertionError("秒查未过不应执行压缩")

    monkeypatch.setattr(ai_coach, "_compact_force", _boom)
    assert client.post("/api/ai/session/compact").json() == {
        "compacted": 0, "reason": "no_model"}
    _activate_model()
    client.patch("/api/settings", json={"ai_enabled": False})
    assert client.post("/api/ai/session/compact").json() == {
        "compacted": 0, "reason": "disabled"}


def test_compact_session_partial_translation(client, monkeypatch):
    """CompactPartialError 翻译 partial（部分成果已落库，再执行从断点续压）；
    其余执行异常翻译 error——端点永不 5xx。"""
    _activate_model()

    def _partial():
        raise ai_data.CompactPartialError(10)

    monkeypatch.setattr(ai_coach, "_compact_force", _partial)
    assert client.post("/api/ai/session/compact").json() == {
        "compacted": 10, "reason": "partial"}

    def _crash():
        raise RuntimeError("炸了")

    monkeypatch.setattr(ai_coach, "_compact_force", _crash)
    assert client.post("/api/ai/session/compact").json() == {
        "compacted": 0, "reason": "error"}


# —— 历史 / 建议回写 ——


def test_get_messages_endpoint(monkeypatch, client):
    _activate_model()
    _patch_graph(monkeypatch, final={"final_reply": "收到"})
    _stream_events(client, message="在吗")
    resp = client.get("/api/ai/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["role"] for m in body] == ["user", "assistant"]
    assert body[0]["content"] == "在吗"


def test_compact_history_async_persists_and_swallows(monkeypatch):
    """滚动上下文后台压缩执行体（2026-10-20）：正常路径落库推进游标；
    LLM 异常路径吞为 warning 不上抛（退化 = 无摘要 + 近期原文，下轮重试）。"""
    import database as database_mod

    monkeypatch.setattr(database_mod, "SessionLocal", TestingSessionLocal)
    with TestingSessionLocal() as db:
        for i in range(18):
            ai_data.append_message(db, role="user", content=f"m-{i}")
    monkeypatch.setattr("services.llm.call_llm", lambda *a, **k: "新摘要")
    ai_coach._compact_history_async()  # 同步直调（协程壳 fire-and-forget 的执行体）
    with TestingSessionLocal() as db:
        state = ai_data.get_chat_state(db)
        assert state.summary == "新摘要"
        assert state.summarized_count == 8  # n=18，保留最近 K=10 → 片段 8 条

    def boom(*_a, **_k):
        raise RuntimeError("LLM 炸了")

    monkeypatch.setattr("services.llm.call_llm", boom)
    ai_coach._compact_history_async()  # 异常被吞（不抛即过）


def test_proposal_outcome_endpoint(client):
    with TestingSessionLocal() as db:
        pid = ai_data.create_proposal(db, kind="schedule", payload={}).id
    resp = client.post(f"/api/ai/proposals/{pid}/outcome", json={"outcome": "rejected", "reason": "时间冲突"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "rejected"
    # 404 与非法枚举
    assert client.post("/api/ai/proposals/不存在/outcome", json={"outcome": "accepted"}).status_code == 404
    assert client.post(f"/api/ai/proposals/{pid}/outcome", json={"outcome": "bogus"}).status_code == 422


# —— 记忆画像 CRUD 全链路 ——


def test_memory_rules_crud_api(client):
    assert client.get("/api/ai/memory-rules").json() == []
    created = client.post(
        "/api/ai/memory-rules", json={"category": "开会习惯", "content": "不排晚 8 点后的会"}
    )
    assert created.status_code == 201
    assert created.json()["source"] == "user_manual" and created.json()["confidence"] == 1.0
    rid = created.json()["id"]

    patched = client.patch(f"/api/ai/memory-rules/{rid}", json={"content": "晚间尽量不排会"})
    assert patched.status_code == 200 and patched.json()["content"] == "晚间尽量不排会"

    assert client.patch("/api/ai/memory-rules/不存在", json={"content": "x"}).status_code == 404
    assert client.post("/api/ai/memory-rules", json={"category": "", "content": "x"}).status_code == 422
    assert client.delete(f"/api/ai/memory-rules/{rid}").status_code == 204
    assert client.delete(f"/api/ai/memory-rules/{rid}").status_code == 404
    assert client.get("/api/ai/memory-rules").json() == []


# —— _finalize_turn 纯函数直测 ——


def test_finalize_turn_empty_final():
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {})
        assert events == []
        assert ai_data.list_messages(db) == []  # 空回复不落 assistant 消息


def test_finalize_turn_initial_state_shape(monkeypatch, client):
    # initial_state 契约：user_input + memory_rules（注入序格式化）
    with TestingSessionLocal() as db:
        ai_data.create_memory_rule(db, MemoryRuleCreate(category="作息规律", content="早睡"))
        state = ai_coach.build_initial_state(db, "你好")
    assert state["user_input"] == "你好"
    assert state["memory_rules"] == ["[作息规律·用户设定] 早睡"]  # 批 4 置信分级


def test_philosophies_endpoint(client):
    # 内置哲学清单：6 则严选、只出 id/name/desc/conflicts（text 不出——提示词只留后端单源）
    res = client.get("/api/ai/philosophies")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 6
    assert set(items[0].keys()) == {"id", "name", "desc", "conflicts"}
    assert all(it["id"] and it["name"] and it["desc"] for it in items)


def test_build_initial_state_prepends_personality(client):
    # 人格规则前置（2026-10-20）：balanced 默认零注入形状不变；配置后排记忆规则之前
    from services.settings import get_settings
    with TestingSessionLocal() as db:
        assert ai_coach.build_initial_state(db, "你好")["memory_rules"] == []
        ai_data.create_memory_rule(db, MemoryRuleCreate(category="作息规律", content="早睡"))
        s = get_settings(db)
        s.ai_style = "detailed"
        s.ai_philosophy_custom = "先完成再完美"
        db.commit()
        state = ai_coach.build_initial_state(db, "你好")
    assert state["memory_rules"][0].startswith("[回复风格] ")
    assert state["memory_rules"][1] == "[自定义哲学] 先完成再完美"
    assert state["memory_rules"][-1] == "[作息规律·用户设定] 早睡"  # 批 4 置信分级


# —— 采集建议：inbox 卡 / evidence 并入 / 台账结算 / 信任端点 ——————————————————


def test_chat_inbox_items_event(monkeypatch, client):
    _activate_model()
    final = {
        "final_reply": "提炼出 2 条收件箱事项",
        "inbox_items": [{"title": "买牛奶", "duration": 15, "notes": None, "source_ref": None},
                        {"title": "回信给张三"}],
    }
    _patch_graph(monkeypatch, final=final)
    events = _stream_events(client)
    types = [e["type"] for e in events]
    assert types == ["message", "reply", "proposal", "done"]
    proposal = next(e for e in events if e["type"] == "proposal")
    assert proposal["kind"] == "inbox"
    assert [it["title"] for it in proposal["payload"]["items"]] == ["买牛奶", "回信给张三"]
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "inbox"


def test_chat_ingest_turn_cards_stay_out_of_panel(monkeypatch, client):
    # 功能边界（2026-10-25）：intent=ingest 轮建议卡照建但不出卡事件、不挂消息
    # 锚点——对话框零卡片（CopilotPanel 按 proposalId find 渲染，无锚即不显），
    # 数据源页是采集建议唯一视图；ingest_done 轻量事件通知前端刷徽标
    _activate_model()
    final = {
        "intent": "ingest",
        "final_reply": "本轮采集提炼出 1 条建议。",
        "inbox_items": [{"title": "买牛奶", "duration": 15, "notes": None, "source_ref": None}],
    }
    _patch_graph(monkeypatch, final=final)
    events = _stream_events(client)
    assert [e["type"] for e in events] == ["message", "reply", "ingest_done", "done"]
    done = next(e for e in events if e["type"] == "ingest_done")
    assert done["count"] == 1
    assert "数据源" in next(e for e in events if e["type"] == "reply")["content"]
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "inbox"
        assistant = [m for m in ai_data.list_messages(db) if m.role == "assistant"]
        assert len(assistant) == 1 and assistant[0].proposal_id is None


def test_finalize_turn_ingest_draft_marks_from_ingest():
    # ingest 轮 draft 卡补 from_ingest 标记（缺标即数据源页过滤后两处视图都不
    # 见）——曾按「chat 的 schedule 出口不标」执行（draft 与排期草案共用出口），
    # 边界重构后 ingest 轮的 draft 也归数据源页，按 intent 精确分标
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {
            "intent": "ingest",
            "final_reply": "已提炼。",
            "draft_plan": [{"task_id": None, "title": "写周报",
                            "start": "2026-10-15T09:00:00", "duration": 30}],
        })
        assert [e["type"] for e in events if e["type"] != "reply"] == ["ingest_done"]
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert proposals[0].kind == "schedule"
        assert proposals[0].payload["from_ingest"] is True
        assistant = [m for m in ai_data.list_messages(db) if m.role == "assistant"]
        assert len(assistant) == 1 and assistant[0].proposal_id is None


def test_finalize_turn_bulk_event_no_draft():
    # 批量排期轮（2026-10-25 功能边界）：draft 卡照建（payload.bulk 审计标记）
    # 但不发 draft 事件改发 bulk 事件（前端弹窗清单确认）、不挂消息锚点
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {
            "final_reply": "已为 2 条待办生成排期建议，请在弹窗中核对确认。",
            "bulk": True,
            "draft_plan": [
                {"task_id": "t1", "title": "写周报", "start": "2026-10-17T09:00:00",
                 "duration": 60},
                {"task_id": "t2", "title": "回信", "start": "2026-10-17T10:00:00",
                 "duration": 30}],
        })
        types = [e["type"] for e in events]
        assert "draft" not in types and "bulk" in types
        bulk = next(e for e in events if e["type"] == "bulk")
        assert len(bulk["draft_plan"]) == 2 and bulk["proposal_id"]
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert proposals[0].payload["bulk"] is True
        assistant = [m for m in ai_data.list_messages(db) if m.role == "assistant"]
        assert len(assistant) == 1 and assistant[0].proposal_id is None


def test_chat_bulk_event_over_sse(monkeypatch, client):
    # SSE 全链：bulk 帧随 reply 后发出（App 开弹窗），无 draft 帧
    _activate_model()
    _patch_graph(monkeypatch, final={
        "final_reply": "已为 1 条待办生成排期建议，请在弹窗中核对确认。",
        "bulk": True,
        "draft_plan": [{"task_id": "t1", "title": "写周报",
                        "start": "2026-10-17T09:00:00", "duration": 60}],
    })
    events = _stream_events(client)
    assert [e["type"] for e in events] == ["message", "reply", "bulk", "done"]
    assert events[2]["draft_plan"][0]["title"] == "写周报"


def test_finalize_turn_evidence_into_schedule(monkeypatch, client):
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {
            "final_reply": "已排",
            "draft_plan": [{"task_id": None, "title": "周会", "start": "2026-10-15T10:00:00",
                            "duration": 60}],
            "evidence": [{"idx": 0, "source": "Gmail", "source_key": "ca-1",
                          "item_id": "m1", "title": "周会改期", "time": None,
                          "fingerprint": "f" * 40}],
        })
        assert events[1]["type"] == "draft"
        proposals = ai_data.list_proposals(db)
        assert proposals[0].payload["evidence"][0]["fingerprint"] == "f" * 40


def test_finalize_turn_delete_plan_builds_delete_card():
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {
            "final_reply": "已定位「牙医预约」，请在下方确认删除。",
            "delete_plan": [{"task_id": "t1", "title": "牙医预约"},
                            {"title": "无 id 条目滤除"}],
        })
        proposal_ev = next(e for e in events if e["type"] == "proposal")
        assert proposal_ev["kind"] == "delete"
        assert proposal_ev["payload"]["tasks"] == [{"task_id": "t1", "title": "牙医预约"}]
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "delete"
        # reply 消息关联删除卡（首建议行）
        assert ai_data.list_messages(db)[0].proposal_id == proposals[0].id


def test_finalize_turn_empty_delete_plan_no_card():
    # 节点的“未命中/重复系列”形态：空清单不出卡，只有文字指引
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {"delete_plan": [], "final_reply": "没找到该任务"})
        assert [e["type"] for e in events] == ["reply"]
        assert ai_data.list_proposals(db, unresolved_only=True) == []


def test_resolve_proposal_settles_ingest_ledger(client, tmp_path):
    # 拒绝 schedule 卡 → payload 内全部指纹在台账置 rejected_at（M2-① 钩子）
    from models import IngestionItem
    from services import data_ingest

    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    with TestingSessionLocal() as db:
        from models import IntegrationSettings

        db.add(IntegrationSettings(id=1, local_folder_paths=[str(tmp_path)],
                                   local_folder_enabled=True))
        db.commit()
        out = data_ingest.fetch_recent(db, hours=24)
        fp = out["sources"][0]["items"][0]["fingerprint"]
        proposal = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [], "evidence": [{"idx": 0, "source": "本地文件夹",
                                            "source_key": "local_folder", "item_id": "x",
                                            "title": "a.md", "time": None, "fingerprint": fp}],
        })
    resp = client.post(f"/api/ai/proposals/{proposal.id}/outcome", json={"outcome": "rejected"})
    assert resp.status_code == 200
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        row = db.scalar(select(IngestionItem).where(IngestionItem.fingerprint == fp))
    assert row is not None and row.rejected_at is not None


def test_settle_items_partial_shrinks_payload(client):
    # 条目级部分结算（2026-10-24）：拒绝 1/N 条 → 卡仍待处理、payload 收缩 + evidence
    # 重排；最后 1 条结算 → outcome 回写
    with TestingSessionLocal() as db:
        proposal = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [
                {"title": "甲", "start": "2026-10-25T09:00:00", "duration": 30},
                {"title": "乙", "start": "2026-10-25T10:00:00", "duration": 30},
            ],
            "evidence": [
                {"idx": 0, "source": "滴答清单", "source_key": "s1", "item_id": "a",
                 "title": "原始甲", "time": None, "fingerprint": "f1"},
                {"idx": 1, "source": "滴答清单", "source_key": "s1", "item_id": "b",
                 "title": "原始乙", "time": None, "fingerprint": "f2"},
            ],
        })
    resp = client.post(f"/api/ai/proposals/{proposal.id}/settle-items",
                       json={"indexes": [0], "outcome": "rejected"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] is None and len(body["payload"]["draft_plan"]) == 1
    assert body["payload"]["draft_plan"][0]["title"] == "乙"
    assert body["payload"]["evidence"][0]["idx"] == 0  # 重排对齐
    assert body["payload"]["evidence"][0]["fingerprint"] == "f2"

    resp = client.post(f"/api/ai/proposals/{proposal.id}/settle-items",
                       json={"indexes": [0], "outcome": "accepted"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "accepted"  # 清空 → 整卡结束


def test_settle_items_reject_only_dropped_fingerprint(client, tmp_path):
    # 条目级拒绝只静默被拒条目的指纹：mini payload 喂钩子，同卡其余条目台账不动
    from models import IngestionItem
    from services import data_ingest

    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    (tmp_path / "b.md").write_text("内容2", encoding="utf-8")
    with TestingSessionLocal() as db:
        from models import IntegrationSettings

        db.add(IntegrationSettings(id=1, local_folder_paths=[str(tmp_path)],
                                   local_folder_enabled=True))
        db.commit()
        out = data_ingest.fetch_recent(db, hours=24)
        fp_a = out["sources"][0]["items"][0]["fingerprint"]
        fp_b = out["sources"][0]["items"][1]["fingerprint"]
        proposal = ai_data.create_proposal(db, kind="inbox", payload={
            "items": [
                {"title": "甲", "source_ref": {"source": "本地文件夹", "source_key":
                 "local_folder", "item_id": "a", "title": "a.md", "time": None,
                 "fingerprint": fp_a}},
                {"title": "乙", "source_ref": {"source": "本地文件夹", "source_key":
                 "local_folder", "item_id": "b", "title": "b.md", "time": None,
                 "fingerprint": fp_b}},
            ],
        })
    resp = client.post(f"/api/ai/proposals/{proposal.id}/settle-items",
                       json={"indexes": [0], "outcome": "rejected"})
    assert resp.status_code == 200
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        a = db.scalar(select(IngestionItem).where(IngestionItem.fingerprint == fp_a))
        b = db.scalar(select(IngestionItem).where(IngestionItem.fingerprint == fp_b))
    assert a is not None and a.rejected_at is not None
    assert b is not None and b.rejected_at is None  # 未拒条目不受牵连


def test_settle_items_guards(client):
    # 404 行不存在 / 422 已决 / 422 序号越界 / 422 空表
    resp = client.post("/api/ai/proposals/nope/settle-items",
                       json={"indexes": [0], "outcome": "accepted"})
    assert resp.status_code == 404
    with TestingSessionLocal() as db:
        p1 = ai_data.create_proposal(db, kind="inbox", payload={"items": [{"title": "甲"}]})
        p2 = ai_data.create_proposal(db, kind="inbox", payload={"items": [{"title": "乙"}]})
    resp = client.post(f"/api/ai/proposals/{p1.id}/settle-items",
                       json={"indexes": [5], "outcome": "accepted"})
    assert resp.status_code == 422
    resp = client.post(f"/api/ai/proposals/{p1.id}/settle-items",
                       json={"indexes": [0], "outcome": "accepted"})
    assert resp.status_code == 200
    resp = client.post(f"/api/ai/proposals/{p1.id}/settle-items",
                       json={"indexes": [0], "outcome": "accepted"})
    assert resp.status_code == 422  # 已决（清空回写）不再部分结算


def test_settle_items_minutes_and_hardtask_kinds(client):
    """kind 扩展（2026-11-11 确认卡三件套统一批）：minutes 卡按 items 键收缩、
    hardtask 卡按 steps 键收缩（纯标题字符串数组）；清空自动回写整卡 outcome。"""
    with TestingSessionLocal() as db:
        minutes = ai_data.create_proposal(db, kind="minutes", payload={
            "meeting_task_id": "m1", "items": [
                {"title": "整理反馈", "duration": 30, "priority": 1},
                {"title": "预约评审", "duration": None, "priority": 2},
            ]})
        hard = ai_data.create_proposal(db, kind="hardtask", payload={
            "headline": "拆解", "steps": ["列大纲", "找参考", "写初稿"],
            "advice": "先列大纲"})
    # minutes：删首条 → 剩 1 条未决；再删空 → 回写 rejected
    resp = client.post(f"/api/ai/proposals/{minutes.id}/settle-items",
                       json={"indexes": [0], "outcome": "rejected"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] is None
    assert [it["title"] for it in body["payload"]["items"]] == ["预约评审"]
    resp = client.post(f"/api/ai/proposals/{minutes.id}/settle-items",
                       json={"indexes": [0], "outcome": "rejected"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "rejected"  # 清空 → 整卡结束
    # hardtask：字符串条目收缩（删两条）→ 清空回写
    resp = client.post(f"/api/ai/proposals/{hard.id}/settle-items",
                       json={"indexes": [0, 2], "outcome": "rejected"})
    assert resp.status_code == 200
    assert resp.json()["payload"]["steps"] == ["找参考"]
    resp = client.post(f"/api/ai/proposals/{hard.id}/settle-items",
                       json={"indexes": [0], "outcome": "rejected"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "rejected"


def test_chat_applied_event_persists_and_settles(monkeypatch, client):
    # origin=user 过全链校验 → 壳直接落库：帧序 reply→applied、任务真入库、
    # kind=schedule 行直置 accepted + payload.auto、assistant 消息挂行
    _activate_model()
    final = {
        "final_reply": "已添加：体检，今天 10:00–11:00。",
        "applied_plan": [{"task_id": None, "title": "体检",
                          "start": "2026-10-20T10:00:00", "duration": 60,
                          "explicit_time": "上午10点", "origin": "user"}],
    }
    _patch_graph(monkeypatch, final=final)
    events = _stream_events(client)

    assert [e["type"] for e in events] == ["message", "reply", "applied", "done"]
    applied = events[2]
    assert applied["tasks"][0]["title"] == "体检"
    with TestingSessionLocal() as db:
        titles = [t.title for t in tasks_svc.list_tasks(db)]
        assert titles == ["体检"]
        proposals = ai_data.list_proposals(db)
        assert len(proposals) == 1 and proposals[0].kind == "schedule"
        assert proposals[0].outcome == "accepted"
        assert proposals[0].payload["auto"] is True
        assert ai_data.list_messages(db)[1].proposal_id == applied["proposal_id"]


def test_chat_applied_value_error_degrades_to_draft(monkeypatch, client):
    # 落库 ValueError → 整批原子降级为 draft 确认卡：零任务入库、帧序回 draft
    _activate_model()
    _patch_graph(monkeypatch, final={
        "final_reply": "已安排",
        "applied_plan": [{"task_id": None, "title": "体检",
                          "start": "2026-10-20T10:00:00", "duration": 60}],
    })

    def boom(db, entries):
        raise ValueError("时间无法解析")

    monkeypatch.setattr(tasks_svc, "apply_plan_entries", boom)
    events = _stream_events(client)

    assert [e["type"] for e in events] == ["message", "reply", "draft", "done"]
    with TestingSessionLocal() as db:
        assert tasks_svc.list_tasks(db) == []
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "schedule"
        assert proposals[0].payload["draft_plan"][0]["title"] == "体检"


def test_finalize_turn_question_row_takes_message_anchor():
    # mixed 轮次：question 行零卡零事件，且优先占消息锚点——被 applied 行抢走
    # 锚点的话，下轮 active_pending 判定将失活（问询流断裂）
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {
            "final_reply": "「体检」要的 10-20 10:00 已有安排，建议改到 10-20 14:00，可以吗？",
            "question_plan": {"kind": "conflict",
                              "entries": [{"task_id": None, "title": "体检",
                                           "start": "2026-10-20T10:00:00", "duration": 60}],
                              "suggestion": "2026-10-20T14:00:00", "similar_to": None,
                              "date_hint": None, "original_input": "记一下，20号上午10点体检"},
            "applied_plan": [{"task_id": None, "title": "牙医",
                              "start": "2026-10-21T09:00:00", "duration": 60}],
        })
        assert [e["type"] for e in events] == ["reply", "applied"]
        rows = ai_data.list_proposals(db)
        kinds = {r.kind for r in rows}
        assert kinds == {"question", "schedule"}
        # applied 行直置 accepted（不在未决集）；问询行未决待下轮消费
        unresolved = {r.kind for r in ai_data.list_proposals(db, unresolved_only=True)}
        assert unresolved == {"question"}
        q_row = next(r for r in rows if r.kind == "question")
        assert q_row.payload["ask_text"].startswith("「体检」")
        assert ai_data.list_messages(db)[0].proposal_id == q_row.id  # 锚点归问询行
        snap = ai_data.active_pending(db)
        assert snap["proposal_id"] == q_row.id and snap["kind"] == "conflict"
        assert snap["entries"][0]["title"] == "体检"


def test_finalize_turn_resolved_proposal_replays_outcome():
    with TestingSessionLocal() as db:
        q = ai_data.create_proposal(db, kind="question", payload={
            "kind": "no_time", "entries": [{"task_id": None, "title": "修车",
                                            "start": None, "duration": 90}]})
        events = ai_coach._finalize_turn(db, {
            "final_reply": "好的", "resolved_proposal": {"proposal_id": q.id,
                                                         "outcome": "accepted"}})
        assert [e["type"] for e in events] == ["reply"]
        assert ai_data.get_proposal(db, q.id).outcome == "accepted"
        # outcome 缺省按 ignored 结算（防御形态）
        q2 = ai_data.create_proposal(db, kind="question", payload={"kind": "past", "entries": [
            {"task_id": None, "title": "旧事", "start": "2026-10-01T10:00:00", "duration": 30}]})
        ai_coach._finalize_turn(db, {"resolved_proposal": {"proposal_id": q2.id}})
        assert ai_data.get_proposal(db, q2.id).outcome == "ignored"


def test_active_pending_three_forms():
    with TestingSessionLocal() as db:
        assert ai_data.active_pending(db) is None  # ① 无消息
        # ② 活跃：未决 question 行 + 锚点消息（中间穿插无锚点闲聊不杀问询）
        q = ai_data.create_proposal(db, kind="question", payload={
            "kind": "conflict", "entries": [{"task_id": None, "title": "体检",
                                             "start": "2026-10-20T10:00:00", "duration": 60}],
            "suggestion": "2026-10-20T14:00:00",
            "ask_text": "「体检」要的 10-20 10:00 已有安排，建议改到 14:00，可以吗？"})
        ai_data.append_message(db, role="user", content="记一下，20号上午10点体检")
        ai_data.append_message(db, role="assistant", content=q.payload["ask_text"],
                               proposal_id=q.id)
        ai_data.append_message(db, role="user", content="顺便问下明天天气")
        ai_data.append_message(db, role="assistant", content="晴")  # 无锚点闲聊
        snap = ai_data.active_pending(db)
        assert snap["proposal_id"] == q.id and snap["ask_text"].startswith("「体检」")
        # ③ 被顶替：锚点消息挂到已决行（如本轮 draft）后失活
        d = ai_data.create_proposal(db, kind="schedule", payload={"draft_plan": []})
        ai_data.append_message(db, role="assistant", content="换个事先", proposal_id=d.id)
        assert ai_data.active_pending(db) is None


def test_build_initial_state_injects_pending(client):
    with TestingSessionLocal() as db:
        assert ai_coach.build_initial_state(db, "你好")["pending"] is None
        q = ai_data.create_proposal(db, kind="question", payload={
            "kind": "no_time", "entries": [{"task_id": None, "title": "修车",
                                            "start": None, "duration": 90}],
            "ask_text": "「修车」安排在后天几点？"})
        ai_data.append_message(db, role="assistant", content=q.payload["ask_text"],
                               proposal_id=q.id)
        state = ai_coach.build_initial_state(db, "下午三点吧")
    assert state["pending"]["proposal_id"] == q.id
    assert state["user_input"] == "下午三点吧"


# —— messages.source 来源标记（2026-10-23 聊天通讯批 2）——————————————————————


def test_message_source_persisted_and_echoed(client):
    """渠道消息 source 落库并经 GET /messages 回显；桌面消息缺省 None。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="手机上发来的",
                               source="feishu")
        ai_data.append_message(db, role="user", content="桌面端发的")
        rows = ai_data.list_messages(db)
        assert [m.source for m in rows] == ["feishu", None]
    out = client.get("/api/ai/messages").json()
    assert [m["source"] for m in out] == ["feishu", None]


def test_finalize_turn_source_marks_assistant_reply(client):
    """finalize_turn 的 source 透传到 assistant 回复消息（远程渠道轮的来源标记）。"""
    with TestingSessionLocal() as db:
        events = ai_coach._finalize_turn(db, {"final_reply": "收到"}, source="dingtalk")
        assert events[0]["type"] == "reply"
        assert ai_data.list_messages(db)[0].source == "dingtalk"
        # 桌面路径缺省 None
        ai_coach._finalize_turn(db, {"final_reply": "好的"})
        assert ai_data.list_messages(db)[1].source is None


# —— TURN_LOCK 轮次串行（2026-10-23 聊天通讯批 3）——————————————————————


def test_turn_lock_serializes_concurrent_turns():
    """并发轮共锁串行：in/out 成对不交错（桌面 SSE 轮与远程渠道轮同一把锁）。
    单事件循环内验证锁语义；跨循环 / 跨线程形态由生产单循环架构保证。"""
    import asyncio

    from services import chat_turn

    order: list[str] = []

    async def worker(name: str) -> None:
        async with chat_turn.get_turn_lock():
            order.append(f"{name}-in")
            await asyncio.sleep(0.02)
            order.append(f"{name}-out")

    async def main() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(main())
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


def test_clear_session_waits_for_turn_lock():
    """/clear 与轮次共锁：同一事件循环内，持锁者未放行前清库协程不执行
    （防清库撞在途轮的消息追加）。注：必须在单 loop 内验证——asyncio.Lock
    跨 loop 唤醒不保证（TestClient 每请求独立 portal loop 的形态由生产单
    uvicorn 循环架构规避）。"""
    import asyncio

    from services import chat_turn

    order: list[str] = []

    async def holder() -> None:
        lock = chat_turn.get_turn_lock()
        await lock.acquire()
        await asyncio.sleep(0.05)
        order.append("turn-done")
        lock.release()

    async def clearer() -> None:
        await asyncio.sleep(0.01)  # 确保 holder 先拿锁
        async with chat_turn.get_turn_lock():
            order.append("clear-ran")

    async def main() -> None:
        await asyncio.gather(holder(), clearer())

    asyncio.run(main())
    assert order == ["turn-done", "clear-ran"]


def test_compact_session_waits_for_turn_lock():
    """/compact 与轮次共锁（同 /clear 语义，2026-10-26）：持锁者未放行前压缩
    协程不执行（防压缩游标推进撞在途轮的消息追加）。"""
    import asyncio

    from services import chat_turn

    order: list[str] = []

    async def holder() -> None:
        lock = chat_turn.get_turn_lock()
        await lock.acquire()
        await asyncio.sleep(0.05)
        order.append("turn-done")
        lock.release()

    async def compactor() -> None:
        await asyncio.sleep(0.01)  # 确保 holder 先拿锁
        async with chat_turn.get_turn_lock():
            order.append("compact-ran")

    async def main() -> None:
        await asyncio.gather(holder(), compactor())

    asyncio.run(main())
    assert order == ["turn-done", "compact-ran"]


def test_build_initial_state_grades_ai_reflect_confidence(client):
    # 置信分级（2026-10-26 批 4）：ai_reflect 行带「AI学习·置信N」标签——LLM 可
    # 分级信任（user_manual/chat=用户设定铁律，AI 学习按置信参考）
    from models import MemoryRule
    with TestingSessionLocal() as db:
        db.add(MemoryRule(id="r1", category="开会习惯", content="晚间不开会",
                          source="ai_reflect", confidence=0.55))
        db.commit()
        rules = ai_coach.build_initial_state(db, "你好")["memory_rules"]
    assert rules == ["[开会习惯·AI学习·置信0.55] 晚间不开会"]


def test_messages_after_filter(client):
    """未读位点恢复（2026-11-04 通知分轨配套）：after 只返回此后消息（前端启动
    计「上次见面后到达的 assistant 消息」数作徽标初值）。位点取旧消息的实际
    created_at（微秒时钟推进保证新消息严格晚于位点）。"""
    from services.ai_data import append_message

    with TestingSessionLocal() as db:
        append_message(db, role="user", content="旧的提问")
        old_reply = append_message(db, role="assistant", content="旧的回答")
        cutoff = old_reply.created_at
        append_message(db, role="assistant", content="新消息")
    resp = client.get("/api/ai/messages", params={"after": cutoff.isoformat(sep=" ")})
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["content"] for r in rows] == ["新消息"]
