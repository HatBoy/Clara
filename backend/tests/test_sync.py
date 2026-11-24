"""同步备份：配置端点 + 备份两件套（自关于页迁入，端点 /api/sync/*）+ 自动备份。

覆盖：sync_settings 单行 get_or_create / PATCH 白名单与归一校验（url 前缀、
目录存在性）/ 改凭证清探测结果 / GET 组装字段；备份导出全链（快照落 zip /
manifest 纳入 / 路径校验三拒 / 不含 tauri 目录）；恢复两段式（staging +
PENDING → apply 原子替换 + 清理 / 非法包全拒 / 手压 zip 多一层目录前缀
兼容）；自动备份（文件名格式 / 7 天清理红线不伤 pre-restore-* / 三槽认领
与开关）。
文件系统全隔离（test_about 原 _isolate_fs 模式）：DATABASE_PATH / get_data_dir
全部 monkeypatch 到 tmp_path。
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


def test_config_server_change_resets_sync_baseline(client, monkeypatch, tmp_path):
    """换 URL/账号必须清同步基线并清同步状态（2026-11-16 优化批 3）：
    否则新服务器缺的行被判「对端删除」，小表直接丢数据。"""
    from services import sync_engine

    monkeypatch.setattr(sync_engine, "get_data_dir", lambda: tmp_path)
    client.patch("/api/sync", json={"webdav_url": "https://dav-a.example.com"})
    (tmp_path / "sync_snapshot").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sync_snapshot" / "meta.json").write_text("{}", encoding="utf-8")
    assert sync_engine.has_base()

    # 换服务器
    res = client.patch("/api/sync", json={"webdav_url": "https://dav-b.example.com"})
    assert res.status_code == 200
    assert not sync_engine.has_base()
    assert res.json()["last_sync_ok"] is None


def test_config_password_only_keeps_baseline(client, monkeypatch, tmp_path):
    """仅密码变更（同服务器同账号）不清基线。"""
    from services import sync_engine

    monkeypatch.setattr(sync_engine, "get_data_dir", lambda: tmp_path)
    client.patch("/api/sync", json={"webdav_url": "https://dav-a.example.com"})
    (tmp_path / "sync_snapshot").mkdir(exist_ok=True)
    (tmp_path / "sync_snapshot" / "meta.json").write_text("{}", encoding="utf-8")
    client.patch("/api/sync", json={"webdav_password": "new-pass"})
    assert sync_engine.has_base()


def test_config_server_change_conflicts_with_running_sync(client, monkeypatch, tmp_path):
    """换址清基线与在途同步轮互斥（2026-11-17 二轮批 1）：锁被占时 409
    （reset-base 同口径），锁空闲照常清基线。"""
    from services import sync_engine

    monkeypatch.setattr(sync_engine, "get_data_dir", lambda: tmp_path)
    client.patch("/api/sync", json={"webdav_url": "https://dav-a.example.com"})
    (tmp_path / "sync_snapshot").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sync_snapshot" / "meta.json").write_text("{}", encoding="utf-8")

    with sync_engine._sync_lock:  # 模拟在途同步轮持锁
        res = client.patch("/api/sync",
                           json={"webdav_url": "https://dav-b.example.com"})
        assert res.status_code == 409
        assert sync_engine.has_base()  # 基线未动

    res = client.patch("/api/sync",
                       json={"webdav_url": "https://dav-b.example.com"})
    assert res.status_code == 200
    assert not sync_engine.has_base()


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
        # 2026-11-14 真机批：Tauri 偏好（窗口/布局/快捷键）是设备本地属性，
        # 备份不再携带 tauri/ 目录
        assert not any(n.startswith("tauri/") for n in names)
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


# —— 恢复的 sync_settings 对账（2026-11-17 二轮批 1）——————————————————————

_SYNC_DDL = (
    "CREATE TABLE IF NOT EXISTS sync_settings (id INTEGER PRIMARY KEY, "
    "webdav_enabled BOOLEAN, webdav_url TEXT, webdav_username TEXT, "
    "webdav_password TEXT, auto_backup_enabled BOOLEAN, auto_backup_dir TEXT, "
    "device_id TEXT, last_sync_at TEXT, last_sync_ok BOOLEAN, last_sync_error TEXT, "
    "last_test_at TEXT, last_test_ok BOOLEAN, last_test_ms INTEGER, "
    "created_at TEXT, updated_at TEXT)"
)


def _seed_sync_row(path, *, device, url="https://dav.example.com", user="u", pwd="p"):
    conn = sqlite3.connect(path)
    conn.execute(_SYNC_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO sync_settings (id, webdav_enabled, webdav_url, "
        "webdav_username, webdav_password, auto_backup_enabled, device_id, "
        "created_at, updated_at) "
        "VALUES (1, 1, ?, ?, ?, 1, ?, '2026-11-15 09:00:00', '2026-11-15 09:00:00')",
        (url, user, pwd, device))
    conn.commit()
    conn.close()


def _sync_field(path, col):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {col} FROM sync_settings WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def _sync_task_row() -> dict:
    """同步格式物化用的全列任务行（物化 INSERT 按全列绑定参数，缺键即报错；
    列集从模型元数据单源取，NOT NULL 列给值其余 None）。"""
    from database import Base

    filled = {"id": "t9", "title": "远端任务", "all_day": 0, "priority": 2,
              "sort_order": 0, "reschedule_count": 0, "ever_scheduled": 1,
              "is_deleted": 0, "created_at": "2026-11-15T09:00:00",
              "updated_at": "2026-11-15T09:00:00"}
    return {c: filled.get(c) for c in Base.metadata.tables["tasks"].columns.keys()}


def test_restore_keeps_local_device_id_and_backup_credentials(client, tmp_path):
    """备份格式恢复：device_id 恒保本机（防双机同署名）；凭证跟随备份。"""
    _make_db(tmp_path / "clara.db", rows=2)
    _seed_sync_row(tmp_path / "clara.db", device="dev-backup")
    src = _make_backup(tmp_path)
    # 本机当前：另一套凭证与署名
    _make_db(tmp_path / "clara.db", rows=1)
    _seed_sync_row(tmp_path / "clara.db", device="dev-local",
                   url="https://other.example.com")

    client.post("/api/sync/restore", json={"path": str(src)})
    assert backup_svc.apply_pending_restore() is True
    db = tmp_path / "clara.db"
    assert _sync_field(db, "device_id") == "dev-local"  # 署名保本机
    assert _sync_field(db, "webdav_url") == "https://dav.example.com"  # 凭证跟随备份


def test_restore_sync_format_keeps_local_sync_row(client, tmp_path):
    """同步格式恢复（物化库只有 SYNC_TABLES、sync_settings 空表）：本机行整行
    回写——此前恢复后 get_or_create 重建默认行，WebDAV 凭证/备份目录全丢须重配。"""
    import json as _json

    _make_db(tmp_path / "clara.db", rows=1)
    _seed_sync_row(tmp_path / "clara.db", device="dev-local",
                   url="https://dav.example.com")
    z = _zip_of(tmp_path, "sync.zip", {
        "manifest.json": _json.dumps({"app": "clara-sync", "format": 1}),
        "tables/tasks.json": _json.dumps({"table": "tasks", "rows": [_sync_task_row()]}),
    })
    res = client.post("/api/sync/restore", json={"path": str(z)})
    assert res.status_code == 200, res.text
    assert backup_svc.apply_pending_restore() is True
    db = tmp_path / "clara.db"
    assert _sync_field(db, "device_id") == "dev-local"
    assert _sync_field(db, "webdav_url") == "https://dav.example.com"
    assert _sync_field(db, "webdav_username") == "u"
    assert _titles(db) == ["远端任务"]  # 数据本体照常恢复


def test_restore_to_new_device_regenerates_device_id(client, tmp_path):
    """备份恢复到新设备（本机无 sync_settings）：备份凭证照收免重配，但
    device_id 重新生成（绝不能双机同署名）。"""
    _make_db(tmp_path / "clara.db", rows=2)
    _seed_sync_row(tmp_path / "clara.db", device="dev-backup")
    src = _make_backup(tmp_path)
    _make_db(tmp_path / "clara.db", rows=1)
    conn = sqlite3.connect(tmp_path / "clara.db")  # _make_db 不清残留表：显式去表
    conn.execute("DROP TABLE IF EXISTS sync_settings")
    conn.commit()
    conn.close()

    client.post("/api/sync/restore", json={"path": str(src)})
    assert backup_svc.apply_pending_restore() is True
    db = tmp_path / "clara.db"
    assert _sync_field(db, "device_id") != "dev-backup"  # 新署名
    assert _sync_field(db, "webdav_url") == "https://dav.example.com"  # 凭证照收


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


def test_restore_accepts_sync_zip_with_extra_dir_prefix(client, tmp_path):
    """手压 zip 常见形态：右键压缩 clara-sync 文件夹 → 成员多一层目录前缀。
    覆盖恢复与合并导入都必须识别（用户实测反馈的可用性坑）。"""
    members = {
        "clara-sync/manifest.json": json.dumps({"app": "clara-sync", "format": 1}),
        "clara-sync/tables/app_settings.json": json.dumps(
            {"table": "app_settings", "rows": []}),
        "clara-sync/": b"",  # 目录占位条目（部分压缩工具会写）
    }
    path = _zip_of_raw(tmp_path, "wrapped.zip", members)
    res = client.post("/api/sync/restore", json={"path": str(path)})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    staging = tmp_path / "restore_staging"
    assert (staging / "clara.db").is_file()  # 物化成功

    # 合并导入同口径
    res = client.post("/api/sync/merge-analyze", json={"path": str(path)})
    assert res.status_code == 200
    assert res.json()["new_count"] == 0  # 空行集：仅验证解析通过


def _zip_of_raw(tmp_path, name, members: dict):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in members.items():
            if member.endswith("/"):
                zf.writestr(zipfile.ZipInfo(member), b"")  # 目录条目
            elif isinstance(data, str):
                zf.writestr(member, data.encode("utf-8"))
            else:
                zf.writestr(member, data)
    return path


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
    # 旧文件 mtime 也拨回 8 天前（双保险——文件名已是主判据）；安全副本与用户
    # 文件的 mtime 同样拨老，回归"不匹配文件名回退 mtime 删除"的旧缺陷
    stale = datetime.now() - timedelta(days=8)
    import os
    for f in (old, safe, foreign):
        os.utime(f, (stale.timestamp(), stale.timestamp()))

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
        # 补领截止窗 2 小时（二轮批 1）：20:30 距晚槽 150 分钟超窗不补领
        #（旧版晚开机 60s tick 逐槽连发三份全量 zip）
        assert workflows._claim_autobackup(db, datetime(2026, 11, 15, 20, 30)) is False
        # 19:50 距晚槽 110 分钟在窗内 → 补领晚槽
        assert workflows._claim_autobackup(db, datetime(2026, 11, 15, 19, 50)) is True


# —— 批 3：危险操作与同步互斥 / 清空标记版本 ——————————————————————————


def test_dangerous_endpoints_409_while_syncing(client):
    """同步进行中，重置基线/恢复/合并导入一律 409（2026-11-16 优化批 3）。"""
    from services import sync_engine

    assert sync_engine._sync_lock.acquire(blocking=False)
    try:
        assert client.post("/api/sync/reset-base").status_code == 409
        assert client.post("/api/sync/restore",
                           json={"path": "不存在.zip"}).status_code == 409
        assert client.post("/api/sync/merge-apply",
                           json={"path": "不存在.zip", "strategy": "overwrite"}).status_code == 409
    finally:
        sync_engine._sync_lock.release()


def test_cleared_marker_is_versioned(monkeypatch, tmp_path):
    """同步轮只消耗本轮开始前的标记版本；期间新写入的标记保留（优化批 3）。"""
    from services import sync_engine

    monkeypatch.setattr(sync_engine, "get_data_dir", lambda: tmp_path)
    sync_engine.note_table_cleared("messages")
    state = sync_engine._read_cleared_marker()
    v1 = state["version"]
    assert state["tables"] == ["messages"]
    # 模拟同步期间用户又 /clear 一次 → 版本 +1
    sync_engine.note_table_cleared("stats_messages")
    sync_engine._consume_cleared_marker(v1)  # 旧版本号：不消耗
    assert "stats_messages" in sync_engine._load_cleared_tables()
    # 本轮开始读到的是最新版本 → 消耗
    sync_engine._consume_cleared_marker(sync_engine._read_cleared_marker()["version"])
    assert sync_engine._load_cleared_tables() == set()


def test_create_backup_atomic_and_no_temp_leftovers(tmp_path, monkeypatch):
    """备份先写 .tmp 再原子替换，成功后不留临时文件（优化批 3）。"""
    from services import backup as backup_svc

    monkeypatch.setattr(backup_svc, "get_data_dir", lambda: tmp_path)
    dest = tmp_path / "b.zip"
    backup_svc.create_backup(str(dest))
    assert dest.is_file()
    assert not list(tmp_path.glob("backup-tmp-*"))
    assert not list(tmp_path.glob("*.tmp"))


def test_create_backup_failure_leaves_no_half_zip(tmp_path, monkeypatch):
    """失败时目标 zip 不出现（不会留下名字合法的半截文件）。"""
    from services import backup as backup_svc

    monkeypatch.setattr(backup_svc, "get_data_dir", lambda: tmp_path)

    def boom(path):
        raise OSError("磁盘满")

    monkeypatch.setattr(backup_svc, "_snapshot_db_to", boom)
    dest = tmp_path / "b.zip"
    with pytest.raises(OSError):
        backup_svc.create_backup(str(dest))
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_draft_destination_does_not_inherit_credentials(client, monkeypatch):
    from routers import sync
    seen = []
    class Probe:
        def __init__(self, *args):
            seen.append(args)
        def test_connection(self):
            return True, 1, '测试替身'
        def close(self):
            pass
    monkeypatch.setattr(sync, 'WebDavClient', Probe)
    client.patch('/api/sync', json={'webdav_url': 'https://a.example',
                 'webdav_username': 'u', 'webdav_password': '假密码'})
    assert client.post('/api/sync/test', json={'url': 'https://b.example'}).is_success
    assert seen == [('https://b.example', None, None)]
    assert client.get('/api/sync').json()['last_test_ok'] is None
    client.patch('/api/sync', json={'webdav_url': 'https://b.example'})
    client.post('/api/sync/test', json={})
    assert seen[-1][-1] is None


def test_restore_interruption_keeps_old_db_and_retryable_marker(tmp_path, monkeypatch):
    src = _make_backup(tmp_path)
    _make_db(tmp_path / 'clara.db', rows=2)
    backup_svc.prepare_restore(str(src))
    real_replace = backup_svc.os.replace
    def fail_publish(source, target):
        if target == backup_svc.DATABASE_PATH:
            raise OSError('故障注入')
        real_replace(source, target)
    with monkeypatch.context() as m:
        m.setattr(backup_svc.os, 'replace', fail_publish)
        with pytest.raises(OSError, match='故障注入'):
            backup_svc.apply_pending_restore()
    assert _titles(tmp_path / 'clara.db') == ['任务0', '任务1']
    assert backup_svc.apply_pending_restore()
    assert _titles(tmp_path / 'clara.db') == ['任务0']


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_restore_process_death_is_retryable(tmp_path, phase):
    """切换边界前后强退子进程；不运行清理钩子，不影响应用默认数据目录。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    archive = _make_backup(tmp_path)
    _make_db(tmp_path / 'clara.db', rows=2)
    backup_svc.prepare_restore(str(archive))
    env = {**os.environ, 'CLARA_DATA_DIR': str(tmp_path), 'PYTHONUTF8':'1'}
    # 先制造未检查点的已提交 WAL，再模拟应用进程终止。
    def run(code):
        return subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                              env=env, timeout=45, capture_output=True)
    interrupted_writer = run('import os,sqlite3; from config import DATABASE_PATH; '
                             'db=sqlite3.connect(DATABASE_PATH); db.execute("pragma journal_mode=wal"); '
                             'db.execute("insert into tasks values (?, ?)", ("wal", "未检查点")); '
                             'db.commit(); os._exit(73)')
    assert interrupted_writer.returncode == 73, interrupted_writer.stderr
    crash = run('''import os
from services import backup
from database import Base
import models
original = os.replace
def stop(source, target):
    if target == backup.DATABASE_PATH:
        if PHASE == 'after': original(source, target)
        os._exit(74)
    return original(source, target)
os.replace = stop
backup.apply_pending_restore()
'''.replace('PHASE', repr(phase)))
    assert crash.returncode == 74, crash.stderr
    assert (tmp_path / 'restore_staging' / backup_svc._PENDING_FLAG).is_file()
    assert _titles(tmp_path / 'clara.db') == (['任务0'] if phase == 'after' else sorted(['任务0','任务1','未检查点']))
    recovered = run('import models; from services.backup import apply_pending_restore; assert apply_pending_restore(); assert not apply_pending_restore()')
    assert recovered.returncode == 0, recovered.stderr
    assert _titles(tmp_path / 'clara.db') == ['任务0']
