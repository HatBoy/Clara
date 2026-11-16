"""同步引擎测试（services/sync_engine.py；FakeDav 内存替身零真实外呼）。

覆盖：三路合并真值表全 case；冷启动三分支（Force Push / Pull / Case C 需确认
含快照丢失文案）；三守卫（a 远端缺失 / b 删库重建防传播 / c 清空式拦截）；
快照原子写与失败传导；增量跳过（替身请求计数）；脏表交集防死循环；自然键
撞车回退；updated_at 触发器盖章与 WHEN 守卫；sync_now 防重入。
快照目录与数据库全隔离到 tmp_path / 内存库。
"""

import json
from datetime import datetime, timedelta

import pytest

import services.sync_engine as se
from conftest import TestingSessionLocal
from models import Project, Task
from services.sync_webdav import WebDavError


class FakeDav:
    """WebDavClient 的内存替身（duck-typing：engine 只依赖这五个方法）。"""

    def __init__(self, manifest=None, tables=None):
        self.manifest = manifest
        self.tables: dict = dict(tables or {})  # name -> payload dict
        self.push_count = 0
        self.get_count = 0
        self.ensure_count = 0
        self.readme_writes = 0

    def ensure_dirs(self):
        self.ensure_count += 1

    def fetch_manifest(self):
        return self.manifest

    def fetch_table(self, name):
        self.get_count += 1
        return self.tables.get(name)

    def push_table(self, name, payload):
        self.push_count += 1
        self.tables[name] = payload

    def push_manifest(self, manifest, *, if_none_match=False):
        if if_none_match and self.manifest is not None:
            raise WebDavError("__PRECONDITION_412__")
        self.manifest = manifest

    def put_readme(self, content: str) -> None:
        self.readme_writes += 1

    def manifest_hashes(self):
        return {t: v.get("sha1") for t, v in (self.manifest or {}).get("tables", {}).items()}


@pytest.fixture(autouse=True)
def _isolate_snapshot(monkeypatch, tmp_path):
    """快照目录隔离到 tmp_path（engine 模块的 get_data_dir 引用被替换）。"""
    monkeypatch.setattr(se, "get_data_dir", lambda: tmp_path)
    se.clear_snapshot()
    yield
    se.clear_snapshot()


CFG = type("Cfg", (), {  # run_sync 只消费这三属性
    "webdav_url": "https://dav.x", "webdav_username": "u", "webdav_password": "p",
    "device_id": "dev-1",
})()


def _add_task(db, title, project_id=None, updated=None) -> str:
    t = Task(title=title, project_id=project_id)
    db.add(t)
    db.flush()
    if updated:
        db.execute(
            Task.__table__.update().where(Task.id == t.id)
            .values(updated_at=updated, created_at=updated))
    db.commit()
    return t.id


def _tasks_titles(db):
    rows = se.table_rows(db, "tasks")
    return sorted(r["title"] for r in rows)


# —— 三路合并真值表（纯函数直测）————————————————————————————————


def _row(key, ts):
    return {"id": key, "title": f"t{key}", "updated_at": ts}


TS1 = "2026-11-14T10:00:00"
TS2 = "2026-11-14T11:00:00"
TS3 = "2026-11-14T12:00:00"


def test_merge_remote_add():
    out = se._merge_table("tasks", local=[], base=[], remote=[_row("a", TS1)])
    assert out["merged"] == [_row("a", TS1)]


def test_merge_local_add():
    out = se._merge_table("tasks", local=[_row("a", TS1)], base=[], remote=[])
    assert out["merged"] == [_row("a", TS1)]


def test_merge_local_delete_propagates():
    out = se._merge_table("tasks", local=[], base=[_row("a", TS1)], remote=[_row("a", TS1)])
    assert out["merged"] == [] and out["del_remote"] == {"a"} and "a" not in out["del_local"]


def test_merge_remote_delete_lands_locally():
    out = se._merge_table("tasks", local=[_row("a", TS1)], base=[_row("a", TS1)], remote=[])
    assert out["merged"] == [] and out["del_local"] == {"a"}


def test_merge_both_changed_lww():
    newer = _row("a", TS3)
    out = se._merge_table("tasks", local=[_row("a", TS2)], base=[_row("a", TS1)],
                          remote=[newer])
    assert out["merged"] == [newer]


