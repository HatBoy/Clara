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


def test_merge_tie_converges_deterministically():
    """平局按 device_id 字典序（2026-11-16 优化批 3）：双端视角选出同一条物理行，
    不再"本地优先"导致 ping-pong。"""
    local_row = _row("a", TS1)
    remote_row = {"id": "a", "title": "远端", "updated_at": TS1}
    out1 = se._merge_table("tasks", local=[local_row], base=[_row("a", TS1)],
                           remote=[remote_row],
                           local_device="dev-a", remote_device="dev-b")
    out2 = se._merge_table("tasks", local=[remote_row], base=[_row("a", TS1)],
                           remote=[local_row],
                           local_device="dev-b", remote_device="dev-a")
    assert out1["merged"] == out2["merged"] == [local_row]  # dev-a 胜


def test_merge_remote_modify_beats_local_delete():
    """对端删 + 本地后改 → 改者胜（此前删除一律获胜，编辑静默丢失）。"""
    modified = _row("a", TS3)
    out = se._merge_table("tasks", local=[modified], base=[_row("a", TS1)], remote=[])
    assert out["merged"] == [modified]
    # 本地删 + 对端后改 → 对端改者胜
    remote_modified = _row("a", TS3)
    out2 = se._merge_table("tasks", local=[], base=[_row("a", TS1)],
                           remote=[remote_modified])
    assert out2["merged"] == [remote_modified]


def test_merge_delete_wins_when_unmodified():
    """未改过的一侧删除仍然获胜（保持原删除传播语义）。"""
    out = se._merge_table("tasks", local=[_row("a", TS1)], base=[_row("a", TS1)], remote=[])
    assert out["merged"] == []


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


def test_natural_key_collision_local_win_not_deleted():
    """自然键撞车 + 本地较新：LWW 保留本地行后，末尾删除集不得把它当"本地删"
    （2026-11-16 优化批 3——旧实现会让本地行消失、下一轮把远端也删掉）。"""
    with TestingSessionLocal() as db:
        db.add(Project(name="工作", color="red"))
        db.commit()
        dav = FakeDav()
        se.run_sync(db, dav, CFG)  # 推上去建立 Base
        # 本地改（新）→ 触发器刷新 updated_at
        local = se.table_rows(db, "projects")[0]
        db.execute(Project.__table__.update().where(
            Project.id == local["id"]).values(color="purple"))
        db.commit()
        # 远端换成同名不同 id、时间更旧的行（模拟对端删重建）
        old = "2020-01-01T00:00:00"
        dav.tables["projects"] = {"table": "projects", "rows": [
            {"id": "remote-uuid", "name": "工作", "color": "blue",
             "created_at": old, "updated_at": old}]}
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "projects": {**m["tables"]["projects"], "sha1": "stale"}}
        dav.manifest = m
        se.run_sync(db, dav, CFG)
        rows = se.table_rows(db, "projects")
        assert len(rows) == 1 and rows[0]["color"] == "purple"


def test_guard_c_protects_small_table_clear():
    """小表（1 行）被远端清空 + 本地未动 → 守卫 c 拦截（优化批 3：取消 >100 下限）。"""
    with TestingSessionLocal() as db:
        db.add(Project(name="唯一项目", color="red"))
        db.commit()
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        # 远端被外部写成空数组（文件仍在）
        dav.tables["projects"] = {"table": "projects", "rows": []}
        m = dict(dav.manifest)
        m["tables"] = {**m["tables"], "projects": {**m["tables"]["projects"], "sha1": "stale"}}
        dav.manifest = m
        with pytest.raises(se.SyncAbort):
            se.run_sync(db, dav, CFG)
        assert [r["name"] for r in se.table_rows(db, "projects")] == ["唯一项目"]


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


def test_new_empty_table_must_push_into_manifest():
    """运行中新增的空表（真机二轮实测 bug）：远端 manifest 未收录时，即使内容
    与远端（无文件=空）一致也必须推送一次——否则 manifest 永不收录该表，而
    Base 已落快照 → 下轮表级守卫误拦「远端缺失表文件」。"""
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()
        # 第一轮：远端只放部分表（模拟对端旧版本 manifest——无 stats_messages）
        se.run_sync(db, dav, CFG)
        assert "stats_messages" in dav.tables  # 空新表被推（修复点）
        assert "stats_messages" in dav.manifest["tables"]  # manifest 收录
        # 第二轮：不再被表级守卫拦截，正常通过
        out = se.run_sync(db, dav, CFG)
        assert "error" not in out


def test_guard_not_triggered_by_empty_new_table_snapshot():
    """守卫不拦空 Base 快照（真机二轮死锁场景）：Base 已落新表空快照 + 远端
    manifest 未收录 → 同步必须通过并把空表推上云收录，而非误拦。"""
    with TestingSessionLocal() as db:
        _add_task(db, "x")
        dav = FakeDav()
        se.run_sync(db, dav, CFG)
        # 人为制造畸形态：删远端某空表的 manifest 记录与文件（Base 空快照仍在）
        empty_table = next(t for t in se.SYNC_TABLES
                           if t not in ("tasks", "projects") and len(se._load_base(t) or []) == 0)
        m = dict(dav.manifest)
        m["tables"] = {k: v for k, v in m["tables"].items() if k != empty_table}
        dav.manifest = m
        dav.tables.pop(empty_table, None)
        # 不拦：同步通过（若 Base 非空才拦——已有用例覆盖）
        out = se.run_sync(db, dav, CFG)
        assert "error" not in out
        assert empty_table in dav.manifest["tables"]  # 重新收录自愈


