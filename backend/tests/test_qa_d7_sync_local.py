"""QA 维度七：同步备份确定性层（七维度端到端测试批；真实 WebDAV A/B 见
test_qa_real_webdav.py）。既有 test_sync*.py 已盖三路真值表/冷启动/守卫——
本文件补 QA 视角缺口：端点契约、凭证红线、安全边界与恢复链。

断言口径经读码核实：
- 三守卫文案与 SyncAbort 传导（last_sync_error）；
- SYNC_TABLES 排除 {sync_settings, workflow_runs, ai_usage_events}——凭证
  绝不上服务器（红线断言）；
- LWW 平局：时间相等取规范行 JSON 字典序小者（双端确定性收敛）；
- 备份 zip 三件套 {manifest.json, clara.db, config_snapshot.json}；
- prune 只删严格匹配 `clara-autobackup-*.zip`（红线：不误伤用户文件）；
- 合并导入 analyze dry-run 零写入 / apply 单事务 + mark_tables_dirty。
"""

import csv
import sqlite3
from contextlib import closing
import zipfile
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import MemoryRule, Project, Task
from services import backup, sync_engine as se, sync_merge


class FakeDav:
    """同 test_sync_engine 的内存替身（duck-typing 五方法 + manifest_hashes）。"""

    def __init__(self, manifest=None, tables=None):
        self.manifest = manifest
        self.tables = dict(tables or {})
        self.writes = 0

    def ensure_dirs(self):
        pass

    def fetch_manifest(self):
        return self.manifest

    def fetch_table(self, name):
        return self.tables.get(name)

    def push_table(self, name, payload):
        self.writes += 1
        self.tables[name] = payload

    def push_manifest(self, manifest, *, if_none_match=False):
        self.writes += 1
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
    monkeypatch.setattr(backup, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(backup, "DATABASE_PATH", tmp_path / "clara.db")
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
    """未配置地址不应进入前端等待同步结束的轮询。"""
    out = client.get("/api/sync").json()
    for key in ("webdav_enabled", "device_id", "last_sync_ok", "syncing"):
        assert key in out, key
    patch = client.patch("/api/sync", json={"webdav_enabled": False})
    assert patch.status_code == 200
    run = client.post("/api/sync/run")
    assert run.status_code == 200
    assert run.json() == {"started": False, "error": "未配置 WebDAV 服务地址"}


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


@pytest.mark.parametrize(("local", "remote"), [("A", "Z"), ("Z", "A")])
def test_lww_tie_breaks_deterministic(monkeypatch, local, remote):
    """同一基线后双方均编辑，仅标题不同；交换角色仍须选 A。"""
    dav = FakeDav()
    with TestingSessionLocal() as db:
        db.add(Task(title="原始"))
        db.commit()
    _sync(monkeypatch, dav)
    with TestingSessionLocal() as db:
        row = db.query(Task).one()
        row.title = local
        row.updated_at = datetime(2030, 1, 2)
        db.commit()
        remote_rows = se.table_rows(db, "tasks")
    remote_rows[0]["title"] = remote
    dav.tables["tasks"]["rows"] = remote_rows
    dav.manifest["tables"]["tasks"]["sha1"] = se.row_codec.hash_rows(remote_rows)
    _sync(monkeypatch, dav)
    assert _task_titles() == ["A"]
    assert [r["title"] for r in dav.tables["tasks"]["rows"]] == ["A"]
    writes = dav.writes
    stats = _sync(monkeypatch, dav)
    assert stats["tables_changed"] == stats["bytes_pushed"] == 0
    assert dav.writes == writes


# —— 备份 / 恢复 / 清理红线 —————————————————————————————————————————————————


def test_backup_zip_members_and_restore(tmp_path):
    """create_backup zip 三件套；prepare_restore 两段式 staging → apply_pending_
    restore 完成置换（恢复后 clear_snapshot——新进程 lifespan 口径）。"""
    # 快照是可选成员，显式构造测试输入，不能依赖开发目录碰巧存在。
    (backup.get_data_dir() / "config_snapshot.json").write_text("{}", encoding="utf-8")

    with closing(sqlite3.connect(backup.DATABASE_PATH)) as db:
        db.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT)")
        db.execute("INSERT INTO tasks VALUES(1, '备份时点')")
        db.commit()
    dest = tmp_path / "qa-backup.zip"
    out = backup.create_backup(dest, version="qa")
    assert out is not None
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert {"manifest.json", "clara.db", "config_snapshot.json"} <= names
    with closing(sqlite3.connect(backup.DATABASE_PATH)) as db:
        db.execute("UPDATE tasks SET title='备份后偏离'")
        db.commit()
    assert backup.prepare_restore(str(dest))["requires_restart"]
    with closing(sqlite3.connect(backup.DATABASE_PATH)) as db:
        assert db.execute("SELECT title FROM tasks").fetchall() == [("备份后偏离",)]
    assert backup.apply_pending_restore() is True
    with closing(sqlite3.connect(backup.DATABASE_PATH)) as db:
        assert db.execute("SELECT id,title FROM tasks").fetchall() == [(1, "备份时点")]
    assert backup.apply_pending_restore() is False


