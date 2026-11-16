"""日程模板接口（2026-11-17 批 2）：CRUD + 应用到某日生成任务。薄壳层。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import TaskOut, TemplateApplyIn, TemplateIn, TemplateOut, TemplateUpdate
from services import templates as template_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)):
    return template_service.list_templates(db)


@router.post("", response_model=TemplateOut, status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    try:
        return template_service.create_template(db, body)
    except template_service.TemplateConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.patch("/{template_id}", response_model=TemplateOut)
def update_template(template_id: str, body: TemplateUpdate,
                    db: Session = Depends(get_db)):
    try:
        row = template_service.update_template(db, template_id, body)
    except template_service.TemplateConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return row


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db)):
    if not template_service.delete_template(db, template_id):
        raise HTTPException(status_code=404, detail="模板不存在")


@router.post("/{template_id}/apply", response_model=TaskOut, status_code=201)
def apply_template(template_id: str, body: TemplateApplyIn,
                   db: Session = Depends(get_db)):
    try:
        task = template_service.apply_template(db, template_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if task is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return task
