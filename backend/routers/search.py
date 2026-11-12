"""搜索端点薄壳（2026-11-06 搜索批）：匹配逻辑一律在 services/search，
此处只做 HTTP 编解码。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import SearchOut
from services import search as search_service

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=SearchOut)
def search(
    q: str = Query(default="", description="关键词（标题/备注/项目名/建议条目子串；空串返回空结果）"),
    include_done: bool = Query(default=False, description="包含已完成任务（默认隐藏）"),
    db: Session = Depends(get_db),
):
    return search_service.search_all(db, q, include_done=include_done)
