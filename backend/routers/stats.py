"""统计报告接口：聚合查询 / 历史报告 / 生成周月报 / 导出 .md / AI 动态洞察 /
统计问答（SSE 流式，独立轻通道）。

薄壳层。报告生成与洞察是 LLM 同步调用（秒级）——同步 def 端点本就跑 FastAPI
线程池，直接调用不占事件循环（to_thread 是 async 端点的包装法，此处不适用）；
/ask 是流式端点，to_thread + 队列桥照 ai_coach.chat 先例。
"""

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from schemas import StatsReportOut
from services import stats as stats_service
from services.llm import NoActiveModelError, call_llm_stream
from services.model_providers import get_active_model
from services.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])


class _StreamDone:
    """工作线程正常收尾哨兵（携带聚合全文）。"""

    def __init__(self, text: str):
        self.text = text


class _StreamFailure:
    """工作线程异常哨兵（消费侧翻译为 error 帧）。"""

    def __init__(self, exc: BaseException):
        self.exc = exc


class ReportCreate(BaseModel):
    """生成入参：week = 本周报（周一起 7 天）、month = 本月报（1 号起当月）。"""

    period: str = Field(pattern="^(week|month)$")


class ReportExportRequest(BaseModel):
    """导出入参：path 来自前端原生保存对话框（.md 文件）。"""

    path: str = Field(min_length=4, max_length=500)


class ReportExportOut(BaseModel):
    ok: bool = True
    path: str
    size_bytes: int


class StatsAskTurn(BaseModel):
    """问答历史单轮（前端带最近几轮实现多轮追问；端点无状态）。"""

    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class StatsAskIn(BaseModel):
    """统计问答入参：days 跟随页面范围（7/30/90），question 用户提问；
    mode=review 为复盘引导会话（结构化块约定见 service._ASK_REVIEW_DIRECTIVE）。"""

    days: Literal[7, 30, 90]
    question: str = Field(min_length=1, max_length=2000)
    history: list[StatsAskTurn] | None = None
    mode: Literal["ask", "review"] = "ask"


class AdviceStatusIn(BaseModel):
    """建议打卡入参：index = payload.advice_items 序号；status 三态。"""

    index: int = Field(ge=0)
    status: str = Field(pattern="^(proposed|completed|dropped)$")


class StatsFactIn(BaseModel):
    """复盘反思结论沉淀入参（用户确认后落 Tier 2 情节事实）。"""

    topic: str = Field(min_length=1, max_length=60)
    fact: str = Field(min_length=1, max_length=200)


def _event_bytes(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/summary")
def get_summary(days: int = 30, db: Session = Depends(get_db)):
    """统计聚合（days = 近 N 天含今天，7/30/90 三档由前端切换）。"""
    if days not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="days 仅支持 7/30/90")
    return stats_service.stats_summary(db, days=days)


@router.get("/reports", response_model=list[StatsReportOut])
def get_reports(db: Session = Depends(get_db)):
    """历史报告列表（最新在前）。"""
    return stats_service.list_reports(db)


@router.post("/reports", response_model=StatsReportOut)
def create_report(body: ReportCreate, db: Session = Depends(get_db)):
    """生成周/月报（聚合 + LLM 总结 + 落库；LLM 不可用回落纯数据模板）。"""
    return stats_service.generate_report(db, body.period)


@router.post("/reports/{report_id}/export", response_model=ReportExportOut)
def export_report(report_id: str, body: ReportExportRequest, db: Session = Depends(get_db)):
    """导出报告为 .md 文件（本地写盘毫秒级，同步 def 即可）。"""
    try:
        return stats_service.export_report_md(db, report_id, body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/insight")
def get_insight(days: int = 30, refresh: int = 0, db: Session = Depends(get_db)):
    """AI 动态洞察（页面顶部一卡三段）：基于统计数据生成个性化教练式导读。

    区别于周/月报：
    - 动态洞察：滚动窗口教练式导读，Markdown 不落库，页面顶部实时展示
    - 周/月报：完整周期数据报告，Markdown 落库留档

    返回 {"insight": Markdown 文本}；服务层带一天 TTL 缓存（低频场景，反复进出
    页面不重复烧 LLM），refresh=1 跳过缓存手动重新生成（仍刷新缓存）。
    """
    if days not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="days 仅支持 7/30/90")
    return {"insight": stats_service.generate_insight(db, days=days, refresh=bool(refresh))}


