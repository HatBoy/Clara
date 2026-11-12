"""数据接入业务逻辑层（2026-09-18 增）：Composio 云端应用的接入闭环。

Key 管理 / 应用搜索 / OAuth 连接发起（link）/ 状态轮询 / 已连接列表 / 断开，
全部经 Composio SDK 调用云端（前端不直接请求 Composio——密钥不进浏览器网络面板、
口径单一，同 model_providers 的探测先例）。

设计要点：
- composio 延迟 import（模块级 import 实测冷启动 ~1.1s，只有设置页用到，不该让
  每次应用启动都付这笔钱——区别于 litellm 模块级先例：模型探测是核心路径）。
  _composio_factory 是唯一 SDK 入口，测试 monkeypatch 此单点即可替换整个 SDK。
- 每请求新建 Composio 实例：构造只建 HttpClient 配置不发请求，轻量。
- 已连接列表真源 = Composio 云端实时拉取（本地不镜像表）；connected_accounts
  内嵌 toolkit 只有 slug，name/logo/description 由本层按唯一 slug 调 toolkits.get
  补全（enrich），单应用元数据拉不到容忍为 None 不中断整个列表。
- ⚠️ 发起连接必须用 connected_accounts.link()：initiate()/toolkits.authorize()
  走的 legacy 端点已过退休日期（2026-07-03 全量），对 Composio-managed OAuth
  会抛 ComposioLegacyConnectedAccountsEndpointRetiredError。
- 异常捕获链注意：composio_client 的 APITimeoutError 是 APIConnectionError 的
  子类（父子嵌套，与 litellm 的兄弟关系相反），必须先捕超时再捕连接。
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import IngestSource, IntegrationSettings
from schemas import (
    ComposioAppOut,
    ComposioConnectIn,
    ComposioConnectOut,
    ComposioConnectStatusOut,
    ComposioConnectionOut,
    ComposioKeyIn,
    ComposioKeyOut,
)

logger = logging.getLogger(__name__)

# 单用户桌面应用的 Composio 外部用户标识（Composio 按 user_id 隔离连接）
COMPOSIO_USER_ID = "clara"

# SDK HTTP 超时（秒）与重试次数（连接类操作一次重试足够，避免久挂线程池）
_TIMEOUT_S = 10.0
_MAX_RETRIES = 1

# 应用搜索一次返回上限（1500+ 应用不分页，前端两列网格截断展示即可）
_SEARCH_LIMIT = 20


class ConflictError(Exception):
    """业务冲突（该应用已有 ACTIVE 连接），router 翻译为 409。"""


class KeyNotConfiguredError(Exception):
    """尚未配置 Composio API Key，router 翻译为 409。"""


class UpstreamError(Exception):
    """Composio 云端调用失败（附中文摘要），router 翻译为 502。"""


def _composio_factory():
    """SDK 类入口（延迟 import，见模块 docstring）；测试 monkeypatch 此符号。"""
    from composio import Composio

    return Composio


def _build_client(api_key: str):
    """按给定 Key 新建 Composio 客户端（构造轻量不发请求）。"""
    return _composio_factory()(api_key=api_key, timeout=_TIMEOUT_S, max_retries=_MAX_RETRIES)


def _translate_upstream(e: Exception) -> UpstreamError:
    """Composio SDK 异常 → 带中文摘要的 UpstreamError。

    延迟 import 异常类（生产路径必先经过 _build_client 的真 import，此处已缓存）。
    固定文案不读 str(e)——上游报错可能回显 Key 片段。APITimeoutError 是
    APIConnectionError 的子类，顺序必须超时在前。
    """
    from composio_client import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
    )

    if isinstance(e, APITimeoutError):
        return UpstreamError("Composio 云端响应超时，请稍后重试")
    if isinstance(e, APIConnectionError):
        return UpstreamError("无法连接 Composio 云端：检查网络")
    if isinstance(e, AuthenticationError):
        return UpstreamError("API Key 无效或已失效，请重新配置")
    if isinstance(e, APIStatusError):
        return UpstreamError(f"Composio 云端返回 HTTP {getattr(e, 'status_code', '')}".rstrip())
    return UpstreamError(f"Composio 调用异常：{type(e).__name__}")


def _parse_iso(value: str | None) -> datetime | None:
    """Composio 云端 ISO 时间串 → datetime（保留时区信息，naive 归一在契约层）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _require_client(db: Session):
    """读库取 Key 建 SDK 客户端；未配置则抛 KeyNotConfiguredError。"""
    row = get_key_settings(db)
    if not row.composio_api_key:
        raise KeyNotConfiguredError("尚未配置 Composio API Key，请先在上方配置")
    return _build_client(row.composio_api_key)