def test_auto_backup_prune_strict_match(tmp_path):
    """prune 7 天滚动只删 `clara-autobackup-*.zip` 严格匹配——用户手工备份
    （其他命名）绝不误伤（红线）。"""
    now = datetime.now()
    keep_user = tmp_path / "我的手工备份.zip"
    keep_other = tmp_path / "clara-backup-20260101.zip"
    recent = tmp_path / f"clara-autobackup-{now - timedelta(days=6):%Y%m%d-%H%M%S}.zip"
    hit1 = tmp_path / f"clara-autobackup-{now - timedelta(days=8):%Y%m%d-%H%M%S}.zip"
    hit2 = tmp_path / f"clara-autobackup-{now - timedelta(days=9):%Y%m%d-%H%M%S}.zip"
    for f in (keep_user, keep_other, recent, hit1, hit2):
        f.write_bytes(b"zip")
    # 文件名时间戳优先（20260101 旧日期即删，解析失败回退 mtime）
    assert backup.prune_auto_backups(tmp_path, keep_days=7) == 2
    assert recent.exists()
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
    from database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    source_engine = create_engine(f"sqlite:///{backup.DATABASE_PATH}")
    Base.metadata.create_all(source_engine)
    with Session(source_engine) as db:
        db.add(Project(name="合并项目", color="#123456"))
        db.add(Task(title="合并任务", duration_mins=30))
        db.commit()
    source_engine.dispose()
    backup.create_backup(src, version="qa")
    with TestingSessionLocal() as target:
        target.add(MemoryRule(category="边界", content="本地手写", source="user_manual"))
        target.commit()
        analyze = sync_merge.analyze_merge(target, src)
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
    # 源库和目标库必须独立；预检本身不写目标库。
    assert before == []
    with TestingSessionLocal() as target:
        assert [(r.source, r.content) for r in target.query(MemoryRule)] == [("user_manual", "本地手写")]


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
    base = {p.name: p.read_bytes() for p in se.snapshot_dir().iterdir()}
    writes = dav.writes
    assert se.has_base() and base
    dav.manifest = None
    with pytest.raises(se.SyncAbort, match="意外缺失"):
        _sync(monkeypatch, dav)
    assert dav.manifest is None and dav.writes == writes
    assert {p.name: p.read_bytes() for p in se.snapshot_dir().iterdir()} == base
    assert _task_titles() == ["守卫行"]


def test_exit_flush_pushes_last_edit(monkeypatch):
    """启用同步时退出冲刷将最后一次提交发布到远端。"""
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
        cfg.webdav_enabled = True
        db.commit()
    out = se.flush_on_exit()
    assert out["bytes_pushed"] > 0
    assert [r["title"] for r in dav.tables["tasks"]["rows"]] == ["退出冲刷行"]


def test_sync_password_write_only(client):
    marker = 'QA-ONLY-fake-DAV-password'
    response = client.patch('/api/sync', json={'webdav_password': marker})
    for reply in (response, client.get('/api/sync')):
        assert reply.is_success
        assert marker not in reply.text
        assert 'webdav_password' not in reply.json()
        assert reply.json()['webdav_password_set'] is True
    assert client.patch('/api/sync', json={'webdav_password': None}).json()['webdav_password_set'] is False


