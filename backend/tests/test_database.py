"""database 模块单测：轻量列迁移 + cancelled_dates→墓碑一次性迁移。

conftest 走 create_all 建新表、不经过 init_db，迁移分支（给旧 schema 补列）在
常规测试里零覆盖——2026-09-14 曾因此漏掉 Inspector 方法名写错导致的启动崩溃。
这里用独立内存库模拟旧 schema 直测两个迁移函数。
"""

import json

from sqlalchemy import create_engine, inspect, text

from database import (
    _migrate_cancelled_dates_to_tombstones,
    _migrate_chat_channel_tables,
    _migrate_integration_settings_columns,
    _migrate_local_folder_paths,
    _migrate_mcp_server_columns,
    _migrate_task_columns,
)


def test_migrate_task_columns_adds_and_idempotent():
    """旧 schema（缺新列）跑迁移补列；重复跑不炸（幂等）。"""
    engine = create_engine("sqlite://")  # 独立内存库，不碰 conftest 共享库
    with engine.connect() as conn:
        # 旧表只需含迁移关心的最小列集（函数只查列名集合，不校验其余结构）；
        # start_time 是 backfill 的谓词列（真实旧表必有，day-one 列）
        conn.execute(
            text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY, title VARCHAR(30), start_time DATETIME)")
        )
        conn.commit()

    _migrate_task_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("tasks")}
    assert {"cancelled_dates", "ever_scheduled", "deleted_at"} <= cols

    # 已补列后再跑：不应再发 ALTER（SQLite 重复加列会报 duplicate column）
    _migrate_task_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("tasks")}
    assert "cancelled_dates" in cols


def test_migrate_cancelled_dates_to_tombstones():
    """旧 cancelled_dates JSON 数据一次性转墓碑行 + ever_scheduled backfill +
    旧列置 NULL（幂等标记）+ 重跑不叠加墓碑。"""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        # 2026-09-13 时期的真实旧 schema：有 cancelled_dates、无 ever_scheduled/deleted_at；
        # created_at/updated_at 供墓碑 Core insert（default）与 UPDATE（onupdate）触达
        conn.execute(text(
            "CREATE TABLE tasks ("
            "id VARCHAR(36) PRIMARY KEY, title VARCHAR(30), start_time DATETIME, "
            "cancelled_dates JSON, parent_id VARCHAR(36), original_time DATETIME, "
            "is_deleted BOOLEAN DEFAULT 0, priority INTEGER DEFAULT 2, "
            "sort_order INTEGER DEFAULT 0, reschedule_count INTEGER DEFAULT 0, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        # weekly 系列（钟点 09:30）带着两个"仅删本次"的旧数据
        conn.execute(
            text("INSERT INTO tasks (id, title, start_time, cancelled_dates) "
                 "VALUES (:i, :t, :s, :c)"),
            {"i": "t1", "t": "英语课", "s": "2026-09-14 09:30:00",
             "c": '["2026-09-15","2026-09-17"]'},
        )
        conn.commit()

    _migrate_task_columns(engine)  # 补 ever_scheduled/deleted_at + backfill（已排期 → 1）
    _migrate_cancelled_dates_to_tombstones(engine)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT original_time, is_deleted, title FROM tasks WHERE parent_id = 't1'"
        )).all()
        # 墓碑两行：original_time = 被取消日期 + 主任务钟点（[:16] 去毫秒尾）、
        # title 复制主任务、is_deleted=1（永久软删）
        assert sorted(str(r[0])[:16] for r in rows) == ["2026-09-15 09:30", "2026-09-17 09:30"]
        assert all(r[1] == 1 for r in rows)
        assert all(r[2] == "英语课" for r in rows)
        # 主任务：旧列置 NULL（幂等标记）+ backfill 生效
        row = conn.execute(text(
            "SELECT cancelled_dates, ever_scheduled FROM tasks WHERE id = 't1'"
        )).one()
        assert row[0] is None and row[1] == 1

    # 幂等重跑：cancelled_dates 已 NULL 不再命中、墓碑不叠加
    _migrate_cancelled_dates_to_tombstones(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE parent_id = 't1'")
        ).scalar_one() == 2


def test_migrate_integration_settings_columns_adds_and_idempotent():
    """旧 schema（Composio 时期、缺本地文件夹各列）跑迁移补列；重复跑不炸（幂等）。"""
    engine = create_engine("sqlite://")  # 独立内存库，不碰 conftest 共享库
    with engine.connect() as conn:
        # 2026-09-18 Composio 落地时的真实列集：只有 id/凭证四列/时间戳
        conn.execute(text(
            "CREATE TABLE integration_settings ("
            "id INTEGER PRIMARY KEY, composio_api_key VARCHAR(200), "
            "composio_key_ok BOOLEAN, composio_key_error VARCHAR(200), "
            "composio_key_checked_at DATETIME, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.commit()

    _migrate_integration_settings_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("integration_settings")}
    assert {
        "local_folder_path",
        "local_folder_enabled",
        "local_folder_paths",
        "local_folder_test_ok",  # 2026-09-20 卡上测试结果持久化
        "local_folder_test_files",
    } <= cols

    # 已补列后再跑：不应再发 ALTER（SQLite 重复加列会报 duplicate column）
    _migrate_integration_settings_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("integration_settings")}
    assert "local_folder_enabled" in cols


