"""用户配置快照与自愈恢复（2026-10-22 增，真机「配置反复丢失」根治）。

背景：全部用户配置（模型接入/数据接入/偏好/记忆画像）与业务数据同库同生死——
按 CLAUDE.md 指引删 backend/data/clara.db 清测试数据时，配置连坐清空（2026-10-21
真机实测复现）。机制：每次启动把配置表（清单见 _CONFIG_TABLES）整表 dump 到
data/config_snapshot.json；下次启动若检测到「库刚重建」特征（模型/数据接入四表
全空）且快照存在，先整表恢复再继续——无论谁删了库文件，配置都能回来。

- 快照时点：启动时覆盖写（上次会话的全部变更都在库里）。用户改配置后未重启
  就删库的窗口内快照滞后一版——恢复旧 key 也远好于全丢，日志明示行数可核对；
- 触发特征：model_providers / mcp_servers / cli_tools / ingest_sources /
  chat_channels 五表全零行。正常使用中这几类配置同时清空的概率近乎零（真删光
  恢复回来也无害，重删即可），app_settings / custom_briefs 不参与判定（默认行
  本就可能「空」）；
- 表清单单源 _CONFIG_TABLES；Core 读写（Base.metadata 按名取表），不依赖
  ORM 类——新增配置表只需清单加一行。
"""

import json
import logging
from pathlib import Path

from sqlalchemy import delete, select

from config import get_data_dir
from database import Base

logger = logging.getLogger(__name__)

# 配置表清单（dump/恢复范围；顺序即恢复顺序，无外键依赖约束下任意）
_CONFIG_TABLES = [
    "model_providers",      # 模型接入（含 API key 与激活态）
    "integration_settings", # Composio key / 本地文件夹路径
    "mcp_servers",          # MCP 连接
    "cli_tools",            # CLI 工具行（含自定义命令）
    "ingest_sources",       # 采集源（游标/健康度一并带回）
    "app_settings",         # 用户偏好（开关/时刻/天气城市/人格）
    "custom_briefs",        # 自定义简报
    "chat_channels",        # 聊天通讯渠道（含应用凭据——快照随库同目录，安全注记见 docs/chat-comm.md）
    # 2026-11-03 记忆重构补：记忆画像与纠偏账——此前删库重建后记忆全丢（用户画像
    # 亦属「配置」）；不进 _EMPTY_TRIGGER_TABLES 判定（记忆表空是合法常态，如 seed 前）
    "memory_rules",
    "memory_dismissals",
    # 2026-11-14 同步备份补：WebDAV 配置与自动备份目录（含明文密码——快照随库
    # 同目录，安全注记同 chat_channels 行）；不进 _EMPTY_TRIGGER_TABLES（开关/目录
    # 是设备本地配置，恢复回来无害且应该恢复）；**不参与 WebDAV 同步与合并导入**
    # （sync_engine.SYNC_TABLES 排除——凭证绝不上服务器）
    "sync_settings",
]
# 「库刚重建」判定表（同时空 = 删库重灌特征）
_EMPTY_TRIGGER_TABLES = ["model_providers", "mcp_servers", "cli_tools",
                         "ingest_sources", "chat_channels"]


def snapshot_path() -> Path:
    """快照文件路径（与数据库同目录；删库只删 .db，快照独立存活）。"""
    return get_data_dir() / "config_snapshot.json"


def dump_config(db) -> dict:
    """七张配置表整表 dump → {表名: [行 dict, ...]}（值口径单源 row_codec.ser_value
    ——与同步域规范行同内核，2026-11-17 架构批 2）。"""
    from services.row_codec import ser_value

    tables = {t.name: t for t in Base.metadata.sorted_tables}
    snap: dict = {}
    for name in _CONFIG_TABLES:
        table = tables[name]
        snap[name] = [
            {c: ser_value(v) for c, v in zip(table.columns.keys(), row)}
            for row in db.execute(select(table)).all()
        ]
    return snap


def restore_config(db, snap: dict) -> int:
    """整表恢复（先清后插，幂等）。返回恢复总行数。isoformat 字符串按目标列
    类型转回 datetime/date/time 对象（SQLite DateTime 方言只认对象不接受字符串）。"""
    from services.row_codec import bindable_value

    tables = {t.name: t for t in Base.metadata.sorted_tables}

    # 值级内核单源 row_codec（2026-11-17 架构批 2）：datetime 复活 / 非空布尔
    # None 归 False（历史 ADD COLUMN 踩坑防御）/ JSON 列显式 None → null()
    # （af6daf2 同型修复，恢复路径与同步路径自此同一实现）
    total = 0
    for name in _CONFIG_TABLES:
        rows = snap.get(name) or []
        table = tables[name]
        db.execute(delete(table))
        if rows:
            db.execute(table.insert(), [
                {c: bindable_value(table.columns[c], v) for c, v in row.items()}
                for row in rows])
        total += len(rows)
    db.commit()
    return total


def maybe_restore_and_snapshot(db) -> bool:
    """启动自愈主入口：检测库刚重建特征 → 恢复快照；随后无条件覆盖写新快照。
    返回是否发生了恢复（调用方仅用于日志，行为已自足）。"""
    path = snapshot_path()
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    rebuilt = all(
        db.execute(select(tables[name]).limit(1)).first() is None
        for name in _EMPTY_TRIGGER_TABLES)
    if rebuilt and path.exists():
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
            total = restore_config(db, snap)
            logger.warning("检测到配置表全空（库被删重建？），已从快照恢复 %d 行配置"
                           "（快照 %s）", total, path.name)
            # 恢复可能带回 chat_channels 凭据——通知 worker 重启（惰性导入防环；
            # worker 未运行时 no-op，启动时 start_all 读的即恢复后的最终态）
            from services import chat_channels
            chat_channels.invalidate()
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("配置快照损坏，跳过恢复（%s）", exc)
    try:
        path.write_text(json.dumps(dump_config(db), ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    except OSError as exc:  # 快照失败不阻塞启动——下次启动再试
        logger.warning("配置快照写入失败（%s）", exc)
    return rebuilt
