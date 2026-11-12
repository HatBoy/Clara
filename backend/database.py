"""SQLite 引擎与会话管理。

- check_same_thread=False：FastAPI 的同步端点跑在线程池里，连接会跨线程使用
- WAL 模式：读写不互相阻塞，桌面单机场景下最稳的 SQLite 并发配置
- get_db 是 FastAPI 依赖注入函数：每个请求一个 Session，用完即关
"""

import json
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """每个新连接上开启 WAL 与外键约束（SQLite 的 PRAGMA 是连接级的）。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# expire_on_commit=False：commit 之后 ORM 对象属性仍可直接读取，
# service 层提交后可以直接把对象交给 router 序列化
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类（models.py 中的表都继承它）。"""


def _migrate_task_columns(eng) -> None:
    """轻量列迁移：create_all 只建新表、不改已有表——模型新增列在此补 ALTER TABLE
    ADD COLUMN（SQLite 加列无锁表开销）。迁移需求再多时引入 Alembic，当前列清单驱动。
    engine 经参数注入而非绑死模块级全局——测试可用独立内存库直测本函数。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "tasks" not in inspector.get_table_names():
        return
    # 注意方法名是 get_columns（Inspector 无 get_table_columns，写错启动即崩）
    cols = {c["name"] for c in inspector.get_columns("tasks")}
    required = {
        "cancelled_dates": "JSON",  # 2026-09：重复系列"仅删除本次"的旧落点（已废弃，见墓碑迁移）
        "ever_scheduled": "BOOLEAN DEFAULT 0",  # 2026-09-14：删除策略"曾经排期过"判定
        "deleted_at": "DATETIME",  # 2026-09-14：软删时刻（TTL 计时锚）
        "done_at": "DATETIME",  # 2026-10-22：完成时刻（与软删正交，空 = 未完成）
        "source_key": "VARCHAR(60)",  # 2026-10-23：采集溯源源键（口径同 ingest_sources）
        "source_fingerprint": "VARCHAR(40)",  # 2026-10-23：来源条目指纹 sha1
        "source_evidence": "VARCHAR(500)",  # 2026-10-23：提炼时引用的素材摘录
        "all_day": "BOOLEAN DEFAULT 0",  # 2026-11-06 批 B：全天/跨天日程（占天不占时段）
    }
    missing = {name: ddl for name, ddl in required.items() if name not in cols}
    if not missing:
        return
    with eng.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}"))
        # 补 ever_scheduled 当次顺带 backfill：已排期（start_time 非空）旧行必然"曾经排期过"
        if "ever_scheduled" in missing:
            conn.execute(text("UPDATE tasks SET ever_scheduled = 1 WHERE start_time IS NOT NULL"))


def _migrate_app_settings_columns(eng) -> None:
    """app_settings 表的轻量列迁移（机制同 tasks 版：create_all 不改已有表，
    新增列在此补 ALTER TABLE ADD COLUMN；列清单驱动，需求再多再泛化）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "app_settings" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("app_settings")}
    required = {
        "default_view": "VARCHAR(10) DEFAULT 'day' NOT NULL",  # 2026-09-15：启动默认视图
        "widget_transparency": "INTEGER DEFAULT 20 NOT NULL",  # 2026-09-17：小部件透明度（同日默认 38 调 20）
        "ai_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-09-20：AI 功能主开关（阶段二）
        "morning_brief_min": "INTEGER DEFAULT 510 NOT NULL",  # 2026-09-20：晨间对齐时刻（分钟）
        "ingest_enabled": "BOOLEAN DEFAULT 0 NOT NULL",  # 2026-10-14：每日数据采集开关（默认关）
        "ingest_brief_min": "INTEGER DEFAULT 540 NOT NULL",  # 2026-10-14：数据采集时刻（分钟）
        "ingest_runs_per_day": "INTEGER DEFAULT 2 NOT NULL",  # 2026-10-23：每日采集轮数（1-4 档）
        "ingest_llm_budget": "INTEGER DEFAULT 30 NOT NULL",  # 2026-10-14：单日采集 LLM 调用上限（0=不限）
        "weather_city": "VARCHAR(50)",  # 2026-10-18：排期天气对照城市（空=停用）
        "ai_style": "VARCHAR(10) DEFAULT 'balanced' NOT NULL",  # 2026-10-20：AI 回复风格
        "ai_philosophy": "VARCHAR(100)",  # 2026-10-20：内置哲学预设 id 逗号串
        "ai_philosophy_custom": "TEXT",  # 2026-10-20：自定义哲学文本
        "nightly_start_min": "INTEGER DEFAULT 1200 NOT NULL",  # 2026-10-20：夜间工作流报点时刻（分钟）
        "morning_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-21：晨间对齐开关（复查批 D1）
        "nightly_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-21：夜间整理开关
        "prep_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-21：会前准备开关
        "hardtask_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-21：高难协助开关
        "weekly_report_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-11-07：周报自动生成开关（周日晚随夜间报点）
        "minutes_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-11-10：会后纪要邀请开关（重要会议结束后询问补记）
        "chat_autocompact_enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-26：对话上下文自动压缩开关
        "coach_probe_deep": "BOOLEAN DEFAULT 0 NOT NULL",  # 2026-11-01：教练溯源深探（有界工具循环，默认关）
        "assistant_chime": "BOOLEAN DEFAULT 0 NOT NULL",  # 2026-11-04：助手消息轻提示音（通知分轨，默认关）
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE app_settings ADD COLUMN {name} {required[name]}"))


def _migrate_integration_settings_columns(eng) -> None:
    """integration_settings 表的轻量列迁移（机制同 app_settings 版：create_all
    不改已有表，新增列在此补 ALTER TABLE ADD COLUMN；列清单驱动）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "integration_settings" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("integration_settings")}
    required = {
        "local_folder_path": "VARCHAR(500)",  # 2026-09-18：内置本地文件夹 MCP 服务根路径（同日废弃，迁移源）
        "local_folder_enabled": "BOOLEAN DEFAULT 0 NOT NULL",  # 2026-09-18：本地文件夹服务开关
        "local_folder_paths": "JSON",  # 2026-09-18：多目录根路径列表（单路径列废弃后的落点）
        "local_folder_test_ok": "BOOLEAN",  # 2026-09-20：上次卡上「测试」结果（持久化展示在卡标题旁徽标）
        "local_folder_test_files": "INTEGER",  # 2026-09-20：上次测试文件计数（无效时为空）
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE integration_settings ADD COLUMN {name} {required[name]}"))


def _migrate_mcp_server_columns(eng) -> None:
    """mcp_servers 表的轻量列迁移（机制同 app_settings 版：create_all 不改已有表，
    新增列在此补 ALTER TABLE ADD COLUMN；列清单驱动）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "mcp_servers" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("mcp_servers")}
    required = {
        "description": "VARCHAR(100)",  # 2026-09-19：服务描述（添加/JSON 配置双模式共用）
        "last_test_tools": "INTEGER",  # 2026-09-20：上次测试工具数（徽标"连通 · Nms · N 个工具"）
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE mcp_servers ADD COLUMN {name} {required[name]}"))


def _migrate_memory_rule_columns(eng) -> None:
    """memory_rules 补列（2026-11-01 优化批 8 structured_anchor；2026-11-03 记忆
    重构扩 slot 槽位 / observation_count 印证计数）——可空/带默认列，老库残留无害
    （同 CliTool.test_* 先例）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "memory_rules" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("memory_rules")}
    required = {
        "structured_anchor": "JSON",            # 高置信规则的结构化避让窗锚
        "slot": "VARCHAR(10)",                  # 固定槽位（NULL=旧行待夜间回填）
        "observation_count": "INTEGER DEFAULT 0 NOT NULL",  # 同义印证计数（置信演化）
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE memory_rules ADD COLUMN {name} {required[name]}"))


def _migrate_stats_report_columns(eng) -> None:
    """stats_reports 补列（建议追踪闭环）：payload 结构化留档（advice_items/
    health 快照），可空列老库残留无害（同 memory_rules 补列先例）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "stats_reports" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("stats_reports")}
    required = {
        "payload": "JSON",  # 结构化留档：advice_items/health/keep_habits
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE stats_reports ADD COLUMN {name} {required[name]}"))


# episodic_facts FTS5 检索能力开关（_ensure_episodic_fts probe 置 False 后检索层
# 恒走 LIKE 回落——老 SQLite（<3.34 无 trigram 分词器）打包形态的功能不残废）
_FTS_TRIGRAM_OK = True


def _ensure_episodic_fts(eng) -> None:
    """episodic_facts 的 FTS5 trigram 全文索引（2026-11-03 记忆重构 Tier 2）。

    - 虚表 external-content 模式（content='episodic_facts'）：索引挂在主表 rowid
      上，原文只存主表一份——ORM 写主表，FTS 行由三个触发器（INSERT/DELETE/
      UPDATE）单点同步。应用层双写（五条写路径漏一即索引漂移）不可靠，故选
      触发器；create_all 不认虚表，故新库老库恒经本函数（init_db 挂载）。
    - tokenize='trigram'：中文无空格分词，unicode61 把连续汉字当一个 token 无法
      检索；trigram 按 3 字符滑窗切分，中文子串匹配唯一正确选择（要求 SQLite
      ≥3.34，实测打包环境 3.53.2）。<3 字符查询无 3-gram 可配，检索层回落 LIKE。
    - capability probe：trigram 不可用时 OperationalError 捕获 → _FTS_TRIGRAM_OK
      = False + ERROR 日志（启动日志即打包抽查锚点），检索层自动 LIKE 回落。
    - 全部 IF NOT EXISTS 幂等；主表 drop 重建时触发器随表删除、本函数重建。
    """
    global _FTS_TRIGRAM_OK
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "episodic_facts" not in inspector.get_table_names():
        return
    ddl = [
        "CREATE VIRTUAL TABLE IF NOT EXISTS episodic_facts_fts USING fts5("
        "topic, fact, content='episodic_facts', content_rowid='rowid', tokenize='trigram')",
        "CREATE TRIGGER IF NOT EXISTS episodic_facts_ai AFTER INSERT ON episodic_facts BEGIN "
        "INSERT INTO episodic_facts_fts(rowid, topic, fact) VALUES (new.rowid, new.topic, new.fact); END",
        # external-content 表的删除语法：'delete' 指令行带原文供索引器定位 3-gram
        "CREATE TRIGGER IF NOT EXISTS episodic_facts_ad AFTER DELETE ON episodic_facts BEGIN "
        "INSERT INTO episodic_facts_fts(episodic_facts_fts, rowid, topic, fact) "
        "VALUES ('delete', old.rowid, old.topic, old.fact); END",
        "CREATE TRIGGER IF NOT EXISTS episodic_facts_au AFTER UPDATE ON episodic_facts BEGIN "
        "INSERT INTO episodic_facts_fts(episodic_facts_fts, rowid, topic, fact) "
        "VALUES ('delete', old.rowid, old.topic, old.fact); "
        "INSERT INTO episodic_facts_fts(rowid, topic, fact) VALUES (new.rowid, new.topic, new.fact); END",
    ]
    try:
        with eng.begin() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))
    except Exception as exc:  # OperationalError：sqlite 无 fts5/trigram 编译单元
        _FTS_TRIGRAM_OK = False
        import logging

        logging.getLogger(__name__).error(
            "episodic FTS5 trigram 索引不可用（%s），事实检索回落 LIKE 扫描", exc)


