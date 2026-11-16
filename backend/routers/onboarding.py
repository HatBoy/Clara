"""首启入职访谈接口（2026-11-17 批 1）。薄壳：校验入参、翻译异常；落库在
services/onboarding.py。完成/跳过后写 onboarded_at，前端据此不再弹向导。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import OnboardingCompleteIn, OnboardingCompleteOut, SettingsOut
from services import onboarding as onboarding_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("/complete", response_model=OnboardingCompleteOut)
def complete(body: OnboardingCompleteIn, db: Session = Depends(get_db)):
    try:
        result = onboarding_service.complete_onboarding(
            db, settings=body.settings, rules=body.rules, skipped=body.skipped)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return OnboardingCompleteOut(
        settings=SettingsOut.model_validate(result["row"]),
        rules_created=result["rules_created"],
    )
