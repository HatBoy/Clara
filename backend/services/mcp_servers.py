"""MCP 服务接入业务逻辑层：CRUD、连通性探测与阶段二连接装配。

经 langchain-mcp-adapters（MultiServerMCPClient）统一协议连接任意 MCP 服务端，
stdio（npx/uvx 子进程）/ streamable_http / sse 三种传输。精选应用（钉钉/
Obsidian/滴答国内版）与手动添加同形落库——前端模板预填配置，本层零精选知识。

连通性测试（probe_server）由后端统一执行——密钥不进浏览器网络面板；探测
永不抛出（失败是数据，同 probe_model 口径）：get_tools() 完成握手即连通，
往返耗时作测速值、工具数作能力摘要。
"""

import asyncio
import logging
import time
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import McpServer
from schemas import (
    McpServerCreate,
    McpServerTestOut,
    McpServerUpdate,
)
from services import mcp_session

logger = logging.getLogger(__name__)


class ConflictError(Exception):
    """业务冲突（显示名称重名），router 翻译为 409。"""


# Optional 放行显式 null 后由 service 拦（NOT NULL 列）：键 → 中文标签
_NOT_NULL_FIELDS = {"name": "显示名称", "transport": "传输方式"}

# 连接握手超时（秒），按传输方式分档：stdio 服务首次运行需下载依赖（npx 拉包），
# 给足首跑窗口；http/sse 纯网络往返，短档即可
_TEST_TIMEOUT_STDIO_S = 60.0
_TEST_TIMEOUT_HTTP_S = 10.0
# 外层硬帽余量（秒）：内层超时后若清理挂起（anyio 取消传播慢 / 进程树终止卡在
# 可打断的 await 上），外层到点强制返回的额外窗口
_HARD_GRACE_S = 10.0
# 弃养前的收尾宽限（秒）：外层到点 cancel 后再等这一窗——多数清理在收到取消后
# 立即结束；仍不退出则弃养任务、探测直接返回。须恒满足不等式：
# timeout_s + _HARD_GRACE_S + _CANCEL_WAIT_S < 前端 80s AbortController
# （最紧的 stdio 档 60 + 10 + 5 = 75s < 80s）——改任一常量须复核此约束
_CANCEL_WAIT_S = 5.0
# 探测场景 httpx 读超时短档（秒）：探测握手无长 SSE 流；短读让被弃养的收尾
# DELETE 不被 SDK 默认 read=300s 拖住，孤儿任务在短窗内自然失败收场
_PROBE_READ_S = 15.0


