"""同步备份：配置端点 + 备份两件套（自关于页迁入，端点 /api/sync/*）+ 自动备份。

覆盖：sync_settings 单行 get_or_create / PATCH 白名单与归一校验（url 前缀、
目录存在性）/ 改凭证清探测结果 / GET 组装字段；备份导出全链（快照落 zip /
manifest 纳入 / 路径校验三拒）；恢复两段式（staging + PENDING → apply 原子
替换 + 清理 / 非法包全拒）；自动备份（文件名格式 / 7 天清理红线不伤
pre-restore-* / 三槽认领与开关）。
文件系统全隔离（test_about 原 _isolate_fs 模式）：DATABASE_PATH / get_data_dir
/ Tauri 探测全部 monkeypatch 到 tmp_path。
"""

import json
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest

from services import backup as backup_svc
from services.sync_config import effective_backup_dir

# —— 文件系统隔离（备份域）————————————————————————————————————————


@pytest.fixture(autouse=True)
def _isolate_fs(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_svc, "DATABASE_PATH", tmp_path / "clara.db")
    monkeypatch.setattr(backup_svc, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(backup_svc, "_tauri_config_dir", lambda: None)
    _make_db(tmp_path / "clara.db")
    yield


def _make_db(path, rows=1):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT)")
        for i in range(rows):
            conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (f"t{i}", f"任务{i}"))
        conn.commit()
    finally:
        conn.close()


