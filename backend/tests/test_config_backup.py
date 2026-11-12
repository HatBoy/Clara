"""配置快照自愈测试（2026-10-22 批 2）：dump/恢复 roundtrip（datetime/JSON/布尔
列全形态）/「库刚重建」特征检测恢复 / 非重建不覆盖既有配置 / 快照损坏静默跳过。
快照路径 monkeypatch 到 tmp_path——绝不污染真实 data 目录。
"""

from datetime import datetime

import pytest

import services.config_backup as cb
from conftest import TestingSessionLocal
from models import AppSettings, CliTool, ModelProvider


@pytest.fixture
def snap_path(monkeypatch, tmp_path):
    path = tmp_path / "config_snapshot.json"
    monkeypatch.setattr(cb, "snapshot_path", lambda: path)
    return path


def _fill_config(db):
    """灌一套有代表性的配置：datetime/布尔/str/int + JSON 列。"""
    db.add(ModelProvider(id="m1", name="主力", model_name="deepseek/chat",
                         api_key="sk-123", is_active=True, last_test_ok=True,
                         last_test_at=datetime(2026, 10, 21, 20, 0)))
    db.add(CliTool(adapter_id="custom:x", name="我的命令",
                   custom_bin="C:/bin/foo.exe",
                   custom_argv=["foo", "--json", "{since}"],
                   bin_at=datetime(2026, 10, 20, 9, 30)))
    db.add(AppSettings(id=1, weather_city="上海", ai_style="concise"))
    db.commit()


def test_roundtrip_restores_all_columns(fresh_db):
    with TestingSessionLocal() as db:
        _fill_config(db)
        snap = cb.dump_config(db)
        assert snap["model_providers"][0]["api_key"] == "sk-123"
        total = cb.restore_config(db, snap)  # 先清后插幂等：原地恢复
        assert total >= 3
        row = db.get(ModelProvider, "m1")
        assert row.is_active and row.api_key == "sk-123"
        assert row.last_test_at == datetime(2026, 10, 21, 20, 0)
        cli = db.get(CliTool, "custom:x")
        assert cli.custom_argv == ["foo", "--json", "{since}"]  # JSON 列对象往返
        assert db.get(AppSettings, 1).weather_city == "上海"


def test_maybe_restore_recovers_after_db_rebuild(monkeypatch, fresh_db, snap_path):
    with TestingSessionLocal() as db:
        _fill_config(db)
        assert cb.maybe_restore_and_snapshot(db) is False  # 正常启动：只写快照
    assert snap_path.exists()
    with TestingSessionLocal() as db:  # 模拟删库重灌：配置表清空、快照留存
        for t in (ModelProvider, CliTool):
            for row in db.query(t).all():
                db.delete(row)
        db.commit()
        rebuilt = cb.maybe_restore_and_snapshot(db)
        assert rebuilt is True
        assert db.get(ModelProvider, "m1").api_key == "sk-123"   # 四表全空 → 恢复
        assert db.get(CliTool, "custom:x").custom_argv is not None


def test_maybe_restore_keeps_existing_config(monkeypatch, fresh_db, snap_path):
    with TestingSessionLocal() as db:
        _fill_config(db)
        cb.maybe_restore_and_snapshot(db)  # 首次启动写快照
        # 二次启动：配置在库（用户已重配新 key）——快照旧值不得覆盖现值
        db.get(ModelProvider, "m1").api_key = "sk-new"
        db.commit()
        cb.maybe_restore_and_snapshot(db)
        assert db.get(ModelProvider, "m1").api_key == "sk-new"


def test_maybe_restore_corrupt_snapshot_skips(monkeypatch, fresh_db, snap_path):
    snap_path.write_text("{broken json", encoding="utf-8")
    with TestingSessionLocal() as db:  # 空库 + 损坏快照 → 不抛、不恢复，快照重写
        cb.maybe_restore_and_snapshot(db)
        assert db.query(ModelProvider).count() == 0
    assert snap_path.read_text(encoding="utf-8") != "{broken json"