def _migrate_model_provider_columns(eng) -> None:
    """model_providers 补列（2026-11-01 优化批 9）：is_aux 轻量助手槽——
    布尔默认 0，老库行为不变。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "model_providers" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("model_providers")}
    if "is_aux" not in cols:
        with eng.begin() as conn:
            conn.execute(text(
                "ALTER TABLE model_providers ADD COLUMN is_aux BOOLEAN DEFAULT 0"))


def _migrate_cli_tool_columns(eng) -> None:
    """cli_tools 表的轻量列迁移（机制同 mcp_servers 版：列清单驱动 ADD COLUMN）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "cli_tools" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("cli_tools")}
    required = {
        # 2026-10-16：自定义行「测试」按钮结果（近 24h 窗真跑采集命令 + 条目数）
        "test_ok": "BOOLEAN",
        "test_items": "INTEGER",
        "test_at": "DATETIME",
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE cli_tools ADD COLUMN {name} {required[name]}"))


def _migrate_ingest_source_columns(eng) -> None:
    """ingest_sources 表的轻量列迁移（机制同 app_settings 版）。表本身由 create_all
    建新库；老库（首版只有游标两列）在此补健康度 / 配置 / 降频列。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "ingest_sources" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("ingest_sources")}
    required = {
        "fail_streak": "INTEGER DEFAULT 0 NOT NULL",  # 2026-10-14：连败计数（M2-⑤A 健康度）
        "last_error": "VARCHAR(200)",  # 2026-10-14：最近失败摘要
        "last_ok_at": "DATETIME",  # 2026-10-14：最近成功时刻
        "enabled": "BOOLEAN DEFAULT 1 NOT NULL",  # 2026-10-14：参与每日采集开关（M2-②）
        "fetch_tool": "VARCHAR(100)",  # 2026-10-14：采集工具名覆盖
        "fetch_args": "JSON",  # 2026-10-14：采集参数覆盖
        "window_hours": "INTEGER",  # 2026-10-14：覆盖时间窗（小时）
        "pause_until": "DATETIME",  # 2026-10-14：信任降频暂停至（M2-③）
        "trusted": "BOOLEAN DEFAULT 0 NOT NULL",  # 2026-10-24：高可信直通开关
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE ingest_sources ADD COLUMN {name} {required[name]}"))


def _migrate_ingestion_item_columns(eng) -> None:
    """ingestion_items 表的轻量列迁移（机制同上：老库补 2026-10-24 高可信直通的
    条目域列；空 = 未声明恒不直通——存量行跨轮次由采集端再见到时自然补齐）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "ingestion_items" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("ingestion_items")}
    required = {
        "domain": "VARCHAR(10)",  # 2026-10-24：条目域 todo/calendar/doc（空=未声明）
    }
    missing = [name for name in required if name not in cols]
    if not missing:
        return
    with eng.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE ingestion_items ADD COLUMN {name} {required[name]}"))