def _titles(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT title FROM tasks"))
    finally:
        conn.close()


# —— 配置端点（GET/PATCH /api/sync）————————————————————————————————


def test_config_get_or_create_with_defaults(client):
    res = client.get("/api/sync")
    assert res.status_code == 200
    body = res.json()
    assert body["webdav_enabled"] is False
    assert body["auto_backup_enabled"] is True  # 默认开（2026-11-14 需求）
    assert body["device_id"]  # get_or_create 生成
    assert body["syncing"] is False
    assert body["auto_backup_dir_resolved"]  # 组装字段：默认目录解析


def test_config_patch_partial_and_normalization(client):
    res = client.patch("/api/sync", json={"webdav_url": "https://dav.example.com/dav/"})
    assert res.status_code == 200
    assert res.json()["webdav_url"] == "https://dav.example.com/dav"  # 尾 / 已剥

    # 空串归 None（停用态）
    res = client.patch("/api/sync", json={"webdav_url": "  "})
    assert res.status_code == 200
    assert res.json()["webdav_url"] is None


def test_config_patch_rejects_bad_url(client):
    res = client.patch("/api/sync", json={"webdav_url": "ftp://x.com"})
    assert res.status_code == 422


def test_config_patch_rejects_missing_dir(client, tmp_path):
    res = client.patch("/api/sync", json={"auto_backup_dir": str(tmp_path / "不存在")})
    assert res.status_code == 422
    # 合法目录可设
    res = client.patch("/api/sync", json={"auto_backup_dir": str(tmp_path)})
    assert res.status_code == 200
    assert res.json()["auto_backup_dir"] == str(tmp_path)
    assert effective_backup_dir() == tmp_path


def test_config_credential_change_clears_last_test(client):
    client.patch("/api/sync", json={"webdav_url": "https://dav.x.com"})
    from conftest import TestingSessionLocal
    from models import SyncSettings

    with TestingSessionLocal() as db:
        row = db.get(SyncSettings, 1)
        row.last_test_ok, row.last_test_ms = True, 42
        db.commit()
    res = client.patch("/api/sync", json={"webdav_password": "新密码"})
    assert res.status_code == 200
    assert res.json()["last_test_ok"] is None  # 凭证变更即清探测结果
    assert res.json()["last_test_ms"] is None


def test_config_enable_requires_url(client):
    res = client.patch("/api/sync", json={"webdav_enabled": True})
    assert res.status_code == 422
    assert "地址" in res.json()["detail"]


# —— 备份导出（POST /api/sync/backup；自 /api/about/backup 迁入）——————


def test_backup_creates_zip(client, tmp_path):
    dest = tmp_path / "out" / "clara-backup.zip"
    dest.parent.mkdir()
    res = client.post("/api/sync/backup", json={"path": str(dest), "version": "v1.0.0"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert dest.is_file() and dest.stat().st_size > 0
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "clara.db"} <= names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["app"] == "clara" and manifest["version"] == "v1.0.0"
        probe = tmp_path / "probe.db"
        probe.write_bytes(zf.read("clara.db"))
        assert _titles(probe) == ["任务0"]


def test_backup_includes_config_snapshot(client, tmp_path):
    (tmp_path / "config_snapshot.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "b.zip"
    res = client.post("/api/sync/backup", json={"path": str(dest)})
    assert res.status_code == 200
    with zipfile.ZipFile(dest) as zf:
        assert "config_snapshot.json" in zf.namelist()


@pytest.mark.parametrize(
    "path", ["relative/no.zip", "C:\\tmp\\backup.txt", "Z:\\不存在目录\\b.zip"]
)
def test_backup_rejects_bad_paths(client, path):
    res = client.post("/api/sync/backup", json={"path": path})
    assert res.status_code == 422


# —— 恢复两段式（POST /api/sync/restore + apply_pending_restore）——————


def _make_backup(tmp_path, name="src.zip"):
    dest = tmp_path / name
    out = backup_svc.create_backup(str(dest))
    assert out["ok"] is True
    return dest


def test_restore_two_phase_roundtrip(client, tmp_path):
    _make_db(tmp_path / "clara.db", rows=2)
    src = _make_backup(tmp_path)
    _make_db(tmp_path / "clara.db", rows=1)

    res = client.post("/api/sync/restore", json={"path": str(src)})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["requires_restart"] is True
    staging = tmp_path / "restore_staging"
    assert (staging / "PENDING.restore").is_file()
    assert list((tmp_path / "backups").glob("pre-restore-*.db"))

    assert backup_svc.apply_pending_restore() is True
    assert _titles(tmp_path / "clara.db") == ["任务0", "任务1"]
    assert not staging.exists()


def test_apply_without_pending_is_noop(tmp_path):
    assert backup_svc.apply_pending_restore() is False


def _zip_of(tmp_path, name, members: dict):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return path


def _db_bytes(tmp_path, rows=1):
    p = tmp_path / "_seed_db.sqlite"
    _make_db(p, rows)
    return p.read_bytes()


def test_restore_rejects_non_zip(client, tmp_path):
    fake = tmp_path / "fake.zip"
    fake.write_text("not a zip", encoding="utf-8")
    res = client.post("/api/sync/restore", json={"path": str(fake)})
    assert res.status_code == 422


def test_restore_rejects_missing_db(client, tmp_path):
    path = _zip_of(tmp_path, "nodb.zip", {"manifest.json": json.dumps({"app": "clara"})})
    res = client.post("/api/sync/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "缺少数据库" in res.json()["detail"]


def test_restore_rejects_illegal_member(client, tmp_path):
    path = _zip_of(
        tmp_path, "evil.zip",
        {"manifest.json": json.dumps({"app": "clara"}), "../evil.py": "x"},
    )
    res = client.post("/api/sync/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "未知成员" in res.json()["detail"]


def test_restore_rejects_foreign_manifest(client, tmp_path):
    path = _zip_of(
        tmp_path, "other.zip",
        {"manifest.json": json.dumps({"app": "other"}), "clara.db": _db_bytes(tmp_path)},
    )
    res = client.post("/api/sync/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "Clara" in res.json()["detail"]


def test_restore_rejects_corrupt_db(client, tmp_path):
    path = _zip_of(
        tmp_path, "corrupt.zip",
        {"manifest.json": json.dumps({"app": "clara"}), "clara.db": b"garbage"},
    )
    res = client.post("/api/sync/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert not (tmp_path / "restore_staging").exists()


# —— 自动备份（services/backup.py auto_backup / prune；槽位在 test 层直测谓词）——


def test_auto_backup_names_and_manifest(client, tmp_path):
    out = backup_svc.auto_backup(str(tmp_path / "auto"))
    assert out["ok"] is True
    files = list((tmp_path / "auto").glob("clara-autobackup-*.zip"))
    assert len(files) == 1
    # 文件名时间戳格式
    import re
    assert re.match(r"^clara-autobackup-\d{8}-\d{6}\.zip$", files[0].name)
    # 兼容性①：产物可直接被恢复功能消费（manifest.app=="clara"）
    with zipfile.ZipFile(files[0]) as zf:
        assert json.loads(zf.read("manifest.json"))["app"] == "clara"


def test_prune_keeps_week_and_spares_others(tmp_path, monkeypatch):
    """红线：只删 clara-autobackup-*.zip 严格匹配的过期文件——pre-restore-*.db
    安全副本与用户自放文件绝不能被波及。"""
    old = tmp_path / "clara-autobackup-20260101-080000.zip"
    old.write_bytes(b"old")
    new = tmp_path / f"clara-autobackup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    new.write_bytes(b"new")
    safe = tmp_path / "pre-restore-20260101-000000.db"
    safe.write_bytes(b"safe")
    foreign = tmp_path / "我的备份.zip"
    foreign.write_bytes(b"mine")
    # 旧文件 mtime 也拨回 8 天前（双保险——文件名已是主判据）
    stale = datetime.now() - timedelta(days=8)
    import os
    os.utime(old, (stale.timestamp(), stale.timestamp()))

    removed = backup_svc.prune_auto_backups(str(tmp_path))
    assert removed == 1
    assert not old.exists()
    assert new.exists() and safe.exists() and foreign.exists()


def test_claim_autobackup_three_slots(monkeypatch):
    """三槽认领 + 开关关闭不落行（谓词直测——不依赖时钟等待）。"""
    from services import workflows
    from services.sync_config import get_sync_config
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        cfg.auto_backup_enabled = False
        db.commit()
        # 关开关：任何时刻不认领
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 20, 0)) is False

        cfg.auto_backup_enabled = True
        db.commit()
        # 07:59 三槽全未到
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 7, 59)) is False
        # 08:00 认领首槽
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 8, 0)) is True
        # 同日再判：首槽已占，12:00 前无新槽
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 9, 30)) is False
        # 12:00 认领次槽
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 12, 0)) is True
        # 18:00 第三槽
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 18, 0)) is True
        # 晚间补账：21:00 三槽全占，不再认领
        assert workflows._claim_autobackup(db, datetime(2026, 11, 14, 21, 0)) is False
        # 次日 08:00 重新认领
        assert workflows._claim_autobackup(db, datetime(2026, 11, 15, 8, 0)) is True
