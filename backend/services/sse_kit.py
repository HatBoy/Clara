"""SSE / 后台任务运行时（2026-11-17 架构批 1）：流式脚手架单源。

原先四类脚手架散装复制——data 帧拼装 + ping 保活循环三份（reminders /
ai_coach /stream 内联）、跨线程队列桥信封两份逐行同构（ai_coach chat 与
stats ask）、后台任务强引用池四份（asyncio 只持弱引用、无人引用的长任务
会在跑完前被 GC——同一坑修了 4 遍）。连接生命周期从此有了 seam：

- 帧：sse_frame（协议单源，坏帧口径由 json.dumps 保证）
- 订阅流：subscription_response（connected 头 + 空转 ping + data 帧 + finally 退订）
- 线程桥：StreamDone / StreamFailure 信封 + deliver（call_soon_threadsafe，
  事件循环先关的极端窗口静默）+ stream_worker（同步 work(deliver) 进线程池，
  异常收尾投信封——消费侧只认信封不认线程）
- 强引用池：spawn（create_task 后挂池，done 自动出池）

纪律：本 module 不懂业务帧语义（帧里的 type/code 是调用方契约），也不做
错误翻译（no_model / agent_error 等分类是各端点的降级矩阵职责）。
"""

import asyncio
import json
from collections.abc import Callable

from sse_starlette import EventSourceResponse, ServerSentEvent

# 无事件保活间隔（秒）——防中间层掐空闲连接（原三处 _PING_SECONDS 单源）
PING_SECONDS = 30

def sse_frame(event: dict) -> str:
    """事件 data 帧（载荷恒单行 JSON，ensure_ascii=False 保中文原文）。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def subscription_response(subscribe: Callable, unsubscribe: Callable) -> EventSourceResponse:
    """常驻订阅须在 Uvicorn 等待连接排空时结束，不能等 lifespan shutdown。"""
    async def frames():
        q = subscribe()
        try:
            yield b': connected\n\n'
            while True:
                yield sse_frame(await q.get()).encode("utf-8")
        finally:
            unsubscribe(q)

    return EventSourceResponse(frames(), sep='\n', ping=PING_SECONDS,
                               ping_message_factory=lambda: ServerSentEvent(comment="ping", sep='\n'))


class StreamDone:
    """工作线程正常收尾信封：payload = work() 的返回值（聚合全文 / 终态 state）。"""

    __slots__ = ("payload",)

    def __init__(self, payload):
        self.payload = payload


class StreamFailure:
    """工作线程异常信封：exc 原样包装入队，消费侧 raise 后落入既有翻译分支。"""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException):
        self.exc = exc


# 强引用池：asyncio 只持任务弱引用，无人引用的长任务可能在跑完前被 GC
# 静默蒸发（「手动采集偶发无后续」的真身）；done 回调自动出池
_STRONG_REFS: set[asyncio.Task] = set()


def spawn(task: asyncio.Task) -> asyncio.Task:
    """后台任务挂强引用池（原 routers/sync · ingest · ai_coach 三处拷贝单源）。"""
    _STRONG_REFS.add(task)
    task.add_done_callback(_STRONG_REFS.discard)
    return task


def deliver(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, item) -> None:
    """线程安全投递（asyncio.Queue 非线程安全——工作线程经本函数回事件循环）。

    事件循环先关的极端窗口投递失败（RuntimeError）静默：工作线程自然退出，
    队列无人消费（无界）不阻塞。
    """
    try:
        loop.call_soon_threadsafe(queue.put_nowait, item)
    except RuntimeError:
        pass


def stream_worker(work: Callable[[Callable], object]) -> asyncio.Queue:
    """把同步 work(deliver) 放线程池执行，返回事件队列（原 stats ask 线程桥单源）。

    work 接一个 deliver(item) 回调投递增量（delta 帧等）；正常返回值装
    StreamDone、任何异常装 StreamFailure 投尾——消费循环按信封分拣即可，
    不再各自手写 try/except/else + call_soon_threadsafe 三连。
    线程不可中断：消费侧提前退出时工作线程自然跑完（产物入无人读的无界
    队列，无害），任务由强引用池托管、done 自动出池——无需 cancel 句柄。
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _wrapped() -> None:
        try:
            payload = work(lambda item: deliver(queue, loop, item))
        except BaseException as exc:  # 消费侧 raise 后落入调用方翻译分支
            deliver(queue, loop, StreamFailure(exc))
        else:
            deliver(queue, loop, StreamDone(payload))

    spawn(asyncio.create_task(asyncio.to_thread(_wrapped)))
    return queue
