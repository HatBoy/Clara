"""CLI 工具接入测试（第四源 kind="cli"）：执行器 / 注册表 / 探测授权 / 窄口自定义 / 路由。

真实适配器命令待实测，行为验证全走 fake 替身（launchers 指向 tests/fixtures/
fake_cli.py，sys.executable 启动）。fake 适配器经 monkeypatch.setitem 注入
ADAPTERS（用例结束自动还原），不污染真实注册表。
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import TestingSessionLocal
from services import cli_tools
from services.cli_tools import CliAdapter, CliCommand

FIXTURE = Path(__file__).parent / "fixtures" / "fake_cli.py"


@pytest.fixture
def fake_adapter(monkeypatch):
    """注入 fake 适配器：全量声明（版本/授权/采集/补采），镜像 lark 的能力面。"""
    spec = CliAdapter(
        adapter_id="fake", name="假 CLI", description="测试替身",
        launchers=((sys.executable, (str(FIXTURE),)),),
        version_argv=("version",),
        auth_check=CliCommand("auth status", "检查授权状态", ("auth-status",), 10.0),
        auth_start=CliCommand("auth login", "发起授权", ("auth-start",), 10.0),
        auth_url_re=r"https?://\S+",
        fetches=(CliCommand("fetch", "静态采集", ("fetch",), 15.0),),
        readonly=(CliCommand("lookup", "补采查询", ("fetch", "{arg_keyword}"), 15.0),),
    )
    monkeypatch.setitem(cli_tools.ADAPTERS, "fake", spec)
    return spec


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setenv("FAKE_AUTHED", "1")


# —— 执行器 ————————————————————————————————————————————————————————————————


def test_discover_env_override_priority(monkeypatch, fake_adapter, tmp_path):
    target = tmp_path / "lark.exe"
    target.write_text("")
    monkeypatch.setenv("CLARA_CLI_BIN_FAKE", str(target))
    # env 覆盖优先于 launchers，且沿用首条 launcher 的前缀参数
    assert cli_tools.discover_bin(fake_adapter) == (str(target), (str(FIXTURE),))


def test_discover_not_found(monkeypatch):
    monkeypatch.setattr(cli_tools, "ADAPTERS", {})  # 排除真实注册表干扰
    spec = CliAdapter(
        adapter_id="nope", name="x", description="",
        launchers=(("clara-no-such-bin-xyz", ()),),
    )
    assert cli_tools.discover_bin(spec) is None


def test_run_cli_success(fake_adapter):
    ok, out, err = cli_tools.run_cli(fake_adapter, [sys.executable, str(FIXTURE), "version"], 10)
    assert ok and "1.0.0-fake" in out and err == ""


def test_run_cli_failure_summary_capped(fake_adapter):
    ok, out, err = cli_tools.run_cli(fake_adapter, [sys.executable, str(FIXTURE), "fail"], 10)
    assert not ok and len(err) <= 200 and "boom" in err


def test_run_cli_timeout_kills_tree(fake_adapter):
    ok, out, err = cli_tools.run_cli(fake_adapter, [sys.executable, str(FIXTURE), "slow"], 1)
    assert not ok and "超时" in err


def test_run_cli_output_capped(fake_adapter):
    ok, out, err = cli_tools.run_cli(fake_adapter, [sys.executable, str(FIXTURE), "big"], 30)
    assert ok and "已截断" in out and len(out) < 2 * 1024 * 1024 + 200


def test_run_cli_gbk_decoding():
    spec = CliAdapter(
        adapter_id="gbk", name="x", description="",
        launchers=(("python", ()),), encoding="gbk",
    )
    ok, out, err = cli_tools.run_cli(spec, [sys.executable, str(FIXTURE), "gbk"], 10)
    assert ok and "中文GBK内容" in out


def test_render_argv_placeholders():
    since, until = datetime(2026, 10, 15, 9), datetime(2026, 10, 16, 9)
    rendered = cli_tools.render_argv(
        ("fetch", "--from={since}", "--to", "{until}"), since=since, until=until)
    assert rendered == ["fetch", "--from=2026-10-15T09:00:00", "--to", "2026-10-16T09:00:00"]
    assert cli_tools.render_argv(("x", "{arg_k}"), arguments={"k": "v"}) == ["x", "v"]
    assert cli_tools.render_argv(("x", "{unknown}")) is None  # 残留占位拒绝执行
    assert cli_tools.render_argv(("{since}",)) is None  # 窗口缺参同样拒绝


def test_static_tokens_denied_scope():
    assert cli_tools.static_tokens_denied(("fetch", "--create")) is True   # 声明笔误拦下
    assert cli_tools.static_tokens_denied(("fetch", "{arg_q}")) is False  # 占位不查
    assert cli_tools.static_tokens_denied(("list", "--json")) is False    # 正常只读


# —— 路由：清单 / 探测 / 授权 / 接入管理 ————————————————————————————————


def test_list_tools_zero_subprocess(client, fake_adapter, monkeypatch):
    called = []
    monkeypatch.setattr(cli_tools, "run_cli",
                        lambda *a, **k: called.append(1) or (False, "", "x"))
    r = client.get("/api/cli/tools")
    assert r.status_code == 200
    data = {t["adapter_id"]: t for t in r.json()}
    assert "fake" in data and "lark" in data
    fake = data["fake"]
    assert fake["connected"] is False and fake["auth_mode"] == "auto"
    assert fake["fetch_ready"] and fake["actions"]  # fetch + lookup 两条声明
    assert not called  # 清单零子进程（探测态只读上次落库结果）


def test_probe_without_row_does_not_connect(client, fake_adapter, authed):
    """精选卡「检测」不建行——防探测即接入混进采集。"""
    r = client.post("/api/cli/tools/fake/probe")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["auth_ok"] is True
    data = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}
    assert data["fake"]["connected"] is False


def test_connect_snapshots_probe_columns(client, fake_adapter, authed):
    """接入即检测通过的持久化：connect 建行同时落库探测快照——行上不记的话
    已连接卡徽标落到「未检测」态（模态里明明检测通过才放行接入）。"""
    r = client.post("/api/cli/tools/fake/connect")
    assert r.status_code == 201
    body = r.json()
    assert body["bin_ok"] is True and body["auth_ok"] is True
    assert body["bin_version"]  # 快照带版本（徽标不展示，诊断仍留行上）


def test_probe_persists_to_connected_row(client, fake_adapter, authed):
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    body = client.post("/api/cli/tools/fake/probe").json()
    assert body["bin_ok"] and "1.0.0-fake" in body["bin_version"]
    data = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}
    assert data["fake"]["connected"] is True
    assert data["fake"]["auth_ok"] is True and data["fake"]["bin_version"]


def test_probe_unauthed_marks_auth_false(client, fake_adapter):
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    body = client.post("/api/cli/tools/fake/probe").json()  # FAKE_AUTHED 未设
    assert body["ok"] is False and body["auth_ok"] is False


def test_probe_ok_contains_body_anchor(client, monkeypatch, fake_adapter, authed):
    """lark-cli 实测口径：参数错误也 rc=0、错误体在 body——退出码不可信，授权
    判定须 ok_contains body 锚点。fake auth-status 成功输出 {"ok": true}：
    锚点不命中（rc=0 但 body 无标记）→ 未授权；锚点命中 → 已授权。"""
    import dataclasses

    miss = dataclasses.replace(fake_adapter, auth_check=CliCommand(
        "auth status", "x", ("auth-status",), 10.0,
        ok_contains='"tokenStatus": "valid"'))
    monkeypatch.setitem(cli_tools.ADAPTERS, "fake", miss)
    body = client.post("/api/cli/tools/fake/probe").json()
    assert body["bin_ok"] and body["auth_ok"] is False and body["ok"] is False

    hit = dataclasses.replace(fake_adapter, auth_check=CliCommand(
        "auth status", "x", ("auth-status",), 10.0, ok_contains='"ok": true'))
    monkeypatch.setitem(cli_tools.ADAPTERS, "fake", hit)
    assert client.post("/api/cli/tools/fake/probe").json()["auth_ok"] is True


def test_probe_needs_refresh_counts_authed(client, monkeypatch, fake_adapter):
    """lark 真机场景回归：tokenStatus=needs_refresh（access token 过期待刷，
    refresh token 仍在、CLI 真实调用时自动刷）也算已授权——曾只锚 valid，
    授权完成却误报未授权。ok_contains 多锚点任一命中即通过。"""
    import dataclasses

    refreshed = dataclasses.replace(fake_adapter, auth_check=CliCommand(
        "auth list", "x", ("auth-refresh",), 10.0,
        ok_contains=('"tokenStatus": "valid"', '"tokenStatus": "needs_refresh"')))
    monkeypatch.setitem(cli_tools.ADAPTERS, "fake", refreshed)
    body = client.post("/api/cli/tools/fake/probe").json()
    assert body["bin_ok"] and body["auth_ok"] is True


def test_anchor_hit_multi_anchor_forms():
    """锚点判定纯函数：tuple 任一命中 / str 单锚兼容旧声明 / None 只看退出码。"""
    anchors = ('"tokenStatus": "valid"', '"tokenStatus": "needs_refresh"')
    assert cli_tools._anchor_hit(anchors, '[{"tokenStatus": "needs_refresh"}]') is True
    assert cli_tools._anchor_hit(anchors, '[{"tokenStatus": "valid"}]') is True
    assert cli_tools._anchor_hit(anchors, "[]") is False
    assert cli_tools._anchor_hit('"ok": true', '{"ok": true}') is True
    assert cli_tools._anchor_hit(None, "anything") is True


def test_auth_start_extracts_url(client, fake_adapter):
    r = client.post("/api/cli/tools/fake/auth/start")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["url"] == "https://example.com/auth?code=abc123"


def test_auth_status_poll(client, fake_adapter, authed):
    r = client.get("/api/cli/tools/fake/auth/status")
    assert r.json()["authenticated"] is True


def test_update_enabled_mirrors_ingest_source(client, fake_adapter, authed):
    from services import data_ingest

    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    r = client.put("/api/cli/tools/fake", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    with TestingSessionLocal() as db:
        row = data_ingest._source_row(db, "cli:fake")
        assert row is not None and row.enabled is False


def test_delete_keeps_cli_credentials(client, fake_adapter, authed):
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    assert client.delete("/api/cli/tools/fake").status_code == 204
    data = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}
    assert data["fake"]["connected"] is False
    assert client.delete("/api/cli/tools/fake").status_code == 404  # 重复删 404


def test_connect_not_installed_422(client, monkeypatch):
    monkeypatch.setitem(cli_tools.ADAPTERS, "nope", CliAdapter(
        adapter_id="nope", name="x", description="",
        launchers=(("clara-no-such-bin-xyz", ()),)))
    r = client.post("/api/cli/tools/nope/connect")
    assert r.status_code == 422 and "未检测到" in r.json()["detail"]


def test_connect_unknown_adapter_404(client):
    assert client.post("/api/cli/tools/no-such-adapter/connect").status_code == 404


# —— 窄口自定义 ————————————————————————————————————————————————————————————


def test_custom_create_rejects_write_markers(client):
    r = client.post("/api/cli/tools/custom", json={
        "name": "x", "bin_path": "C:/x.exe", "argv": ["list", "--create"]})
    assert r.status_code == 422


def test_custom_create_rejects_arg_placeholder(client):
    r = client.post("/api/cli/tools/custom", json={
        "name": "x", "bin_path": "C:/x.exe", "argv": ["fetch", "{arg_q}"]})
    assert r.status_code == 422


def test_custom_create_ok_and_lists(client, fake_adapter, authed):
    """伪 spec 采集走通 + 清单标注 custom；readonly 恒空（不进补采）。"""
    r = client.post("/api/cli/tools/custom", json={
        "name": "我的清单", "bin_path": sys.executable,
        "argv": [str(FIXTURE), "fetch", "{since}"]})
    assert r.status_code == 201
    adapter_id = r.json()["adapter_id"]
    assert adapter_id.startswith("custom:")
    data = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}
    mine = data[adapter_id]
    assert mine["custom"] is True and mine["connected"] and mine["fetch_ready"]
    assert mine["actions"] == []  # 白名单恒空
    # 探测/授权端点不适用自定义行（404——无注册表条目）
    assert client.post(f"/api/cli/tools/{adapter_id}/probe").status_code == 404


def _create_custom(client, argv=None) -> str:
    r = client.post("/api/cli/tools/custom", json={
        "name": "测试行", "bin_path": sys.executable,
        "argv": argv or [str(FIXTURE), "fetch"]})
    assert r.status_code == 201
    return r.json()["adapter_id"]


def test_custom_test_ok_counts_items(client):
    """「测试」成功路径：fake fetch 出 3 条（测试不过时间窗，窗外条目也计数——
    报原始返回数防"有数据"被砍成"0 条"的误判）；结果落库供徽标展示。"""
    adapter_id = _create_custom(client)
    body = client.post(f"/api/cli/tools/{adapter_id}/test").json()
    assert body["ok"] is True and body["items_count"] == 3
    assert body["latency_ms"] is not None and body["error"] is None
    mine = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}[adapter_id]
    assert mine["test_ok"] is True and mine["test_items"] == 3


def test_custom_test_failure_marks_error(client):
    adapter_id = _create_custom(client, argv=[str(FIXTURE), "fail"])
    body = client.post(f"/api/cli/tools/{adapter_id}/test").json()
    assert body["ok"] is False and body["items_count"] is None and "boom" in body["error"]
    mine = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}[adapter_id]
    assert mine["test_ok"] is False and "boom" in mine["last_error"]


def test_custom_test_missing_bin(client):
    r = client.post("/api/cli/tools/custom", json={
        "name": "坏路径", "bin_path": "C:/clara/no-such-bin-xyz.exe",
        "argv": ["fetch"]})
    assert r.status_code == 201
    body = client.post(f"/api/cli/tools/{r.json()['adapter_id']}/test").json()
    assert body["ok"] is False and "未找到可执行文件" in body["error"]


def test_custom_test_featured_422_and_missing_404(client, fake_adapter, authed):
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    assert client.post("/api/cli/tools/fake/test").status_code == 422  # 精选行走 probe
    assert client.post("/api/cli/tools/custom:no-such/test").status_code == 404


def test_custom_update_roundtrip_clears_test(client):
    """「配置」编辑 round-trip：全量三字段生效 + 旧测试结果作废置空。"""
    adapter_id = _create_custom(client)
    assert client.post(f"/api/cli/tools/{adapter_id}/test").json()["ok"] is True
    r = client.put(f"/api/cli/tools/{adapter_id}", json={
        "name": "改名了", "bin_path": sys.executable,
        "argv": [str(FIXTURE), "fetch2"]})
    assert r.status_code == 200
    mine = {t["adapter_id"]: t for t in client.get("/api/cli/tools").json()}[adapter_id]
    assert mine["name"] == "改名了" and mine["custom_argv"] == [str(FIXTURE), "fetch2"]
    assert mine["bin_path"] == sys.executable  # 展示位跟随 custom_bin
    assert mine["test_ok"] is None and mine["test_items"] is None  # 配置变了，旧结果是谎言


def test_custom_update_rejects_write_markers_and_partial(client):
    adapter_id = _create_custom(client)
    # deny-list 命中（与 create 同闸）
    r = client.put(f"/api/cli/tools/{adapter_id}", json={
        "name": "x", "bin_path": sys.executable, "argv": [str(FIXTURE), "--send"]})
    assert r.status_code == 422
    # 部分字段编辑拒绝（须三字段全量同给）
    r = client.put(f"/api/cli/tools/{adapter_id}", json={"name": "只改名"})
    assert r.status_code == 422


def test_custom_update_featured_row_422(client, fake_adapter, authed):
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    r = client.put("/api/cli/tools/fake", json={
        "name": "x", "bin_path": sys.executable, "argv": ["fetch"]})
    assert r.status_code == 422  # 精选行显示名与命令以注册表声明为准


def test_custom_draft_test_runs_without_creating_row(client):
    """表单态直测（「保存前先试」）：真跑命令数条目，但不建行不落库——
    GET /tools 依旧零自定义行。"""
    body = client.post("/api/cli/tools/custom/test", json={
        "name": "草稿", "bin_path": sys.executable,
        "argv": [str(FIXTURE), "fetch"]}).json()
    assert body["ok"] is True and body["items_count"] == 3
    data = client.get("/api/cli/tools").json()
    assert not any(t["custom"] for t in data)  # 试跑不落行


def test_custom_draft_test_same_validation_gate(client):
    """校验闸与保存同源：deny-list 命中 422（能保存的命令必能测试）。"""
    r = client.post("/api/cli/tools/custom/test", json={
        "name": "x", "bin_path": sys.executable,
        "argv": [str(FIXTURE), "--send"]})
    assert r.status_code == 422


# —— 用户介入即重置连败冷却（2026-10-16）—————————————————————————————————————


def _ingest_cli_row():
    from sqlalchemy import select
    from models import IngestSource

    with TestingSessionLocal() as db:
        return db.scalar(select(IngestSource).where(IngestSource.source_key == "cli:fake"))


def test_cli_user_actions_reset_ingest_cooldown(client, fake_adapter, authed):
    """接入 / 行编辑（启停）/ 检测通过 → cli:fake 连败证据清零——修好凭据的
    CLI 源不被旧失败记录拦在冷却里（对齐 MCP/Composio 的重置口径）。"""
    from services import data_ingest

    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "cli:fake", False, "CLI 执行失败")  # 预置连败
    assert client.post("/api/cli/tools/fake/connect").status_code == 201
    row = _ingest_cli_row()
    assert row.fail_streak == 0 and row.last_error is None

    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "cli:fake", False, "再次失败")
    assert client.put("/api/cli/tools/fake", json={"enabled": False}).status_code == 200
    assert _ingest_cli_row().fail_streak == 0

    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "cli:fake", False, "第三次失败")
    assert client.post("/api/cli/tools/fake/probe").json()["ok"] is True
    assert _ingest_cli_row().fail_streak == 0
