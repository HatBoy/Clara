"""同步备份接口（2026-11-14 增，「设置 → 同步备份」）：配置读写 / 测试连接 /
手动同步 / 重置基线 / 备份两件套（从 about 迁入，service 层不动）/ 合并导入。

薄壳层——调 service、按约定翻译状态码（ValueError→422）；不含业务逻辑。
手动同步（POST /run）：create_task 后台执行（前端轮询 GET 的 syncing /
last_sync_* 看进度与结果——不做长请求悬挂）；冷启动全自动合并（2026-11-15
迭代四起无确认流程）。
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    BackupOut,
    BackupRequest,
    MergeAnalyzeIn,
    MergeAnalyzeOut,
    MergeApplyIn,
    MergeApplyOut,
    RestoreOut,
    RestoreRequest,
    SyncConfigOut,
    SyncConfigUpdate,
    SyncRunOut,
    SyncTestIn,
    SyncTestOut,
)
from services import backup as backup_service
from services import sync_config as sync_config_service
from services import sync_engine, sync_merge
from services.sync_config import effective_backup_dir, last_auto_backup_at
from services.sync_webdav import WebDavClient

router = APIRouter(prefix="/api/sync", tags=["sync"])

# /run 后台任务引用（asyncio 只持弱引用——无引用的 task 可能被 GC 中途蒸发，
# 保存引用 + done 回调自清是官方文档要求的写法）
_bg_tasks: set[asyncio.Task] = set()


def _config_out(db: Session, row) -> SyncConfigOut:
    """ORM 行 → 出参（组装目录解析 / 最近备份时刻 / 同步锁状态三个非库字段）。"""
    out = SyncConfigOut.model_validate(row)
    out.auto_backup_dir_resolved = str(effective_backup_dir(row))
    out.auto_backup_last_at = last_auto_backup_at()  # type: ignore[assignment]
    out.syncing = sync_engine.is_syncing()
    return out


@router.get("", response_model=SyncConfigOut)
def get_config(db: Session = Depends(get_db)):
    return _config_out(db, sync_config_service.get_sync_config(db))


@router.patch("", response_model=SyncConfigOut)
def update_config(body: SyncConfigUpdate, db: Session = Depends(get_db)):
    try:
        row = sync_config_service.update_sync_config(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _config_out(db, row)


@router.post("/test", response_model=SyncTestOut)
def test_connection(body: SyncTestIn, db: Session = Depends(get_db)):
    """测试连接：body 可带草稿凭证（缺省回落库内；均无 → 422）。探测失败也
    200（前端 hint 直显 message），结果落 last_test_*。"""
    from datetime import datetime

    row = sync_config_service.get_sync_config(db)
    url = body.url or row.webdav_url
    username = body.username or row.webdav_username
    password = body.password if body.password is not None else row.webdav_password
    if not url:
        raise HTTPException(status_code=422, detail="请先填写 WebDAV 服务地址")
    ok, ms, message = WebDavClient(url, username, password).test_connection()
    row.last_test_at = datetime.now()
    row.last_test_ok = ok
    row.last_test_ms = ms
    db.commit()
    return SyncTestOut(ok=ok, ms=ms, message=message)


@router.post("/run", response_model=SyncRunOut)
async def run_now():
    """手动「立即同步」：后台执行（未启用/未配置的中文提示由 _sync_once 落
    last_sync_error，前端底部行显示——按钮不做禁用拦截）。"""
    if sync_engine.is_syncing():
        return SyncRunOut(started=False, error="上一轮同步仍在进行，请稍候")
    task = asyncio.create_task(asyncio.to_thread(sync_engine.sync_now))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return SyncRunOut(started=True)


@router.post("/reset-base")
def reset_base():
    """重置同步基线：清空本地快照（守卫拦截后 / 远端换库后的重对齐出口；
    下轮同步按冷启动语义重新对齐）。"""
    sync_engine.clear_snapshot()
    return {"ok": True}


@router.post("/backup", response_model=BackupOut)
def create_backup(body: BackupRequest):
    """导出备份 zip（路径来自前端原生保存对话框，非自由输入）。"""
    try:
        return backup_service.create_backup(body.path, body.version)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/restore", response_model=RestoreOut)
def prepare_restore(body: RestoreRequest):
    """恢复第一阶段：校验 + staging 落盘 + 安全副本，返回要求重启。
    双格式：备份格式 zip 与同步格式 zip（手压 WebDAV 目录）均可。"""
    try:
        return backup_service.prepare_restore(body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/merge-analyze", response_model=MergeAnalyzeOut)
def merge_analyze(body: MergeAnalyzeIn, db: Session = Depends(get_db)):
    """合并导入第一阶段（dry-run 零写入）：新增/更新统计 + 冲突明细前 50 条。"""
    try:
        return sync_merge.analyze_merge(db, body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/merge-apply", response_model=MergeApplyOut)
def merge_apply(body: MergeApplyIn, db: Session = Depends(get_db)):
    """合并导入第二阶段：按策略写当前库（单事务；不需重启；不删除本地行）。"""
    try:
        return sync_merge.apply_merge(db, body.path, body.strategy)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
