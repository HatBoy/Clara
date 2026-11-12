"""数据接入接口测试（基建见 conftest.py）。

替身策略：monkeypatch services.integrations._composio_factory（SDK 唯一入口）——
factory 返回闭包内的 FakeComposio 类，其实例持有 toolkits / auth_configs /
connected_accounts 三个可编排子对象（返回值预置、异常注入、调用记录），全程不
真联网。错误分类用例顶部真 import composio_client 异常类与 composio.exceptions
的 ComposioMultipleConnectedAccountsError（收集期一次性付 SDK 冷启动）。

异常构造签名（读源码核实）：APIConnectionError(*, message=…, request) /
APITimeoutError(request)（APIConnectionError 的子类）——翻译链顺序靠用例锁定；
APIStatusError 系（AuthenticationError / NotFoundError）需 httpx.Response。
"""

import httpx
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from composio_client import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    NotFoundError,
)
from composio.exceptions import ComposioMultipleConnectedAccountsError

from conftest import TestingSessionLocal
from models import IngestSource, IntegrationSettings
from services import integrations as itg

_TEST_KEY = "cs_test_key"


# —— 替身基建 ——


class _FakeToolkits:
    """toolkits.get 双形态：query= 走搜索/探针（记录调用、可注入异常），位置参数
    slug 走元数据 enrich（detail_by_slug 预置、fail_slugs 注入失败）。"""

    def __init__(self):
        self.query_calls: list[dict] = []
        self.query_raiser = None  # 搜索/探针抛的异常实例
        self.search_items: list = []
        self.detail_slugs: list[str] = []
        self.detail_by_slug: dict = {}
        self.fail_slugs: set[str] = set()

    def get(self, slug=None, query=None):
        if query is not None:
            self.query_calls.append(query)
            if self.query_raiser is not None:
                raise self.query_raiser
            return list(self.search_items)
        self.detail_slugs.append(slug)
        if slug in self.fail_slugs:
            raise RuntimeError("meta fetch failed")
        return self.detail_by_slug.get(slug)


class _FakeAuthConfigs:
    def __init__(self):
        self.items: list = []
        self.list_calls: list = []
        self.created: list[tuple] = []
        self.create_id = "ac_created"

    def list(self, toolkit_slug=None):
        self.list_calls.append(toolkit_slug)
        return SimpleNamespace(items=list(self.items))

    def create(self, toolkit, options):
        self.created.append((toolkit, options))
        return SimpleNamespace(id=self.create_id)


class _FakeConnectedAccounts:
    def __init__(self):
        self.items: list = []
        self.list_calls: list[dict] = []
        self.link_calls: list[dict] = []
        self.link_raiser = None
        self.link_result = SimpleNamespace(id="ca_new", redirect_url="https://auth.example.com/oauth")
        self.by_id: dict = {}
        self.get_raiser = None
        self.deleted: list[str] = []
        self.delete_raiser = None

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return SimpleNamespace(items=list(self.items))

    def link(self, **kwargs):
        self.link_calls.append(kwargs)
        if self.link_raiser is not None:
            raise self.link_raiser
        return self.link_result

    def get(self, nanoid=None):
        if self.get_raiser is not None:
            raise self.get_raiser
        try:
            return self.by_id[nanoid]
        except KeyError:  # 贴真实 SDK 形态：查无 nanoid 抛 NotFoundError（非 KeyError）
            raise NotFoundError("nf", response=_resp(404), body=None) from None

    def delete(self, nanoid=None):
        self.deleted.append(nanoid)
        if self.delete_raiser is not None:
            raise self.delete_raiser


class _FakeTools:
    """tools.execute 替身（「测试」按钮路径）：记录调用、可编排结果与异常。"""

    def __init__(self):
        self.execute_calls: list[dict] = []
        self.result = SimpleNamespace(successful=True, data={"items": [{"id": "a"}, {"id": "b"}]})
        self.raiser = None

    def execute(self, tool_slug=None, arguments=None, connected_account_id=None):
        self.execute_calls.append({
            "tool_slug": tool_slug, "arguments": arguments,
            "connected_account_id": connected_account_id,
        })
        if self.raiser is not None:
            raise self.raiser
        return self.result


