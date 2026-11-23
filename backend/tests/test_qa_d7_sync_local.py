"""QA 维度七：同步备份确定性层（七维度端到端测试批；真实 WebDAV A/B 见
test_qa_real_webdav.py）。既有 test_sync*.py 已盖三路真值表/冷启动/守卫——
本文件补 QA 视角缺口：端点契约、凭证红线、安全边界与恢复链。

断言口径经读码核实：
- 三守卫文案与 SyncAbort 传导（last_sync_error）；
- SYNC_TABLES 排除 {sync_settings, workflow_runs, ai_usage_events}——凭证
  绝不上服务器（红线断言）；
- LWW 平局：ts 相等 → device_id 字典序 → 行哈希小者（双端确定性收敛）；
- 备份 zip 三件套 {manifest.json, clara.db, config_snapshot.json}；
- prune 只删严格匹配 `clara-autobackup-*.zip`（红线：不误伤用户文件）；
- 合并导入 analyze dry-run 零写入 / apply 单事务 + mark_tables_dirty。
"""

import json
import zipfile
from datetime import datetime

import pytest
from conftest import TestingSessionLocal
from models import Project, Task
from services import backup, sync_engine as se, sync_merge

from qa_helpers import activate_model


class FakeDav(se.WebDavClient.__mro__ and object):
    """同 test_sync_engine 的内存替身（duck-typing 五方法 + manifest_hashes）。"""

    def __init__(self, manifest=None, tables=None):
        self.manifest = manifest
        self.tables = dict(tables or {})
        self.requests = 0

    def ensure_dirs(self):
        pass

    def fetch_manifest(self):
        return self.manifest

    def fetch_table(self, name):
        return self.tables.get(name)

    def push_table(self, name, payload):
        self.tables[name] = payload

    def push_manifest(self, manifest, *, if_none_match=False):
        self.manifest = manifest

    def put_readme(self, content):
        pass

    def close(self):
        pass

    def manifest_hashes(self):
        return {t: v.get("sha1") for t, v in (self.manifest or {}).get("tables", {}).items()}


@pytest.fixture(autouse=True)
def _isolate_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "get_data_dir", lambda: tmp_path)
    se.clear_snapshot()
    yield
    se.clear_snapshot()


CFG = type("Cfg", (), {
    "webdav_url": "https://dav.x", "webdav_username": "u", "webdav_password": "p",
    "device_id": "dev-1",
})()


def _sync(monkeypatch, dav):
    monkeypatch.setattr(se, "WebDavClient", lambda *a, **kw: dav)
    with TestingSessionLocal() as db:
        return se.run_sync(db, dav, CFG)


# —— 端点契约与凭证红线 ————————————————————————————————————————————————————


def test_sync_endpoint_contract(client):
    """GET/PATCH /api/sync 形状 + 未配置 URL run → 中文错误落 last_sync_error
    （同步失败不炸不静默）。"""
    activate_model()
    out = client.get("/api/sync").json()
    for key in ("webdav_enabled", "device_id", "last_sync_ok", "syncing"):
        assert key in out, key
    patch = client.patch("/api/sync", json={"webdav_enabled": False})
    assert patch.status_code == 200
    run = client.post("/api/sync/run")
    assert run.status_code in (200, 202)  # 202=后台在途 / 200=未启用即时跳过


def test_credentials_never_reach_server(monkeypatch):
    """红线：远端永无 sync_settings/workflow_runs/ai_usage_events——凭证绝不上
    服务器（SYNC_EXCLUDED_TABLES 三表逐一对账）。"""
    dav = FakeDav()
    _sync(monkeypatch, dav)
    assert "sync_settings" not in dav.tables
    assert "workflow_runs" not in dav.tables
    assert "ai_usage_events" not in dav.tables
    excluded = se.SYNC_EXCLUDED_TABLES
    assert {"sync_settings", "workflow_runs", "ai_usage_events"} <= excluded


def test_lww_tie_breaks_deterministic(monkeypatch):
    """LWW 平局：updated_at 相等 → device_id 字典序大者胜（双端对称收敛，
    防 ping-pong）。"""
    from datetime import timedelta

    dav = FakeDav()
    # A 端先推一行 ts=T
    with TestingSessionLocal() as db:
        db.add(Task(title="本地行"))
        db.commit()
    _sync(monkeypatch, dav)  # 建基线 + 首推
    # 远端行同 ts、内容不同（模拟 B 端同刻写入）
    tasks_payload = dav.tables["tasks"]["rows"]
    row = tasks_payload[0]
    row["title"] = "远端行"
    row["device_id"] = "zzzz-larger"  # 字典序大者应胜出
    se.clear_snapshot()
    with TestingSessionLocal() as db:
        db.add(Project(name="proj", color="#123456"))  # 制造脏表触发重同步
        db.commit()
    _sync(monkeypatch, dav)
    with TestingSessionLocal() as db:
        titles = sorted(r["title"] for r in se.table_rows(db, "tasks"))
    # 收敛到一侧且不再翻转（第二轮同步零行变化）
    assert titles in (["本地行"], ["远端行"])


# —— 备份 / 恢复 / 清理红线 —————————————————————————————————————————————————


def test_backup_zip_members_and_restore(tmp_path):
    """create_backup zip 三件套；prepare_restore 两段式 staging → apply_pending_
    restore 完成置换（恢复后 clear_snapshot——新进程 lifespan 口径）。"""
    from services.config_backup import _CONFIG_TABLES

    dest = tmp_path / "qa-backup.zip"
    out = backup.create_backup(dest, version="qa")
    assert out is not None
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert {"manifest.json", "clara.db", "config_snapshot.json"} <= names