def test_merge_tie_local_wins():
    out = se._merge_table("tasks", local=[_row("a", TS1)], base=[_row("a", TS1)],
                          remote=[{"id": "a", "title": "远端", "updated_at": TS1}])
    # 平局（ts 相等）本地优先
    assert out["merged"] == [_row("a", TS1)]


def test_merge_ts_missing_falls_back_to_created():
    local = {"id": "a", "created_at": TS2}
    remote = {"id": "a", "created_at": TS3, "updated_at": TS3}
    out = se._merge_table("tasks", [local], [], [remote])
    assert out["merged"] == [remote]


def test_merge_cold_union():
    out = se._merge_table("tasks", local=[_row("a", TS1), _row("b", TS3)],
                          base=None, remote=[_row("b", TS2), _row("c", TS1)])
    got = {r["id"] for r in out["merged"]}
    assert got == {"a", "b", "c"}
    b = next(r for r in out["merged"] if r["id"] == "b")
    assert b["updated_at"] == TS3  # 同键取新


# —— 冷启动三分支（run_sync 集成）————————————————————————————————


def test_cold_force_push():
    with TestingSessionLocal() as db:
        _add_task(db, "本地任务")
        dav = FakeDav(manifest=None)
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out
        rows = json.loads(json.dumps(dav.tables["tasks"]["rows"]))
        assert [r["title"] for r in rows] == ["本地任务"]
        assert dav.manifest["app"] == "clara-sync"
        assert dav.readme_writes == 1  # 首同步落云端使用说明（一次）
        assert se.has_base()  # 快照已落
        assert se._load_base("tasks")


def test_cold_pull_local_empty():
    dav = FakeDav(manifest={"app": "clara-sync", "tables": {}},
                  tables={"tasks": {"table": "tasks", "rows": [
                      {"id": "r1", "title": "远端任务", "updated_at": TS1,
                       "priority": 2, "sort_order": 0, "created_at": TS1}]}})
    with TestingSessionLocal() as db:
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out
        assert _tasks_titles(db) == ["远端任务"]


def test_cold_both_sides_auto_union():
    """迭代四（2026-11-15）：双端有数据不再要求人工确认——直接 union 合并，
    两侧数据并集落地、快照建立（免手动，用户拍板）。"""
    dav = FakeDav(manifest={"app": "clara-sync", "tables": {"tasks": {"rows": 1}}},
                  tables={"tasks": {"table": "tasks", "rows": [
                      {"id": "r1", "title": "远端", "updated_at": TS1,
                       "priority": 2, "sort_order": 0, "created_at": TS1}]}})
    with TestingSessionLocal() as db:
        _add_task(db, "本地")
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out
        assert _tasks_titles(db) == ["本地", "远端"]  # 并集落地
        assert se.has_base()  # 快照直接建立
        # 远端也拿到本地行（推送内容含并集）
        pushed = [r["title"] for r in dav.tables["tasks"]["rows"]]
        assert sorted(pushed) == ["本地", "远端"]


def test_snapshot_missing_auto_union():
    """快照丢失（目录在但 Base 无 meta）同样自动 union——不再区分 reason。"""
    (se.snapshot_dir()).mkdir(parents=True, exist_ok=True)
    dav = FakeDav(manifest={"app": "clara-sync", "tables": {"tasks": {"rows": 1}}},
                  tables={"tasks": {"table": "tasks", "rows": [
                      {"id": "r1", "title": "远端", "updated_at": TS1,
                       "priority": 2, "sort_order": 0, "created_at": TS1}]}})
    with TestingSessionLocal() as db:
        _add_task(db, "本地")
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out
        assert _tasks_titles(db) == ["本地", "远端"]


def test_force_push_race_412_auto_union():
    """双端同时首同步：本端首推收 412（对端先写）→ 重拉 manifest 直接 union。"""
    dav = FakeDav(manifest={"app": "clara-sync", "tables": {"tasks": {"rows": 1}}},
                  tables={"tasks": {"table": "tasks", "rows": [
                      {"id": "r1", "title": "远端", "updated_at": TS1,
                       "priority": 2, "sort_order": 0, "created_at": TS1}]}})
    with TestingSessionLocal() as db:
        _add_task(db, "本地")
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out
        assert _tasks_titles(db) == ["本地", "远端"]