def _client_factory():
    """MultiServerMCPClient 延迟 import 单点（同 _composio_factory 先例——测试
    monkeypatch 唯一入口；包冷启动 ~0.4s，不让应用启动期白付）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient


def _direct_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """复刻 mcp SDK 的 create_mcp_http_client，唯一差异 trust_env=False。

    2026-09-20 Obsidian MCP 排障实证：httpx 默认读环境代理（HTTP_PROXY 等），
    本机代理普遍拒绝转发 127.0.0.1 目标、回空 body 的 502——MCP 服务绝大多数
    是本地/局域网端点（stdio 本地进程、插件回环端口），一律禁用环境代理直连。
    键经 conn dict 透传进 adapters 的 streamable_http/sse 会话（两者签名均
    收 httpx_client_factory）。取舍：需经代理访问的外网 MCP 服务会被直连
    （国内主流云端端点直连可达，登记此口径）。
    """
    if timeout is None:
        timeout = httpx.Timeout(30.0, read=300.0)  # 同 SDK 默认：读长档兜 SSE 流
    kwargs: dict = {"timeout": timeout, "trust_env": False}
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def _probe_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """探测专用直连 factory：与 _direct_http_client_factory 唯一差异是默认读
    超时取短档 _PROBE_READ_S——真硬帽弃养任务后，收尾链最迟在短读超时后自行
    失败收场，不再驻留 300s。真实调用路径（get_connections）仍用默认长读档。
    签名逐参镜像直连 factory（SDK 按名传 headers/timeout/auth）。"""
    if timeout is None:
        timeout = httpx.Timeout(30.0, read=_PROBE_READ_S)
    return _direct_http_client_factory(headers=headers, timeout=timeout, auth=auth)


def _conn_dict(
    transport: str,
    command: str | None,
    args: list[str] | None,
    env: dict[str, str] | None,
    url: str | None,
    headers: dict[str, str] | None,
) -> dict:
    """按 transport 只放合法键组装连接描述（MultiServerMCPClient 的 TypedDict
    不认多余键——stdio 混入 url 或 http 混入 command 都会 TypeError；
    httpx_client_factory 是 http/sse 会话函数签名的合法参数故可携带）。"""
    if transport == "stdio":
        conn: dict = {"transport": "stdio", "command": command, "args": args or []}
        if env:
            conn["env"] = env
        return conn
    conn = {
        "transport": transport,
        "url": url,
        "httpx_client_factory": _direct_http_client_factory,
    }
    if headers:
        conn["headers"] = headers
    return conn


def _row_conn(row: McpServer) -> dict:
    return _conn_dict(row.transport, row.command, row.args, row.env, row.url, row.headers)


def list_servers(db: Session) -> list[McpServer]:
    rows = list(db.scalars(select(McpServer).order_by(McpServer.created_at)))
    logger.info("读取 MCP 服务列表：%d 条", len(rows))
    return rows


def get_server(db: Session, server_id: str) -> McpServer | None:
    row = db.get(McpServer, server_id)
    if row is None:
        logger.info("MCP 服务不存在: %s", server_id)
    return row


def create_server(db: Session, body: McpServerCreate) -> McpServer:
    dup = db.scalar(select(McpServer).where(McpServer.name == body.name))
    if dup is not None:
        raise ConflictError(f"显示名称已存在: {body.name}")
    data = body.model_dump()
    # 空容器归一 None 落库（stdio 行不存 url/headers、http 行不存 command 侧字段）——
    # 契约的 default_factory=dict 让缺省键成 {}，归一后读侧单一 None 口径
    for key in ("args", "env", "headers"):
        data[key] = data[key] or None
    row = McpServer(**data)
    db.add(row)
    db.commit()
    db.refresh(row)
    # 同名重建防卡死（同 cli_tools.connect 的 _mirror 口径）：残留 ingest_sources 行
    # 可能带着旧服务的 enabled=False / 连败冷却，把新服务拦在采集之外
    from services import data_ingest

    # 高可信默认值策略（2026-10-24）：任务型模板（滴答）接入即预开直通——特征串
    # 判定与采集映射 _MCP_FETCH_TOOLS 同口径；仅建行时生效，存量行不重置用户开关
    _default_trusted = any(m in (data.get("url") or "") or m in (data.get("command") or "")
                           for m in data_ingest.DEFAULT_TRUSTED_MCP_MARKERS)
    data_ingest.mirror_source_enabled(db, row.name, True, trusted=_default_trusted)
    data_ingest.reset_source_health(db, row.name)
    mcp_session.invalidate(row.name)  # 旧同名服务的池会话（若有）一并失效
    logger.info("新建 MCP 服务 %s (%s, %s)", row.name, row.id, row.transport)
    return row


def update_server(db: Session, server_id: str, patch: McpServerUpdate) -> McpServer | None:
    row = get_server(db, server_id)
    if row is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return row  # 空 patch 短路：不发 UPDATE、不误抬 updated_at
    for key, label in _NOT_NULL_FIELDS.items():
        if key in data and data[key] is None:
            raise ValueError(f"{label}不允许置空")
    if "name" in data and data["name"] != row.name:
        dup = db.scalar(
            select(McpServer).where(McpServer.name == data["name"], McpServer.id != server_id)
        )
        if dup is not None:
            raise ConflictError(f"显示名称已存在: {data['name']}")

    # 合并态 transport 分支校验（Update 是部分更新，须与库中现值合并后判定）：
    # stdio 须有 command、http/sse 须有 url
    merged = {
        "transport": data.get("transport", row.transport),
        "command": data.get("command", row.command),
        "url": data.get("url", row.url),
    }
    if merged["transport"] == "stdio" and not merged["command"]:
        raise ValueError("stdio 传输必须填写启动命令")
    if merged["transport"] != "stdio" and not merged["url"]:
        raise ValueError("该传输方式必须填写服务 URL")

    old_transport = row.transport
    for key, value in data.items():
        setattr(row, key, value)
    # 切换 transport 时清对侧字段（stdio ↔ http）：一次 PATCH 完成切换，无需再发
    # 清除命令；JSON 列整体替换（setattr），符合 ORM 变更检测约定。未切换不动
    if row.transport != old_transport:
        if row.transport == "stdio":
            row.url, row.headers = None, None
        else:
            row.command, row.args, row.env = None, None, None
    db.commit()
    db.refresh(row)
    # 用户介入即重置（旧连败证据是对旧配置的记录，凭据修好后冷却闸不该继续拦）
    # + 启停联动镜像 ingest_sources.enabled（采集配置 UI 已撤，卡片停用 = 退出采集）
    # + 池会话主动失效（凭据轮换立即生效，最长残留 15min）——延迟导入避免循环
    # 依赖（data_ingest 顶层导入本模块）
    from services import data_ingest

    data_ingest.reset_source_health(db, row.name)
    if "enabled" in data:
        data_ingest.mirror_source_enabled(db, row.name, bool(row.enabled))
    mcp_session.invalidate(row.name)
    logger.info("更新 MCP 服务 %s：字段 %s", row.name, sorted(data))
    return row


def delete_server(db: Session, server_id: str) -> bool:
    row = get_server(db, server_id)
    if row is None:
        return False
    name = row.name  # 删前留名：失效同名池会话（重名重建不再撞旧会话）
    db.delete(row)  # 硬删：env/headers 含凭据不留墓碑（口径见 ORM docstring）
    db.commit()
    mcp_session.invalidate(name)
    logger.info("删除 MCP 服务 %s (%s)", row.name, row.id)
    return True


def _unwrap_exceptions(exc: BaseException) -> list[BaseException]:
    """递归解包 ExceptionGroup（anyio TaskGroup 把传输层异常包组冒出——mcp 客户端
    的 httpx.HTTPStatusError 等从不裸抛，2026-09-20 排障实证 502 即以组形态到达，
    原 except httpx.HTTPError 分支因此永不命中）；非组返回自身。"""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_unwrap_exceptions(sub))
        return leaves or [exc]
    return [exc]


def _describe_error(exc: BaseException) -> str:
    """探测异常 → 中文摘要（叶子逐个识别取首个命中；只读类型与 status_code，
    不读 str(e)——env/headers 可能含凭据片段）。"""
    for leaf in _unwrap_exceptions(exc):
        if isinstance(leaf, httpx.HTTPStatusError):
            code = leaf.response.status_code
            if code in (401, 403):
                return f"鉴权失败（HTTP {code}）：检查 API Key / 凭据是否正确"
            if code == 404:
                return "端点不存在（HTTP 404）：检查服务 URL 路径是否正确"
            if code >= 500:
                return f"服务端错误（HTTP {code}）：服务不可用，或被本机代理/网关拦截"
            return f"服务拒绝了请求（HTTP {code}）"
        if isinstance(leaf, httpx.HTTPError):
            return "无法连接服务：检查网络与 URL 是否可达"
        if isinstance(leaf, OSError):
            # 子进程拉起失败（WinError 2 找不到命令；FileNotFoundError 是其子类）
            return "无法启动服务进程：找不到命令或无执行权限（检查 command 是否已安装，如 npx/uvx）"
    return f"连接失败：{type(exc).__name__}"


def _reap_orphan(task: asyncio.Task) -> None:
    """弃养任务的异常消费回调：exception() 取出即消费，防 asyncio "Task
    exception was never retrieved" 噪音；正常完成 / 已取消则无事可做。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("已弃养的探测任务随后以异常收场：%s", type(exc).__name__)


