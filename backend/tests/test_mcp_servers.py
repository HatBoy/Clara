"""MCP 服务接入测试（基建见 conftest.py）。

替身策略：monkeypatch services.mcp_servers._client_factory（MultiServerMCPClient
唯一入口）——FakeClient 捕获 connections 参数形状、返回预置工具或注入异常，
全程不真联网、不起子进程。异步 service 用 asyncio.run 直调（无 pytest-asyncio，
不引新依赖）。防泄露锁定：兜底文案只含异常类型名，不透传 str(e)。
"""

import asyncio
import time

import httpx

from conftest import TestingSessionLocal
from models import IntegrationSettings, McpServer
from services import mcp_servers as mcp_service


# —— 替身基建 ——


def _install_fake(monkeypatch, tools=None, raiser=None) -> dict:
    """替换 _client_factory：返回捕获器（connections 参数形状落 captured["conns"]）。"""
    captured: dict = {}

    class FakeClient:
        def __init__(self, *, connections=None):
            captured["conns"] = connections

        async def get_tools(self):
            if raiser is not None:
                raise raiser
            return list(tools or [])

    monkeypatch.setattr(mcp_service, "_client_factory", lambda: FakeClient)
    return captured


_STDIO_BODY = {
    "name": "示例服务",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "some-mcp-server", "-a", "app_id_x", "-s", "secret_x"],
    "env": {"EXAMPLE_REGION": "cn"},
}

_HTTP_BODY = {
    "name": "滴答清单",
    "transport": "streamable_http",
    "url": "https://mcp.dida365.com",
    "headers": {"Authorization": "Bearer tok_x"},
}


def _create(body) -> str:
    """直插一行（不经 API，用例只关心后续操作）；返回 id。"""
    with TestingSessionLocal() as db:
        row = McpServer(**body)
        db.add(row)
        db.commit()
        return row.id


# —— CRUD ——


def test_create_stdio_full_roundtrip(client):
    r = client.post("/api/mcp", json=_STDIO_BODY)
    assert r.status_code == 201
    out = r.json()
    assert out["name"] == "示例服务" and out["transport"] == "stdio"
    assert out["args"] == _STDIO_BODY["args"]  # JSON 列 round-trip 原样
    assert out["env"] == _STDIO_BODY["env"]
    assert out["url"] is None and out["headers"] is None
    assert out["enabled"] is True and out["last_test_ok"] is None  # 从未测试

    listed = client.get("/api/mcp").json()  # GET 回显（免重填口径）
    assert listed[0]["headers"] is None and listed[0]["env"] == _STDIO_BODY["env"]


def test_create_http_url_headers(client):
    r = client.post("/api/mcp", json=_HTTP_BODY)
    assert r.status_code == 201
    out = r.json()
    assert out["url"] == "https://mcp.dida365.com"
    assert out["headers"] == {"Authorization": "Bearer tok_x"}
    assert out["command"] is None and out["args"] is None and out["env"] is None  # 空容器归一 None


def test_create_duplicate_name_409(client):
    assert client.post("/api/mcp", json=_STDIO_BODY).status_code == 201
    r = client.post("/api/mcp", json={**_STDIO_BODY, "command": "uvx"})
    assert r.status_code == 409


def test_create_stdio_missing_command_422(client):
    body = {k: v for k, v in _STDIO_BODY.items() if k not in ("command",)}
    assert client.post("/api/mcp", json=body).status_code == 422


def test_create_http_missing_url_422(client):
    body = {k: v for k, v in _HTTP_BODY.items() if k != "url"}
    assert client.post("/api/mcp", json=body).status_code == 422


def test_list_creation_order(client):
    for i in range(3):
        assert client.post("/api/mcp", json={**_STDIO_BODY, "name": f"服务{i}"}).status_code == 201
    names = [s["name"] for s in client.get("/api/mcp").json()]
    assert names == ["服务0", "服务1", "服务2"]


def test_patch_rename_conflict_409(client):
    sid1 = _create(_STDIO_BODY)
    assert client.post("/api/mcp", json={**_HTTP_BODY, "name": "另一个"}).status_code == 201
    r = client.patch(f"/api/mcp/{sid1}", json={"name": "另一个"})
    assert r.status_code == 409
    # 改回自身原名不冲突（排除自身）
    assert client.patch(f"/api/mcp/{sid1}", json={"name": "示例服务"}).status_code == 200