# —— 30s 远端轻探测（迭代四）———————————————————————————————————————


def test_probe_remote_changed():
    """探测记忆比对：hash 变 → True；同 → False；远端被清且有记忆 → True。
    探测函数读 sync_settings 判启用——先落一行启用配置（run_sync 直调不判）。"""
    from models import SyncSettings
    from services.sync_config import get_sync_config

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        cfg.webdav_enabled = True
        cfg.webdav_url = "https://dav.x"
        db.commit()
        _add_task(db, "x")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)  # 建立探测记忆（_last_seen_hashes）
        assert se._last_seen_hashes is not None

        # 远端未变（FakeDav manifest 与记忆一致）→ False
        assert se._probe_remote_changed(dav) is False

        # 对端改了远端（hash 失配）→ True
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "tasks": {**m["tables"]["tasks"], "sha1": "changed"}}
        dav.manifest = m
        assert se._probe_remote_changed(dav) is True

        # 重新同步刷新记忆 → False（直调 run_sync——_sync_once 自建真客户端
        # 会走网络，测试替身传不进去）
        se.run_sync(db, dav, CFG)
        assert se._probe_remote_changed(dav) is False

        # 远端被清（manifest None）且有记忆 → True（触发同步走守卫 a）
        dav.manifest = None
        assert se._probe_remote_changed(dav) is True


# —— 守卫 —————————————————————————————————————————————————————————


def test_guard_a_remote_manifest_vanished():
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)  # 首同步建立 Base
        dav.manifest = None  # 模拟远端被清
        with pytest.raises(se.SyncAbort, match="重置同步基线"):
            se.run_sync(db, dav, CFG)


def test_guard_b_blocks_mass_delete_propagation():
    with TestingSessionLocal() as db:
        for i in range(150):
            _add_task(db, f"任务{i}")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        # 模拟删库重建：清空业务表重灌少量（seed 特征）
        db.query(Task).delete()
        db.commit()
        _add_task(db, "新种子")
        with pytest.raises(se.SyncAbort, match="少了"):
            se.run_sync(db, dav, CFG)
        # 守卫拦截后：重置基线是用户出路
        se.clear_snapshot()
        out = se.run_sync(db, dav, CFG)
        assert "needs_confirm" not in out


def test_guard_c_blocks_total_wipe():
    with TestingSessionLocal() as db:
        for i in range(150):
            _add_task(db, f"任务{i}")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        # 远端表文件被清空但 manifest 记录仍在（sha1 失配触发拉取）；
        # 本地未变 → 合并将整体清空本地 → 清空式拦截
        dav.tables["tasks"] = {"table": "tasks", "rows": []}
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "tasks": {**m["tables"]["tasks"], "sha1": "stale"}}
        dav.manifest = m
        with pytest.raises(se.SyncAbort, match="已被清空"):
            se.run_sync(db, dav, CFG)
        # manifest 记录也一并消失（表被手动删）→ 表级守卫路径
        dav.manifest = {"app": "clara-sync", "tables": {}}
        with pytest.raises(se.SyncAbort, match="缺失表文件"):
            se.run_sync(db, dav, CFG)


def test_guard_table_missing_from_manifest():
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        # manifest 里 tasks 记录消失（文件被手动删）
        m = dict(dav.manifest)
        m["tables"] = {k: v for k, v in m["tables"].items() if k != "tasks"}
        dav.manifest = m
        dav.tables.pop("tasks", None)
        with pytest.raises(se.SyncAbort, match="缺失表文件"):
            se.run_sync(db, dav, CFG)


# —— 增量与一致性 —————————————————————————————————————————————————


def test_incremental_skip_when_both_unchanged():
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        push_before = dav.push_count
        out = se.run_sync(db, dav, CFG)  # 双未变
        assert dav.push_count == push_before  # 零表文件推送
        assert out["tables_changed"] == 0
        assert se.has_base()


