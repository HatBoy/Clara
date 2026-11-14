"""关于页接口（2026-11-06 批 A；2026-11-14 同步备份起只留检查更新——备份导出/
恢复两端点迁至 /api/sync（routers/sync.py），service 层 services/backup.py 不变）。"""

from fastapi import APIRouter

from schemas import LatestReleaseOut
from services import update_check

router = APIRouter(prefix="/api/about", tags=["about"])


@router.get("/latest-release", response_model=LatestReleaseOut)
def check_update():
    """GitHub 最新 Release 探测（永不抛出：网络失败/未上传统一归不可用态）。
    版本比较在前端做——前端持 getVersion() 真源。"""
    return update_check.latest_release()