async def probe_server(
    transport: str,
    command: str | None,
    args: list[str] | None,
    env: dict[str, str] | None,
    url: str | None,
    headers: dict[str, str] | None,
) -> McpServerTestOut:
    """连通性探测（纯函数无 db，永不抛出——失败是数据不是异常）。

    构造单连接的 MultiServerMCPClient，get_tools() 完成握手（stdio = 拉起子进程
    并完成 initialize、http = 端点可达且协议正确）；异常经 _describe_error
    解包分类翻译中文摘要。

    真硬帽（2026-10-15 滴答清单国内版排障）：wait_for 超时后仍要等任务真正退出
    才返回，而 streamable_http 收尾链（anyio TaskGroup 取消传播 + 终止 DELETE，
    SDK 默认读超时 300s）可能不响应取消——双层 wait_for 会一起挂到前端 80s 兜底。
    改 create_task + asyncio.wait 到点即脱钩：pending 先 cancel，再等 _CANCEL_WAIT_S
    收尾；仍不退出则弃养（异常经 _reap_orphan 消费），探测必然按上限时间返回。
    """
    conn = _conn_dict(transport, command, args, env, url, headers)
    if transport != "stdio":
        # 探测用读短档 factory 覆写（get_connections 真实调用路径不受影响）
        conn["httpx_client_factory"] = _probe_http_client_factory
    timeout_s = _TEST_TIMEOUT_STDIO_S if transport == "stdio" else _TEST_TIMEOUT_HTTP_S
    start = time.perf_counter()

    def _timeout_out() -> McpServerTestOut:
        return McpServerTestOut(
            ok=False,
            error=f"连接超时：服务未在 {int(timeout_s)} 秒内完成握手"
            "（stdio 服务首次运行需下载依赖，请稍后重试）",
        )

    async def _handshake():
        client = _client_factory()(connections={"probe": conn})
        return await asyncio.wait_for(client.get_tools(), timeout=timeout_s)

    task = asyncio.create_task(_handshake())
    done, pending = await asyncio.wait({task}, timeout=timeout_s + _HARD_GRACE_S)
    if pending:
        task.cancel()
        await asyncio.wait({task}, timeout=_CANCEL_WAIT_S)
        if not task.done():
            task.add_done_callback(_reap_orphan)
            logger.warning("探测清理链未在宽限内退出，弃养任务（transport=%s）", transport)
        return _timeout_out()
    if task.cancelled():  # 防御：取消态调 .exception() 会抛 CancelledError
        return _timeout_out()
    exc = task.exception()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        # 内层握手超时：保留分档文案，不走 _describe_error（否则退化为裸类型名）
        return _timeout_out()
    if exc is not None:
        return McpServerTestOut(ok=False, error=_describe_error(exc))
    tools = task.result()
    if not tools:
        return McpServerTestOut(ok=False, error="已连接但服务未暴露任何工具")
    return McpServerTestOut(
        ok=True,
        latency_ms=round((time.perf_counter() - start) * 1000),
        tools_count=len(tools),
    )