def test_patch_switch_transport_merged_422(client):
    """stdio 行 PATCH 切 http 但未给 url：合并态校验拦（Create 的 validator 看不到库）。"""
    sid = _create(_STDIO_BODY)
    r = client.patch(f"/api/mcp/{sid}", json={"transport": "streamable_http"})
    assert r.status_code == 422


def test_patch_stdio_command_null_422(client):
    """stdio 行显式清 command：合并态仍 stdio 但无 command → 422。"""
    sid = _create(_STDIO_BODY)
    assert client.patch(f"/api/mcp/{sid}", json={"command": None}).status_code == 422


def test_patch_empty_no_updated_at_bump(client):
    sid = _create(_STDIO_BODY)
    with TestingSessionLocal() as db:
        before = db.get(McpServer, sid).updated_at
    time.sleep(0.02)
    assert client.patch(f"/api/mcp/{sid}", json={}).status_code == 200
    with TestingSessionLocal() as db:
        assert db.get(McpServer, sid).updated_at == before  # 未抬


def test_patch_switch_transport_clears_opposite(client):
    """切 transport 清对侧字段：一次 PATCH 完成切换，无需再发清除命令。"""
    sid = _create(_STDIO_BODY)
    r = client.patch(f"/api/mcp/{sid}", json={"transport": "streamable_http", "url": "http://x/mcp"})
    assert r.status_code == 200
    out = r.json()
    assert out["command"] is None and out["args"] is None and out["env"] is None
    assert out["url"] == "http://x/mcp"
    # 未切换时不动对侧（纯改名不清 http 字段）
    sid2 = _create(_HTTP_BODY)
    out2 = client.patch(f"/api/mcp/{sid2}", json={"name": "改名"}).json()
    assert out2["url"] == _HTTP_BODY["url"] and out2["headers"] == _HTTP_BODY["headers"]


def test_delete_204_and_404(client):
    sid = _create(_STDIO_BODY)
    assert client.delete(f"/api/mcp/{sid}").status_code == 204
    assert client.delete(f"/api/mcp/{sid}").status_code == 404
    assert client.get("/api/mcp").json() == []


def test_patch_404(client):
    assert client.patch("/api/mcp/nope", json={"name": "x"}).status_code == 404


def test_description_roundtrip_and_clear(client):
    """服务描述（2026-09-19 增）：Create 携带 → GET 回显；PATCH 显式 null 清空。"""
    r = client.post("/api/mcp", json={**_HTTP_BODY, "description": "滴答清单官方 MCP 服务"})
    assert r.status_code == 201 and r.json()["description"] == "滴答清单官方 MCP 服务"
    assert client.get("/api/mcp").json()[0]["description"] == "滴答清单官方 MCP 服务"

    sid = r.json()["id"]
    out = client.patch(f"/api/mcp/{sid}", json={"description": None}).json()
    assert out["description"] is None
    with TestingSessionLocal() as db:  # 真清到库里，非仅响应缺省
        assert db.get(McpServer, sid).description is None


def test_description_overlong_422(client):
    assert (
        client.post("/api/mcp", json={**_HTTP_BODY, "description": "长" * 101}).status_code == 422
    )
    sid = _create(_HTTP_BODY)
    assert (
        client.patch(f"/api/mcp/{sid}", json={"description": "长" * 101}).status_code == 422
    )


def test_description_empty_string_to_none(client):
    """trim 后空串归一 None（validator 口径，同 command/url）——前端发空串不落空串。"""
    r = client.post("/api/mcp", json={**_HTTP_BODY, "description": "   "})
    assert r.status_code == 201 and r.json()["description"] is None


# —— probe_server（service 层直调，永不抛）——


