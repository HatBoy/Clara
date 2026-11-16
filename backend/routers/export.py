"""数据导出接口（2026-11-17 批 4）：任务 CSV / .ics 日历。

薄壳：路径校验与写盘在 services/export.py；ValueError → 422。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from services import export as export_service

router = APIRouter(prefix="/api/export", tags=["export"])


class ExportRequest(BaseModel):
    """导出路径（来自前端原生保存对话框）。"""

    path: str = Field(min_length=4, max_length=500)


@router.post("/tasks-csv")
def export_tasks_csv(body: ExportRequest, db: Session = Depends(get_db)):
    """全量活跃任务导出 CSV（utf-8-sig，Excel 直开中文正常）。"""
    try:
        return export_service.export_tasks_csv(db, body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/tasks-ics")
def export_tasks_ics(body: ExportRequest, db: Session = Depends(get_db)):
    """已排期任务导出 .ics（RRULE + EXDATE；全天半开日期区间）。"""
    try:
        return export_service.export_tasks_ics(db, body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
