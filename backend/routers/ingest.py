"""数据采集接口（2026-10-14 增，M1-⑤a / M2-②）：手动立即采集 + 每源采集配置。

薄壳层——POST /run 只做前置快查与在途窥视后 create_task 并出（async def 路由与
agent_loop 同在事件循环，同步路由拿不到 loop）：202 立即返回，结果走既有 ingest
广播 + 前端 refreshKey 重拉，零轮询零新 SSE。GET/PUT /sources 调 data_ingest 的
枚举与更新，校验在 service / schema 层，这里只翻译状态码。
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import IcsImportOut, IcsImportRequest, IngestSourceOut, IngestSourceUpdate
from services import data_ingest, ics_import, workflows
from services.model_providers import get_active_model
from services.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.post("/run", status_code=202)
async def run_ingest_now(wait: bool = False, db: Session = Depends(get_db)):
    """手动立即采集：前置快查（采集开关 / AI 开关 / 零激活 → 422 引导去设置）→
    在途窥视（409）→ create_task(_run_ingest) 并出。注意当日调度认领与手动在跑
    撞车时，调度侧静默让位（认领行已落不重试，登记 docs/tech-debt.md）。

    wait=true（2026-11-04 手动刷新批，数据源页刷新钮）：等待本轮完成后返回
    结果摘要 {skipped, silent, drafts, inbox, direct_calendar, direct_inbox}
    ——刷新语义需要完成反馈（"新增 N 条"/"无新数据"）；采集数秒到十几秒
    （Map 并发 LLM），await 期间事件循环自由不挡其他请求。"""
    settings = get_settings(db)
    if not settings.ingest_enabled:
        raise HTTPException(status_code=422, detail="每日数据采集未开启（设置 → 定时任务）")
    if not settings.ai_enabled:
        raise HTTPException(status_code=422, detail="AI 功能已在设置中关闭")
    if get_active_model(db) is None:
        raise HTTPException(status_code=422, detail="未激活任何模型，请到设置 → 模型接入中激活")
    if workflows.ingest_in_flight():
        raise HTTPException(status_code=409, detail="上一轮采集仍在进行中，请稍后再试")
    if wait:
        logger.info("手动触发数据采集（等待完成）")
        summary = await workflows._run_ingest(manual=True)
        return summary
    asyncio.create_task(workflows._run_ingest(manual=True))
    logger.info("手动触发数据采集")
    return {"started": True}


@router.post("/ics", response_model=IcsImportOut)
async def import_ics_file(body: IcsImportRequest, db: Session = Depends(get_db)):
    """导入 .ics 日历文件（数据接入页底部入口，2026-11-11 批）：默认信任直落——
    VEVENT 建日历日程 / VTODO 进收件箱，不经建议结算。本地读盘+解析毫秒到亚秒级，
    等待型返回汇总（与 /run?wait=true 同构）；async def 使落库后的 tasks_changed
    广播在事件循环线程内调用。业务与校验全在 services/ics_import.py，这里只翻译
    状态码。"""
    try:
        return IcsImportOut.model_validate(
            ics_import.import_ics_file(db, body.path))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/sources", response_model=list[IngestSourceOut])
def list_sources(db: Session = Depends(get_db)):
    """每源采集配置与健康度（零额外网络——available_tools 仅 MCP 会话池命中时填充，
    前端见空引导先点数据接入卡上的「测试」刷新工具清单）。"""
    return [IngestSourceOut.model_validate(s) for s in data_ingest.list_ingest_sources(db)]


@router.put("/sources/{key}", response_model=IngestSourceOut)
def update_source(key: str, body: IngestSourceUpdate, db: Session = Depends(get_db)):
    """更新单源采集配置（参与开关 / 采集动作覆盖 / 时间窗）。保存走 await 回填
    不乐观（同数据接入启停按钮口径）——响应重查 list 口径，含健康度与工具清单。"""
    try:
        row = data_ingest.update_ingest_source(db, key, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="该数据源未登记（先在数据接入中启用）")
    updated = next((s for s in data_ingest.list_ingest_sources(db) if s["key"] == key), None)
    return IngestSourceOut.model_validate(updated)