def get_key_settings(db: Session) -> IntegrationSettings:
    """数据接入配置单行（id 恒 1，get_or_create——同 settings.get_settings 先例）。"""
    row = db.get(IntegrationSettings, 1)
    if row is None:
        row = IntegrationSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _key_out(row: IntegrationSettings) -> ComposioKeyOut:
    """ORM 行 → 契约。列名带 composio_ 前缀、契约字段用短名——名字不一致，
    from_attributes 按字段名取属性取不到（会静默落 None），必须显式映射。"""
    return ComposioKeyOut(
        api_key=row.composio_api_key,
        key_ok=row.composio_key_ok,
        key_error=row.composio_key_error,
        key_checked_at=row.composio_key_checked_at,
    )


def get_key_info(db: Session) -> ComposioKeyOut:
    """Key 配置读（GET /key）。"""
    return _key_out(get_key_settings(db))


def verify_api_key(api_key: str) -> tuple[bool, str | None]:
    """验证 Key 有效性（纯函数无 db，永不抛出——验证失败是结果不是异常，同 probe_model）。

    调最轻的已鉴权 GET（toolkits 拉一条）作探针：不抛异常即有效；失败走
    _translate_upstream 同款分类取中文摘要。
    """
    try:
        _build_client(api_key).toolkits.get(query={"limit": 1})
    except Exception as e:  # 兜底：验证永不向上抛
        return False, str(_translate_upstream(e))
    return True, None


def _apply_verification(db: Session, row: IntegrationSettings) -> ComposioKeyOut:
    """对行内当前 Key 跑一次验证并把 ok/error/checked_at 三列落库。"""
    if row.composio_api_key:
        ok, error = verify_api_key(row.composio_api_key)
    else:  # Key 已清除：验证状态整体复位
        ok, error = None, None
    row.composio_key_ok = ok
    row.composio_key_error = error
    row.composio_key_checked_at = datetime.now() if ok is not None else None
    db.commit()
    db.refresh(row)
    logger.info("Composio Key 验证：ok=%s error=%s", ok, error)
    return _key_out(row)


def save_api_key(db: Session, body: ComposioKeyIn) -> ComposioKeyOut:
    """保存（或清除）Key 并立即验证一次（失败是数据不是异常——响应照常 200）。"""
    row = get_key_settings(db)
    row.composio_api_key = body.api_key
    db.commit()
    logger.info("保存 Composio Key：%s", "已清除" if body.api_key is None else "已更新")
    return _apply_verification(db, row)


def reverify_key(db: Session) -> ComposioKeyOut:
    """对已存 Key 手动重验（POST /key/verify；未配置时状态复位）。"""
    return _apply_verification(db, get_key_settings(db))