@router.post("/reports/{report_id}/advice", response_model=StatsReportOut)
def set_report_advice(report_id: str, body: AdviceStatusIn,
                      db: Session = Depends(get_db)):
    """建议打卡：用户确认上期建议的执行情况（完成/放弃）——下期报告对照点评
    的反馈信号（completed 标注点评、dropped 不再追问）。"""
    try:
        return stats_service.set_advice_status(db, report_id, body.index, body.status)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/facts")
def create_stats_fact(body: StatsFactIn, db: Session = Depends(get_db)):
    """沉淀复盘反思结论为情节事实（Tier 2 记忆，source=stats_chat；用户确认后
    调用——同 topic+fact 印证不堆行）。"""
    row = stats_service.upsert_review_fact(db, body.topic, body.fact)
    if row is None:
        raise HTTPException(status_code=422, detail="主题与结论不能为空")
    return {"ok": True, "id": row.id}


@router.post("/ask")
async def ask_stats(body: StatsAskIn, db: Session = Depends(get_db)):
    """统计问答（SSE 流式）：右侧对话面板专用独立轻通道。

    stats_summary 精简注入 system 上下文 + call_llm_stream 流式输出；不进
    ChatTurn、不走意图路由、不注入人格——与日程助手上下文完全隔离。帧协议
    对齐 /api/ai/chat（delta / reply / done / error），前端流式解析同款。
    """
    async def event_stream():
        yield ": connected\n\n"
        if not get_settings(db).ai_enabled:
            yield _event_bytes({"type": "error", "code": "disabled",
                                "message": "AI 功能已在设置中关闭"})
            yield _event_bytes({"type": "done"})
            return
        if get_active_model(db) is None:
            yield _event_bytes({"type": "error", "code": "no_model",
                                "message": "未激活任何模型，请到设置 → 模型接入中激活"})
            yield _event_bytes({"type": "done"})
            return

        # 消息构建（纯读库毫秒级，留在事件循环线程；此后 db 不再触碰）
        messages = stats_service.build_ask_messages(
            db, body.days, body.question,
            [t.model_dump() for t in body.history] if body.history else None,
            mode=body.mode)

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run_llm() -> None:
            """工作线程：call_llm_stream 同步驱动，增量经 call_soon_threadsafe
            投回事件循环（asyncio.Queue 非线程安全——桥接同 ai_coach.chat）。"""
            try:
                text = call_llm_stream(
                    messages,
                    on_chunk=lambda t: loop.call_soon_threadsafe(
                        queue.put_nowait, {"type": "delta", "text": t}),
                )
            except BaseException as exc:  # 消费侧 raise 后落入下方翻译分支
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, _StreamFailure(exc))
                except RuntimeError:
                    pass  # 事件循环先关的极端窗口：静默退出
            else:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, _StreamDone(text))
                except RuntimeError:
                    pass

        task = asyncio.create_task(asyncio.to_thread(_run_llm))
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _StreamDone):
                    # 复盘引导总结轮可能带结构化尾部：剥离后 reply 发正文、
                    # facts 帧带候选（前端确认条消费；普通模式无块零影响）
                    reply, facts = stats_service.split_ask_reply(item.text)
                    yield _event_bytes({"type": "reply", "content": reply})
                    if facts:
                        yield _event_bytes({"type": "facts", "candidates": facts})
                    break
                if isinstance(item, _StreamFailure):
                    if isinstance(item.exc, NoActiveModelError):
                        yield _event_bytes({"type": "error", "code": "no_model",
                                            "message": "未激活任何模型"})
                    else:
                        logger.exception("统计问答异常：%s", body.question[:50])
                        yield _event_bytes({"type": "error", "code": "agent_error",
                                            "message": "回答生成失败，请稍后重试"})
                    break
                yield _event_bytes(item)
        finally:
            task.cancel()  # 正常路径任务已结束；故障路径防泄漏（线程不可中断，自然跑完丢弃）
        yield _event_bytes({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
