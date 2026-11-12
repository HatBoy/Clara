"""定时任务接口（2026-10-20 增，「设置-定时任务」配套）。

薄壳层——GET /api/jobs 出注册表与运行状态；/briefs 四端点透传 services.jobs
CRUD，ValueError→422、LookupError→404（同 settings / mcp_servers 惯例）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    CustomBriefCreate,
    CustomBriefOut,
    CustomBriefUpdate,
    JobOut,
    JobsOut,
)
from services import jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=JobsOut)
def list_jobs(db: Session = Depends(get_db)):
    """定时任务总清单（内置六流 + 自定义简报逐行），文案与识别口径后端单源。"""
    return JobsOut(jobs=[JobOut.model_validate(j) for j in jobs.list_jobs(db)])


@router.get("/briefs", response_model=list[CustomBriefOut])
def list_briefs(db: Session = Depends(get_db)):
    return [CustomBriefOut.model_validate(b) for b in jobs.list_briefs(db)]


@router.post("/briefs", response_model=CustomBriefOut, status_code=201)
def create_brief(body: CustomBriefCreate, db: Session = Depends(get_db)):
    try:
        row = jobs.create_brief(db, brief_min=body.brief_min, instruction=body.instruction)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return CustomBriefOut.model_validate(row)


@router.patch("/briefs/{brief_id}", response_model=CustomBriefOut)
def update_brief(brief_id: str, body: CustomBriefUpdate, db: Session = Depends(get_db)):
    try:
        row = jobs.update_brief(
            db, brief_id,
            brief_min=body.brief_min,
            instruction=body.instruction,
            enabled=body.enabled,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="简报不存在")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return CustomBriefOut.model_validate(row)


@router.delete("/briefs/{brief_id}", status_code=204)
def delete_brief(brief_id: str, db: Session = Depends(get_db)):
    try:
        jobs.delete_brief(db, brief_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="简报不存在")