def search_apps(db: Session, keyword: str) -> list[ComposioAppOut]:
    """搜索 Composio 应用市场（toolkits 高层 list() 未暴露 search 参数，走 get(query=...)）。

    云端命中原样返回不做过滤（2026-09-18 实测：曾按 no_auth/deprecated/type 过滤
    与云端真实数据形态不符，全量命中被误杀为 0；能否连接由 link() 真实流程判定，
    搜索层不预判）。
    """
    client = _require_client(db)
    try:
        items = client.toolkits.get(query={"search": keyword, "limit": _SEARCH_LIMIT})
    except Exception as e:
        raise _translate_upstream(e) from e
    apps = [
        ComposioAppOut(
            slug=item.slug,
            name=item.name,
            description=item.meta.description,
            logo=item.meta.logo,
            tools_count=int(item.meta.tools_count),
        )
        for item in items
    ]
    logger.info("搜索 Composio 应用 %r：命中 %d", keyword, len(apps))
    return apps


def _ensure_auth_config_id(client, toolkit_slug: str) -> str:
    """取工具的 auth config id（link 的前置参数）。

    仿写 SDK 私有 toolkits._get_auth_config_id：优先复用已有的 composio-managed
    配置（按 created_at 降序取最新），没有则创建一个托管配置。
    """
    configs = client.auth_configs.list(toolkit_slug=toolkit_slug)
    if configs.items:
        latest = sorted(configs.items, key=lambda c: str(c.created_at), reverse=True)[0]
        return latest.id
    return client.auth_configs.create(
        toolkit_slug,
        {
            "type": "use_composio_managed_auth",
            "tool_access_config": {"tools_for_connected_account_creation": []},
        },
    ).id


def start_connect(db: Session, body: ComposioConnectIn) -> ComposioConnectOut:
    """发起 OAuth 连接：返回重定向 URL（系统浏览器完成授权）与轮询锚 ca_id。

    产品语义"一个应用一个连接"按 slug 前置查重（比 SDK 按 auth_config 判更贴切：
    同一 toolkit 的多个 composio-managed auth config 会被视为可连）；SDK 的
    ComposioMultipleConnectedAccountsError 兜底再拦一次（双保险）。
    """
    from composio.exceptions import ComposioMultipleConnectedAccountsError

    client = _require_client(db)
    slug = body.toolkit_slug
    try:
        active = client.connected_accounts.list(
            user_ids=[COMPOSIO_USER_ID], statuses=["ACTIVE"]
        )
        if any(item.toolkit.slug == slug for item in active.items):
            raise ConflictError("该应用已连接，请先断开后再连接")
        auth_config_id = _ensure_auth_config_id(client, slug)
        request = client.connected_accounts.link(
            user_id=COMPOSIO_USER_ID, auth_config_id=auth_config_id
        )
    except (ConflictError, UpstreamError):
        raise
    except ComposioMultipleConnectedAccountsError as e:
        raise ConflictError("该应用已连接，请先断开后再连接") from e
    except Exception as e:
        raise _translate_upstream(e) from e
    logger.info("发起 Composio 连接 %s → %s", slug, request.id)
    return ComposioConnectOut(connected_account_id=request.id, redirect_url=request.redirect_url)


def get_connect_status(db: Session, connected_account_id: str) -> ComposioConnectStatusOut | None:
    """查连接状态（前端轮询入口；不用 SDK 阻塞式的 wait_for_connection）。"""
    from composio_client import NotFoundError

    client = _require_client(db)
    try:
        ca = client.connected_accounts.get(nanoid=connected_account_id)
    except NotFoundError:
        return None
    except Exception as e:
        raise _translate_upstream(e) from e
    return ComposioConnectStatusOut(
        connected_account_id=ca.id,
        status=str(ca.status),
        toolkit_slug=ca.toolkit.slug,
    )


def _connection_enabled_map(db: Session, ids: list[str]) -> dict[str, bool]:
    """连接 → 本地采集开关（ingest_sources.enabled，source_key=连接 ID——与采集
    枚举同口径）。行缺省视为启用（enabled 只做减法，同 _source_enabled 语义）。"""
    if not ids:
        return {}
    rows = db.scalars(select(IngestSource).where(IngestSource.source_key.in_(ids))).all()
    return {r.source_key: r.enabled for r in rows}


