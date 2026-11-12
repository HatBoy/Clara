"""日程提醒 SSE 接口（长链接）。

薄壳层——只做流式编排；订阅/退订与事件生产在 services/reminders.py 的进程内
队列。协议约定（长链接用 SSE 不用 WebSocket，与 ai_coach 未来流式同例）：
- 连接建立先发 ": connected" 注释行（立即交付，前端 EventSource 进 open 态）
- 30s 无事件发 ": ping" 注释行保活（防中间层掐空闲连接）
- 事件为 "data: {json}\\n\\n"，载荷字段见 services.reminders.claim_due
"""

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services import reminders as reminders_service

router = APIRouter(prefix="/api/reminders", tags=["reminders"])

# 无事件保活间隔（秒）
_PING_SECONDS = 30


@router.get("/stream")
async def stream_reminders():
    """提醒事件流。客户端断开时 FastAPI 取消生成器，finally 退订防队列泄漏。"""
    q = reminders_service.subscribe()

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            reminders_service.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