async def test_server(db: Session, server_id: str) -> McpServerTestOut | None:
    """对已保存服务执行连通性测试并记录结果（last_test_* 四列落库，tools 数供卡
    标题旁徽标"连通 · Nms · N 个工具"持久化展示；与 enabled 正交——停用行也可
    测试，测试不改变装载状态）。"""
    row = get_server(db, server_id)
    if row is None:
        return None
    result = await probe_server(row.transport, row.command, row.args, row.env, row.url, row.headers)
    row.last_test_ok = result.ok
    row.last_test_ms = result.latency_ms
    row.last_test_at = datetime.now()
    row.last_test_tools = result.tools_count  # 失败时为 None
    db.commit()
    if result.ok:
        # 验证成功即重置连败冷却（2026-10-16 排障结论：测试通过 ≠ 下轮采集会重试，
        # 不重置会把修好凭据的好源继续拦 2h～24h）——延迟导入防环（同 update_server）
        from services import data_ingest

        data_ingest.reset_source_health(db, row.name)
    logger.info("MCP 测速 %s (%s)：ok=%s latency=%sms tools=%s", row.name, row.id, result.ok, result.latency_ms, result.tools_count)
    return result


def get_connections(db: Session) -> dict[str, dict]:
    """阶段二 agent 的读入口（本期只写不消费）：enabled 行 → {连接名: 连接描述}。

    本地文件夹服务启用时以虚拟连接 "local_folder" 追加——它是进程内挂载的
    MCP 服务端，端点随本服务端口走（local_folder_mcp_url）。
    """
    from services import local_folder

    conns = {
        row.name: _row_conn(row)
        for row in db.scalars(select(McpServer).where(McpServer.enabled.is_(True)))
    }
    paths, enabled = local_folder.get_local_folder_state(db)
    if enabled and paths:
        conns["local_folder"] = {
            "transport": "streamable_http",
            "url": local_folder.local_folder_mcp_url(),
            # 本机回环端点同 _conn_dict 口径禁环境代理
            "httpx_client_factory": _direct_http_client_factory,
        }
    return conns