def test_probe_ok_stdio_conn_shape(monkeypatch):
    captured = _install_fake(monkeypatch, tools=[object(), object(), object()])
    out = asyncio.run(mcp_service.probe_server(
        "stdio", "npx", ["-y", "pkg"], {"K": "V"}, None, None
    ))
    assert out.ok is True and out.tools_count == 3 and out.latency_ms >= 0
    # 连接参数形状锁定：stdio 只带 command/args/env，不混 url/headers
    assert captured["conns"] == {
        "probe": {"transport": "stdio", "command": "npx", "args": ["-y", "pkg"], "env": {"K": "V"}}
    }


def test_probe_ok_http_conn_shape(monkeypatch):
    captured = _install_fake(monkeypatch, tools=[object()])
    out = asyncio.run(mcp_service.probe_server(
        "streamable_http", None, None, None, "https://x/mcp", {"Authorization": "Bearer t"}
    ))
    assert out.ok is True and out.tools_count == 1
    # http/sse 连接注入 trust_env=False 的直连 factory（2026-09-20 Obsidian 排障：
    # 环境代理劫持 loopback 请求回 502；factory 经 conn dict 透传进 adapters 会话）；
    # 2026-10-15 真硬帽改造起探测覆写为读短档 _probe_http_client_factory
    assert captured["conns"] == {
        "probe": {
            "transport": "streamable_http",
            "url": "https://x/mcp",
            "headers": {"Authorization": "Bearer t"},
            "httpx_client_factory": mcp_service._probe_http_client_factory,
        }
    }


def test_direct_http_client_factory_no_env_proxy():
    """直连 factory 产出的 AsyncClient 不读环境代理（trust_env=False 锁定）。"""
    client = mcp_service._direct_http_client_factory()
    try:
        assert client.trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_probe_env_headers_omitted_when_empty(monkeypatch):
    """env/headers 为空不进连接描述（TypedDict 可选键；空 dict 也不带）。"""
    captured = _install_fake(monkeypatch, tools=[object()])
    asyncio.run(mcp_service.probe_server("stdio", "npx", [], {}, None, None))
    assert captured["conns"]["probe"] == {"transport": "stdio", "command": "npx", "args": []}


def test_probe_zero_tools(monkeypatch):
    _install_fake(monkeypatch, tools=[])
    out = asyncio.run(mcp_service.probe_server("stdio", "npx", None, None, None, None))
    assert out.ok is False and "未暴露任何工具" in out.error


def test_probe_timeout_message(monkeypatch):
    _install_fake(monkeypatch, raiser=asyncio.TimeoutError())
    out = asyncio.run(mcp_service.probe_server("stdio", "npx", None, None, None, None))
    assert out.ok is False and "超时" in out.error and "重试" in out.error


def test_probe_stdio_tier_timeout(monkeypatch):
    """stdio 长档分档生效（2026-09-18 双层超时改造）：挂起的服务在分档超时值后
    被判超时（monkeypatch 调小常量，验证未走默认 60s）。"""

    class HangClient:
        def __init__(self, *, connections=None):
            pass

        async def get_tools(self):
            await asyncio.sleep(5)
            return []

    monkeypatch.setattr(mcp_service, "_TEST_TIMEOUT_STDIO_S", 0.05)
    monkeypatch.setattr(mcp_service, "_client_factory", lambda: HangClient)
    t0 = time.monotonic()
    out = asyncio.run(mcp_service.probe_server("stdio", "npx", None, None, None, None))
    assert out.ok is False and "连接超时" in out.error and "重试" in out.error
    assert time.monotonic() - t0 < 1


def test_probe_hard_cap_forces_return(monkeypatch):
    """外层硬帽兜底：内层超时后的清理挂起（协程吞掉第一次取消继续 await）时，
    外层到点二次取消强制返回——探测永不无界挂起。"""

    class HangOnCleanupClient:
        def __init__(self, *, connections=None):
            pass

        async def get_tools(self):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                await asyncio.sleep(5)  # 吞第一次取消：模拟取消后清理挂起

    monkeypatch.setattr(mcp_service, "_TEST_TIMEOUT_HTTP_S", 0.05)
    monkeypatch.setattr(mcp_service, "_HARD_GRACE_S", 0.05)
    monkeypatch.setattr(mcp_service, "_client_factory", lambda: HangOnCleanupClient)
    t0 = time.monotonic()
    out = asyncio.run(
        mcp_service.probe_server("streamable_http", None, None, None, "https://x", None)
    )
    assert out.ok is False and "连接超时" in out.error
    assert time.monotonic() - t0 < 1


