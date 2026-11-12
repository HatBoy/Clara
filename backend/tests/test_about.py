"""关于页两件套测试（routers/about.py + services/update_check.py + services/backup.py）。

覆盖：更新检查四态（成功/404 预期态/网络失败/响应缺 tag）；备份导出全链
（快照落 zip / manifest 纳入 / 路径校验三拒）；恢复两段式（staging 落盘 +
PENDING → apply 原子替换 + 清理 / 五种非法包全拒 / 无标记 no-op）。
数据库与数据目录全部 monkeypatch 到 tmp_path，零触碰真实 dev 库与
%APPDATA%；Tauri 目录探测恒 None 隔离开发机环境。
"""

import json
import sqlite3
import zipfile

import pytest

from services import backup as backup_svc
from services import update_check


@pytest.fixture(autouse=True)
def _isolate_fs(monkeypatch, tmp_path):
    """备份域文件系统全隔离：DATABASE_PATH / get_data_dir / Tauri 目录探测
    全部指到 tmp_path（Tauri 探测恒 None——开发机真有 com.hat.clara，不隔离
    会把真 shortcuts.json 打进测试备份）。"""
    monkeypatch.setattr(backup_svc, "DATABASE_PATH", tmp_path / "clara.db")
    monkeypatch.setattr(backup_svc, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(backup_svc, "_tauri_config_dir", lambda: None)
    _make_db(tmp_path / "clara.db")
    yield


def _make_db(path, rows=1):
    """建最小库：tasks 表 + rows 行数据（备份/恢复断言载体）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT)"
        )
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


# —— 检查更新（services/update_check.py；网络替身 _fetch_release，零真实外呼）——


def test_update_check_ok(monkeypatch):
    monkeypatch.setattr(
        update_check, "_fetch_release", lambda: (200, {"tag_name": "v1.0.1", "html_url": "u"})
    )
    out = update_check.latest_release()
    assert out == {"available": True, "tag": "v1.0.1", "html_url": "u"}


def test_update_check_404_is_expected_state(monkeypatch):
    """仓库未上传 / 无 Release——预期态而非错误。"""
    monkeypatch.setattr(update_check, "_fetch_release", lambda: (404, {}))
    out = update_check.latest_release()
    assert out["available"] is False
    assert "暂无发布" in out["reason"]


def test_update_check_network_error(monkeypatch):
    import httpx

    def boom():
        raise httpx.ConnectError("网络不可达（测试注入）")

    monkeypatch.setattr(update_check, "_fetch_release", boom)
    out = update_check.latest_release()
    assert out["available"] is False
    assert "网络" in out["reason"]


def test_update_check_missing_tag(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_release", lambda: (200, {"html_url": "u"}))
    out = update_check.latest_release()
    assert out["available"] is False
    assert "版本号" in out["reason"]


# —— 备份导出（POST /api/about/backup）——


def test_backup_creates_zip(client, tmp_path):
    dest = tmp_path / "out" / "clara-backup.zip"
    dest.parent.mkdir()
    res = client.post(
        "/api/about/backup", json={"path": str(dest), "version": "v1.0.0"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert dest.is_file() and dest.stat().st_size > 0
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "clara.db"} <= names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["app"] == "clara" and manifest["version"] == "v1.0.0"
        # 快照可独立打开且含行数据（backup API 产物，非运行中直拷）
        _extract_to = tmp_path / "probe.db"
        _extract_to.write_bytes(zf.read("clara.db"))
        assert _titles(_extract_to) == ["任务0"]


def test_backup_includes_config_snapshot(client, tmp_path):
    (tmp_path / "config_snapshot.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "b.zip"
    res = client.post("/api/about/backup", json={"path": str(dest)})
    assert res.status_code == 200
    with zipfile.ZipFile(dest) as zf:
        assert "config_snapshot.json" in zf.namelist()


@pytest.mark.parametrize(
    "path",
    ["relative/no.zip", "C:\\tmp\\backup.txt", "Z:\\不存在目录\\b.zip"],
)
def test_backup_rejects_bad_paths(client, path):
    res = client.post("/api/about/backup", json={"path": path})
    assert res.status_code == 422


# —— 恢复两段式（POST /api/about/restore + apply_pending_restore）——


def _make_backup(tmp_path, name="src.zip"):
    """直接调 service 造备份产物（路由层已有独立覆盖；此处只需产物）。"""
    dest = tmp_path / name
    out = backup_svc.create_backup(str(dest))
    assert out["ok"] is True
    return dest


def test_restore_two_phase_roundtrip(client, tmp_path):
    """导出（2 行数据）→ 改库（1 行）→ 恢复 → 库回到 2 行、staging 清空。"""
    _make_db(tmp_path / "clara.db", rows=2)
    src = _make_backup(tmp_path)
    _make_db(tmp_path / "clara.db", rows=1)  # 模拟备份后又变了

    res = client.post("/api/about/restore", json={"path": str(src)})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["requires_restart"] is True
    staging = tmp_path / "restore_staging"
    assert (staging / "PENDING.restore").is_file()
    # 安全体已落（恢复失败找回出口）
    assert list((tmp_path / "backups").glob("pre-restore-*.db"))

    # 第二阶段：新进程视角——此刻"旧库"只是磁盘文件（测试里无活动连接）
    assert backup_svc.apply_pending_restore() is True
    assert _titles(tmp_path / "clara.db") == ["任务0", "任务1"]
    assert not staging.exists()


def test_apply_without_pending_is_noop(tmp_path):
    assert backup_svc.apply_pending_restore() is False
    assert not (tmp_path / "restore_staging").exists()


def _zip_of(tmp_path, name, members: dict):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return path


def _db_bytes(tmp_path, rows=1):
    """借临时文件造一个合法 sqlite 库的字节流（connect 不收内存流）。"""
    p = tmp_path / "_seed_db.sqlite"
    _make_db(p, rows)
    return p.read_bytes()


def test_restore_rejects_non_zip(client, tmp_path):
    fake = tmp_path / "fake.zip"
    fake.write_text("not a zip", encoding="utf-8")
    res = client.post("/api/about/restore", json={"path": str(fake)})
    assert res.status_code == 422


def test_restore_rejects_missing_db(client, tmp_path):
    path = _zip_of(
        tmp_path, "nodb.zip", {"manifest.json": json.dumps({"app": "clara"})}
    )
    res = client.post("/api/about/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "缺少数据库" in res.json()["detail"]


def test_restore_rejects_illegal_member(client, tmp_path):
    """白名单外成员（路径穿越载荷）整包拒收。"""
    path = _zip_of(
        tmp_path,
        "evil.zip",
        {"manifest.json": json.dumps({"app": "clara"}), "../evil.py": "x"},
    )
    res = client.post("/api/about/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "未知成员" in res.json()["detail"]


def test_restore_rejects_foreign_manifest(client, tmp_path):
    path = _zip_of(
        tmp_path,
        "other.zip",
        {"manifest.json": json.dumps({"app": "other"}), "clara.db": _db_bytes(tmp_path)},
    )
    res = client.post("/api/about/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert "Clara" in res.json()["detail"]


def test_restore_rejects_corrupt_db(client, tmp_path):
    path = _zip_of(
        tmp_path,
        "corrupt.zip",
        {"manifest.json": json.dumps({"app": "clara"}), "clara.db": b"garbage"},
    )
    res = client.post("/api/about/restore", json={"path": str(path)})
    assert res.status_code == 422
    assert not (tmp_path / "restore_staging").exists()  # 脏 staging 已回滚
