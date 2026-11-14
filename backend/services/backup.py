"""数据备份与恢复服务（关于页两件套之二，2026-11-06 批 A；2026-11-14 同步备份
扩自动备份/双格式恢复/恢复后清同步快照）。

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

双格式恢复（2026-11-14）：除备份格式外兼容「同步格式」zip——用户手动把
WebDAV 同步目录（manifest.json + tables/*.json）压成 zip 也能直接恢复：
_materialize_sync_zip 把散表 JSON 物化为临时 SQLite 后走同一条两段式链；
不动本机 config_snapshot / Tauri 偏好（同步范围本就不含它们）。

自动备份（2026-11-14）：auto_backup 按时间戳落 zip（复用 create_backup），
prune_auto_backups 只保留最近 7 天——**红线：文件名 pattern 严格匹配
clara-autobackup-*.zip，同目录 pre-restore-*.db 安全副本绝不能被波及**。

安全：恢复 zip 成员走白名单（防 zip 路径穿越），恢复前自动把当前库快照
一份到 backups/pre-restore-*.db（恢复失败可手动找回）。
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta
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
# 自动备份文件名（时间戳命名；prune 的严格匹配 pattern——红线见模块头）
AUTO_BACKUP_PREFIX = "clara-autobackup-"
_AUTO_BACKUP_TS = re.compile(r"^clara-autobackup-(\d{8})-(\d{6})\.zip$")


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
    双格式分流（2026-11-14）：备份格式（manifest.app=="clara"，白名单校验）与
    同步格式（app=="clara-sync"，tables/*.json 物化为 clara.db——不动本机
    config_snapshot / Tauri 偏好）。zip 不合法 / 缺关键成员 / 库损坏抛
    ValueError（router 翻 422）。"""
    archive = Path(src)
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ValueError("所选文件不是有效的备份包")
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        try:
            manifest: dict = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}
        except (ValueError, OSError):
            raise ValueError("备份包 manifest 损坏，无法恢复")

        data_dir = get_data_dir()
        staging = data_dir / _STAGING_DIR
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        if manifest.get("app") == "clara-sync":
            _materialize_sync_zip(zf, staging)
        elif manifest.get("app") == "clara":
            illegal = names - _ZIP_MEMBERS
            if illegal:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError(f"备份包含未知成员，拒绝恢复：{sorted(illegal)[:3]}")
            if "clara.db" not in names:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError("备份包缺少数据库文件，无法恢复")
            zf.extractall(staging)  # 成员已全量白名单校验，无穿越风险
        else:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError("这不是 Clara 的备份包")

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


def _materialize_sync_zip(zf: zipfile.ZipFile, staging: Path) -> None:
    """同步格式 zip → staging/clara.db：校验成员白名单（manifest + tables/
    <SYNC_TABLES>.json，防穿越与未知表）→ 建临时 SQLite（create_all 全新
    schema + sorted_tables 序插入 + defer FK 解 tasks.parent_id 自引用）→
    行内键按目标表列集过滤（旧版数据列少不炸 NOT NULL）。"""
    from database import Base
    from services.sync_engine import SYNC_TABLES, _revive_row

    names = set(zf.namelist())
    allowed = {"manifest.json"} | {f"tables/{t}.json" for t in SYNC_TABLES}
    illegal = names - allowed
    if illegal:
        raise ValueError(f"同步备份包含未知成员，拒绝恢复：{sorted(illegal)[:3]}")

    new_db = staging / "clara.db"
    tmp = sqlite3.connect(new_db)
    try:
        tmp.execute("PRAGMA defer_foreign_keys=ON")
        Base.metadata.create_all(_sqlite_engine(tmp))
        # 逐表插入（sorted_tables 序：父表在前）
        meta = {t.name: t for t in Base.metadata.sorted_tables}
        for name in SYNC_TABLES:
            member = f"tables/{name}.json"
            if member not in names:
                continue
            data = json.loads(zf.read(member))
            rows = data.get("rows") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError(f"同步表文件格式异常（{name}）")
            if not rows:
                continue
            table = meta[name]
            pk = table.primary_key.columns[0].name
            cols = table.columns.keys()
            placeholders = ",".join(f":{c}" for c in cols)
            sql = f"INSERT INTO {name} ({','.join(cols)}) VALUES ({placeholders})"
            for row in rows:
                values = _revive_row(table, row)
                values.setdefault(pk, row.get(pk))
                # datetime 值转 isoformat 文本（sqlite3 直插不接受对象）
                flat = {c: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for c, v in values.items()}
                tmp.execute(sql, flat)
        tmp.commit()
        # 完整性探测
        tmp.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"同步数据无法物化为数据库：{e}") from e
    finally:
        tmp.close()
    logger.info("同步格式备份已物化为临时数据库（staging/clara.db）")


def _sqlite_engine(conn: sqlite3.Connection):
    """把已有 sqlite3 连接包成 SQLAlchemy engine（供 create_all 建表）——
    create_all 需要 engine/create 连接对象，这里桥接避免二次建文件。"""
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import create_engine

    return create_engine(
        "sqlite://",
        creator=lambda: conn,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def auto_backup(dest_dir: str | None = None) -> dict:
    """自动备份（workflows._run_autobackup 调用）：落
    {dir}/clara-autobackup-YYYYMMDD-HHMMSS.zip 到配置目录（None = 默认
    data/backups；目录不存在 mkdir 自愈——用户先配目录再建是合法顺序）。
    内容与手动导出完全同构（复用 create_backup），可直接被恢复功能消费。"""
    from services.sync_config import effective_backup_dir

    directory = Path(dest_dir) if dest_dir else effective_backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{AUTO_BACKUP_PREFIX}{datetime.now():%Y%m%d-%H%M%S}.zip"
    result = create_backup(str(dest))
    logger.info("自动备份完成：%s（%.1f MB）", dest.name, result["size_bytes"] / 1048576)
    return result


def prune_auto_backups(dest_dir: str | None = None, keep_days: int = 7) -> int:
    """清理 7 天前的自动备份（按文件名时间戳优先、解析失败回退 mtime）。
    **红线：只删 _AUTO_BACKUP_TS 严格匹配的文件**——同目录的 pre-restore-*.db
    安全副本与用户自放文件绝不能被波及。返回删除数。"""
    from services.sync_config import effective_backup_dir

    directory = Path(dest_dir) if dest_dir else effective_backup_dir()
    if not directory.is_dir():
        return 0
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for f in directory.iterdir():
        if not f.is_file():
            continue
        m = _AUTO_BACKUP_TS.match(f.name)
        try:
            ts = (datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S")
                  if m else datetime.fromtimestamp(f.stat().st_mtime))
        except ValueError:
            continue
        if ts < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("已清理 %d 个超过 %d 天的自动备份", removed, keep_days)
    return removed


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
    # 同步快照失效（2026-11-14 同步备份）：数据回到过去，旧 Base 与现库不再同源，
    # 下轮同步按冷启动语义重新对齐（防旧数据凭旧快照被判定为「远端已删」）。
    # 局部导入防环（backup ← sync_engine 的 import 链在函数体内闭环）
    from services.sync_engine import clear_snapshot

    clear_snapshot()
    logger.info("已从备份恢复数据（clara.db / 配置快照 / Tauri 偏好），同步快照已重置")
    return True
