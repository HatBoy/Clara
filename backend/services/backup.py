"""数据备份与恢复服务（关于页两件套之二，2026-11-06 批 A）。

导出 = SQLite backup API 一致性快照（WAL 常开，运行中直接拷 .db 不安全）
+ 配置文件 zip 成单文件。恢复 = 两段式——本进程无法安全替换自己正打开的
库，故先解包校验到 restore_staging/ 落 PENDING 标记（prepare_restore），
由宿主重启后端进程后，新进程 lifespan 最前 apply_pending_restore() 原子
替换（打包版走 Rust restart_backend 命令；dev 形态提示手动重启 uvicorn）。

备份范围取舍：clara.db（业务+配置同库主体）+ config_snapshot.json（配置
冗余快照，同 data 目录）+ Tauri 侧用户偏好 JSON（shortcuts/layout/widget，
经 %APPDATA%/com.hat.clara 可达）。**不含** server_port.json（端口是本机
环境配置，恢复到异机无意义甚至冲突）与 .window-state.json（窗口几何本机
相关）。清单即 _TAURI_FILES，恢复时同名写回。

安全：恢复 zip 成员走白名单（防 zip 路径穿越），恢复前自动把当前库快照
一份到 backups/pre-restore-*.db（恢复失败可手动找回）。
"""

import json
import logging
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from config import DATABASE_PATH, IS_PACKAGED, get_data_dir

logger = logging.getLogger(__name__)

# zip 内 Tauri 侧偏好文件（→ %APPDATA%/com.hat.clara/<文件名>）；与导出范围一致
_TAURI_FILES = ("shortcuts.json", "layout.json", "widget.json")
# zip 成员白名单（manifest + 库 + 快照 + Tauri 偏好）——prepare_restore 校验
# 全部成员都在此列，防 "../" 路径穿越与夹带任意文件
_ZIP_MEMBERS = {"manifest.json", "clara.db", "config_snapshot.json"} | {
    f"tauri/{name}" for name in _TAURI_FILES
}
_STAGING_DIR = "restore_staging"
_PENDING_FLAG = "PENDING.restore"


def _tauri_config_dir() -> Path | None:
    """Tauri 侧配置目录（Windows %APPDATA%/com.hat.clara）；不存在返回 None
    （如纯后端环境 / 首启动未生成），导出静默跳过、恢复静默忽略。"""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    dir_path = Path(base) / "com.hat.clara"
    return dir_path if dir_path.is_dir() else None


def _snapshot_db_to(tmp_db: Path) -> None:
    """sqlite3 backup API 出一致性快照（WAL 安全；直接拷 .db 会丢 wal 内页）。"""
    src = sqlite3.connect(DATABASE_PATH)
    try:
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_backup(dest: str, version: str | None = None) -> dict:
    """导出备份 zip 到用户指定路径（路径来自前端原生保存对话框）。
    返回 {ok, size_bytes, files}；路径不合法抛 ValueError（router 翻 422）。"""
    out = Path(dest)
    if not out.is_absolute():
        raise ValueError("备份路径须为绝对路径")
    if out.suffix.lower() != ".zip":
        raise ValueError("备份文件须以 .zip 结尾")
    if not out.parent.is_dir():
        raise ValueError(f"目标目录不存在：{out.parent}")

    data_dir = get_data_dir()
    tmp_db = data_dir / "backup_tmp.db"
    if tmp_db.exists():
        tmp_db.unlink()
    _snapshot_db_to(tmp_db)

    files: list[str] = ["manifest.json", "clara.db"]
    tauri_dir = _tauri_config_dir()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "app": "clara",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "packaged": IS_PACKAGED,
            "version": version,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.write(tmp_db, "clara.db")
        snapshot = data_dir / "config_snapshot.json"
        if snapshot.is_file():
            zf.write(snapshot, "config_snapshot.json")
            files.append("config_snapshot.json")
        if tauri_dir is not None:
            for name in _TAURI_FILES:
                member = f"tauri/{name}"
                if (tauri_dir / name).is_file():
                    zf.write(tauri_dir / name, member)
                    files.append(member)
    tmp_db.unlink()
    size = out.stat().st_size
    logger.info("备份已导出：%s（%.1f MB，%d 个成员）", out, size / 1048576, len(files))
    return {"ok": True, "size_bytes": size, "files": files}


