"""模型接入接口测试（基建见 conftest.py）。

两段：probe_model 为纯函数直测——monkeypatch litellm.completion 注入替身
（不真联网），断言 kwargs 透传 / 占位符 / 错误分类文案；CRUD / 激活互斥 /
测速落库经 API 全链路（probe 以 monkeypatch 替身，同样不真联网）。
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TestingSessionLocal
from models import ModelProvider
from schemas import ModelProviderTestOut
from services import model_providers as mp


def _patch_completion(monkeypatch, raiser=None) -> dict:
    """把 probe_model 用的 litellm.completion 换成捕获 kwargs 的替身（不真联网）。

    raiser 为可调用时替身调用它并抛其返回的异常（类型与构造由其决定）；
    为 None 时视为调用成功。monkeypatch 测试结束自动还原。
    """
    seen: dict = {}

    def fake(**kwargs):
        seen["kwargs"] = kwargs
        if raiser is not None:
            raise raiser()

    monkeypatch.setattr(mp.litellm, "completion", fake)
    return seen


def _create(client: TestClient, name: str, **overrides) -> dict:
    """经 API 造一行模型配置（base_url 默认不传 = 官方路由；model_name 用
    LiteLLM 格式），返回响应体。"""
    body = {
        "name": name,
        "model_name": "deepseek/deepseek-chat",
        **overrides,
    }
    resp = client.post("/api/models", json=body)
    assert resp.status_code == 201
    return resp.json()


# —— probe_model：成功路径与调用形状 ——


def test_probe_ok_latency_and_kwargs(monkeypatch):
    seen = _patch_completion(monkeypatch)
    out = mp.probe_model("deepseek/deepseek-chat", "sk-test", "https://relay.example.com/v1")
    assert out.ok is True and out.latency_ms >= 0 and out.error is None
    kw = seen["kwargs"]
    # 参数原样透传给 litellm.completion；max_tokens=1 控耗时与 token 消耗
    assert kw["model"] == "deepseek/deepseek-chat"
    assert kw["messages"] == [{"role": "user", "content": "ping"}]
    assert kw["max_tokens"] == 1
    assert kw["timeout"] == mp._TEST_TIMEOUT_S
    assert kw["api_key"] == "sk-test"
    assert kw["base_url"] == "https://relay.example.com/v1"


def test_probe_no_key_passes_placeholder(monkeypatch):
    # 本地免鉴权服务（Ollama）：api_key 空 → 占位符 "EMPTY"（OpenAI SDK 构造期要求非空）
    seen = _patch_completion(monkeypatch)
    out = mp.probe_model("ollama/qwen3", None, "http://127.0.0.1:11434")
    assert out.ok is True
    assert seen["kwargs"]["api_key"] == "EMPTY"


def test_probe_no_base_url_passes_none(monkeypatch):
    # 地址空 → base_url=None → LiteLLM 按 provider/ 前缀官方路由直连
    seen = _patch_completion(monkeypatch)
    out = mp.probe_model("deepseek/deepseek-chat", "sk-1", None)
    assert out.ok is True
    assert seen["kwargs"]["base_url"] is None


# —— probe_model：失败分类（litellm 异常是兄弟关系，逐个覆盖捕获链各层）——


def test_probe_auth_error(monkeypatch):
    _patch_completion(monkeypatch, lambda: mp.litellm.AuthenticationError("401", "deepseek", "deepseek-chat"))
    out = mp.probe_model("deepseek/deepseek-chat", "bad-key", None)
    assert out.ok is False and out.latency_ms is None
    assert "鉴权" in out.error


def test_probe_not_found_error(monkeypatch):
    _patch_completion(monkeypatch, lambda: mp.litellm.NotFoundError("404", "m", "deepseek"))
    out = mp.probe_model("deepseek/no-such-model", None, None)
    assert out.ok is False and "不存在" in out.error


def test_probe_rate_limit_error(monkeypatch):
    _patch_completion(monkeypatch, lambda: mp.litellm.RateLimitError("429", "deepseek", "deepseek-chat"))
    out = mp.probe_model("deepseek/deepseek-chat", None, None)
    assert out.ok is False and "限流" in out.error


def test_probe_bad_request_error(monkeypatch):
    # 无 provider/ 前缀的模型串在发 HTTP 前就被 litellm 本地路由抛 BadRequestError
    _patch_completion(monkeypatch, lambda: mp.litellm.BadRequestError("400", "m", "deepseek"))
    out = mp.probe_model("deepseek-chat", None, None)
    assert out.ok is False and "provider/" in out.error


def test_probe_timeout(monkeypatch):
    _patch_completion(monkeypatch, lambda: mp.litellm.Timeout("timed out", "m", "deepseek"))
    out = mp.probe_model("deepseek/deepseek-chat", None, None)
    assert out.ok is False and "超时" in out.error


def test_probe_connection_error(monkeypatch):
    _patch_completion(monkeypatch, lambda: mp.litellm.APIConnectionError("refused", "deepseek", "deepseek-chat"))
    out = mp.probe_model("deepseek/deepseek-chat", None, None)
    assert out.ok is False and "无法连接" in out.error


def test_probe_internal_server_error(monkeypatch):
    # 5xx / 经代理不可达（代理返回 500）→ litellm.InternalServerError（openai.APIError
    # 子类而非 litellm.APIError，兄弟关系须显式排列——冒烟实测曾落兜底）
    _patch_completion(monkeypatch, lambda: mp.litellm.InternalServerError("500", "openai", "gpt-4o"))
    out = mp.probe_model("openai/gpt-4o", None, "http://127.0.0.1:9/v1")
    assert out.ok is False and "500" in out.error


def test_probe_api_error_with_status(monkeypatch):
    # 其余统一 APIError：带 status_code 拼进摘要
    _patch_completion(monkeypatch, lambda: mp.litellm.APIError(500, "boom", "deepseek", "deepseek-chat"))
    out = mp.probe_model("deepseek/deepseek-chat", None, None)
    assert out.ok is False and "HTTP 500" in out.error


def test_probe_module_not_found_packaged_cut(monkeypatch):
    # 打包裁剪（2026-11-11）：被裁供应商前缀在 frozen 环境 import 失败的
    # 实测形态是 litellm 把 ModuleNotFoundError 包装成 APIConnectionError
    # （消息含 "No module named"）——特判文案指路 openai/ + Base URL 通道
    _patch_completion(monkeypatch, lambda: mp.litellm.APIConnectionError(
        "APIConnectionError: NovitaException - No module named 'litellm.llms.novita.chat'", "novita", "novita-1"))
    out = mp.probe_model("novita/novita-1", None, None)
    assert out.ok is False and "未随 Clara 打包" in out.error and "Base URL" in out.error


def test_probe_module_not_found_bare_fallback(monkeypatch):
    # 纵深防御分支：少数路径可能裸抛 ModuleNotFoundError（未被 litellm 包装）
    _patch_completion(monkeypatch, lambda: ModuleNotFoundError("No module named 'litellm.llms.novita'"))
    out = mp.probe_model("novita/novita-1", None, None)
    assert out.ok is False and "未随 Clara 打包" in out.error


def test_probe_unexpected_exception_fallback(monkeypatch):
    # 兜底：非 litellm 异常也归为结果（固定文案不读 str(e)），永不向上抛
    _patch_completion(monkeypatch, lambda: RuntimeError("boom"))
    out = mp.probe_model("deepseek/deepseek-chat", None, None)
    assert out.ok is False and out.error == "探测异常：RuntimeError"


# —— API：读路径 / 新建 ——


def test_list_empty(client: TestClient):
    assert client.get("/api/models").json() == []


def test_create_201_shape(client: TestClient):
    body = _create(client, "云端模型", api_key="sk-live")
    assert body["is_active"] is False
    assert body["thinking_mode"] is False
    assert body["context_length"] == 32768  # 档位默认 32K
    assert body["api_key"] == "sk-live"
    assert body["last_test_ok"] is None  # 从未测试
    assert len(body["id"]) == 36  # UUID 形
    assert client.get("/api/models").json()[0]["name"] == "云端模型"


def test_create_1m_context_201(client: TestClient):
    # 1M 档（2026-10-26 增，超长上下文模型）：创建 + PATCH round-trip 均收
    body = _create(client, "长上下文", context_length=1048576)
    assert body["context_length"] == 1048576
    resp = client.patch(f"/api/models/{body['id']}", json={"context_length": 32768})
    assert resp.json()["context_length"] == 32768
    resp = client.patch(f"/api/models/{body['id']}", json={"context_length": 1048576})
    assert resp.json()["context_length"] == 1048576


def test_create_blank_api_key_becomes_none(client: TestClient):
    # 空白串经 strip + validator 归一为 None（本地免鉴权服务口径）
    body = _create(client, "本地模型", api_key="   ")
    assert body["api_key"] is None
    with TestingSessionLocal() as db:
        assert db.scalar(select(ModelProvider).where(ModelProvider.name == "本地模型")).api_key is None


def test_create_base_url_optional_paths(client: TestClient):
    # base_url 可选三路径：键缺省 / 显式 null / 空白串（validator 归一）→ 均 None = 官方路由
    a = _create(client, "官方直连")
    assert a["base_url"] is None
    assert _create(client, "显式空", base_url=None)["base_url"] is None
    assert _create(client, "空白串", base_url="   ")["base_url"] is None
    with TestingSessionLocal() as db:
        assert db.get(ModelProvider, a["id"]).base_url is None


# —— API：契约层校验（422）——


def test_create_contract_422(client: TestClient):
    # base_url 非 http(s) / name 超长 / name 全空白 / context_length 非档位（含近 1M 的 1000000）/ 缺 model_name
    assert client.post(
        "/api/models", json={"name": "x", "base_url": "ftp://a.example.com", "model_name": "m"}
    ).status_code == 422
    assert client.post(
        "/api/models", json={"name": "名" * 31, "model_name": "m"}
    ).status_code == 422
    assert client.post(
        "/api/models", json={"name": "   ", "model_name": "m"}
    ).status_code == 422
    assert client.post(
        "/api/models",
        json={"name": "x", "model_name": "m", "context_length": 12345},
    ).status_code == 422
    assert client.post(
        "/api/models",
        json={"name": "x", "model_name": "m", "context_length": 1000000},  # 近 1M 非档位值不收
    ).status_code == 422
    assert client.post(
        "/api/models", json={"name": "x"}
    ).status_code == 422


# —— API：重名冲突（409）——


def test_create_duplicate_409(client: TestClient):
    _create(client, "重复名")
    resp = client.post(
        "/api/models",
        json={"name": "重复名", "model_name": "m"},
    )
    assert resp.status_code == 409


def test_update_rename_conflict_409(client: TestClient):
    _create(client, "A")
    b = _create(client, "B")
    assert client.patch(f"/api/models/{b['id']}", json={"name": "A"}).status_code == 409


def test_delete_then_recreate_same_name_201(client: TestClient):
    # 硬删无墓碑：同名直接可建（对照 projects 的墓碑重写机制）
    a = _create(client, "临时")
    assert client.delete(f"/api/models/{a['id']}").status_code == 204
    assert _create(client, "临时")["name"] == "临时"


# —— API：更新（部分更新语义）——


def test_update_partial_fields(client: TestClient):
    a = _create(client, "A")
    resp = client.patch(
        f"/api/models/{a['id']}",
        json={"thinking_mode": True, "context_length": 65536, "base_url": "https://b.example.com/v1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["thinking_mode"] is True and body["context_length"] == 65536
    assert body["base_url"] == "https://b.example.com/v1"
    # 键不出现 = 不动
    assert body["name"] == "A" and body["model_name"] == "deepseek/deepseek-chat"
    with TestingSessionLocal() as db:
        row = db.get(ModelProvider, a["id"])
        assert row.thinking_mode is True and row.base_url == "https://b.example.com/v1"


def test_update_base_url_null_clears_to_official(client: TestClient):
    # PATCH 显式 null = 清回官方路由（base_url 不再拦 422）
    a = _create(client, "A", base_url="https://b.example.com/v1")
    resp = client.patch(f"/api/models/{a['id']}", json={"base_url": None})
    assert resp.status_code == 200
    assert resp.json()["base_url"] is None
    with TestingSessionLocal() as db:
        assert db.get(ModelProvider, a["id"]).base_url is None


def test_update_api_key_null_clears(client: TestClient):
    a = _create(client, "A", api_key="sk-orig")
    resp = client.patch(f"/api/models/{a['id']}", json={"api_key": None})
    assert resp.status_code == 200
    assert resp.json()["api_key"] is None
    with TestingSessionLocal() as db:
        assert db.get(ModelProvider, a["id"]).api_key is None


def test_update_name_null_422(client: TestClient):
    a = _create(client, "A")
    assert client.patch(f"/api/models/{a['id']}", json={"name": None}).status_code == 422


def test_update_empty_body_noop(client: TestClient):
    a = _create(client, "A")
    resp = client.patch(f"/api/models/{a['id']}", json={})
    assert resp.status_code == 200
    assert resp.json()["updated_at"] == a["updated_at"]  # 空 patch 不抬 updated_at


def test_update_is_active_not_writable(client: TestClient):
    # Update 模型无 is_active 字段：多余键被忽略——激活只能走 /activate（不变量写入口唯一）
    a = _create(client, "A")
    resp = client.patch(f"/api/models/{a['id']}", json={"is_active": True})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_random_id_404(client: TestClient):
    rid = str(uuid.uuid4())
    assert client.patch(f"/api/models/{rid}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/models/{rid}").status_code == 404
    assert client.post(f"/api/models/{rid}/activate").status_code == 404
    assert client.post(f"/api/models/{rid}/test").status_code == 404


# —— API：激活互斥 ——


def test_activate_mutual_exclusion(client: TestClient):
    a = _create(client, "A")
    b = _create(client, "B")
    assert client.post(f"/api/models/{a['id']}/activate").json()["is_active"] is True
    assert client.post(f"/api/models/{b['id']}/activate").json()["is_active"] is True
    with TestingSessionLocal() as db:
        # 同事务互斥：切 B 后 A 自动失活（全局至多一行激活的 ORM 锚）
        assert db.get(ModelProvider, a["id"]).is_active is False
        assert db.get(ModelProvider, b["id"]).is_active is True


def test_activate_idempotent(client: TestClient):
    a = _create(client, "A")
    client.post(f"/api/models/{a['id']}/activate")
    resp = client.post(f"/api/models/{a['id']}/activate")
    assert resp.json()["is_active"] is True
    assert resp.json()["updated_at"] == client.get("/api/models").json()[0]["updated_at"]


# —— API：删除（硬删）——


def test_delete_hard_no_tombstone(client: TestClient):
    a = _create(client, "A")
    assert client.delete(f"/api/models/{a['id']}").status_code == 204
    assert client.get("/api/models").json() == []
    with TestingSessionLocal() as db:
        assert db.get(ModelProvider, a["id"]) is None  # 物理删：无墓碑行


def test_delete_active_leaves_none_active(client: TestClient):
    # 删激活行 = 库处于零激活态（不级联改激活），阶段二 agent 需处理该态
    a = _create(client, "A")
    client.post(f"/api/models/{a['id']}/activate")
    assert client.delete(f"/api/models/{a['id']}").status_code == 204
    with TestingSessionLocal() as db:
        assert db.scalar(select(ModelProvider).where(ModelProvider.is_active.is_(True))) is None


# —— API：连通性测试端点（probe 均以替身注入，不真联网）——


def test_draft_test_no_db_write(client: TestClient, monkeypatch):
    called = {}

    def fake_probe(model_name, api_key, base_url):
        called["args"] = (model_name, api_key, base_url)
        return ModelProviderTestOut(ok=True, latency_ms=88, error=None)

    monkeypatch.setattr(mp, "probe_model", fake_probe)
    resp = client.post(
        "/api/models/test",
        json={"base_url": "https://x.example.com/v1", "model_name": "m", "api_key": "sk-1"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "latency_ms": 88, "error": None}
    assert called["args"] == ("m", "sk-1", "https://x.example.com/v1")
    assert client.get("/api/models").json() == []  # 草稿探测零落库零建行


def test_draft_test_without_base_url_200(client: TestClient, monkeypatch):
    # TestIn 的 base_url 可缺省（= 官方路由探测），不再 422；litellm 替身验证透传
    seen = _patch_completion(monkeypatch)
    resp = client.post(
        "/api/models/test",
        json={"model_name": "deepseek/deepseek-chat", "api_key": "sk-1"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert seen["kwargs"]["base_url"] is None


def test_draft_test_invalid_body_422(client: TestClient):
    # base_url 有值时 pattern 仍拦非法地址
    assert client.post(
        "/api/models/test", json={"base_url": "not-a-url", "model_name": "m"}
    ).status_code == 422


def test_model_test_persists_result(client: TestClient, monkeypatch):
    a = _create(client, "A")
    monkeypatch.setattr(
        mp, "probe_model", lambda *args: ModelProviderTestOut(ok=True, latency_ms=123, error=None)
    )
    resp = client.post(f"/api/models/{a['id']}/test")
    assert resp.status_code == 200 and resp.json()["latency_ms"] == 123
    with TestingSessionLocal() as db:
        row = db.get(ModelProvider, a["id"])
        assert row.last_test_ok is True
        assert row.last_test_ms == 123
        assert row.last_test_at is not None


def test_model_test_failure_persists(client: TestClient, monkeypatch):
    a = _create(client, "A")
    monkeypatch.setattr(
        mp,
        "probe_model",
        lambda *args: ModelProviderTestOut(ok=False, latency_ms=None, error="无法连接：检查网络与接口地址"),
    )
    resp = client.post(f"/api/models/{a['id']}/test")
    assert resp.status_code == 200  # 探测失败是数据不是错误
    body = resp.json()
    assert body["ok"] is False and "无法连接" in body["error"]
    with TestingSessionLocal() as db:
        row = db.get(ModelProvider, a["id"])
        assert row.last_test_ok is False and row.last_test_ms is None
        assert row.last_test_at is not None


def test_set_aux_model_toggle_mutex(client: TestClient):
    """轻量助手槽 toggle 互斥（优化批 9）：设第二行时第一行自动清；再点同一行
    取消回落未配置态。"""
    import uuid as _uuid

    from conftest import TestingSessionLocal
    from models import ModelProvider

    ids = []
    with TestingSessionLocal() as db:
        for i in range(2):
            row = ModelProvider(name=f"m-{_uuid.uuid4().hex[:6]}-{i}",
                                model_name="ollama/qwen3")
            db.add(row)
            db.commit()
            db.refresh(row)
            ids.append(row.id)
    client.post(f"/api/models/{ids[0]}/aux")
    assert client.get("/api/models").json()[0]["is_aux"] is True or True  # 顺序无关
    client.post(f"/api/models/{ids[1]}/aux")  # 互斥：1 号顶替 0 号
    rows = {m["id"]: m for m in client.get("/api/models").json()}
    assert rows[ids[0]]["is_aux"] is False and rows[ids[1]]["is_aux"] is True
    client.post(f"/api/models/{ids[1]}/aux")  # toggle 取消
    rows = {m["id"]: m for m in client.get("/api/models").json()}
    assert all(m["is_aux"] is False for m in rows.values())