class _Parts:
    def __init__(self):
        self.init_kwargs: list[dict] = []
        self.toolkits = _FakeToolkits()
        self.auth_configs = _FakeAuthConfigs()
        self.connected_accounts = _FakeConnectedAccounts()
        self.tools = _FakeTools()


def _install_fake(monkeypatch) -> _Parts:
    """把 _composio_factory 换成返回 FakeComposio 类的工厂（记录构造 kwargs）。"""
    parts = _Parts()

    def factory():
        class _FakeComposio:
            def __init__(self, **kwargs):
                parts.init_kwargs.append(kwargs)
                self.toolkits = parts.toolkits
                self.auth_configs = parts.auth_configs
                self.connected_accounts = parts.connected_accounts
                self.tools = parts.tools

        return _FakeComposio

    monkeypatch.setattr(itg, "_composio_factory", factory)
    # 元数据进程内缓存换空 dict：隔离跨用例残留（某用例 enrich 过的 slug 不能
    # 让下一个用例的 fail_slugs 注入被缓存短路）
    monkeypatch.setattr(itg, "_TOOLKIT_META_CACHE", {})
    return parts


def _save_key(client: TestClient) -> None:
    """经 API 配好 Key（默认验证成功——替身不抛异常）。"""
    resp = client.put("/api/integrations/key", json={"api_key": _TEST_KEY})
    assert resp.status_code == 200 and resp.json()["key_ok"] is True


def _req() -> httpx.Request:
    return httpx.Request("GET", "https://backend.composio.dev/toolkits")


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, request=_req())


def _search_item(slug, *, no_auth=False, deprecated=False, type_="native"):
    return SimpleNamespace(
        slug=slug,
        name=slug.replace("_", " ").title(),
        no_auth=no_auth,
        deprecated=deprecated,
        type=type_,
        meta=SimpleNamespace(
            description=f"{slug} description",
            logo=f"https://logo/{slug}.png",
            tools_count=5.0,
        ),
    )


def _ca_item(ca_id="ca_1", slug="notion", status="ACTIVE", created_at="2026-09-18T02:00:00Z"):
    return SimpleNamespace(
        id=ca_id,
        status=status,
        toolkit=SimpleNamespace(slug=slug),
        created_at=created_at,
        updated_at=created_at,
    )


def _tk_detail(name, logo="https://logo/x.png"):
    return SimpleNamespace(
        name=name,
        meta=SimpleNamespace(logo=logo, description=f"{name} description"),
    )


def _local_iso(iso_with_tz: str) -> str:
    """云端 ISO（带时区）→ 本地 naive ISO 串——列表响应时区归一的期望值（跟随
    测试机时区，断言确定性）。"""
    return (
        datetime.fromisoformat(iso_with_tz.replace("Z", "+00:00"))
        .astimezone()
        .replace(tzinfo=None)
        .isoformat()
    )


# —— Key 配置：读写 / 自动验证 / 失败也是 200 ——


def test_key_get_creates_row(client: TestClient):
    resp = client.get("/api/integrations/key")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"api_key": None, "key_ok": None, "key_error": None, "key_checked_at": None}
    with TestingSessionLocal() as db:  # 空库 GET 已 get_or_create 落行
        assert db.get(IntegrationSettings, 1) is not None


