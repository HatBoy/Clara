"""MCP 会话复用池（M2-⑤C，设计见 docs/ai-ingest.md §九-9.2C）：常驻后台事件循环
线程 + 按服务名缓存会话。

动机：若逐次采集都经 asyncio.run 一次性桥接——stdio 传输每次都重新拉子进程
（首跑 npx 下载依赖动辄数十秒），单用户每日一次的采集场景里握手开销远大于调用本身。

结构：
- 常驻 daemon 线程跑一个 asyncio 事件循环（惰性启动，首次调用才拉起）；
- 会话池 {服务名: (client, tools, 命中时刻)}，TTL 15 分钟——命中直接复用工具
  句柄，miss / 过期 / 调用异常即丢弃重建（重建 = 重拉子进程，回退语义同一次性桥接）；
- 两个入口：acall（async，供采集 gather 协程用——wrap_future 桥接不阻塞调用者
  事件循环）、call（同步，供 list_source_actions 等同步 service 用）。

降级铁律（docs/ai-ingest.md §九-9.2C）：**池化是性能优化不是正确性依赖**——本模块
任何未预期异常都由调用方（data_ingest）捕获并回落一次性桥接，池自身永不把异常
扩散成采集失败。

已知取舍（docs/tech-debt.md）：adapters 0.3.2 的 client 无会话关闭 API（__aenter__
已废弃直接 raise），stdio 子进程随 client 对象 GC 回收、进程退出兜底——TTL 丢弃
不保证即时杀子进程。

返回形态镜像 data_ingest._mcp_bridge：{"result": 工具返回} / {"missing": 工具名} /
{"tools": [{name, description}]}——调用方（含回落路径）零分支切换。
"""

import asyncio
import atexit
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 会话闲置过期（秒）：过期后下次调用重建（配置变更 / 凭据轮换 / 子进程僵死自愈）
_TTL_S = 15 * 60

_loop: asyncio.AbstractEventLoop | None = None
_sessions: dict[str, tuple] = {}  # 服务名 -> (client, tools, 命中时刻 monotonic)
_start_lock = threading.Lock()
_pool_lock = threading.Lock()


def _run_loop() -> None:
    """后台线程主体：新建事件循环并 run_forever（daemon 线程，进程退出不阻塞）。"""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """惰性启动后台循环线程（首次调用拉起；已活直接返回）。"""
    global _loop
    with _start_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        threading.Thread(target=_run_loop, name="mcp-session-loop", daemon=True).start()
        while _loop is None or not _loop.is_running():
            time.sleep(0.01)
        return _loop


def _shutdown() -> None:
    """进程退出收尾：停后台循环（atexit 注册；会话对象与 stdio 子进程随 GC 回收）。"""
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)


atexit.register(_shutdown)


def _drop(name: str) -> None:
    """丢弃一条会话（调用异常自愈：下次调用重建）。"""
    with _pool_lock:
        _sessions.pop(name, None)


def invalidate(name: str) -> None:
    """配置写入（新建/编辑/删除）后主动失效该服务的池会话（_drop 公共壳，
    2026-10-16 起）：会话按服务名缓存，凭据轮换后旧会话最长残留 TTL 15 分钟——
    采集路径有一次性桥接兜底不会因此失败，但清单/补采等同步路径会先撞一次旧
    会话；主动失效让「改完配置立即生效」成立。"""
    _drop(name)


async def _connect(conn: dict):
    """后台循环内建会话：factory → get_tools（握手定位一次完成）。异常向上抛
    （调用方回落），坏会话不入池。"""
    from services import mcp_servers

    client = mcp_servers._client_factory()(connections={"target": conn})
    tools = await client.get_tools()
    return client, tools


async def _acquire_and_call(conn: dict, name: str, tool_name: str | None, arguments: dict) -> dict:
    """后台循环内执行：池命中（TTL 内）复用；miss 建会话入池。工具调用异常先
    丢弃会话再向上抛（会话可能已坏，不让下次命中僵尸会话）。"""
    now = time.monotonic()
    with _pool_lock:
        hit = _sessions.get(name)
        if hit is not None and now - hit[2] >= _TTL_S:
            _sessions.pop(name, None)
            hit = None
    if hit is None:
        client, tools = await _connect(conn)
        with _pool_lock:
            _sessions[name] = (client, tools, time.monotonic())
    else:
        client, tools = hit[0], hit[1]

    if tool_name is None:
        return {"tools": [{"name": t.name, "description": (t.description or "")[:120]}
                          for t in tools[:20]]}
    for t in tools:
        if t.name == tool_name:
            try:
                return {"result": await t.ainvoke(arguments)}
            except Exception:
                _drop(name)
                raise
    return {"missing": tool_name}


def peek_tools(name: str) -> list[dict]:
    """零成本窥视：池命中（TTL 内）返回工具清单，未命中返回空列表——GET
    /api/ingest/sources 不打网络，前端见空引导「先点卡上『测试』刷新工具清单」。"""
    with _pool_lock:
        hit = _sessions.get(name)
    if hit is None or time.monotonic() - hit[2] >= _TTL_S:
        return []
    return [{"name": t.name, "description": (t.description or "")[:120]} for t in hit[1][:20]]


async def acall(conn: dict, tool_name: str | None, arguments: dict, name: str,
                timeout_s: float) -> dict:
    """async 入口（采集 gather 协程用）：经 run_coroutine_threadsafe 把工作派给
    后台循环，wrap_future 桥接回调用者事件循环（不阻塞——gather 并发不失效）。
    超时取消后台任务后向上抛 TimeoutError（调用方回落或翻译为失败数据）。"""
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _acquire_and_call(conn, name, tool_name, arguments), loop
    )
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout_s)
    except asyncio.TimeoutError:
        fut.cancel()
        raise


def call(conn: dict, tool_name: str | None, arguments: dict, name: str,
         timeout_s: float) -> dict:
    """同步入口（list_source_actions 等同步 service 用；只能在无事件循环的线程
    调用——在事件循环线程同步阻塞 = 卡死整个服务，显式拒绝而非静默挂死）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # 无运行中循环，允许
    else:
        raise RuntimeError("mcp_session.call 需在无事件循环的线程中调用")
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _acquire_and_call(conn, name, tool_name, arguments), loop
    )
    try:
        return fut.result(timeout=timeout_s)
    except TimeoutError:
        # 超时后取消后台协程（镜像 acall）：不取消则废弃协程继续在后台循环里
        # 跑完整个握手/调用，泄漏一次完整连接开销（Python 3.11+ 起
        # concurrent.futures.TimeoutError 即内建 TimeoutError 的别名）
        fut.cancel()
        raise


def _reset_for_tests() -> None:
    """测试间清模块态（conftest autouse 调用）：池清空防跨用例串台；后台循环
    保留（线程与真实运行形态一致，假 client 由 monkeypatch factory 注入）。"""
    with _pool_lock:
        _sessions.clear()