# toolkit 展示元数据（name/logo/description）进程内缓存：公共目录数据极稳定
# （slug 不变则内容几乎不变），缓存命中让二次起的列表 enrich 零云端往返
_TOOLKIT_META_CACHE: dict[str, tuple[str | None, str | None, str | None]] = {}


def _toolkit_meta(client, slug: str) -> tuple[str | None, str | None, str | None]:
    """单 slug 展示元数据：缓存优先，未命中调一次 toolkits.get。

    失败容忍 (None, None, None) 但不写缓存——网络抖动不该被固化，下次照常重试。"""
    cached = _TOOLKIT_META_CACHE.get(slug)
    if cached is not None:
        return cached
    try:
        detail = client.toolkits.get(slug)
        meta = (detail.name, detail.meta.logo, detail.meta.description)
        _TOOLKIT_META_CACHE[slug] = meta
        return meta
    except Exception:
        logger.warning("Composio 应用元数据拉取失败：%s", slug, exc_info=True)
        return (None, None, None)


def list_connections(db: Session, enrich: bool = True) -> list[ComposioConnectionOut]:
    """已连接应用列表（真源 = Composio 云端，只取 ACTIVE）。

    内嵌 toolkit 只有 slug，name/logo/description 按唯一 slug 调 toolkits.get
    补全；单应用元数据拉取失败容忍为 None，不毁整个列表。enabled 为本地采集开关
    （SQLite 一查，两种形态都带——data_ingest 只消费 id/slug 不受影响）。两种调用
    形态：设置页"已连接"卡展示（默认 enrich=True）；源枚举/键校验只消费
    id/slug/kind（data_ingest 传 enrich=False，省 N×(10s 超时×2 重试) 的云端
    往返——2026-10-15 数据接入页打开慢排障）。
    """
    client = _require_client(db)
    try:
        result = client.connected_accounts.list(
            user_ids=[COMPOSIO_USER_ID], statuses=["ACTIVE"]
        )
    except Exception as e:
        raise _translate_upstream(e) from e

    enrich_map: dict[str, tuple[str | None, str | None, str | None]] = {}
    if enrich:
        slugs = list(dict.fromkeys(item.toolkit.slug for item in result.items))
        if slugs:
            # 并发拉取（2026-10-16 Composio 卡恒比 SQLite 源晚出排障：曾按 slug
            # 串行 N 次 toolkits.get，N 段云端往返逐段叠加；并发 + 元数据进程内
            # 缓存后，冷启只剩 1 段、缓存热后 0 段——连接清单整体只剩
            # connected_accounts.list 一次权威云端往返）
            with ThreadPoolExecutor(max_workers=min(8, len(slugs))) as pool:
                enrich_map = dict(
                    zip(slugs, pool.map(lambda s: _toolkit_meta(client, s), slugs))
                )

    enabled_map = _connection_enabled_map(db, [item.id for item in result.items])
    connections = [
        ComposioConnectionOut(
            id=item.id,
            status=str(item.status),
            toolkit_slug=item.toolkit.slug,
            name=enrich_map.get(item.toolkit.slug, (None, None, None))[0],
            logo=enrich_map.get(item.toolkit.slug, (None, None, None))[1],
            description=enrich_map.get(item.toolkit.slug, (None, None, None))[2],
            enabled=enabled_map.get(item.id, True),
            created_at=_parse_iso(item.created_at),
            updated_at=_parse_iso(item.updated_at),
        )
        for item in result.items
    ]
    logger.info("读取 Composio 已连接列表：%d 条（enrich=%s）", len(connections), enrich)
    return connections


