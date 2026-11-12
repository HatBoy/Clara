"""关于页接口（2026-11-06 批 A）：检查更新 / 数据备份导出 / 恢复准备。

薄壳层——调 service、按约定翻译状态码（ValueError→422）；不含业务逻辑。
恢复端点只做第一阶段（staging 落盘），第二阶段由重启后的后端在
lifespan 里 apply_pending_restore 完成（services/backup.py 模块头有全流程）。
"""

from fastapi import APIRouter, HTTPException

from schemas import BackupOut, BackupRequest, LatestReleaseOut, RestoreOut, RestoreRequest
from services import backup as backup_service
from services import update_check

router = APIRouter(prefix="/api/about", tags=["about"])


@router.get("/latest-release", response_model=LatestReleaseOut)
def check_update():
    """GitHub 最新 Release 探测（永不抛出：网络失败/未上传统一归不可用态）。
    版本比较在前端做——前端持 getVersion() 真源。"""
    return update_check.latest_release()


@router.post("/backup", response_model=BackupOut)
def create_backup(body: BackupRequest):
    """导出备份 zip（路径来自前端原生保存对话框，非自由输入）。"""
    try:
        return backup_service.create_backup(body.path, body.version)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/restore", response_model=RestoreOut)
def prepare_restore(body: RestoreRequest):
    """恢复第一阶段：校验 + staging 落盘 + 安全副本，返回要求重启。"""
    try:
        return backup_service.prepare_restore(body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
