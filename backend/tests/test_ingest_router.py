"""routers/ingest.py 测试（基建见 conftest.py）。

POST /run 手动立即采集的三态（422 前置快查 / 409 在途 / 202 并出——_run_ingest 用
替身拦截，不真跑采集）；GET/PUT /sources 每源配置全链路。数据源形态细节在
test_data_ingest.py 直测 service 层，这里只过 HTTP 契约。
"""

import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from models import ModelProvider
from services import workflows


def _open_ingest(client: TestClient) -> None:
    """开齐三前置（采集开关 / AI 开关默认开 / 激活模型）。"""
    with TestingSessionLocal() as db:
        from services.settings import get_settings

        s = get_settings(db)
        s.ingest_enabled = True
        db.commit()
        db.add(ModelProvider(name=f"m-{uuid.uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def test_run_now_rejects_when_disabled(client: TestClient):
    # 采集开关默认关 → 422 引导（不区分后续前置，首个失败即返回）
    resp = client.post("/api/ingest/run")
    assert resp.status_code == 422
    assert "未开启" in resp.json()["detail"]


def test_run_now_rejects_without_model(client: TestClient):
    with TestingSessionLocal() as db:
        from services.settings import get_settings

        get_settings(db).ingest_enabled = True
        db.commit()
    resp = client.post("/api/ingest/run")
    assert resp.status_code == 422 and "模型" in resp.json()["detail"]


def test_run_now_202_and_409(client: TestClient, monkeypatch):
    _open_ingest(client)
    fired: list[str] = []

    async def _fake_run(manual: bool = False):
        fired.append("run")

    monkeypatch.setattr(workflows, "_run_ingest", _fake_run)
    resp = client.post("/api/ingest/run")
    assert resp.status_code == 202 and resp.json() == {"started": True}
    # 在途闸占用 → 409（不动真闸的模块态，patch 只读窥视的返回）
    monkeypatch.setattr(workflows, "ingest_in_flight", lambda: True)
    assert client.post("/api/ingest/run").status_code == 409


def test_sources_get_and_put_roundtrip(client: TestClient, tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    with TestingSessionLocal() as db:
        from models import IntegrationSettings

        db.add(IntegrationSettings(id=1, local_folder_paths=[str(tmp_path)],
                                   local_folder_enabled=True))
        db.commit()

    rows = client.get("/api/ingest/sources").json()
    assert [r["key"] for r in rows] == ["local_folder"]
    assert rows[0]["kind"] == "local_folder" and rows[0]["enabled"] is True

    # 未登记键 404；正常更新回填（await 回填不乐观）
    assert client.put("/api/ingest/sources/查无", json={"enabled": False}).status_code == 404
    resp = client.put("/api/ingest/sources/local_folder",
                      json={"enabled": False, "window_hours": 48})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["window_hours"] == 48
    # 越界窗口在 schema 层拦
    assert client.put("/api/ingest/sources/local_folder",
                      json={"window_hours": 200}).status_code == 422


def test_sources_includes_cli_with_declared_tools(client: TestClient, monkeypatch):
    """CLI 源出现在 /sources，available_tools 来自 spec 声明清单（零子进程）。
    源形态细节在 test_data_ingest.py 直测，这里只过 HTTP 契约。"""
    import sys
    from pathlib import Path

    from models import CliTool
    from services import cli_tools
    from services.cli_tools import CliAdapter, CliCommand

    fixture = Path(__file__).parent / "fixtures" / "fake_cli.py"
    monkeypatch.setitem(cli_tools.ADAPTERS, "fake", CliAdapter(
        adapter_id="fake", name="假 CLI", description="",
        launchers=((sys.executable, (str(fixture),)),),
        version_argv=("version",),
        fetches=(CliCommand("fetch", "静态采集", ("fetch",), 15.0),),
        readonly=(CliCommand("lookup", "补采查询", ("fetch", "{arg_keyword}"), 15.0),),
    ))
    with TestingSessionLocal() as db:
        db.add(CliTool(adapter_id="fake", enabled=True))
        db.commit()

    rows = client.get("/api/ingest/sources").json()
    mine = next(r for r in rows if r["key"] == "cli:fake")
    assert mine["kind"] == "cli" and mine["enabled"] is True
    assert [t["name"] for t in mine["available_tools"]] == ["fetch", "lookup"]


def test_run_now_wait_returns_summary(client: TestClient, monkeypatch):
    """等待型端点（2026-11-04 手动刷新批，数据源页刷新钮）：wait=true 等本轮
    完成返回摘要——静默轮 / 有产出两形态；非 wait 形态维持 202 旧契约。"""
    _open_ingest(client)

    async def _fake_silent(manual: bool = False):
        return {"skipped": None, "silent": True, "drafts": 0, "inbox": 0,
                "direct_calendar": 0, "direct_inbox": 0}

    monkeypatch.setattr(workflows, "_run_ingest", _fake_silent)
    resp = client.post("/api/ingest/run?wait=true")
    assert resp.status_code == 202 and resp.json()["silent"] is True  # 端点装饰器恒 202，摘要随体返回

    async def _fake_produced(manual: bool = False):
        return {"skipped": None, "silent": False, "drafts": 2, "inbox": 1,
                "direct_calendar": 3, "direct_inbox": 4}

    monkeypatch.setattr(workflows, "_run_ingest", _fake_produced)
    resp = client.post("/api/ingest/run?wait=true")
    body = resp.json()
    assert (body["drafts"], body["inbox"], body["direct_calendar"],
            body["direct_inbox"]) == (2, 1, 3, 4)
    # 竞态二道防线：skipped 键透传（采集熔断闸已于 2026-11-04 移除——采集是数据
    # 同步不是排期；此分支只剩 in_flight/ai_off/no_model/not_ready/error）
    async def _fake_skipped(manual: bool = False):
        return {"skipped": "not_ready", "silent": False, "drafts": 0, "inbox": 0,
                "direct_calendar": 0, "direct_inbox": 0}

    monkeypatch.setattr(workflows, "_run_ingest", _fake_skipped)
    assert client.post("/api/ingest/run?wait=true").json()["skipped"] == "not_ready"
