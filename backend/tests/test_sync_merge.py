"""合并导入测试（services/sync_merge.py）：双格式 zip（备份格式 / 同步格式）、
analyze 两阶段统计、overwrite/skip 两策略、不删除语义、损坏包 422。"""

import json
import zipfile

import pytest

from conftest import TestingSessionLocal
from models import Task
from services import sync_merge


def _sync_zip(tmp_path, tables: dict, name="sync.zip"):
    """造同步格式 zip（manifest + tables/*.json）。"""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"app": "clara-sync", "format": 1}))
        for t, rows in tables.items():
            zf.writestr(f"tables/{t}.json", json.dumps({"table": t, "rows": rows}))
    return path


def _backup_zip(tmp_path, db_rows, name="backup.zip"):
    """造备份格式 zip（manifest + 最小 clara.db：tasks 表）。"""
    import sqlite3

    db_path = tmp_path / f"_{name}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?)", db_rows)
    conn.commit()
    conn.close()
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"app": "clara"}))
        zf.write(db_path, "clara.db")
    return path


def _titles(db):
    return sorted(r["title"] for r in
                  db.execute(Task.__table__.select()).mappings().all())


def _task_row(tid, title, ts="2026-11-14T10:00:00"):
    return {"id": tid, "title": title, "updated_at": ts, "created_at": ts,
            "priority": 2, "sort_order": 0}


# —— load_zip_tables 双格式 ———————————————————————————————————————


def test_load_sync_format_zip(tmp_path):
    z = _sync_zip(tmp_path, {"tasks": [_task_row("a", "A")]})
    tables = sync_merge.load_zip_tables(str(z))
    assert tables["tasks"][0]["title"] == "A"
    assert "sync_settings" not in tables  # 凭证域过滤


def test_load_allows_excluded_table_members(tmp_path):
    # 2026-11-17 二轮批 1：旧同步目录残留的排除表成员（workflow_runs 等）放行、
    # 数据丢弃——README 指引的「下载目录压 zip 合并导入」灾备路径此前直接 422；
    # 真未知表仍拒
    z = _sync_zip(tmp_path, {
        "tasks": [_task_row("a", "A")],
        "workflow_runs": [{"id": "w1"}],   # 2026-11-16 前同步范围的远端残留
        "ai_usage_events": [{"id": "u1"}],  # 设备本地遥测（恒排除）
        "bogus_table": [{"id": "?"}],
    })
    try:
        sync_merge.load_zip_tables(str(z))
        raise AssertionError("真未知表应拒绝")
    except ValueError as e:
        assert "bogus_table" in str(e)
    z2 = _sync_zip(tmp_path, {
        "tasks": [_task_row("a", "A")],
        "workflow_runs": [{"id": "w1"}],
        "ai_usage_events": [{"id": "u1"}],
    }, name="sync2.zip")
    tables = sync_merge.load_zip_tables(str(z2))
    assert set(tables) == {"tasks"}  # 排除表成员放行但数据丢弃


def test_load_backup_format_zip(tmp_path):
    z = _backup_zip(tmp_path, [("a", "A")])
    tables = sync_merge.load_zip_tables(str(z))
    assert any(r["title"] == "A" for r in tables["tasks"])


def test_load_rejects_foreign(tmp_path):
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"app": "other"}))
    with pytest.raises(ValueError, match="Clara"):
        sync_merge.load_zip_tables(str(path))


def test_load_rejects_non_zip(tmp_path):
    p = tmp_path / "fake.zip"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        sync_merge.load_zip_tables(str(p))


# —— analyze / apply 两阶段 ——————————————————————————————————————


def test_analyze_counts_and_conflicts(tmp_path):
    z = _sync_zip(tmp_path, {
        "tasks": [_task_row("a", "新增A"),
                  {**_task_row("b", "远端B改"), "title": "远端B改"}]})
    with TestingSessionLocal() as db:
        db.add(Task(id="b", title="本地B"))
        db.commit()
        out = sync_merge.analyze_merge(db, str(z))
        assert out["new_count"] == 1
        assert out["updated_count"] == 1
        assert out["conflicts"][0]["table"] == "tasks"
        assert _titles(db) == ["本地B"]  # dry-run 零写入


def test_apply_skip_strategy(tmp_path):
    z = _sync_zip(tmp_path, {"tasks": [
        _task_row("a", "新增A"),
        {**_task_row("b", "远端B改"), "title": "远端B改"}]})
    with TestingSessionLocal() as db:
        db.add(Task(id="b", title="本地B"))
        db.commit()
        out = sync_merge.apply_merge(db, str(z), "skip")
        assert out["inserted"] == 1 and out["updated"] == 0 and out["skipped"] == 1
        assert _titles(db) == ["新增A", "本地B"]


def test_apply_overwrite_strategy(tmp_path):
    z = _sync_zip(tmp_path, {"tasks": [
        _task_row("a", "新增A"),
        {**_task_row("b", "远端B改"), "title": "远端B改"}]})
    with TestingSessionLocal() as db:
        db.add(Task(id="b", title="本地B"))
        db.commit()
        out = sync_merge.apply_merge(db, str(z), "overwrite")
        assert out["inserted"] == 1 and out["updated"] == 1
        assert _titles(db) == ["新增A", "远端B改"]


def test_apply_never_deletes_local(tmp_path):
    """两路合并不删除：本地多出的行（zip 里没有）必须保留。"""
    z = _sync_zip(tmp_path, {"tasks": [_task_row("a", "A")]})
    with TestingSessionLocal() as db:
        db.add(Task(id="local-only", title="仅本地"))
        db.commit()
        sync_merge.apply_merge(db, str(z), "overwrite")
        assert _titles(db) == ["A", "仅本地"]


def test_apply_backup_format_zip(tmp_path):
    z = _backup_zip(tmp_path, [("z1", "备份行")])
    with TestingSessionLocal() as db:
        out = sync_merge.apply_merge(db, str(z), "overwrite")
        assert out["inserted"] >= 1
        assert "备份行" in _titles(db)


def test_dump_normalizes_datetime_and_json(tmp_path):
    """备份包 dump 与 table_rows 同口径（2026-11-16 优化批 3）：DateTime 为
    isoformat（T 分隔）、JSON 列为 Python 对象——否则 analyze 全表判"不同"。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                 "start_time DATETIME, cancelled_dates JSON)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'x', "
                 "'2026-11-20 09:00:00.000000', '[\"2026-11-21\"]')")
    conn.commit()
    conn.close()

    rows = sync_merge._dump_sqlite_tables(db_path)["tasks"]
    assert rows[0]["start_time"] == "2026-11-20T09:00:00"
    assert rows[0]["cancelled_dates"] == ["2026-11-21"]


def test_apply_sync_zip_json_not_double_encoded(tmp_path):
    """同步格式 zip 的 JSON 列落库后仍是对象（优化批 3）。"""
    z = _sync_zip(tmp_path, {"tasks": [{
        **_task_row("j1", "JSON 行"), "cancelled_dates": ["2026-11-21"]}]})
    with TestingSessionLocal() as db:
        out = sync_merge.apply_merge(db, str(z), "overwrite")
        assert out["inserted"] >= 1
        row = db.execute(Task.__table__.select().where(Task.id == "j1")).mappings().first()
        assert row["cancelled_dates"] == ["2026-11-21"]