def _migrate_chat_channel_tables(eng) -> None:
    """聊天通讯两张表的轻量列迁移（2026-10-23 增，机制同 cli_tools 版）：messages
    补 source 来源列（老库既有消息视为桌面端 NULL）；chat_channels 为整新表由
    create_all 建新库，老库（二批前接入过渠道）在此补推送开关/目标两列。"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    tables = inspector.get_table_names()
    if "messages" in tables:
        cols = {c["name"] for c in inspector.get_columns("messages")}
        required = {
            "source": "VARCHAR(20)",  # 2026-10-23：消息来源渠道；空 = 桌面端
        }
        missing = [name for name in required if name not in cols]
        if missing:
            with eng.begin() as conn:
                for name in missing:
                    conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {required[name]}"))
    if "chat_channels" in tables:
        cols = {c["name"] for c in inspector.get_columns("chat_channels")}
        required = {
            # 2026-10-23 二批：定时任务推送渠道开关与目标缓存
            "push_enabled": "BOOLEAN",
            "push_target": "VARCHAR(200)",
        }
        missing = [name for name in required if name not in cols]
        if missing:
            with eng.begin() as conn:
                for name in missing:
                    conn.execute(text(f"ALTER TABLE chat_channels ADD COLUMN {name} {required[name]}"))
        # 非空列回填：SQLite ADD COLUMN 不带 DEFAULT 时既有行为 NULL（真机 500
        # 实锤——ChatChannelOut.push_enabled: bool 校验炸），无论本次新加还是历史
        # 版本已加未填，统一归 0；push_target 可空不回填
        with eng.begin() as conn:
            conn.execute(text(
                "UPDATE chat_channels SET push_enabled = 0 WHERE push_enabled IS NULL"))


def _migrate_local_folder_paths(eng) -> None:
    """一次性数据迁移：旧单路径列 local_folder_path（2026-09-18 上午形态）转为
    多目录列表 local_folder_paths（同日下午多目录扩容）。旧值非空的行转为
    [旧值] 列表后置旧列 NULL——置 NULL 即幂等标记，二次启动不再命中
    （机制同 _migrate_cancelled_dates_to_tombstones；本列是 String 非 JSON，
    无序列化坑，置 NULL 走原生 SQL 仅为统一惯例）。"""
    from sqlalchemy import text

    with eng.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, local_folder_path FROM integration_settings "
                "WHERE local_folder_path IS NOT NULL"
            )
        ).all()
        for rid, old_path in rows:
            conn.execute(
                text(
                    "UPDATE integration_settings SET local_folder_paths = :paths, "
                    "local_folder_path = NULL WHERE id = :rid"
                ),
                {"paths": json.dumps([old_path]), "rid": rid},
            )


def _migrate_cancelled_dates_to_tombstones(eng) -> None:
    """一次性数据迁移：旧 cancelled_dates JSON 列（2026-09-13 单次例外落点）转为
    墓碑行建模（2026-09-14 起）。对每个有旧数据的行，逐日期生成墓碑行（parent_id
    指向主任务 + original_time = 该日期 + 主任务开始钟点 + title 复制），完成后置旧列
    NULL——置 NULL 即幂等标记，二次启动不再命中。删除策略判定见 services.delete_task。"""
    import models  # 延迟导入（同 init_db 惯例，避免循环依赖）
    from sqlalchemy import select, text

    with eng.begin() as conn:
        rows = conn.execute(
            select(
                models.Task.id, models.Task.title, models.Task.start_time, models.Task.cancelled_dates
            ).where(
                models.Task.cancelled_dates.is_not(None),
                models.Task.parent_id.is_(None),  # 墓碑行自身不带 cancelled_dates
                models.Task.is_deleted.is_(False),
            )
        ).all()
        for task_id, title, start_time, dates in rows:
            # 实例钟点取主任务开始时刻，无排期基准则当日 0 点
            clock = start_time.time() if start_time is not None else None
            for date_str in dates or []:
                y, m, d = (int(x) for x in date_str.split("-"))
                original = datetime(y, m, d, clock.hour, clock.minute) if clock else datetime(y, m, d)
                # Core insert：显式给墓碑四要素，其余列（priority/时间戳等）走默认值
                conn.execute(
                    models.Task.__table__.insert().values(
                        id=models._new_uuid(), title=title, parent_id=task_id,
                        original_time=original, is_deleted=True,
                    )
                )
            # 原生 SQL 置 NULL：JSON 列走 Core update 会把 None 序列化成字符串
            # 'null' 落库（读回虽也解析为 None，但破坏 is_not(None) 幂等判定）
            conn.execute(
                text("UPDATE tasks SET cancelled_dates = NULL WHERE id = :tid"),
                {"tid": task_id},
            )


def _drop_task_dependencies(eng) -> None:
    """一次性清理：DROP 任务依赖表（2026-10-22 批 4 引入、2026-10-23 整功能撤除
    ——前端 UI 与后端全套同批退役）。create_all 不会删既有表，旧库残留死表会误导
    "功能还在"，这里显式 DROP（IF EXISTS 幂等，新库无表零影响）。"""
    from sqlalchemy import text

    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS task_dependencies"))


def init_db() -> None:
    """按 models 建表（已存在的表不会被改动，新增列由 _migrate_task_columns 补齐，
    旧 cancelled_dates 数据由墓碑迁移一次性转换）。"""
    import models  # noqa: F401  # 导入以确保模型类注册到 Base.metadata

    Base.metadata.create_all(engine)
    _migrate_task_columns(engine)
    _migrate_app_settings_columns(engine)
    _migrate_integration_settings_columns(engine)
    _migrate_mcp_server_columns(engine)
    _migrate_cli_tool_columns(engine)
    _migrate_ingest_source_columns(engine)
    _migrate_ingestion_item_columns(engine)
    _migrate_chat_channel_tables(engine)
    _migrate_memory_rule_columns(engine)
    _migrate_stats_report_columns(engine)
    _migrate_model_provider_columns(engine)
    _migrate_local_folder_paths(engine)
    _migrate_cancelled_dates_to_tombstones(engine)
    _drop_task_dependencies(engine)
    _ensure_episodic_fts(engine)


def get_db():
    """FastAPI 依赖：请求级 Session，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