def test_probe_hard_cap_abandons_unresponsive_cleanup(monkeypatch):
    """真硬帽弃养：清理链吞掉所有取消（第二次 cancel 也不退出）时，外层在
    收尾宽限后弃养任务直接返回——探测不再等清理链真正结束（2026-10-15 滴答
    清单国内版排障：双层 wait_for 会一起挂到前端 80s 兜底）。替身必须有界
    （4×0.15s 后自然结束）：asyncio.run 退出时会 cancel+gather 所有残留任务，
    无限替身会把测试挂死在 loop 关闭阶段。"""

    class UnresponsiveClient:
        def __init__(self, *, connections=None):
            pass

        async def get_tools(self):
            for _ in range(4):  # 共 0.6s 自然结束，远大于断言窗口
                try:
                    await asyncio.sleep(0.15)
                except asyncio.CancelledError:
                    pass  # 吞掉所有取消：模拟不可取消的收尾链
            return []

    monkeypatch.setattr(mcp_service, "_TEST_TIMEOUT_HTTP_S", 0.05)
    monkeypatch.setattr(mcp_service, "_HARD_GRACE_S", 0.05)
    monkeypatch.setattr(mcp_service, "_CANCEL_WAIT_S", 0.1)
    monkeypatch.setattr(mcp_service, "_client_factory", lambda: UnresponsiveClient)
    t0 = time.monotonic()
    out = asyncio.run(
        mcp_service.probe_server("streamable_http", None, None, None, "https://x", None)
    )
    assert out.ok is False and "连接超时" in out.error
    assert time.monotonic() - t0 < 0.5  # ~0.2s 返回，不等 0.6s 自然结束


def test_probe_http_client_factory_short_read():
    """探测 factory：直连（trust_env=False）+ 读超时短档（弃养任务的收尾 DELETE
    不被 SDK 默认 read=300s 拖住）。"""
    client = mcp_service._probe_http_client_factory()
    try:
        assert client.trust_env is False
        assert client.timeout.read == mcp_service._PROBE_READ_S
    finally:
        asyncio.run(client.aclose())


def test_reap_orphan_consumes_and_tolerates_cancel():
    """弃养回调：异常收场的任务取出即消费（防 never retrieved 噪音）、被取消的
    任务容忍不抛（.exception() 对取消态任务会抛 CancelledError 的防御）。"""

    async def _boom():
        raise RuntimeError("orphan")

    async def _main():
        t1 = asyncio.create_task(_boom())
        await asyncio.sleep(0)  # 让 t1 以异常收场
        mcp_service._reap_orphan(t1)
        t2 = asyncio.create_task(asyncio.sleep(10))
        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass
        mcp_service._reap_orphan(t2)

    asyncio.run(_main())  # 两种形态都不抛即通过


def test_probe_file_not_found(monkeypatch):
    _install_fake(monkeypatch, raiser=FileNotFoundError("WinError 2"))
    out = asyncio.run(mcp_service.probe_server("stdio", "npx", None, None, None, None))
    assert out.ok is False and "无法启动服务进程" in out.error


def test_probe_httpx_connect_error(monkeypatch):
    _install_fake(monkeypatch, raiser=httpx.ConnectError("refused"))
    out = asyncio.run(mcp_service.probe_server("streamable_http", None, None, None, "https://x", None))
    assert out.ok is False and "无法连接服务" in out.error


def test_probe_generic_exception_no_leak(monkeypatch):
    """兜底文案只含类型名——不透传 str(e)（env/headers 可能含凭据片段）。"""
    _install_fake(monkeypatch, raiser=RuntimeError("secret-leak"))
    out = asyncio.run(mcp_service.probe_server("stdio", "npx", None, None, None, None))
    assert out.ok is False and "连接失败：RuntimeError" in out.error
    assert "secret-leak" not in out.error


def _status_error(code: int) -> httpx.HTTPStatusError:
    """构造带 request/response 的 HTTPStatusError（文案分类只读 status_code）。"""
    req = httpx.Request("POST", "http://x/mcp")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


