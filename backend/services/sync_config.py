"""同步备份配置业务层（2026-11-14 同步备份）：sync_settings 单行宽表的读写。

惯例对齐 services/settings.py：GET 即 get_or_create（device_id 缺则补 uuid4）、
PATCH 走 exclude_unset（键不出现 = 不动）+ 合并态校验，ValueError 由 router 翻
422。凭证域隔离（同 integration_settings 先例）：不混入 /api/settings 契约。

校验规则：
- webdav_url：strip 尾 "/"、空串归 None（停用态）、须 http(s):// 前缀；
- url/username/password 任一实际变更 → 清 last_test_*（旧探测结果对新配置是谎言）；
- auto_backup_dir：strip 空串归 None（回默认目录）、非空须绝对路径且已存在；
- webdav_enabled False→True：凭证须已配齐（url 必填），开启即经 sync_engine
  调度首同步（防抖通道——否则最长空等 300s 周期）。
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from models import SyncSettings

logger = logging.getLogger(__name__)

# 单行 id（恒 1，同 app_settings 惯例）
SYNC_ROW_ID = 1


def get_sync_config(db: Session) -> SyncSettings:
    """读取配置行；无则按默认值落行 + 生成 device_id（get_or_create，二次幂等）。"""
    row = db.get(SyncSettings, SYNC_ROW_ID)
    if row is None:
        row = SyncSettings(
            id=SYNC_ROW_ID,
            webdav_enabled=False,
            auto_backup_enabled=True,
            device_id=str(uuid.uuid4()),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info("初始化同步备份配置行（device_id=%s）", row.device_id)
    if not row.device_id:  # 防御：异常历史行补签
        row.device_id = str(uuid.uuid4())
        db.commit()
    return row


def _normalize_webdav_url(value: str | None) -> str | None:
    """URL 归一：strip + 尾 "/" 去掉 + 空串归 None；非 http(s) 前缀抛 ValueError。"""
    if value is None:
        return None
    url = value.strip().rstrip("/")
    if not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"WebDAV 地址须以 http:// 或 https:// 开头，收到：{url}")
    return url


def update_sync_config(db: Session, patch) -> SyncSettings:
    """部分更新（入参为 schemas.SyncConfigUpdate）：归一 → 校验 → 落库；
    开启同步时调度首同步。返回更新后的行。"""
    row = get_sync_config(db)
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return row  # 空 patch 短路：不发 UPDATE 不误抬触发器（同 settings 惯例）

    # —— 归一 ——
    if "webdav_url" in data:
        data["webdav_url"] = _normalize_webdav_url(data["webdav_url"])
    if isinstance(data.get("webdav_username"), str):
        data["webdav_username"] = data["webdav_username"].strip() or None
    if isinstance(data.get("webdav_password"), str):
        data["webdav_password"] = data["webdav_password"].strip() or None
    if "auto_backup_dir" in data and data["auto_backup_dir"] is not None:
        raw = data["auto_backup_dir"].strip()
        if not raw:
            data["auto_backup_dir"] = None
        else:
            p = Path(raw)
            if not p.is_absolute():
                raise ValueError(f"备份目录须为绝对路径，收到：{raw}")
            if not p.is_dir():
                raise ValueError(f"备份目录不存在：{raw}")
            data["auto_backup_dir"] = str(p)

    # —— 校验：开启同步须凭证齐（URL 必填）——
    merged_url = data.get("webdav_url", row.webdav_url)
    if data.get("webdav_enabled", row.webdav_enabled) and not merged_url:
        raise ValueError("开启同步前须先填写 WebDAV 服务地址")

    # —— 凭证变更清探测结果 ——
    cred_changed = any(
        key in data and data[key] != getattr(row, key)
        for key in ("webdav_url", "webdav_username", "webdav_password"))
    if cred_changed:
        data["last_test_at"] = None
        data["last_test_ok"] = None
        data["last_test_ms"] = None

    # —— 落库（last_* 回显态不设 NOT NULL，白名单外键进不来：schema 层已拦）——
    turning_on = (data.get("webdav_enabled") is True and not row.webdav_enabled)
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    logger.info("更新同步备份配置：本次字段 %s", sorted(data))

    # —— 开启即调度首同步（局部导入防环：sync_engine 顶层不依赖本模块状态）——
    if turning_on:
        from services import sync_engine

        sync_engine.schedule_now()
        logger.info("WebDAV 同步已开启，首同步已调度")
    return row


def effective_backup_dir(cfg: SyncSettings | None = None) -> Path:
    """自动备份目录解析：配置值优先，空/无行 = 默认数据目录/backups。
    无参调用时自开会话读库（database.SessionLocal 动态查找——测试 monkeypatch
    与生产同路径）。"""
    if cfg is None:
        import database

        with database.SessionLocal() as db:
            cfg = get_sync_config(db)
    if not (cfg.auto_backup_dir or "").strip():
        from config import get_data_dir

        return get_data_dir() / "backups"
    return Path(cfg.auto_backup_dir)


def last_auto_backup_at(directory: Path | None = None) -> object:
    """最近一次自动备份时刻（glob clara-autobackup-*.zip 取 mtime 最大；
    目录不存在返回 None）——GET /api/sync 回显组装用。"""
    from datetime import datetime

    dir_path = directory or effective_backup_dir()
    if not dir_path.is_dir():
        return None
    files = list(dir_path.glob("clara-autobackup-*.zip"))
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files))