def test_migrate_mcp_server_columns_adds_and_idempotent():
    """旧 schema（2026-09-18 落地时、无 description 列）跑迁移补列；重复跑不炸。"""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        # 2026-09-18 MCP 服务接入落地时的真实列集（无 description）
        conn.execute(text(
            "CREATE TABLE mcp_servers ("
            "id VARCHAR(36) PRIMARY KEY, name VARCHAR(30), transport VARCHAR(20), "
            "command VARCHAR(200), args JSON, env JSON, url VARCHAR(500), headers JSON, "
            "enabled BOOLEAN, last_test_ok BOOLEAN, last_test_ms INTEGER, "
            "last_test_at DATETIME, created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO mcp_servers (id, name, transport, url, enabled) "
            "VALUES ('m1', '滴答清单', 'streamable_http', 'https://mcp.dida365.com', 1)"
        ))
        conn.commit()

    _migrate_mcp_server_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("mcp_servers")}
    assert "description" in cols
    assert "last_test_tools" in cols  # 2026-09-20 卡上测试工具数持久化

    _migrate_mcp_server_columns(engine)  # 幂等：已补列后再跑不发 ALTER
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mcp_servers")).scalar_one() == 1  # 数据不丢


def test_migrate_local_folder_paths_one_shot():
    """单路径 → 多目录列表一次性迁移：旧值转 [旧值] 后置 NULL（幂等标记）；
    二次跑不命中；旧值本就 NULL 的行不动。"""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE integration_settings ("
            "id INTEGER PRIMARY KEY, local_folder_path VARCHAR(500), "
            "local_folder_enabled BOOLEAN, local_folder_paths JSON)"
        ))
        conn.execute(text(
            "INSERT INTO integration_settings (id, local_folder_path, local_folder_enabled) "
            "VALUES (1, 'D:\\docs', 1)"
        ))
        conn.commit()

    _migrate_local_folder_paths(engine)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT local_folder_path, local_folder_paths FROM integration_settings"
        )).one()
        assert row.local_folder_path is None  # 幂等标记
        assert json.loads(row.local_folder_paths) == ["D:\\docs"]

    # 二次跑：旧列已全 NULL，不再命中（列表值不被改写）
    _migrate_local_folder_paths(engine)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT local_folder_paths FROM integration_settings"
        )).one()
        assert json.loads(row.local_folder_paths) == ["D:\\docs"]


def test_migrate_chat_channel_push_columns_adds_and_idempotent():
    """旧 schema（2026-10-23 首批渠道表、无推送两列）跑迁移补列；重复跑不炸；
    且 messages 表缺席时 chat_channels 段仍独立生效（两表互不早退）。"""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        # 首批落地时的真实列集（凭据/启停/探测三列 + 时间戳，无推送两列）
        conn.execute(text(
            "CREATE TABLE chat_channels ("
            "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20), "
            "app_id VARCHAR(100), app_secret VARCHAR(200), enabled BOOLEAN, "
            "conn_ok BOOLEAN, conn_error VARCHAR(200), conn_at DATETIME, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO chat_channels (id, provider, app_id, app_secret) "
            "VALUES ('c1', 'feishu', 'cli-x', 'sec-x')"
        ))
        conn.commit()

    _migrate_chat_channel_tables(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("chat_channels")}
    assert {"push_enabled", "push_target"} <= cols

    # 既有行回填：ADD COLUMN 不带 DEFAULT 时旧行为 NULL，非空 bool 列必须归 0
    # （真机 500 回归哨兵——ChatChannelOut 校验对 None 直接炸）
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT push_enabled FROM chat_channels WHERE id = 'c1'")).scalar_one() == 0

    _migrate_chat_channel_tables(engine)  # 幂等：已补列后再跑不发 ALTER
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM chat_channels")).scalar_one() == 1  # 数据不丢

    # 历史踩坑库形态（2026-10-23 真机 500）：列已在但旧行为 NULL——不带
    # DEFAULT 的 ADD COLUMN 所致；重跑迁移必须统一回填归 0
    with engine.connect() as conn:
        conn.execute(text("UPDATE chat_channels SET push_enabled = NULL WHERE id = 'c1'"))
        conn.commit()
    _migrate_chat_channel_tables(engine)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT push_enabled FROM chat_channels WHERE id = 'c1'")).scalar_one() == 0
