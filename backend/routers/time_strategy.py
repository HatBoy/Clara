"""时间策略的只读预览和显式确认入口。"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from schemas import MemoryRuleOut
from services import time_strategy as strategy

router = APIRouter(prefix='/api/strategy', tags=['strategy'])


@router.get('/focus')
def preview(task_id: str, original: date, day: date, start: int = Query(ge=0, lt=1440),
            end: int = Query(gt=0, le=1440), db: Session = Depends(get_db)):
    try:
        return strategy.focus_preview(db, task_id, original, day, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get('/calibrations')
def calibrations(db: Session = Depends(get_db)):
    return strategy.weekly_calibrations(db)


@router.post('/calibrations')
def accept(title: str, minutes: int = Query(gt=0, le=1440), db: Session = Depends(get_db)):
    try:
        strategy.accept_calibration(db, title, minutes)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {'ok':True}


@router.post('/rules/{rule_id}/pause', response_model=MemoryRuleOut)
def pause_rule(rule_id: str, pause: bool, db: Session = Depends(get_db)):
    try:
        return strategy.pause_this_week(db, rule_id, pause)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