def test_connection(db: Session, connected_account_id: str) -> dict | None:
    """已连接应用「测试」按钮（ComposioConnectionTestOut 形状，失败是数据）：
    与采集同源的动作解析（行配置 fetch_tool > 内置映射 _COMPOSIO_FETCH_ACTIONS），
    execute 真调云端数条目——不过时间窗（测试报原始返回数，时间窗会把"有数据"
    砍成"0 条"造成误判）；未内置采集命令的应用退化为仅验连接状态（note 说明）。
    结果不落库（云端真源无本地行，前端会话态展示）。连接不存在返回 None（404）。"""
    from composio_client import NotFoundError

    client = _require_client(db)
    try:
        ca = client.connected_accounts.get(nanoid=connected_account_id)
    except NotFoundError:
        return None
    except Exception as e:
        raise _translate_upstream(e) from e

    t0 = time.monotonic()

    def _done(ok: bool, items=None, note=None, error=None) -> dict:
        return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000),
                "items_count": items, "note": note, "error": error}

    if str(ca.status) != "ACTIVE":
        return _done(False, error=f"连接状态为 {ca.status}，请删除后重新授权")

    # 与 _composio_one_sync 完全同构的动作解析——测的是"实际采集会跑的动作"
    from services.data_ingest import (  # 惰性导入防环（data_ingest 顶层 import 本模块）
        _COMPOSIO_FETCH_ACTIONS,
        _extract_items,
        _resolve_action,
        _source_row,
        reset_source_health,
    )
    mapped = _resolve_action(_source_row(db, connected_account_id),
                             _COMPOSIO_FETCH_ACTIONS.get(ca.toolkit.slug))
    if mapped is None:
        reset_source_health(db, connected_account_id)  # 验证成功即重置连败冷却
        return _done(True, note="该应用未内置采集命令，仅验证连接状态可用")

    tool_slug, arguments = mapped
    try:
        resp = client.tools.execute(
            tool_slug, arguments=dict(arguments), connected_account_id=connected_account_id
        )
    except Exception as e:
        raise _translate_upstream(e) from e
    if not resp.successful:
        return _done(False, error=f"云端执行 {tool_slug} 失败：{resp.error or '未返回原因'}")
    reset_source_health(db, connected_account_id)  # 验证成功即重置连败冷却
    return _done(True, items=len(_extract_items(resp.data)))


def set_connection_enabled(db: Session, connected_account_id: str, enabled: bool) -> bool:
    """连接启停（卡片「停用/启用」）：镜像 ingest_sources（source_key=连接 ID，与
    采集枚举同口径）——停用即不参与每日采集，云端连接本体不动（重启用无感恢复）。
    连接不存在返回 False（404，防拼错 id 静默建行）。"""
    from composio_client import NotFoundError

    client = _require_client(db)
    try:
        client.connected_accounts.get(nanoid=connected_account_id)
    except NotFoundError:
        return False
    except Exception as e:
        raise _translate_upstream(e) from e

    from services.data_ingest import (  # 惰性导入防环
        mirror_source_enabled,
        reset_source_health,
    )

    mirror_source_enabled(db, connected_account_id, enabled)
    if enabled:
        reset_source_health(db, connected_account_id)  # 启用 = 用户要求立即再试
    logger.info("Composio 连接 %s 采集开关 → %s", connected_account_id, enabled)
    return True


def disconnect(db: Session, connected_account_id: str) -> bool:
    """断开连接（Composio 云端删除）。删除后清理本地启停镜像行（key=连接 ID）——
    nanoid 不复用、残留行永不命中，但顺手清（曾停用的行会一直躺在 sources 列表）。"""
    from composio_client import NotFoundError

    client = _require_client(db)
    try:
        client.connected_accounts.delete(nanoid=connected_account_id)
    except NotFoundError:
        return False
    except Exception as e:
        raise _translate_upstream(e) from e
    row = db.scalar(select(IngestSource).where(IngestSource.source_key == connected_account_id))
    if row is not None:
        db.delete(row)
        db.commit()
    logger.info("断开 Composio 连接 %s", connected_account_id)
    return True
