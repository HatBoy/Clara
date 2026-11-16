"""最近一次操作撤销接口（2026-11-17 批 1）。

薄壳层：状态读取与快照回写都在 services/undo.py；进程内栈，重启后不可用。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from services import undo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/undo", tags=["undo"])


@router.get("")
def get_undo_state():
    """当前可撤销操作（栈顶）；无则 available=false。"""
    return undo.state()


@router.post("")
def apply_undo(db: Session = Depends(get_db)):
    """撤销最近一次操作；空栈 409。"""
    try:
        label = undo.apply_last(db)
    except Exception as exc:  # noqa: BLE001——回写失败给出可读中文
        logger.exception("[undo] 撤销失败")
        raise HTTPException(status_code=409, detail=f"撤销失败：{exc}") from exc
    if label is None:
        raise HTTPException(status_code=409, detail="没有可撤销的操作")
    return {"ok": True, "label": label}
