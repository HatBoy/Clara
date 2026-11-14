"""WebDAV 客户端测试（services/sync_webdav.py；httpx.MockTransport 零真实外呼）。

覆盖：Basic 头 utf-8 编码（非 ASCII 密码——httpx 内置 BasicAuth 的 latin-1 坑）；
test_connection 四态（成功/401/403 只读/网络异常）；fetch_manifest 404→None 与
损坏包抛错；fetch_table 404→None 与超限拒绝；push_manifest 的 If-None-Match
412 竞态信号；PROPFIND 207 多命名空间解析（Nextcloud 绝对 href）。
"""

import json

import httpx
import pytest

from services.sync_webdav import WebDavClient, WebDavError, parse_propfind_hrefs


def _client(handler, **kw) -> WebDavClient:
    return WebDavClient("https://dav.example.com", kw.pop("username", "u"),
                        kw.pop("password", "p"), transport=httpx.MockTransport(handler))


# —— 认证头 ———————————————————————————————————————————————————————


def test_basic_auth_header_is_utf8():
    """非 ASCII 密码必须走 utf-8 Base64（httpx 内置 BasicAuth 按 latin-1 会炸）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(207)

    _client(handler, username="用户", password="密码123")._propfind("/", "0")
    import base64

    expect = "Basic " + base64.b64encode("用户:密码123".encode("utf-8")).decode()
    assert captured["auth"] == expect


def test_no_username_no_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(207)

    _client(handler, username=None, password=None)._propfind("/", "0")
    assert captured["auth"] is None


# —— test_connection ——————————————————————————————————————————————


def _propfind_body() -> bytes:
    return (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:href>/</d:href></d:response></d:multistatus>")


def test_connection_ok_with_mkdir():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            return httpx.Response(207, content=_propfind_body())
        return httpx.Response(201)  # MKCOL

    ok, ms, msg = _client(handler).test_connection()
    assert ok is True and ms >= 0 and "成功" in msg
    assert ("PROPFIND", "/") in seen
    assert ("MKCOL", "/clara-sync") in seen  # 可写性探测


def test_connection_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    ok, _, msg = _client(handler).test_connection()
    assert ok is False and "认证失败" in msg


def test_connection_readonly():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, content=_propfind_body())
        return httpx.Response(403)  # MKCOL 无写权限

    ok, _, msg = _client(handler).test_connection()
    assert ok is False and "写入权限" in msg


def test_connection_mkdcol_405_already_exists_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, content=_propfind_body())
        return httpx.Response(405)  # Alist：目录已存在

    ok, _, _msg = _client(handler).test_connection()
    assert ok is True


# —— manifest / 表文件 —————————————————————————————————————————————


def test_fetch_manifest_404_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _client(handler).fetch_manifest() is None


def test_fetch_manifest_corrupt_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    with pytest.raises(WebDavError, match="损坏"):
        _client(handler).fetch_manifest()


def test_fetch_table_404_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _client(handler).fetch_table("tasks") is None


def test_fetch_table_corrupt_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    with pytest.raises(WebDavError):
        _client(handler).fetch_table("tasks")


def test_fetch_table_oversize_rejected(monkeypatch):
    import services.sync_webdav as svc

    monkeypatch.setattr(svc, "MAX_TABLE_BYTES", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(WebDavError, match="超限"):
        _client(handler).fetch_table("tasks")


def test_push_table_and_manifest_roundtrip():
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        state[request.url.path] = request.read()
        return httpx.Response(201)

    c = _client(handler)
    c.push_table("tasks", {"table": "tasks", "rows": []})
    c.push_manifest({"app": "clara-sync"})
    assert json.loads(state["/clara-sync/tables/tasks.json"])["table"] == "tasks"
    assert json.loads(state["/clara-sync/manifest.json"])["app"] == "clara-sync"


def test_push_manifest_if_none_match_412_signal():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == "*"
        return httpx.Response(412)  # 竞态：对端已先写

    with pytest.raises(WebDavError, match="PRECONDITION_412"):
        _client(handler).push_manifest({"app": "clara-sync"}, if_none_match=True)


def test_push_manifest_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(WebDavError, match="manifest"):
        _client(handler).push_manifest({"app": "clara-sync"})


# —— PROPFIND 解析（多命名空间 / 绝对 href）——————————————————————————


def test_parse_propfind_hrefs_namespaces_and_absolute():
    xml = (
        b'<?xml version="1.0"?>'
        b'<D:multistatus xmlns:D="DAV:" xmlns:s="http://s.example/">'
        b"<D:response><D:href>https://dav.example.com/dav/clara-sync/</D:href></D:response>"
        b"<D:response><D:href>/dav/clara-sync/tables/tasks.json</D:href></D:response>"
        b"<s:custom><s:value>noise</s:value></s:custom>"
        b"</D:multistatus>"
    )
    hrefs = parse_propfind_hrefs(xml)
    assert "https://dav.example.com/dav/clara-sync/" in hrefs
    assert "/dav/clara-sync/tables/tasks.json" in hrefs
    assert len(hrefs) == 2  # s:value 不误报