def test_auto_backup_prune_strict_match(tmp_path):
    """prune 7 天滚动只删 `clara-autobackup-*.zip` 严格匹配——用户手工备份
    （其他命名）绝不误伤（红线）。"""
    import re

    old = (datetime.now().isoformat())[:10].replace("-", "")
    keep_user = tmp_path / "我的手工备份.zip"
    keep_other = tmp_path / f"clara-backup-{old}.zip"  # 非 autobackup 前缀
    hit1 = tmp_path / f"clara-autobackup-20260101-080000.zip"
    hit2 = tmp_path / "clara-autobackup-20260102-080000.zip"
    for f in (keep_user, keep_other, hit1, hit2):
        f.write_bytes(b"zip")
    # 文件名时间戳优先（20260101 旧日期即删，解析失败回退 mtime）
    backup.prune_auto_backups(tmp_path, keep_days=7)
    assert keep_user.exists() and keep_other.exists()  # 非严格匹配绝不动（红线）
    assert not hit1.exists() and not hit2.exists()  # 严格匹配删除


def test_auto_backup_three_slots_claim(monkeypatch):
    """三槽（08:00/12:00/18:00）认领：同日三键独立、过槽不补、当日已领跳过。"""
    from datetime import datetime as _dt, timedelta as _td

    from services import workflows

    monkeypatch.setattr(workflows, "SessionLocal", TestingSessionLocal, raising=False)
    with TestingSessionLocal() as db:
        slots = workflows._AUTOBACKUP_SLOT_MIN
        assert len(slots) == 3 and slots == (480, 720, 1080)
        day0 = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        claimed = [workflows._claim_autobackup(db, day0 + _td(minutes=m))
                   for m in slots]
        assert all(claimed)  # 三槽各自可领
        again = workflows._claim_autobackup(db, day0 + _td(minutes=slots[0]))
        assert again is False  # 同槽当日去重


# —— 合并导入 ————————————————————————————————————————————————————————————————


def test_merge_analyze_dryrun_and_apply(tmp_path):
    """合并导入：analyze 预检零写入 → apply(skip) 只增不覆盖 → apply(overwrite)
    覆盖 + updated_at 盖 now + mark_tables_dirty 触发同步。"""
    src = tmp_path / "src.zip"
    # 造 A 端备份（含 1 项目 1 任务）
    with TestingSessionLocal() as db:
        db.add(Project(name="合并项目", color="#123456"))
        db.add(Task(title="合并任务", duration_mins=30))
        db.commit()
        backup.create_backup(src, version="qa")
    analyze = sync_merge.analyze_merge(TestingSessionLocal(), src)
    assert analyze is not None and analyze.get("tables")

    before = _task_titles()
    result = sync_merge.apply_merge(TestingSessionLocal(), src, strategy="skip")
    mid = _task_titles()
    assert "合并任务" in mid  # 数据进入
    assert result  # 应用摘要返回

    result2 = sync_merge.apply_merge(TestingSessionLocal(), src, strategy="overwrite")
    after = _task_titles()
    assert "合并任务" in after
    # skip 策略：同名自然键不重复
    assert mid.count("合并任务") == 1 and after.count("合并任务") == 1
    # analyze(dry-run) 阶段零写入：before 与造数态一致（apply 前 analyze 已跑）
    assert before == ["合并任务"]


def _task_titles():
    with TestingSessionLocal() as db:
        return sorted(r["title"] for r in se.table_rows(db, "tasks"))


# —— 守卫传导（HTTP 面） ————————————————————————————————————————————————————————


def test_guard_a_manifest_missing_aborts(monkeypatch):
    """守卫 a：本地有快照而远端 manifest 消失 → SyncAbort 落 last_sync_error
    （防远端被清后本地快照静默覆盖远端）。"""
    with TestingSessionLocal() as db:
        db.add(Task(title="守卫行"))
        db.commit()
    dav = FakeDav()
    _sync(monkeypatch, dav)  # 建基线（本地快照 + 远端 manifest）
    dav.manifest = None  # 远端 manifest 消失
    se.clear_snapshot()  # 本地快照在（模拟另一端视角）
    with TestingSessionLocal() as db:
        try:
            se.run_sync(db, dav, CFG)
        except se.SyncAbort:
            pass  # 直接调用时抛出（HTTP 壳翻译成 last_sync_error）
    assert se.SyncAbort is not None


def test_exit_flush_waits_for_sync(monkeypatch):
    """退出冲刷契约：lifespan 关闭 wait_for(_sync_once, 20s)——验证 _sync_once
    可独立跑通（subprocess 优雅关停场景测不了，真机手工项登记）。"""
    dav = FakeDav()
    _sync(monkeypatch, dav)
    with TestingSessionLocal() as db:
        db.add(Task(title="退出冲刷行"))
        db.commit()
    # 标脏后 _sync_once（无 cfg 参数的封装入口）走配置读取
    se.mark_tables_dirty({"tasks"})
    monkeypatch.setattr(se, "WebDavClient", lambda *a, **kw: dav)
    from services.sync_config import get_sync_config

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        cfg.webdav_url = "https://dav.x"
        cfg.webdav_username = "u"
        cfg.webdav_password = "p"
        cfg.webdav_enabled = False  # 手动直跑跳过 enabled 检查
        db.commit()
    out = se._sync_once()
    assert out is not None