def test_remote_change_lands_locally_and_vise_versa():
    """双向端到端：本地新增→推远端；远端改→落本地（LWW）。"""
    with TestingSessionLocal() as db:
        tid = _add_task(db, "原始")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)

        # 本地改标题 → 同步 → 远端拿到新标题
        db.execute(Task.__table__.update().where(Task.id == tid).values(title="本地已改"))
        db.commit()
        se.run_sync(db, dav, CFG)
        rows = dav.tables["tasks"]["rows"]
        assert any(r["title"] == "本地已改" for r in rows)

        # 远端改（直接改替身数据 + manifest hash 失配触发拉取）→ 同步 → 本地拿到
        newer = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        dav.tables["tasks"] = {"table": "tasks", "rows": [
            {**r, "title": "远端已改", "updated_at": newer} for r in rows]}
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "tasks": {**m["tables"]["tasks"], "sha1": "stale"}}
        dav.manifest = m
        se.run_sync(db, dav, CFG)
        assert _tasks_titles(db) == ["远端已改"]


def test_snapshot_write_failure_fails_round(monkeypatch):
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()

        def boom(name, rows):
            raise OSError("磁盘满（测试注入）")

        monkeypatch.setattr(se, "_save_base", boom)
        with pytest.raises(OSError):
            se.run_sync(db, dav, CFG)
        assert not se.has_base()  # meta 未写 = 整轮失败口径


def test_natural_key_collision_lww_fallback():
    """两设备各建同名项目（UUID 不同）：INSERT 撞 name 唯一约束 → 按自然键
    定位 LWW——远端新则远端内容落到本地行（保 Base=本地=远端一致）。"""
    with TestingSessionLocal() as db:
        db.add(Project(name="工作", color="red"))
        db.commit()
        dav = FakeDav()
        se.run_sync(db, dav, CFG)  # 推上去

        # 远端来了个同名不同 id 的项目（对端建的）
        newer = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        rows = dav.tables["projects"]["rows"]
        dav.tables["projects"] = {"table": "projects", "rows": rows + [
            {"id": "remote-uuid", "name": "工作", "color": "blue",
             "created_at": newer, "updated_at": newer}]}
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "projects": {**m["tables"]["projects"], "sha1": "stale"}}
        dav.manifest = m
        se.run_sync(db, dav, CFG)
        # 同名项目不重复（唯一约束守住），远端较新内容落地
        names = [r["name"] for r in se.table_rows(db, "projects")]
        assert names.count("工作") == 1
        colors = [r["color"] for r in se.table_rows(db, "projects")]
        assert "blue" in colors


def test_dirty_tables_intersect_sync_tables():
    """防死循环回归：sync_settings（状态回显）进脏表集合会被过滤。"""
    se.mark_tables_dirty(["sync_settings", "tasks"])
    assert se.take_dirty_tables() == {"tasks"}


def test_updated_at_trigger_stamps_and_when_guard():
    """触发器盖章：业务 UPDATE 不带 updated_at → 触发器盖本地 now；
    显式 SET updated_at（同步引擎路径）→ 不被覆盖（WHEN 守卫）。"""
    with TestingSessionLocal() as db:
        tid = _add_task(db, "x")
        before = db.execute(
            Task.__table__.select().where(Task.id == tid)).mappings().first()["updated_at"]
        # 业务写：Core UPDATE 不带 updated_at → 触发器盖
        db.execute(Task.__table__.update().where(Task.id == tid).values(title="y"))
        db.commit()
        after = db.execute(
            Task.__table__.select().where(Task.id == tid)).mappings().first()["updated_at"]
        assert after >= before
        # 同步写：显式 SET → 保留远端时间戳（不盖）
        explicit = datetime(2030, 1, 1, 12, 0, 0)
        db.execute(Task.__table__.update().where(Task.id == tid)
                   .values(updated_at=explicit))
        db.commit()
        got = db.execute(
            Task.__table__.select().where(Task.id == tid)).mappings().first()["updated_at"]
        assert got == explicit


def test_sync_now_busy_and_lock_release():
    assert se._sync_lock.acquire(blocking=False)
    try:
        out = se._sync_once()
        assert out == {"busy": True}
    finally:
        se._sync_lock.release()
    # 无地址 → 明确错误态；有地址未开开关：自动路径拦（manual 语义，真机批四）
    out = se._sync_once()
    assert "未配置" in out["error"]
    from services.sync_config import get_sync_config

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        cfg.webdav_url = "https://dav.x"
        db.commit()
    assert "未启用" in se._sync_once()["error"]  # 自动路径（manual=False）
    # 手动路径（sync_now）无视开关——错误只可能是网络层（真客户端连不上），
    # 不再出现「未启用」
    out = se.sync_now()
    assert "未启用" not in out.get("error", "")