def test_key_put_verifies_and_builds_client(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    resp = client.put("/api/integrations/key", json={"api_key": _TEST_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"] == _TEST_KEY and body["key_ok"] is True
    assert body["key_error"] is None and body["key_checked_at"] is not None
    # 客户端构造参数透传（timeout / max_retries 抑制久挂与长重试）
    assert parts.init_kwargs == [
        {"api_key": _TEST_KEY, "timeout": itg._TIMEOUT_S, "max_retries": itg._MAX_RETRIES}
    ]
    # 验证探针 = 最轻的已鉴权 GET（拉一条 toolkit）
    assert parts.toolkits.query_calls == [{"limit": 1}]


def test_key_put_invalid_key_is_data_not_error(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    parts.toolkits.query_raiser = AuthenticationError("401", response=_resp(401), body=None)
    resp = client.put("/api/integrations/key", json={"api_key": "cs_bad"})
    # 验证失败也是 200：失败是数据（key_ok=False + 摘要），同模型探测口径
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"] == "cs_bad" and body["key_ok"] is False
    assert "无效" in body["key_error"]


def test_key_put_network_error_summary(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    parts.toolkits.query_raiser = APIConnectionError(request=_req())
    resp = client.put("/api/integrations/key", json={"api_key": _TEST_KEY})
    assert resp.status_code == 200
    assert resp.json()["key_ok"] is False
    assert "网络" in resp.json()["key_error"]


def test_key_put_blank_clears_and_resets(monkeypatch, client: TestClient):
    _install_fake(monkeypatch)
    _save_key(client)
    resp = client.put("/api/integrations/key", json={"api_key": ""})  # 空串归一 None = 清除
    assert resp.status_code == 200
    assert resp.json() == {"api_key": None, "key_ok": None, "key_error": None, "key_checked_at": None}


def test_key_verify_rechecks_stored_key(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)  # 先验证成功
    parts.toolkits.query_raiser = AuthenticationError("401", response=_resp(401), body=None)
    resp = client.post("/api/integrations/key/verify")  # 手动重验 → 翻转为失败
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"] == _TEST_KEY and body["key_ok"] is False
    assert "无效" in body["key_error"]


def test_key_put_too_long_422(client: TestClient):
    resp = client.put("/api/integrations/key", json={"api_key": "x" * 201})
    assert resp.status_code == 422


def test_translate_timeout_beats_connection(monkeypatch):
    # APITimeoutError 是 APIConnectionError 的子类：翻译链若顺序颠倒，超时会落进
    # "网络"分支——此用例锁定顺序（与 litellm 的兄弟关系相反）
    _install_fake(monkeypatch)
    msg = str(itg._translate_upstream(APITimeoutError(request=_req())))
    assert "超时" in msg and "网络" not in msg


def test_verify_api_key_never_raises(monkeypatch):
    _install_fake(monkeypatch)
    ok, error = itg.verify_api_key("cs_any")  # 纯函数全捕获：不抛是契约
    assert ok is True and error is None


# —— 应用搜索 ——


def test_apps_search_requires_key(client: TestClient):
    resp = client.get("/api/integrations/apps", params={"search": "no"})
    assert resp.status_code == 409


def test_apps_search_query_and_field_mapping(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.toolkits.search_items = [_search_item("notion"), _search_item("github")]
    resp = client.get("/api/integrations/apps", params={"search": "no"})
    assert resp.status_code == 200
    assert parts.toolkits.query_calls[-1] == {"search": "no", "limit": itg._SEARCH_LIMIT}
    body = resp.json()
    assert [a["slug"] for a in body] == ["notion", "github"]
    assert body[0]["name"] == "Notion"
    assert body[0]["description"] == "notion description"
    assert body[0]["logo"] == "https://logo/notion.png"
    assert body[0]["tools_count"] == 5  # 云端 float → int


def test_apps_search_returns_all_without_filtering(monkeypatch, client: TestClient):
    # 云端命中原样返回（2026-09-18 修复：曾按 no_auth/deprecated/type 过滤，与云端
    # 真实数据形态不符致全量命中被杀为 0）
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.toolkits.search_items = [
        _search_item("normal"),
        _search_item("noauth", no_auth=True),
        _search_item("legacy", deprecated=True),
        _search_item("project_x", type_="custom"),
    ]
    resp = client.get("/api/integrations/apps", params={"search": "x"})
    assert resp.status_code == 200
    assert [a["slug"] for a in resp.json()] == ["normal", "noauth", "legacy", "project_x"]


def test_apps_search_param_validation(client: TestClient):
    assert client.get("/api/integrations/apps").status_code == 422  # 缺参
    assert client.get("/api/integrations/apps", params={"search": ""}).status_code == 422  # 空串
    assert client.get("/api/integrations/apps", params={"search": "x" * 61}).status_code == 422  # 超长


def test_apps_search_upstream_error_502(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.toolkits.query_raiser = RuntimeError("boom")
    resp = client.get("/api/integrations/apps", params={"search": "no"})
    assert resp.status_code == 502
    assert "Composio" in resp.json()["detail"]


# —— 发起连接 / 状态轮询 ——


def test_connect_requires_key(client: TestClient):
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": "notion"})
    assert resp.status_code == 409


def test_connect_conflict_on_active_slug(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.items = [_ca_item(slug="notion")]
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": "notion"})
    assert resp.status_code == 409
    assert parts.connected_accounts.link_calls == []  # 前置查重命中即拦，不走 link


def test_connect_creates_managed_auth_config(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)  # auth_configs.items 为空 → 创建 composio-managed 配置
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": "notion"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["connected_account_id"] == "ca_new"
    assert body["redirect_url"] == "https://auth.example.com/oauth"
    slug, options = parts.auth_configs.created[0]
    assert slug == "notion" and options["type"] == "use_composio_managed_auth"
    assert parts.connected_accounts.link_calls == [
        {"user_id": itg.COMPOSIO_USER_ID, "auth_config_id": "ac_created"}
    ]


def test_connect_reuses_latest_auth_config(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.auth_configs.items = [
        SimpleNamespace(id="ac_old", created_at="2026-09-10T00:00:00Z"),
        SimpleNamespace(id="ac_new", created_at="2026-09-15T00:00:00Z"),
    ]
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": "notion"})
    assert resp.status_code == 201
    assert parts.auth_configs.created == []  # 已有配置不新建，按 created_at 取最新
    assert parts.connected_accounts.link_calls[0]["auth_config_id"] == "ac_new"


def test_connect_multiple_connected_accounts_409(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.link_raiser = ComposioMultipleConnectedAccountsError("dup")
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": "notion"})
    # SDK 兜底拦截（allow_multiple=False）翻译为业务冲突
    assert resp.status_code == 409


def test_connect_blank_slug_422(client: TestClient):
    resp = client.post("/api/integrations/connect", json={"toolkit_slug": ""})
    assert resp.status_code == 422


def test_connect_status_passthrough(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = SimpleNamespace(
        id="ca_1", status="ACTIVE", toolkit=SimpleNamespace(slug="notion")
    )
    resp = client.get("/api/integrations/connect/ca_1")
    assert resp.status_code == 200
    assert resp.json() == {"connected_account_id": "ca_1", "status": "ACTIVE", "toolkit_slug": "notion"}


def test_connect_status_not_found_404(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.get_raiser = NotFoundError("nf", response=_resp(404), body=None)
    resp = client.get("/api/integrations/connect/ca_missing")
    assert resp.status_code == 404


# —— 已连接列表 / 断开 ——


def test_connections_enrich_once_per_slug(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.items = [
        _ca_item("ca_1", slug="notion", created_at="2026-09-18T02:00:00Z"),
        _ca_item("ca_2", slug="notion", created_at="2026-09-18T03:00:00Z"),
        _ca_item("ca_3", slug="github", created_at="2026-09-18T04:00:00Z"),
    ]
    parts.toolkits.detail_by_slug = {
        "notion": _tk_detail("Notion"),
        "github": _tk_detail("GitHub", logo="https://logo/gh.png"),
    }
    resp = client.get("/api/integrations/connections")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body] == ["ca_1", "ca_2", "ca_3"]
    # 同 slug 只 enrich 一次（连接数个位数，N 次调用可忽略）；enrich 并发拉取，
    # 替身记录的发往顺序不定，按集合比
    assert set(parts.toolkits.detail_slugs) == {"notion", "github"}
    notion = body[0]
    assert notion["toolkit_slug"] == "notion" and notion["name"] == "Notion"
    assert notion["logo"] == "https://logo/x.png" and notion["description"] == "Notion description"
    assert body[2]["logo"] == "https://logo/gh.png"
    # 云端 ISO（带时区）→ 本地 naive（跟随测试机时区的确定性期望）
    assert notion["created_at"] == _local_iso("2026-09-18T02:00:00Z")
    assert notion["updated_at"] == _local_iso("2026-09-18T02:00:00Z")


def test_connections_enrich_failure_tolerates_null(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.items = [_ca_item("ca_1", slug="notion")]
    parts.toolkits.fail_slugs = {"notion"}  # 元数据拉取失败：容忍 null，不毁列表
    resp = client.get("/api/integrations/connections")
    assert resp.status_code == 200
    c = resp.json()[0]
    assert c["toolkit_slug"] == "notion"
    assert c["name"] is None and c["logo"] is None and c["description"] is None


def test_connections_meta_cache_second_call_zero_fetch(monkeypatch, client: TestClient):
    """元数据进程内缓存：二次拉取同一连接清单，toolkits.get 元数据零新增调用
    （缓存命中零云端往返——Composio 卡恒比 SQLite 源晚出的排障修复点）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.items = [_ca_item("ca_1", slug="notion")]
    parts.toolkits.detail_by_slug = {"notion": _tk_detail("Notion")}
    assert client.get("/api/integrations/connections").json()[0]["name"] == "Notion"
    assert parts.toolkits.detail_slugs == ["notion"]
    assert client.get("/api/integrations/connections").json()[0]["name"] == "Notion"
    assert parts.toolkits.detail_slugs == ["notion"]  # 二次零新增


def test_list_connections_no_enrich_skips_detail(monkeypatch):
    """service 层 enrich=False（源枚举形态）：跳过每 slug 的 toolkits.get 往返，
    name/logo/description 落 None——2026-10-15 数据接入页打开慢排障（省
    N×(10s 超时×2 重试) 云端调用；API 端点不暴露该形态，直调验证）。"""
    parts = _install_fake(monkeypatch)
    with TestingSessionLocal() as db:
        db.add(IntegrationSettings(id=1, composio_api_key=_TEST_KEY))
        db.commit()
        parts.connected_accounts.items = [_ca_item("ca_1", slug="notion")]
        conns = itg.list_connections(db, enrich=False)
    assert parts.toolkits.detail_slugs == []  # 零元数据往返
    assert len(conns) == 1
    assert conns[0].id == "ca_1" and conns[0].toolkit_slug == "notion"
    assert conns[0].name is None and conns[0].logo is None


def test_connections_requires_key(client: TestClient):
    # 2026-11-04 真机日志批：未配 Key 的已连接列表恒空（GET 被动拉取不弹 409
    # 冲突——POST 类主动作才引导配置）；覆盖用例在 test_list_connections_without_key
    resp = client.get("/api/integrations/connections")
    assert resp.status_code == 200 and resp.json() == []


def test_disconnect_204(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    resp = client.delete("/api/integrations/connections/ca_1")
    assert resp.status_code == 204
    assert parts.connected_accounts.deleted == ["ca_1"]  # 经 nanoid 关键字定位删除


def test_disconnect_not_found_404(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.delete_raiser = NotFoundError("nf", response=_resp(404), body=None)
    resp = client.delete("/api/integrations/connections/ca_missing")
    assert resp.status_code == 404


# —— 连接级：本地采集开关（enabled 镜像）/ 测试 / 断开清理（2026-10-16 增）——


def test_connections_enabled_from_ingest_source(monkeypatch, client: TestClient):
    """列表带本地采集开关：ingest_sources 行 enabled=False → False；无行 → True
    （行缺省视为启用，同 _source_enabled 只做减法语义）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="ca_1", enabled=False))
        db.commit()
    parts.connected_accounts.items = [
        _ca_item("ca_1", slug="notion"),
        _ca_item("ca_2", slug="github"),
    ]
    body = client.get("/api/integrations/connections").json()
    assert [(c["id"], c["enabled"]) for c in body] == [("ca_1", False), ("ca_2", True)]


def test_set_connection_enabled_mirrors_ingest_source(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="notion")
    resp = client.put("/api/integrations/connections/ca_1/enabled", json={"enabled": False})
    assert resp.status_code == 204
    from services import data_ingest

    with TestingSessionLocal() as db:
        row = data_ingest._source_row(db, "ca_1")  # mirror_source_enabled get_or_create 落行
        assert row is not None and row.enabled is False
    assert client.put("/api/integrations/connections/ca_missing/enabled",
                      json={"enabled": True}).status_code == 404


def test_test_connection_mapped_executes_and_counts(monkeypatch, client: TestClient):
    """内置映射命中（gmail）：与采集同源 execute 真调 + _extract_items 数条目
    （不过时间窗——测试报原始返回数）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="gmail")
    resp = client.post("/api/integrations/connections/ca_1/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["items_count"] == 2 and body["error"] is None
    assert parts.tools.execute_calls == [{
        "tool_slug": "GMAIL_FETCH_EMAILS",
        "arguments": {"max_results": 20},
        "connected_account_id": "ca_1",
    }]


def test_test_connection_row_config_overrides_mapping(monkeypatch, client: TestClient):
    """行配置 fetch_tool 三级优先首位：测试与采集跑同一个动作（行覆盖生效）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="gmail")
    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="ca_1", fetch_tool="GMAIL_SEARCH_EMAIL"))
        db.commit()
    assert client.post("/api/integrations/connections/ca_1/test").json()["ok"] is True
    assert parts.tools.execute_calls[0]["tool_slug"] == "GMAIL_SEARCH_EMAIL"


def test_test_connection_unmapped_notes_status_only(monkeypatch, client: TestClient):
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="notion")
    body = client.post("/api/integrations/connections/ca_1/test").json()
    assert body["ok"] is True and body["items_count"] is None and body["note"]
    assert parts.tools.execute_calls == []  # 未映射：零云端执行，仅验状态


def test_test_connection_failure_flows(monkeypatch, client: TestClient):
    """非 ACTIVE / execute 失败：失败也是 200——失败是数据（前端徽标展示）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="gmail", status="EXPIRED")
    body = client.post("/api/integrations/connections/ca_1/test").json()
    assert body["ok"] is False and "EXPIRED" in body["error"]
    parts.connected_accounts.by_id["ca_2"] = _ca_item("ca_2", slug="gmail")
    parts.tools.result = SimpleNamespace(successful=False, error="quota exceeded")
    body = client.post("/api/integrations/connections/ca_2/test").json()
    assert body["ok"] is False and "GMAIL_FETCH_EMAILS" in body["error"]
    parts.connected_accounts.get_raiser = NotFoundError("nf", response=_resp(404), body=None)
    assert client.post("/api/integrations/connections/ca_missing/test").status_code == 404


def test_disconnect_cleans_ingest_source_row(monkeypatch, client: TestClient):
    """断开连带清理本地启停镜像行（曾停用的行不再躺在 sources 列表）。"""
    from sqlalchemy import select

    parts = _install_fake(monkeypatch)
    _save_key(client)
    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="ca_1", enabled=False))
        db.commit()
    assert client.delete("/api/integrations/connections/ca_1").status_code == 204
    with TestingSessionLocal() as db:
        assert db.scalar(select(IngestSource).where(IngestSource.source_key == "ca_1")) is None


def test_user_actions_reset_ingest_cooldown(monkeypatch, client: TestClient):
    """用户介入即重置（2026-10-16）：测试通过 / 重新启用 → 该连接的连败冷却清零
    （修好凭据的好源不被旧证据拦 2h～24h，对齐 MCP/CLI 重置口径）。"""
    parts = _install_fake(monkeypatch)
    _save_key(client)
    parts.connected_accounts.by_id["ca_1"] = _ca_item("ca_1", slug="gmail")
    from services import data_ingest

    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "ca_1", False, "云端执行失败")  # 预置连败
    assert client.post("/api/integrations/connections/ca_1/test").json()["ok"] is True
    with TestingSessionLocal() as db:
        row = data_ingest._source_row(db, "ca_1")
        assert row.fail_streak == 0 and row.last_error is None

    # 停用→再启用同样重置（启用 = 用户要求立即再试）
    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "ca_1", False, "再次失败")
    assert client.put("/api/integrations/connections/ca_1/enabled",
                      json={"enabled": True}).status_code == 204
    with TestingSessionLocal() as db:
        assert data_ingest._source_row(db, "ca_1").fail_streak == 0


def test_list_connections_without_key_returns_empty(client: TestClient):
    """未配 Composio Key：GET /connections 返回空列表而非 409（2026-11-04 真机
    日志批）——GET 是设置页挂载的被动拉取，冲突语义留给 POST 类主动作；旧版
    409 每次打开数据接入页必刷一行错误日志（前端虽静默归空）。"""
    assert client.get("/api/integrations/connections").status_code == 200
    assert client.get("/api/integrations/connections").json() == []
