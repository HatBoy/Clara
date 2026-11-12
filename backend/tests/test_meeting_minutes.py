"""会后纪要 → 行动项（minutes 流，2026-11-10 批 1）测试。

覆盖：_parse_minutes_out 防御（纯函数直测）/ meeting-minutes 端点契约
（降级 503、会议 404、入参 422）/ run_meeting_minutes 集成（会议元信息回读、
无关联会议、建卡 + 消息 + payload 结构、提炼全空 503 不建空壳卡）。
替身模式同 test_agents_nodes._LLMCapture（按 prompt 分发，经 build 层全链）。
"""

import json
from datetime import datetime

from agents.nodes import _parse_minutes_out
from agents.prompts import MINUTES_PROMPT
from conftest import TestingSessionLocal
from models import ModelProvider, Task
from services import ai_data
from services import llm as llm_svc


def _activate_model() -> None:
    import uuid

    with TestingSessionLocal() as db:
        db.add(ModelProvider(
            name=f"m-{uuid.uuid4().hex[:6]}",
            model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def _fake_llm(captured: list, payload: dict):
    """call_llm 替身：识别 MINUTES_PROMPT 返回预设输出，其余返回空 JSON。"""
    def fake(messages, *, json_mode=False, max_tokens=None):
        if MINUTES_PROMPT in messages[0]["content"]:
            captured.append(messages[1]["content"])
            return json.dumps(payload, ensure_ascii=False)
        return "{}"
    return fake


# —— _parse_minutes_out 防御（纯函数直测） ————————————————————————


def test_parse_minutes_non_json_returns_all_empty():
    assert _parse_minutes_out("不是 JSON") == ("", [], "")
    assert _parse_minutes_out("") == ("", [], "")


def test_parse_minutes_field_defenses():
    out = json.dumps({
        "headline": "核心结论敲定方案",
        "items": [
            {"title": "整理客户反馈", "duration": 30, "priority": 1,
             "notes": "含周三电话反馈"},
            {"title": "", "duration": 10, "priority": 2},           # 空标题丢条
            {"title": "预约评审", "duration": "半小时", "priority": 9},  # duration 非法→None，priority 钳 2
            {"title": "更新文档", "duration": 9999, "priority": 3},     # duration 钳 1440
            {"title": "   ", "duration": 5},                        # 空白标题丢条
            {"title": "同步组内", "duration": True},                  # bool→None（bool 是 int 子类陷阱）
            {"notes": "没有标题的条目"},                              # 缺 title 丢条
        ],
        "reply": "已提炼",
    }, ensure_ascii=False)
    headline, items, reply = _parse_minutes_out(out)
    assert headline == "核心结论敲定方案"
    assert reply == "已提炼"
    assert len(items) == 4
    assert items[0] == {"title": "整理客户反馈", "duration": 30,
                        "priority": 1, "notes": "含周三电话反馈"}
    assert items[1]["duration"] is None and items[1]["priority"] == 2
    assert items[2]["duration"] == 1440
    assert items[3] == {"title": "同步组内", "duration": None,
                        "priority": 2, "notes": None}


def test_parse_minutes_over_eight_items_truncated():
    out = json.dumps({
        "headline": "h",
        "items": [{"title": f"行动{i}"} for i in range(12)],
        "reply": "r",
    }, ensure_ascii=False)
    _, items, _ = _parse_minutes_out(out)
    assert len(items) == 8


# —— 端点契约 ————————————————————————————————————————


def test_minutes_endpoint_disabled_ai_503(client, monkeypatch):
    import routers.ai_coach as ai_coach

    class _S:  # 最小设置替身（仅端点前置快查读 ai_enabled）
        ai_enabled = False
    monkeypatch.setattr(ai_coach, "get_settings", lambda db: _S())
    resp = client.post("/api/ai/meeting-minutes",
                       json={"text": "会中决定整理反馈并发给组内"})
    assert resp.status_code == 503


def test_minutes_endpoint_no_model_503(client):
    # 零激活模型（测试库每例 fresh 天然零激活）
    resp = client.post("/api/ai/meeting-minutes",
                       json={"text": "会中决定整理反馈并发给组内"})
    assert resp.status_code == 503


def test_minutes_endpoint_short_text_422(client):
    _activate_model()
    resp = client.post("/api/ai/meeting-minutes", json={"text": "太短"})
    assert resp.status_code == 422


def test_minutes_endpoint_task_not_found_404(client):
    _activate_model()
    resp = client.post("/api/ai/meeting-minutes",
                       json={"task_id": "no-such-id",
                             "text": "会中决定整理反馈并发给组内"})
    assert resp.status_code == 404


# —— 集成：建卡 / 消息 / payload 结构 ————————————————————


def test_minutes_creates_card_with_meeting_link(client, monkeypatch):
    _activate_model()
    captured: list[str] = []
    monkeypatch.setattr(llm_svc, "call_llm", _fake_llm(captured, {
        "headline": "评审通过，分头落实",
        "items": [{"title": "整理客户反馈", "duration": 30, "priority": 1,
                   "notes": None}],
        "reply": "已提炼 1 条行动项",
    }))
    with TestingSessionLocal() as db:
        t = Task(title="周会", priority=1,
                 start_time=datetime(2026, 11, 10, 14, 0), duration_mins=60)
        db.add(t)
        db.commit()
        tid = t.id
    resp = client.post("/api/ai/meeting-minutes",
                       json={"task_id": tid,
                             "text": "会上对齐了排期，张三整理客户反馈，周四前发出"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "minutes"
    p = body["payload"]
    assert p["meeting_task_id"] == tid
    assert p["meeting_title"] == "周会"
    assert p["headline"] == "评审通过，分头落实"
    assert p["items"][0]["title"] == "整理客户反馈"
    assert "排期" in p["source_text"]  # 纪要原文留档
    # 会议元信息进了 LLM 载荷（提炼有上下文）
    assert "周会" in captured[0]
    # 面板消息落库（proposal 锚）
    with TestingSessionLocal() as db:
        msgs = ai_data.list_messages(db, limit=10)
        assert any(m.proposal_id == body["id"] for m in msgs)


def test_minutes_without_task_id_cards_as_unlinked(client, monkeypatch):
    _activate_model()
    captured: list[str] = []
    monkeypatch.setattr(llm_svc, "call_llm", _fake_llm(captured, {
        "headline": "闲聊会",
        "items": [],
        "reply": "未识别出行动项",
    }))
    resp = client.post("/api/ai/meeting-minutes",
                       json={"text": "纯记录：今天聊了架构方向，没有待办事项产生"})
    assert resp.status_code == 201  # 空行动项也建卡（回执语义）
    p = resp.json()["payload"]
    assert p["meeting_task_id"] is None
    assert p["items"] == []
    assert json.loads(captured[0])["meeting"] is None


def test_minutes_parse_all_empty_503_no_card(client, monkeypatch):
    _activate_model()
    monkeypatch.setattr(llm_svc, "call_llm", _fake_llm([], {"unexpected": 1}))
    resp = client.post("/api/ai/meeting-minutes",
                       json={"text": "会上讨论了若干事项但没有可提炼的结构"})
    assert resp.status_code == 503
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


# —— 批 2：聊天预路由 / minutes_extractor / 去重注入 / finalize 建卡 ——————————


def test_router_preroutes_minutes_long_text():
    """纪要词 + 长文本（>80 字或含换行）→ 直判 minutes 零 LLM；短句（代笔
    诉求）不命中。"""
    from agents.nodes import center_router

    long_minutes = "开完会了，纪要如下：\n1. 项目排期对齐到下月 15 号\n2. 张三负责客户反馈"
    out = center_router({"user_input": long_minutes, "memory_rules": []})
    assert out["intent"] == "minutes"
    # 纪要正文含排期词 + 时间词（schedule 预路由的经典命中形态）也不被吞
    tricky = "纪要：会上决定明天上午安排送材料，具体细节再议。\n补充记录若干行\n" * 3
    assert center_router({"user_input": tricky, "memory_rules": []})["intent"] == "minutes"
    # 短句不命中（代笔诉求走幕僚长 LLM 分类）
    assert center_router({"user_input": "帮我写会议纪要", "memory_rules": []}).get("intent") != "minutes"
    # 单行简纪要（30~80 字）命中——2026-11-11 阈值 80→30：简单会议几条关键
    # 结论常不足 80 字；<30 字代笔短句（含纪要词）仍不命中
    one_line = "开完会，纪要：排期方案定了，张三周四前发报告，李五对接客户，周五验收"
    assert 30 < len(one_line) <= 80
    assert center_router({"user_input": one_line, "memory_rules": []})["intent"] == "minutes"
    assert center_router(
        {"user_input": "帮我写一份会议纪要发给全组", "memory_rules": []}
    ).get("intent") != "minutes"
    # 无纪要词的长文本不命中
    assert center_router({"user_input": "这" * 200, "memory_rules": []}).get("intent") != "minutes"


def test_minutes_extractor_node_produces_meta_and_dedup(monkeypatch):
    """minutes_extractor：整条输入即纪要原文 → inbox_items + minutes_meta
    （meeting 恒 None）+ final_reply；去重对照清单进 LLM 载荷。"""
    from agents import nodes as nodes_mod
    from agents.prompts import MINUTES_PROMPT
    from conftest import TestingSessionLocal as _TSL
    from models import Task as _Task
    from services import llm

    captured: list[str] = []

    def fake(messages, *, json_mode=False, max_tokens=None):
        if MINUTES_PROMPT in messages[0]["content"]:
            captured.append(messages[1]["content"])
            return json.dumps({
                "headline": "排期对齐", "items": [{"title": "预约下次评审"}],
                "reply": "已提炼 1 条"}, ensure_ascii=False)
        return "{}"

    monkeypatch.setattr(llm, "call_llm", fake)
    with _TSL() as db:  # 收件箱既有待办（去重对照）
        db.add(_Task(title="整理客户反馈"))
        db.commit()
    out = nodes_mod.minutes_extractor({
        "user_input": "开完会了，纪要：\n1. 对齐排期\n2. 预约下次评审",
        "memory_rules": []})
    assert out["inbox_items"][0]["title"] == "预约下次评审"
    assert out["minutes_meta"]["meeting_task_id"] is None
    assert out["minutes_meta"]["source_text"].startswith("开完会了")
    assert out["final_reply"] == "已提炼 1 条"
    import json as _json
    assert "整理客户反馈" in _json.loads(captured[0])["existing_titles"]


def test_chat_minutes_turn_builds_minutes_card(client, monkeypatch):
    """chat 全链：minutes 轮经 finalize_turn 建 kind=minutes 卡（payload 契约
    同浮层端点）——桌面 CopilotPanel 渲染分支共用。"""
    from agents.prompts import MINUTES_PROMPT
    from services import llm

    _activate_model()

    def fake(messages, *, json_mode=False, max_tokens=None):
        if MINUTES_PROMPT in messages[0]["content"]:
            return json.dumps({
                "headline": "排期对齐",
                "items": [{"title": "预约下次评审", "duration": 30,
                           "priority": 2, "notes": None}],
                "reply": "已提炼 1 条行动项"}, ensure_ascii=False)
        return json.dumps({"intent": "minutes"}, ensure_ascii=False)

    monkeypatch.setattr(llm, "call_llm", fake)
    events = []
    with client.stream("POST", "/api/ai/chat",
                       json={"message": "开完会了，纪要如下：\n1. 对齐排期\n2. 预约下次评审"}) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    prop_events = [e for e in events if e.get("type") == "proposal" and e.get("kind") == "minutes"]
    assert prop_events, f"minutes proposal 事件未出现：{[e.get('type') for e in events]}"
    p = prop_events[0]["payload"]
    assert p["meeting_task_id"] is None
    assert p["items"][0]["title"] == "预约下次评审"
    assert p["source_text"].startswith("开完会了")
    # 落库行同契约
    with TestingSessionLocal() as db:
        row = next(r for r in ai_data.list_proposals(db) if r.kind == "minutes")
        assert row.payload["items"][0]["title"] == "预约下次评审"


# —— 批 3：会后邀请 tick 谓词 / runner / prep 联动 ————————————————————————


def _mk_ended_meeting(db, mins_ago: float, title: str = "周会"):
    """造一场 mins_ago 分钟前结束的 60 分钟高优会议（priority=1 即重要）。"""
    from datetime import datetime as _dt, timedelta as _td

    start = _dt.now() - _td(minutes=mins_ago) - _td(hours=1)
    t = Task(title=title, priority=1, start_time=start, duration_mins=60)
    db.add(t)
    db.commit()
    return t


def test_minutes_invite_claim_window_and_daily_once():
    """窗口边界：结束 <15 分钟太早 / 15~120 分钟命中 / >120 分钟太晚；认领后
    当日不再认领；开关关永不认领。"""
    from services import workflows

    now = datetime.now()
    with TestingSessionLocal() as db:
        _mk_ended_meeting(db, 5)          # 刚散会（太早）
        _mk_ended_meeting(db, 200, title="旧会")  # 三个多小时前（太晚）
        assert workflows._claim_minutes_invite(db, now) is False
    with TestingSessionLocal() as db:
        _mk_ended_meeting(db, 30)         # 半小时前（窗口内）
        assert workflows._claim_minutes_invite(db, now) is True
        assert workflows._claim_minutes_invite(db, now) is False  # 每日至多一次
    with TestingSessionLocal() as db:
        s = db.query(__import__("models").AppSettings).first()
        s.minutes_enabled = False
        db.commit()
        _mk_ended_meeting(db, 30)
        assert workflows._claim_minutes_invite(db, now) is False


def test_minutes_invite_trace_blocks_candidate():
    """已有纪要痕迹（minutes 卡或已决 invite）的会议不再成为候选。"""
    from services import workflows

    with TestingSessionLocal() as db:
        t = _mk_ended_meeting(db, 30)
        ai_data.create_proposal(db, kind="minutes", payload={
            "meeting_task_id": t.id, "items": []})
        assert workflows._pick_minutes_candidate(db, datetime.now()) is None
    with TestingSessionLocal() as db:
        t = _mk_ended_meeting(db, 30)
        p = ai_data.create_proposal(db, kind="minutes_invite", payload={
            "meeting_task_id": t.id})
        ai_data.record_proposal_outcome(db, p.id, "rejected")
        assert workflows._pick_minutes_candidate(db, datetime.now()) is None


def test_run_minutes_invite_creates_card(monkeypatch):
    """runner 全链：窗口内会议 → minutes_invite 卡 + 面板消息 + 广播。"""
    import asyncio

    from services import workflows

    _activate_model()
    monkeypatch.setattr(workflows, "SessionLocal", TestingSessionLocal)  # runner 自开库走测试库（模块级绑定不吃 conftest 替换，test_workflows._use_test_db 先例）
    events: list[dict] = []
    monkeypatch.setattr(workflows, "broadcast", lambda ev: events.append(ev))
    with TestingSessionLocal() as db:
        t = _mk_ended_meeting(db, 30, title="季度评审")
        tid = t.id
    asyncio.run(workflows._run_minutes_invite())
    with TestingSessionLocal() as db:
        rows = [r for r in ai_data.list_proposals(db) if r.kind == "minutes_invite"]
        assert len(rows) == 1
        assert rows[0].payload["meeting_task_id"] == tid
        assert rows[0].payload["meeting_title"] == "季度评审"
    assert events and events[0]["type"] == "minutes"
    assert "季度评审" in events[0]["headline"]


def test_run_minutes_invite_zero_model_skips(monkeypatch):
    """零激活静默跳过（降级矩阵）——认领行已落当日不重试（错过即丢）。"""
    import asyncio

    from services import workflows

    monkeypatch.setattr(workflows, "SessionLocal", TestingSessionLocal)
    events: list[dict] = []
    monkeypatch.setattr(workflows, "broadcast", lambda ev: events.append(ev))
    with TestingSessionLocal() as db:
        _mk_ended_meeting(db, 30)
        assert workflows._claim_minutes_invite(db, datetime.now()) is True
    asyncio.run(workflows._run_minutes_invite())
    assert not events
    with TestingSessionLocal() as db:
        assert not [r for r in ai_data.list_proposals(db)
                    if r.kind == "minutes_invite"]


def test_prep_payload_carries_minutes_leftover(client, monkeypatch):
    """prep 联动：会议的上期纪要行动项完成情况随载荷进 LLM——遗留行动项
    进 checklist 催办（prompt 规则），完成计数入载荷。"""
    import json as _json
    from datetime import date as _date, datetime as _dt, timedelta as _td

    from agents import graph as graph_mod
    from agents.prompts import PREP_PROMPT
    from services import llm

    captured: list[str] = []

    def fake(messages, *, json_mode=False, max_tokens=None):
        if PREP_PROMPT in messages[0]["content"]:
            captured.append(messages[1]["content"])
            return _json.dumps({"meetings": [], "reply": "已备"}, ensure_ascii=False)
        return "{}"

    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        tmrw = _date.today() + _td(days=1)
        meet = Task(title="周会", priority=1,
                    start_time=_dt.combine(tmrw, _dt.min.time()) + _td(hours=9),
                    duration_mins=60)
        db.add(meet)
        db.flush()  # 拿主键供行动项锚定
        # 该会议的上期纪要行动项：1 完成 1 遗留
        db.add(Task(title="整理客户反馈", source_key=f"minutes:{meet.id}",
                    done_at=_dt.now()))
        db.add(Task(title="预约下次评审", source_key=f"minutes:{meet.id}"))
        db.commit()
        out = graph_mod.run_meeting_prep(db, tmrw)
    assert out["meetings"] == []  # 替身返回空清单（载荷断言才是重点）
    entry = next(m for m in _json.loads(captured[0])["meetings"]
                 if m["title"] == "周会")
    assert entry["minutes_carryover"]["total"] == 2
    assert entry["minutes_carryover"]["done"] == 1
    assert entry["minutes_carryover"]["leftover"] == ["预约下次评审"]