def prepare_restore(src: str) -> dict:
    """恢复第一阶段（后端存活时调用）：校验 zip → 解包 staging → 落 PENDING
    标记 → 预写安全副本。返回 {ok, requires_restart: True, message}。
    zip 不合法 / 缺关键成员 / 库损坏抛 ValueError（router 翻 422）。"""
    archive = Path(src)
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ValueError("所选文件不是有效的备份包")
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        illegal = names - _ZIP_MEMBERS
        if illegal:
            raise ValueError(f"备份包含未知成员，拒绝恢复：{sorted(illegal)[:3]}")
        if "clara.db" not in names:
            raise ValueError("备份包缺少数据库文件，无法恢复")
        manifest: dict = {}
        if "manifest.json" in names:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except (ValueError, OSError):
                raise ValueError("备份包 manifest 损坏，无法恢复")
        if manifest.get("app") != "clara":
            raise ValueError("这不是 Clara 的备份包")

        data_dir = get_data_dir()
        staging = data_dir / _STAGING_DIR
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        zf.extractall(staging)  # 成员已全量白名单校验，无穿越风险

    # 快照完整性探测：能开且 sqlite_master 可查即认（旧版本备份表少些无妨，
    # 后续 create_all 会补齐缺表）
    try:
        probe = sqlite3.connect(staging / "clara.db")
        try:
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            probe.close()
    except sqlite3.DatabaseError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"备份数据库无法读取：{e}")

    # 安全体：恢复前把当前库快照一份（恢复失败 / 误恢复的找回出口）
    backups_dir = data_dir / "backups"
    backups_dir.mkdir(exist_ok=True)
    safe_copy = backups_dir / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
    _snapshot_db_to(safe_copy)

    (staging / _PENDING_FLAG).write_text(
        json.dumps({"safe_copy": str(safe_copy)}, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("恢复已就绪（staging 落盘），等待后端重启应用；安全副本 %s", safe_copy)
    return {
        "ok": True,
        "requires_restart": True,
        "message": "备份校验通过，已预写安全副本，等待重启后端应用恢复",
    }


def apply_pending_restore() -> bool:
    """恢复第二阶段（新进程 lifespan 最前调用，须先于任何数据库连接）：
    staging 存在 PENDING 标记则原子替换库/快照/Tauri 偏好并清理。返回是否
    实际应用。此刻旧进程已死、本进程尚未连接数据库，文件无锁可直覆。"""
    staging = get_data_dir() / _STAGING_DIR
    pending = staging / _PENDING_FLAG
    if not pending.is_file():
        return False

    new_db = staging / "clara.db"
    if not new_db.is_file():  # PENDING 在但库不在：脏 staging，直接清退
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning("恢复标记存在但备份数据库缺失，已丢弃 staging")
        return False

    # 清残留 wal/shm（宿主强杀旧进程时可能遗留，与新库不匹配会致读取错乱）
    for suffix in ("-wal", "-shm"):
        stale = Path(str(DATABASE_PATH) + suffix)
        if stale.exists():
            stale.unlink()

    shutil.copy2(new_db, DATABASE_PATH)
    snapshot = staging / "config_snapshot.json"
    if snapshot.is_file():
        shutil.copy2(snapshot, get_data_dir() / "config_snapshot.json")
    tauri_src = staging / "tauri"
    tauri_dir = _tauri_config_dir()
    if tauri_src.is_dir() and tauri_dir is not None:
        for name in _TAURI_FILES:
            member = tauri_src / name
            if member.is_file():
                shutil.copy2(member, tauri_dir / name)
    shutil.rmtree(staging, ignore_errors=True)
    logger.info("已从备份恢复数据（clara.db / 配置快照 / Tauri 偏好）")
    return True
