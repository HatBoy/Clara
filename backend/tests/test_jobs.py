"""定时任务服务与接口测试（2026-10-20 增）：jobs 注册表 / 自定义简报 CRUD /
hardtask 与 brief 两条新工作流（基建见 conftest.py）。

CRUD 走 client（HTTP 层校验 422/404 翻译）；工作流仿 test_workflows 先例
（asyncio.run 同步驱动 + _load_agent 替身 + subscribe 捕获广播）。
"""

import asyncio
from datetime import date, datetime

from fastapi.testclient import TestClient

from conftest import TestingSessionLocal
from models import Task, WorkflowRun
from services import ai_data, jobs, workflows
from services.settings import get_settings


def _use_test_db(monkeypatch) -> None:
    import services.workflows as wf

    monkeypatch.setattr(wf, "SessionLocal", TestingSessionLocal)


def _activate_model() -> None:
    import uuid as _uuid

    from models import ModelProvider

    with TestingSessionLocal() as db:
        db.add(ModelProvider(name=f"m-{_uuid.uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def _add_brief(brief_min: int, instruction: str = "今日日程小结", **kw):
    with TestingSessionLocal() as db:
        row = jobs.create_brief(db, brief_min=brief_min, instruction=instruction, **kw)
        return row.id


# —— 自定义简报 CRUD（HTTP 层） ——————————————————————————————————————————————


def test_brief_crud_roundtrip(client: TestClient):
    resp = client.post("/api/jobs/briefs",
                       json={"brief_min": 540, "instruction": "早报今日安排"})
    assert resp.status_code == 201
    bid = resp.json()["id"]
    assert resp.json()["enabled"] is True
    assert client.get("/api/jobs/briefs").json()[0]["brief_min"] == 540
    # 部分更新：改时刻与指令
    resp = client.patch(f"/api/jobs/briefs/{bid}",
                        json={"brief_min": 1290, "instruction": "晚报复盘"})
    assert resp.status_code == 200
    assert resp.json()["brief_min"] == 1290 and resp.json()["instruction"] == "晚报复盘"
    # 停用保留行
    assert client.patch(f"/api/jobs/briefs/{bid}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/jobs/briefs/{bid}").status_code == 204
    assert client.get("/api/jobs/briefs").json() == []


def test_brief_create_limit_three_422(client: TestClient):
    for i, m in enumerate((480, 720, 960)):
        assert client.post("/api/jobs/briefs",
                           json={"brief_min": m, "instruction": f"简报{i}"}).status_code == 201
    resp = client.post("/api/jobs/briefs", json={"brief_min": 1200, "instruction": "第四条"})
    assert resp.status_code == 422 and "3" in resp.json()["detail"]


def test_brief_create_same_slot_422(client: TestClient):
    client.post("/api/jobs/briefs", json={"brief_min": 600, "instruction": "a"})
    resp = client.post("/api/jobs/briefs", json={"brief_min": 600, "instruction": "b"})
    assert resp.status_code == 422 and "时刻" in resp.json()["detail"]


def test_brief_create_granularity_422(client: TestClient):
    resp = client.post("/api/jobs/briefs", json={"brief_min": 545, "instruction": "x"})
    assert resp.status_code == 422 and "30" in resp.json()["detail"]


def test_brief_patch_same_slot_excludes_self(client: TestClient):
    a = client.post("/api/jobs/briefs", json={"brief_min": 600, "instruction": "a"}).json()["id"]
    b = client.post("/api/jobs/briefs", json={"brief_min": 660, "instruction": "b"}).json()["id"]
    # 撞 a 的时刻 → 422；改回自己的时刻 → 200（排除自身）
    assert client.patch(f"/api/jobs/briefs/{b}", json={"brief_min": 600}).status_code == 422
    assert client.patch(f"/api/jobs/briefs/{b}", json={"brief_min": 660}).status_code == 200


def test_brief_patch_delete_missing_404(client: TestClient):
    assert client.patch("/api/jobs/briefs/nope", json={"enabled": False}).status_code == 404
    assert client.delete("/api/jobs/briefs/nope").status_code == 404


# —— 任务注册表（GET /api/jobs） ——————————————————————————————————————————————


def test_list_jobs_registry_builtins(client: TestClient):
    body = client.get("/api/jobs").json()
    keys = [j["key"] for j in body["jobs"]]
    assert keys == [
        "morning", "ingest", "nightly", "prep", "hardtask", "weekly_report",
        "minutes_invite", "chat_autocompact", "heartbeat",
    ]
    by_key = {j["key"]: j for j in body["jobs"]}
    # 文案后端单源非空；heartbeat 非时刻驱动（无运行痕迹时 last_run 为空不谎报）
    assert all(j["desc"] for j in body["jobs"])
    assert by_key["heartbeat"]["time_min"] is None
    assert by_key["heartbeat"]["time_note"] == "每 3 小时自动检查"
    assert by_key["heartbeat"]["requires_ai"] is False
    assert by_key["heartbeat"]["last_run_at"] is None
    # prep / hardtask / weekly_report 与 nightly 共享报点时刻
    assert by_key["prep"]["time_min"] == by_key["nightly"]["time_min"]
    assert by_key["hardtask"]["time_min"] == by_key["nightly"]["time_min"]
    wk = by_key["weekly_report"]
    assert wk["time_min"] == by_key["nightly"]["time_min"]
    assert wk["time_note"] == "仅周日晚间运行"
    assert wk["enabled"] is True  # 默认开（同四流）
    # 对话上下文压缩（2026-10-26）：挂轮末非 cron，无 last_run 不谎报；默认开
    ac = by_key["chat_autocompact"]
    assert ac["time_min"] is None
    assert ac["time_note"] == "每轮对话结束后自动检查"
    assert ac["requires_ai"] is True
    assert ac["last_run_at"] is None
    assert ac["enabled"] is True


def test_list_jobs_heartbeat_last_run(client: TestClient, monkeypatch):
    # 最近检查时刻（内存口径，2026-10-21 五点修缮）：跑过一次 tick 后注册表带出，
    # 让用户确认负荷监控真的在跑（重启重计——模块态，测试重置防跨用例串味）
    ts = datetime(2026, 10, 21, 14, 0, 0)
    monkeypatch.setattr(workflows, "_last_heartbeat", ts)
    body = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert body["heartbeat"]["last_run_at"] is not None
    assert body["heartbeat"]["last_run_at"].startswith("2026-10-21T14:00")
    monkeypatch.setattr(workflows, "_last_heartbeat", None)


def test_list_jobs_ingest_enabled_mirror(client: TestClient):
    client.get("/api/settings")
    client.patch("/api/settings", json={"ingest_enabled": False})
    body = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert body["ingest"]["enabled"] is False
    assert body["morning"]["enabled"] is True  # 各流独立互不影响


def test_list_jobs_flow_switches_reflect_settings(client: TestClient):
    # 四流开关（2026-10-21 复查批 D1）：PATCH 关闭 → 注册表行 enabled 镜像 False；
    # 关 morning 不连坐 nightly/prep/hardtask；chat_autocompact 同款（2026-10-26）；
    # weekly_report 同款（2026-11-07 统计 120 分档）
    client.get("/api/settings")
    body = client.get("/api/settings").json()
    assert body["chat_autocompact_enabled"] is True  # 默认开（维持既有滚动摘要行为）
    assert body["weekly_report_enabled"] is True  # 默认开（同四流）
    client.patch("/api/settings", json={
        "morning_enabled": False, "nightly_enabled": False,
        "prep_enabled": False, "hardtask_enabled": False,
        "weekly_report_enabled": False,
        "chat_autocompact_enabled": False})
    assert client.get("/api/settings").json()["chat_autocompact_enabled"] is False  # PATCH 往返
    assert client.get("/api/settings").json()["weekly_report_enabled"] is False
    body = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    for key in ("morning", "nightly", "prep", "hardtask", "weekly_report", "chat_autocompact"):
        assert body[key]["enabled"] is False
    client.patch("/api/settings", json={"morning_enabled": True})
    body = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert body["morning"]["enabled"] is True
    assert body["nightly"]["enabled"] is False  # 其余不动


def test_list_jobs_brief_rows_with_last_run(client: TestClient):
    client.get("/api/settings")
    bid = client.post("/api/jobs/briefs",
                      json={"brief_min": 600, "instruction": "我的简报"}).json()["id"]
    # 认领一次（写 workflow_runs）→ last_run_at 出现
    with TestingSessionLocal() as db:
        assert workflows._claim_run(db, f"brief:{bid}", date.today()) is True
    jobs_out = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    row = jobs_out[f"brief:{bid}"]
    assert row["title"] == "定时简报"
    assert row["desc"] == "我的简报"  # 指令即描述
    assert row["enabled"] is True and row["last_run_at"] is not None
    # 停用行仍展示（enabled=False），删除行消失
    client.patch(f"/api/jobs/briefs/{bid}", json={"enabled": False})
    jobs_out = {j["key"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert jobs_out[f"brief:{bid}"]["enabled"] is False
    client.delete(f"/api/jobs/briefs/{bid}")
    keys = [j["key"] for j in client.get("/api/jobs").json()["jobs"]]
    assert f"brief:{bid}" not in keys


# —— 触发谓词 ————————————————————————————————————————————————————————————————


def test_claim_hardtask_threshold_and_idempotent(monkeypatch):
    _use_test_db(monkeypatch)
    from services.settings import get_settings

    with TestingSessionLocal() as db:
        get_settings(db).nightly_start_min = 1080  # 18:00
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows._claim_hardtask(db, datetime.now().replace(hour=17, minute=59)) is False
        assert workflows._claim_hardtask(db, datetime.now().replace(hour=18, minute=0)) is True
        assert workflows._claim_hardtask(db, datetime.now().replace(hour=23, minute=0)) is False


def test_claim_custom_briefs_hit_and_idempotent(monkeypatch):
    _use_test_db(monkeypatch)
    b_on = _add_brief(600)
    b_off = _add_brief(690)
    with TestingSessionLocal() as db:
        from models import CustomBrief

        db.get(CustomBrief, b_off).enabled = False
        db.commit()
    now = datetime.now().replace(hour=11, minute=0)  # 11:00 已过 10:00 档
    with TestingSessionLocal() as db:
        assert workflows._claim_custom_briefs(db, now) == [b_on]  # 停用行不认领
    with TestingSessionLocal() as db:
        assert workflows._claim_custom_briefs(db, now) == []  # 当日幂等
    # 错过时刻补账：12:30 起跑 → 已认领的幂等挡、停用行恒跳过 → 空
    with TestingSessionLocal() as db:
        assert workflows._claim_custom_briefs(
            db, datetime.now().replace(hour=12, minute=30)) == []


# —— 工作流 runner ——————————————————————————————————————————————————————————


def test_run_hardtask_builds_proposal(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    with TestingSessionLocal() as db:
        t = Task(title="写调研方案", duration_mins=120)
        db.add(t)
        db.commit()
        db.refresh(t)
        tid = t.id

    def _fake_build(payload, rules, today_iso, **kw):  # **kw 吸收批 3 core_rules
        assert payload["task_id"] == tid
        return {"headline": "先定调研范围", "steps": ["列问题清单", "找两份参考"],
                "advice": "上午头脑清醒时先列大纲。"}

    import agents.nodes as nodes
    import services.workflows as wf
    monkeypatch.setattr(nodes, "build_hardtask_brief", _fake_build)
    q = wf.subscribe()
    try:
        asyncio.run(wf._run_hardtask())
        event = q.get_nowait()
        assert event["type"] == "hardtask" and event["headline"] == "先定调研范围"
    finally:
        wf.unsubscribe(q)
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "hardtask"
        assert proposals[0].payload["steps"] == ["列问题清单", "找两份参考"]
        assert proposals[0].payload["task_id"] == tid


def test_run_hardtask_no_candidate_silent(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    import services.workflows as wf
    called = []
    monkeypatch.setattr(wf, "_load_agent", lambda *a: lambda db, tid: called.append(tid))
    q = wf.subscribe()
    try:
        asyncio.run(wf._run_hardtask())
        assert q.qsize() == 0  # 零候选零广播
    finally:
        wf.unsubscribe(q)
    assert called == []  # 零候选连 agent 都不进
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


def test_run_hardtask_llm_skip_silent(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    with TestingSessionLocal() as db:
        db.add(Task(title="写周报", duration_mins=120))
        db.commit()
    import agents.nodes as nodes
    import services.workflows as wf
    monkeypatch.setattr(nodes, "build_hardtask_brief", lambda *a, **kw: None)  # LLM skip
    asyncio.run(wf._run_hardtask())
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


def test_run_hardtask_skips_without_model(monkeypatch):
    _use_test_db(monkeypatch)
    import services.workflows as wf
    called = []
    monkeypatch.setattr(wf, "_load_agent", lambda *a: lambda db, tid: called.append(tid))
    asyncio.run(wf._run_hardtask())
    assert called == []


def test_run_custom_brief_builds_proposal(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    bid = _add_brief(600, "今日日程小结")

    def _fake_run(db, brief_id):
        assert brief_id == bid
        return {"headline": "今日 3 件事", "body": "- 上午写方案\n- 下午评审"}

    import services.workflows as wf
    monkeypatch.setattr(wf, "_load_agent", lambda *a: _fake_run)
    q = wf.subscribe()
    try:
        asyncio.run(wf._run_custom_brief(bid))
        event = q.get_nowait()
        assert event["type"] == "brief" and event["headline"] == "今日 3 件事"
    finally:
        wf.unsubscribe(q)
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "brief"
        assert proposals[0].payload["brief_id"] == bid
        assert proposals[0].payload["body"].startswith("- 上午")


def test_run_custom_brief_missing_row_silent(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    import agents.nodes as nodes
    import services.workflows as wf
    called = []
    monkeypatch.setattr(nodes, "build_custom_brief", lambda *a: called.append(1))
    asyncio.run(wf._run_custom_brief("nope"))
    assert called == []  # 行已删：graph 入口静默，agent 不进
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


def test_run_custom_brief_disabled_row_silent(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()
    bid = _add_brief(600)
    with TestingSessionLocal() as db:
        from models import CustomBrief

        db.get(CustomBrief, bid).enabled = False
        db.commit()
    import agents.nodes as nodes
    import services.workflows as wf
    called = []
    monkeypatch.setattr(nodes, "build_custom_brief", lambda *a: called.append(1))
    asyncio.run(wf._run_custom_brief(bid))
    assert called == []  # 已停用：停用意图优先于补账


def test_run_morning_fires_push(monkeypatch):
    """晨间挂点：简报全文（final_reply）经 _fire_push 推渠道（打桩记录——
    fire-and-forget 后台执行由 test_fire_push_background 单独覆盖）。"""
    _use_test_db(monkeypatch)
    _activate_model()
    pushed: list = []
    monkeypatch.setattr(workflows, "_fire_push", pushed.append)

    def _fake_preflight(db, day):
        return {"preflight": {"headline": "今日 3 项"},
                "final_reply": "早报：今日 3 项重点……"}

    monkeypatch.setattr(workflows, "_load_agent", lambda *a: _fake_preflight)
    asyncio.run(workflows._run_morning())
    assert pushed == ["早报：今日 3 项重点……"]


def test_run_hardtask_push_full_text(monkeypatch):
    """高难协助挂点：渠道推送拼全文（headline + 步骤 + 💡 建议——卡片细节
    渠道端看不到，只推标题等于没推）。"""
    _use_test_db(monkeypatch)
    _activate_model()
    pushed: list = []
    monkeypatch.setattr(workflows, "_fire_push", pushed.append)
    with TestingSessionLocal() as db:
        t = Task(title="写调研方案", duration_mins=120)
        db.add(t)
        db.commit()
        db.refresh(t)
        tid = t.id

    import agents.nodes as nodes

    monkeypatch.setattr(nodes, "build_hardtask_brief", lambda *a, **kw: {  # kw 吸收批 3 core_rules
        "headline": "先定调研范围", "steps": ["列问题清单", "找两份参考"],
        "advice": "上午头脑清醒时先列大纲。"})
    asyncio.run(workflows._run_hardtask())
    assert pushed == ["先定调研范围\n1. 列问题清单\n2. 找两份参考\n"
                      "💡 上午头脑清醒时先列大纲。"]
    assert tid  # 建卡主链路不受推送挂点影响（pushed 已证流程走完）


def test_run_meeting_prep_payload_carries_history(client: TestClient, monkeypatch):
    """prep 建议史回放（优化批 7）：该会议此前 prep 卡的拒绝理由随载荷进
    LLM——重推清单不再重复推被拒项。"""
    import json as _json

    from agents import graph as graph_mod
    from agents.prompts import PREP_PROMPT
    from conftest import TestingSessionLocal
    from models import Task
    from services import ai_data, llm
    from services.job_rules import pick_important_meetings

    captured: list[str] = []

    def fake_llm(messages, *, json_mode=False, max_tokens=None):
        if PREP_PROMPT in messages[0]["content"]:
            captured.append(messages[1]["content"])
            return _json.dumps({"meetings": [], "reply": "已备"}, ensure_ascii=False)
        return "{}"

    monkeypatch.setattr(llm, "call_llm", fake_llm)
    with TestingSessionLocal() as db:
        tmrw = __import__("datetime").date.today() + __import__("datetime").timedelta(days=1)
        db.add(Task(title="季度汇报", priority=1,
                    start_time=__import__("datetime").datetime.combine(
                        tmrw, __import__("datetime").datetime.min.time()) + __import__("datetime").timedelta(hours=9),
                    duration_mins=90))
        db.commit()
        meets = pick_important_meetings(db, tmrw)
        assert meets  # 90 分钟高优会确认识别
        old = ai_data.create_proposal(db, kind="prep", payload={
            "meetings": [{"task_id": meets[0][0].id, "title": "季度汇报",
                          "headline": "x", "checklist": ["上次被删的准备项"]}]})
        ai_data.record_proposal_outcome(db, old.id, "rejected",
                                        reason="材料都齐了不用准备")
        out = graph_mod.run_meeting_prep(db, tmrw)
    payload = _json.loads(captured[0])
    entry = next(m for m in payload["meetings"] if m["title"] == "季度汇报")
    assert entry["history"]["outcome"] == "rejected"
    assert entry["history"]["reject_reason"] == "材料都齐了不用准备"