def test_csv_parse_and_ics_reimport_supported_fields(tmp_path):
    """CSV 只支持导出：标准解析器回读；ICS 走产品导入，断言可交换字段。"""
    from database import Base
    from schemas import TaskCreate
    from services.export import export_tasks_csv, export_tasks_ics
    from services.ics_import import import_ics_file
    from services.tasks import create_task
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    start, end = datetime(2030, 1, 7, 9), datetime(2030, 1, 7, 10)
    title, notes = '复盘,周会', '核实"提交"\n保留换行'
    rule = 'FREQ=WEEKLY;BYDAY=MO'
    csv_path, ics_path = tmp_path / '任务.csv', tmp_path / '任务.ics'
    with TestingSessionLocal() as db:
        row = create_task(db, TaskCreate(title=title, notes=notes, priority=1,
                          start_time=start, end_time=end, recurrence_rule=rule))
        assert export_tasks_csv(db, str(csv_path))['count'] == 1
        assert export_tasks_ics(db, str(ics_path))['count'] == 1
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            decoded, = csv.DictReader(stream)
        assert [decoded[k] for k in ('id', '标题', '备注', '开始时间', '结束时间', '优先级', '重复规则')] == [
            row.id, title, notes, '2030-01-07 09:00', '2030-01-07 10:00', '高', rule]
    engine = create_engine(f'sqlite:///{tmp_path / "接收.db"}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            assert import_ics_file(db, str(ics_path))['created_events'] == 1
            assert import_ics_file(db, str(ics_path))['skipped_duplicates'] == 1
            new = db.query(Task).one()
            assert (new.title, new.notes, new.start_time, new.end_time, new.recurrence_rule) == (
                title, notes, start, end, rule)
    finally:
        engine.dispose()


# —— 发版前验收批补充：失败退避曲线 + 覆盖恢复后手动规则最高优 ——————————————————


def test_sync_backoff_period_curve():
    """指数退避曲线锁定（模块五/七韧性口径；渠道层无指数退避——飞书/钉钉
    SDK 内部重连、企微/微信固定 sleep，这是全库唯一指数退避实现）：
    300×2^min(streak,3) 上限 1800；限频（429/503）fail_streak 跳 3 →
    一步进长退避（真机批六根因回归锚）。_backoff_period 由 sync_loop 内联
    公式提取（2026-11-24 验收批），行为零变化。"""
    assert [se._backoff_period(n) for n in range(5)] == [300, 600, 1200, 1800, 1800]
    assert se._backoff_period(99) == 1800  # 封顶不再增长
    assert se._backoff_period(0 + 3) == 1800  # 限频跳升：跳过 600/1200 两档


def test_overwrite_restore_keeps_manual_rules_top_priority(tmp_path):
    """覆盖式恢复（两段置换）后手动最高优先级记忆规则完整还原且注入序最高
    （模块五·导入还原安全性）：备份含 user_manual 铁律 + ai_reflect 低置信行 →
    恢复前偏离（手写行被改写）→ 恢复后备份时点行回来：list_memory_rules 首行
    = user_manual 原文；format_memory_lines core 段含「·用户设定·」标签行、
    ai_reflect 0.7 行只在 reference 段（分级注入契约恢复不破）。"""
    (backup.get_data_dir() / "config_snapshot.json").write_text("{}", encoding="utf-8")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from database import Base
    from services.ai_data import format_memory_lines, list_memory_rules

    engine = create_engine(f"sqlite:///{backup.DATABASE_PATH}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(MemoryRule(category="边界", content="周五下午绝不排会",
                          source="user_manual", confidence=1.0))
        db.add(MemoryRule(category="精力节律", content="上午黄金段排硬骨头",
                          source="ai_reflect", confidence=0.7, evidence="遥测"))
        db.commit()
    engine.dispose()
    src = tmp_path / "qa-manual-restore.zip"
    assert backup.create_backup(src, version="qa") is not None
    with closing(sqlite3.connect(backup.DATABASE_PATH)) as raw:  # 恢复前漂移
        raw.execute("UPDATE memory_rules SET content='恢复前漂移'")
        raw.commit()
    assert backup.prepare_restore(str(src))["requires_restart"]
    assert backup.apply_pending_restore() is True
    engine = create_engine(f"sqlite:///{backup.DATABASE_PATH}")
    try:
        with Session(engine) as db:
            rules = list_memory_rules(db)
            assert rules[0].source == "user_manual"
            assert rules[0].content == "周五下午绝不排会"
            core, ref = format_memory_lines(db)
            assert any("周五下午绝不排会" in ln and "用户设定" in ln for ln in core)
            assert any("上午黄金段排硬骨头" in ln and "AI学习·置信0.7" in ln
                       for ln in ref)
            assert not any("恢复前漂移" in ln for ln in core + ref)
    finally:
        engine.dispose()
    assert backup.apply_pending_restore() is False