def test_clear_exemption_bypasses_guard_b():
    """合法清空豁免（真机二轮实测误伤）：>100 行 messages 被整表 /clear →
    无豁免时守卫 b 拦截；note_table_cleared 后放行且删除传播远端。"""
    from models import Message

    with TestingSessionLocal() as db:
        for i in range(120):
            db.add(Message(role="user", content=f"消息{i}"))
        db.commit()
        dav = FakeDav()
        se.run_sync(db, dav, CFG)  # 建基线
        # 模拟 /clear：整表删 + 打豁免标记
        db.query(Message).delete()
        db.commit()
        se.note_table_cleared("messages")
        out = se.run_sync(db, dav, CFG)  # 不拦
        assert "error" not in out
        remote = dav.tables["messages"]["rows"]
        assert remote == []  # 删除已传播云端
        # 豁免标记已消耗；无标记时同场景仍拦（回归守卫本体）
        for i in range(120):
            db.add(Message(role="user", content=f"消息{i}"))
        db.commit()
        se.run_sync(db, dav, CFG)
        db.query(Message).delete()
        db.commit()
        with pytest.raises(se.SyncAbort, match="少了"):
            se.run_sync(db, dav, CFG)
        se._consume_cleared_marker()  # 清理残留标记


def test_manifest_self_heals_after_previous_push_failure(monkeypatch):
    """表文件推成功但 manifest 推失败后，下一轮即便纯拉取也要补推 manifest
    （2026-11-16 优化批 3：否则其他设备永远跳过新表数据）。"""
    with TestingSessionLocal() as db:
        _add_task(db, "本地新增")
        dav = FakeDav()

        real_push_manifest = dav.push_manifest

        def flaky(manifest, *, if_none_match=False):
            raise WebDavError("503 模拟限频")

        dav.push_manifest = flaky
        with pytest.raises(WebDavError):
            se.run_sync(db, dav, CFG)  # 表已推、manifest 未推

        dav.push_manifest = real_push_manifest
        se.run_sync(db, dav, CFG)  # 下一轮：远端 manifest 缺失表记录
        hashes = dav.manifest_hashes()
        assert "tasks" in hashes  # manifest 被补推收录
        assert any(r["title"] == "本地新增"
                   for r in dav.tables["tasks"]["rows"])


def test_sync_write_committed_before_network_push(monkeypatch):
    """写库事务在推送前已提交（优化批 3：网络推送不再包在长写事务里）。"""
    with TestingSessionLocal() as db:
        _add_task(db, "本地新增")
        dav = FakeDav()
        order: list[str] = []
        real_commit = db.commit

        def commit_spy():
            order.append("commit")
            return real_commit()

        real_push = dav.push_table

        def push_spy(name, payload):
            order.append("push")
            return real_push(name, payload)

        db.commit = commit_spy
        dav.push_table = push_spy
        se.run_sync(db, dav, CFG)
        assert order and order[0] == "commit" and "push" in order
        assert order.index("commit") < order.index("push")


def _enable_sync(url="https://dav.x", enabled=True):
    from services.sync_config import get_sync_config

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        cfg.webdav_url = url
        cfg.webdav_enabled = enabled
        db.commit()


def test_dirty_tables_consumed_after_successful_sync(monkeypatch):
    """脏表一轮成功后清空（优化批 3）：否则每 3s 一轮全表扫描 + 广播。"""
    _enable_sync()
    with TestingSessionLocal() as db:
        _add_task(db, "x")
    dav = FakeDav()
    monkeypatch.setattr(se, "_client_from_config", lambda cfg: dav)
    se.mark_tables_dirty(["tasks"])
    out = se._sync_once()
    assert "error" not in out
    assert se.take_dirty_tables() == set()


def test_dirty_tables_restored_after_failed_sync(monkeypatch):
    """同步失败 → 脏表恢复，下轮重试不丢变更。"""
    _enable_sync()
    with TestingSessionLocal() as db:
        _add_task(db, "x")

    class _Broken(FakeDav):
        def fetch_manifest(self):
            raise WebDavError("网络炸了")

    monkeypatch.setattr(se, "_client_from_config", lambda cfg: _Broken())
    se.mark_tables_dirty(["tasks"])
    out = se._sync_once()
    assert "error" in out
    assert "tasks" in se.take_dirty_tables()


def test_unexpected_exception_sets_last_sync_error(monkeypatch):
    """非 WebDav/守卫类异常也要落 last_sync_error（优化批 3）。"""
    _enable_sync()
    with TestingSessionLocal() as db:
        _add_task(db, "x")

    def boom(db, client, cfg):
        raise KeyError("坏远端行")

    monkeypatch.setattr(se, "_client_from_config", lambda cfg: FakeDav())
    monkeypatch.setattr(se, "run_sync", boom)
    out = se._sync_once()
    assert "KeyError" in out["error"]
    from services.sync_config import get_sync_config

    with TestingSessionLocal() as db:
        cfg = get_sync_config(db)
        assert cfg.last_sync_ok is False and "KeyError" in (cfg.last_sync_error or "")


def test_workflow_runs_excluded_from_sync():
    """workflow_runs 是设备本地执行账本，跨设备同步会抑制另一台的自动备份槽。"""
    assert "workflow_runs" not in se.SYNC_TABLES
    assert "sync_settings" not in se.SYNC_TABLES
    assert "tasks" in se.SYNC_TABLES