def test_probe_status_error_unwrapped_from_group(monkeypatch):
    """anyio TaskGroup 把传输层异常包成 ExceptionGroup 冒出（2026-09-20 Obsidian
    排障实证：502 即以组形态到达，原 except httpx.HTTPError 永不命中）——解包取
    叶子按状态码给可操作文案。"""
    _install_fake(monkeypatch, raiser=ExceptionGroup("tg", [_status_error(401)]))
    out = asyncio.run(mcp_service.probe_server("streamable_http", None, None, None, "http://x", None))
    assert out.ok is False and "鉴权失败" in out.error and "401" in out.error
    assert "凭据" in out.error


def test_probe_status_error_5xx_hint(monkeypatch):
    """5xx → 服务端错误文案并提示可能被本机代理/网关拦截（Obsidian 502 场景）。"""
    _install_fake(monkeypatch, raiser=ExceptionGroup("tg", [_status_error(502)]))
    out = asyncio.run(mcp_service.probe_server("streamable_http", None, None, None, "http://x", None))
    assert out.ok is False and "服务端错误" in out.error and "502" in out.error
    assert "代理" in out.error


# —— 测试端点（API）——


def test_test_endpoint_persists_and_404(client, monkeypatch):
    sid = _create(_STDIO_BODY)
    _install_fake(monkeypatch, tools=[object()])
    r = client.post(f"/api/mcp/{sid}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["tools_count"] == 1
    out = client.get("/api/mcp").json()[0]  # last_test_* 四列落库回显
    assert out["last_test_ok"] is True and out["last_test_ms"] is not None and out["last_test_at"]
    assert out["last_test_tools"] == 1  # 徽标"连通 · Nms · N 个工具"
    assert client.post("/api/mcp/nope/test").status_code == 404


def test_test_endpoint_failure_also_200(client, monkeypatch):
    """探测失败也是 200——失败是数据；结果三列照落（离线徽标口径），tools 为空。"""
    sid = _create(_HTTP_BODY)
    _install_fake(monkeypatch, raiser=httpx.ConnectError("refused"))
    r = client.post(f"/api/mcp/{sid}/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    out = client.get("/api/mcp").json()[0]
    assert out["last_test_ok"] is False and out["last_test_tools"] is None


def test_disabled_row_testable(client, monkeypatch):
    """enabled 与测试正交：停用行仍可测试（锁定——停用仅"阶段二不装载"）。"""
    sid = _create({**_HTTP_BODY, "enabled": False})
    _install_fake(monkeypatch, tools=[object()])
    r = client.post(f"/api/mcp/{sid}/test")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_draft_test_endpoint(client, monkeypatch):
    captured = _install_fake(monkeypatch, tools=[object(), object()])
    r = client.post("/api/mcp/test", json=_HTTP_BODY)
    assert r.status_code == 200 and r.json()["tools_count"] == 2
    assert captured["conns"]["probe"]["url"] == "https://mcp.dida365.com"
    # 草稿探测不落库：列表仍空
    assert client.get("/api/mcp").json() == []


# —— get_connections（阶段二读入口）——


def test_get_connections_only_enabled_plus_local_folder(tmp_path):
    with TestingSessionLocal() as db:
        _create({**_STDIO_BODY, "name": "启用的"})
        _create({**_HTTP_BODY, "name": "停用的", "enabled": False})
        conns = mcp_service.get_connections(db)
        assert set(conns) == {"启用的"}  # 停用行不装载
        assert conns["启用的"]["command"] == "npx"

        # 本地文件夹启用 → 追加虚拟连接（进程内挂载，端点随服务端口走）
        row = db.get(IntegrationSettings, 1)
        row.local_folder_paths, row.local_folder_enabled = [str(tmp_path)], True
        db.commit()
        conns = mcp_service.get_connections(db)
        assert set(conns) == {"启用的", "local_folder"}
        assert conns["local_folder"] == {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:8000/mcp/local/mcp",
            # 本机回环端点同 _conn_dict 口径禁环境代理
            "httpx_client_factory": mcp_service._direct_http_client_factory,
        }
