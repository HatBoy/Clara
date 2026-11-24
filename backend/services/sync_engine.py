"""WebDAV 同步引擎（2026-11-14 同步备份）：快照基线 + 表级增量 + 行级三路合并。

三参考系：Local（当前 SQLite）/ Snapshot=Base（上次同步后每表副本，data/
sync_snapshot/）/ Remote（WebDAV 上 tables/<t>.json）。合并真值表与三守卫、
冷启动三分支的完整语义见 docs/sync-backup.md；本模块头只记实现要点：

- 增量核心：表内容 sha1 双向比对（本地 vs Base、远端 manifest vs Base），两边
  都未变则跳过（manifest.hash==Base.hash 时连 GET 都省，R 直接用 Base 行集）；
- manifest.hash 仅作跳过判据，合并以 fetch_table 实际内容为准（并发 PUT 后的
  短暂不一致可自愈）；推序铁律：先全部表文件后 manifest；
- 防死循环：脏表收集与 SYNC_TABLES 求交集（sync_now 写 last_sync_* 不得触发
  新一轮同步）；本引擎写库走 Core execute 不进 after_flush，须显式 mark；
- 自然键撞车回退：UUID 主键下 add-add 不撞 pk，但 projects.name 等 8 处唯一
  约束仍可能两设备各自建同名行——INSERT 撞约束后按自然键定位本地行走 LWW，
  落库实际行集（actual）作为 Base 与远端推送内容（保 Base=本地=远端三者一致，
  下轮不再撞）；
- 写库单事务 + PRAGMA defer_foreign_keys=ON（解 tasks.parent_id 自引用的插入
  顺序与跨表删除顺序）；事务内零网络 IO（先算后写本地再推远端最后存快照）；
- 快照原子写（tmp + os.replace，中断不留半文件）；快照保存失败 = 整轮失败
  （否则下轮无法检测删除——TabDav 项目最高优先教训）；
- 同步完成后经 workflows.broadcast 发 {"type": "sync", ...} 事件（call_soon_
  threadsafe 桥接——workflows 的 asyncio.Queue 非线程安全，同 ai_coach 惯例），
  前端据此刷新主窗口数据与设置页状态行。
"""

import asyncio
import json
import logging
from services.device_secrets import portable, portable_update
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_data_dir
from database import Base
import database
from services.sync_config import get_sync_config
from services.sync_webdav import WebDavClient, WebDavError

logger = logging.getLogger(__name__)

# —— 同步范围（22 张全部既有表；sync_settings 排除——WebDAV 凭证绝不上 WebDAV
#    服务器、备份目录属设备本地配置；FTS5 虚表不在 Base.metadata 天然排除）——
# 排除表（2026-11-16 优化批 3）：
# - sync_settings：同步自身状态（last_sync_*），同步它会触发无限空转
# - workflow_runs：设备本地执行账本（"今日已跑"认领）——跨设备同步会让先同步
#   到的设备抑制另一台的自动备份/简报槽位
SYNC_EXCLUDED_TABLES = {
    "sync_settings",     # 同步自身状态
    "workflow_runs",     # 设备本地执行账本
    "ai_usage_events",   # 设备本地 AI 用量遥测（2026-11-17 批 3）
}
SYNC_TABLES: list[str] = [
    t.name for t in Base.metadata.sorted_tables if t.name not in SYNC_EXCLUDED_TABLES
]
# 自然键唯一约束（models.py 实际定义）：INSERT 撞约束时按这些列定位本地行走 LWW。
# 单行表（app_settings/integration_settings/chat_state，id 恒 1）同键整行 LWW
# 天然成立不单列
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "projects": ("name",),
    "model_providers": ("name",),
    "mcp_servers": ("name",),
    "chat_channels": ("provider",),
    "reminders": ("task_id", "fire_at"),
    "workflow_runs": ("name", "run_date"),
    "ingest_sources": ("source_key",),
    "ingestion_items": ("source_key", "fingerprint"),
}

# —— 触发节奏 —————————————————————————————————————————————————————
DEBOUNCE_S = 3        # 变更防抖
PROBE_S = 30          # 远端 manifest 轻探测（对端新数据 ~30s 可见，2026-11-15 迭代四）
PERIODIC_S = 300      # 周期兜底（防脏表漏标）
START_DELAY_S = 2     # 启动延迟（lifespan 顺序已保证建库/恢复/seed 先完成；原 15s
                      # 过度保守——2026-11-16 真机批：新装首拉等 15s 才开始是启动
                      # 空白近分钟的直接原因之一）
MAX_PERIOD_S = 1800   # 失败退避上限（300→600→1200→1800，成功复位；限频 503 跳升）

# —— 守卫阈值 —————————————————————————————————————————————————————
DELETE_GUARD_MIN = 100  # 删除行数绝对下限

# 首同步落在云端的使用说明（回答「打开 WebDAV 全是 json，数据在哪里」——
# tables/*.json 即数据文件；附手动恢复路径）。仅 manifest 404 时写一次
_README = """\
这是 Clara（AI 日程助手）的同步数据目录，由设备的「设置 → 同步备份 → WebDAV 同步」自动维护。

数据在哪：tables/ 目录下每个 .json 文件就是一张数据表（tasks=日程任务、
messages=聊天记录、app_settings=设置项、memory_rules=记忆画像……），
文件内的 rows 数组就是行数据；manifest.json 只是各表版本的索引。

如何手动恢复（设备丢失/换新设备的灾难恢复路径）：
1. 把整个 clara-sync 目录（或其上级目录）下载后压缩为 zip；
2. 在 Clara 的「设置 → 同步备份 → 导入导出」中选「覆盖恢复」或「合并导入」，
   选择该 zip 即可——两种压缩方式（zip 根就是本目录 / 多包一层目录）都能识别。

请勿手动编辑这些 json 文件——多设备同步按内容指纹增量合并，手改会被
下一次同步覆盖或引发保守拦截。
"""


class SyncAbort(RuntimeError):
    """守卫命中：中止同步（中文 message 落 last_sync_error 直显用户）。"""


# —— 脏表收集（增量核心；after_flush 挂 Session 类——conftest 换 sessionmaker
#    后测试替身会话同样覆盖）—————————————————————————————————————————


_dirty_lock = threading.Lock()
_dirty_tables: set[str] = set()


def mark_tables_dirty(names) -> None:
    """标脏（∩ SYNC_TABLES——防 sync_settings 状态写入触发无限空转）。"""
    with _dirty_lock:
        _dirty_tables.update(n for n in names if n in SYNC_TABLES)


def take_dirty_tables() -> set[str]:
    """取走脏表集合（清空；同步自身写库后由实际比对兜底，丢标记无害）。"""
    with _dirty_lock:
        taken = set(_dirty_tables)
        _dirty_tables.clear()
        return taken


def peek_dirty_tables() -> set[str]:
    """窥视不清空（sync_loop 判定要不要跑）。"""
    with _dirty_lock:
        return set(_dirty_tables)


_dirty_hooked = False


def _reset_for_tests() -> None:
    """复位模块态（conftest fresh_db 调用）：脏表集合清空、唤醒信号复位、
    探测记忆清空、锁确保释放（异常路径残留会锁死后用例）。"""
    with _dirty_lock:
        _dirty_tables.clear()
    _wake.clear()
    global _main_loop, _last_seen_hashes
    _main_loop = None
    _last_seen_hashes = None
    if _sync_lock.locked():
        _sync_lock.release()


def register_dirty_hook() -> None:
    """挂 SQLAlchemy after_flush 事件（幂等；Core execute 不走 unit of work——
    sync/merge 自身批量写后显式 mark_tables_dirty）。"""
    global _dirty_hooked
    if _dirty_hooked:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession

    @event.listens_for(OrmSession, "after_flush")
    def _on_flush(session, flush_context):
        names: set[str] = set()
        for obj in session.new:
            names.add(type(obj).__tablename__)
        for obj in session.dirty:
            names.add(type(obj).__tablename__)
        for obj in session.deleted:
            names.add(type(obj).__tablename__)
        if names:
            mark_tables_dirty(names)

    _dirty_hooked = True
    logger.info("同步脏表 after_flush 钩子已挂载")


# —— 序列化与哈希（2026-11-17 架构批 2：口径族单源迁移 services/row_codec.py，
#    此处旧名降级为薄委托——测试与跨模块 import 的锚点逐字保真；新代码一律
#    直连 row_codec）———————————————————————————————————————————————————————

from services import row_codec  # noqa: E402（延迟至口径段集中导入）

_ser = row_codec.ser_value
_table_meta = row_codec.table_meta
table_rows = row_codec.dump_table
_hash_rows = row_codec.hash_rows
_row_ts = row_codec.row_ts
_rows_equal = row_codec.rows_equal
_revive_value = row_codec.revive_value
_revive_row = row_codec.revive_row
_writable_row = row_codec.writable_row


# —— 快照（Base；原子写）——————————————————————————————————————————————


def snapshot_dir() -> Path:
    return get_data_dir() / "sync_snapshot"


def has_base() -> bool:
    """是否存有快照（冷启动判定；meta 存在即认——个别表文件缺失由守卫 a 兜）。"""
    return (snapshot_dir() / "meta.json").is_file()


def _base_path(name: str) -> Path:
    return snapshot_dir() / f"{name}.json"


def _load_base(name: str) -> list[dict] | None:
    p = _base_path(name)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [portable(name, row) for row in data] if isinstance(data, list) else None
    except (ValueError, OSError):
        # 单表快照损坏按缺失处理（该表走 union 合并——保守不丢数据）
        logger.warning("同步快照损坏按缺失处理：%s", p.name)
        return None


def _save_base(name: str, rows: list[dict]) -> None:
    """原子写快照（tmp + os.replace；失败抛 OSError → 整轮标失败）。"""
    d = snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.json.tmp"
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _base_path(name))


def _save_base_meta(device_id: str) -> None:
    d = snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".meta.json.tmp"
    tmp.write_text(json.dumps({"device_id": device_id, "saved_at":
                               datetime.now().isoformat(timespec="seconds")}),
                   encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def forget_remote_memory() -> None:
    """清远端探测记忆（换服务器/账号时：旧服务器的 manifest hash 会误判
    "有变化/无变化"，2026-11-16 优化批 3）。"""
    global _last_seen_hashes
    _last_seen_hashes = None


def clear_snapshot() -> None:
    """清空快照（重置基线 / 覆盖恢复后调用——数据回到过去，Base 失效，下轮退化
    冷启动语义；守卫拦截后的用户出路）。"""
    import shutil

    d = snapshot_dir()
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        logger.info("同步快照已清空（基线重置）")


# —— 合法清空豁免（2026-11-16 真机二轮：守卫 b 误伤 /clear）————————————————
# /clear 类合法操作会把整表删空（messages / stats_messages 聊天攒过百条后必超
# 守卫 b 阈值被拦，且「重置基线」指引在该场景是错的——union 会把清空的消息
# 全拉回来）。clear 入口打表级豁免标记（落盘 cleared.json 防重启丢失），引擎
# 对豁免表跳过守卫 b 一次并消耗标记，删除正常传播到云端。

def _cleared_marker_path() -> Path:
    return get_data_dir() / "sync_cleared.json"


_cleared_lock = threading.Lock()


def _read_cleared_marker() -> dict:
    """读豁免标记 → {"version": int, "tables": [...]}（旧格式纯数组兼容）。"""
    p = _cleared_marker_path()
    if not p.is_file():
        return {"version": 0, "tables": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"version": 0, "tables": []}
    if isinstance(data, list):  # 旧格式（无版本号）
        return {"version": 0, "tables": [t for t in data if isinstance(t, str)]}
    if not isinstance(data, dict):
        return {"version": 0, "tables": []}
    tables = data.get("tables")
    return {"version": int(data.get("version") or 0),
            "tables": [t for t in (tables or []) if isinstance(t, str)]}


def note_table_cleared(table: str) -> None:
    """打合法清空标记（clear 入口调用）。幂等。

    版本号递增（2026-11-16 优化批 3）：同步轮只在"本轮开始时读到的版本"仍是
    当前版本时才消耗标记——同步期间用户再次 /clear 写的新标记不会被误删。
    同时标脏并唤醒同步（Core 批量删除不走 after_flush，此前清空要等 300s 或
    退出冲刷才传播）。"""
    with _cleared_lock:
        state = _read_cleared_marker()
        tables = set(state["tables"])
        tables.add(table)
        p = _cleared_marker_path()
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"version": state["version"] + 1,
                                       "tables": sorted(tables)}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            logger.warning("清空豁免标记落盘失败", exc_info=True)
    mark_tables_dirty([table])
    schedule_now()


def _load_cleared_tables() -> set:
    """读豁免标记（run_sync 每轮开头；同步成功才消耗——失败轮保留重试）。"""
    return set(_read_cleared_marker()["tables"])


def _consume_cleared_marker(version: int | None = None) -> None:
    """消耗豁免标记（run_sync 全链成功尾部调用）。version 不匹配 = 本轮期间
    有新清空标记写入 → 保留给下一轮。"""
    with _cleared_lock:
        state = _read_cleared_marker()
        if version is not None and state["version"] != version:
            return
        _cleared_marker_path().unlink(missing_ok=True)


# —— 三路合并（纯函数，直测友好）————————————————————————————————————————


def _winner(left: dict, right: dict) -> dict:
    """平局比规范行内容，不以整份 manifest 的最后写入设备冒充行作者。"""
    lt, rt = _row_ts(left), _row_ts(right)
    if lt != rt:
        return left if rt is None or (lt is not None and lt > rt) else right
    return min((left, right), key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))


def _canonical_rekeys(name, pk, local, remote) -> bool:
    """全部旧主键消失时，只有每个自然身份都保留且换为较小主键才属于重编号。"""
    keys = NATURAL_KEYS.get(name)
    if not keys:
        return False
    remote_ids = {tuple(row.get(k) for k in keys): row[pk] for row in remote}
    for row in local:
        identity = tuple(row.get(k) for k in keys)
        new_id = remote_ids.get(identity)
        if None in identity or new_id is None or not new_id < row[pk]:
            return False
    return True


def _merge_table(name: str, local: list[dict], base: list[dict] | None,
                 remote: list[dict] | None) -> dict:
    """行级三路合并。返回 {merged, del_remote, del_local}（del_* 为相对 R/L 的
    删除主键集，供守卫判定；merged 为最终行集）。

    真值表（键=主键；ts 判据 _row_ts；单行表同键自然整行 LWW）：
      L-  S-  R+ → 取 R（对端新增）
      L+  S-  R- → 取 L（本地新增）
      L-  S+  R+ → 若 R 比 Base 新（对端改过）取 R，否则删（本地删除传播）
      L+  S+  R- → 若 L 比 Base 新（本地改过）取 L，否则删（对端删除落地）
      其余（双方都有）→ ts 新者
    2026-11-16 优化批 3：删除 vs 修改不再"删除一律获胜"（此前对端删 + 本地
    后改的编辑静默丢失）——用 Base 行做"是否改过"判据，改过者胜。
    base=None（冷启动）：union + 同键取新（Case A/B/C 的统一公式——L 空则纯
    Pull、R 空则纯 Force Push、双有则并集）。

    平局收敛（优化批 3）：ts 完全相等时按规范行内容 做确定性
    选择——此前"本地优先"会让双端各留己方版本、互相推送 ping-pong 永不收敛。
    """
    _, pk = _table_meta(name)
    l_map = {r[pk]: r for r in local}
    r_map = {r[pk]: r for r in (remote or [])}
    s_map = {r[pk]: r for r in (base or [])} if base is not None else None

    merged: dict = {}

    def choose(left, right):
        winner = _winner(left, right)
        if name == 'proposals':
            from services.proposal_decisions import merge_synced
            try:
                return merge_synced(left, right, winner)
            except ValueError as exc:
                raise SyncAbort(f'建议结算冲突，未覆盖任何一端：{exc}') from exc
        return winner

    def modified_since(row: dict, base_row: dict) -> bool:
        rt, bt = _row_ts(row), _row_ts(base_row)
        return rt is not None and bt is not None and rt > bt

    for key in set(l_map) | set(r_map):
        l_row, r_row = l_map.get(key), r_map.get(key)
        if s_map is None:
            # 冷启动：union + 同键取新
            if l_row is None:
                merged[key] = r_row
            elif r_row is None:
                merged[key] = l_row
            else:
                merged[key] = choose(l_row, r_row)
            continue
        s_row = s_map.get(key)
        if l_row is not None and r_row is not None:
            merged[key] = choose(l_row, r_row)
        elif l_row is not None:
            if s_row is None:
                merged[key] = l_row            # 本地新增
            elif modified_since(l_row, s_row):
                merged[key] = l_row            # 对端删但本地后改过：改者胜
            # else：对端删且本地未动 → 本地删（不进 merged）
        elif r_row is not None:
            if s_row is None:
                merged[key] = r_row            # 对端新增
            elif modified_since(r_row, s_row):
                merged[key] = r_row            # 本地删但对端后改过：改者胜
            # else：本地删且对端未动 → 远端删（不进 merged）
        # 双无：不可能（键来自 L∪R）

    ordered = sorted(merged.values(), key=lambda r: str(r.get(pk, "")))  # 规范序
    return {"merged": ordered,
            "del_remote": set(r_map) - set(merged),
            "del_local": set(l_map) - set(merged)}


# —— 写库（单事务 + defer FK；返回实际落库行集）———————————————————————————
# 行口径族（_revive_value/_revive_row/_writable_row）已单源迁移 row_codec，
# 本文件顶部以同名薄委托续租——af6daf2 类「一处修三处漏」自此结构性不可能。


def _write_local(db: Session, name: str, merged: list[dict],
                 local_map: dict, deletable: set[str] | None = None) -> tuple[list[dict], int, int, set]:
    """把合并行集写进本地（单事务由调用方统一 commit；本函数只 execute）。
    返回 (实际落库行集, 插入数, 更新数, 待删键集)。

    - 键不在本地 → INSERT；撞自然键唯一约束 → pk 收敛（canonical = 双方 pk
      字典序小者；内容 ts 新者胜、平局规范内容小者；本地 pk 败时子表外键改指
      canonical——2026-11-17 真机实证旧版双端各持己 pk 互推永不收敛）；
    - 键在本地且内容不同 → UPDATE（显式带 updated_at——触发器 WHEN 守卫不覆盖）；
    - 本地有而 merged 无 → 进待删键集（**不内联删**——2026-11-17 真机批：
      父表先处理时子表删除行仍在库，immediate FK 语句级即炸；调用方收集后
      按逆拓扑序子先父后统一执行，不再依赖 defer_foreign_keys 落在对的连接）。
    实际落库行集 = merged 经撞车收敛后的形态，按 pk 规范序（保 Base=本地=远端
    一致且跨轮 sha1 稳定）。
    """
    table, pk = _table_meta(name)
    cols = table.columns.keys()
    actual: dict[str, dict] = {}  # pk → 行（撞车收敛可能重复触达同键，dict 去重）
    inserted = updated = 0

    def update_winner(key, row, old):
        target = table.update().where(getattr(table.c, pk) == key)
        if "updated_at" in cols and row.get("updated_at") == old.get("updated_at"):
            # 同时间戳直接 UPDATE 会触发业务时钟盖章；过渡值只在写事务内可见。
            db.execute(target.values(updated_at=text("''")))
        db.execute(target.values(**_writable_row(table, portable_update(name, row, old))))

    merged_map = {r[pk]: r for r in merged}
    # 迭代副本（2026-11-16 优化批 3）：撞自然键时会重定向 merged_map 的键，
    # 边迭代边改会 RuntimeError
    for key, row in list(merged_map.items()):
        local_row = local_map.get(key)
        if local_row is None:
            values = _writable_row(table, row)
            try:
                db.execute(table.insert().values(**values))
                inserted += 1
                actual[row[pk]] = row
            except IntegrityError:
                # 自然键 add-add 撞车（UUID 主键不撞 pk）→ pk 收敛（2026-11-17
                # 双机真机实证修复）：旧版各自保留本地 pk（远端内容落本地 pk /
                # 平局无条件本地胜），双端各持一版互推——远端 pk 逐轮翻转永不
                # 收敛，每轮探测白推一次（限频服务持续烧配额）。现规则双端
                # 对称：canonical pk = 双方 pk 字典序小者；内容 = ts 新者（平局
                # 取规范内容小者，双端独立计算同一胜者）；本地 pk 败时子表外键
                # 改指胜者、删本地行、插胜者行。
                hit = _find_by_natural_key(db, name, row)
                if hit is None:
                    # 未落库的行绝不能进 actual（优化批 3）：推送它会让远端持有
                    # 本地不存在的行，下一轮判"本地删除"并把远端也删掉。
                    logger.warning("同步插入撞约束且无法按自然键定位（%s/%s），跳过",
                                   name, key)
                    merged_map.pop(key, None)
                    continue
                canonical = min(key, hit[pk])
                winner = _winner(hit, row)
                out = {**winner, pk: canonical}
                if canonical != hit[pk]:
                    # 本地 pk 败：子表外键改指 canonical → 删本地行 → 插胜者行。
                    # 中间态（子表先指新 pk、新父行后落）在 immediate FK 下必炸
                    # ——defer_foreign_keys 把检查推到事务提交（run_sync/apply_merge
                    # 经 writing() 已设；此处保留为 _write_local 直调场景的兜底，
                    # test_sync_engine 的 pk 收敛直测依赖）
                    db.execute(text("PRAGMA defer_foreign_keys=ON"))
                    _repoint_children(db, name, hit[pk], canonical)
                    # 修改原行主键而非删建，保留同目的地设备授权，绝不带进 actual。
                    update_winner(hit[pk], out, hit)
                    updated += 1
                elif winner is row and not _rows_equal(out, hit, cols):
                    update_winner(canonical, out, hit)
                    updated += 1
                local_map.pop(hit[pk], None)
                # 键重定向（优化批 3 语义保留）：末尾 doomed 不把已收敛行当
                # "本地删除"误删（pk 败者已显式删并移出 local_map）；union 里
                # 败者键（可能已被先行处理进 actual）一并清除，防 actual 残留
                # 本地已不存在的幽灵行
                merged_map.pop(key, None)
                merged_map.pop(hit[pk], None)
                actual.pop(hit[pk], None)
                merged_map[canonical] = out
                actual[canonical] = out
                continue
        if local_row is not None:
            if _rows_equal(row, local_row, cols):
                actual[local_row[pk]] = local_row  # 内容相同：不写，推送本地形态
                continue
            update_winner(key, row, local_row)
            updated += 1
            actual[key] = row

    # 删除（本地有而 merged 无）。deletable 限定删除范围（优化批 3）：规划与
    # 写库之间的新写入不在规划键集内，绝不能被"本地删除传播"误删。
    # 2026-11-17 真机批：**不再内联执行**——表按父前子后处理，父表删除时子表
    # 的对端删除行仍在库，immediate FK 下语句级即炸（defer_foreign_keys 在
    # 与后台线程共用连接池时可能不落在执行连接上，实测偶发 IntegrityError
    # 卡死整轮同步）。改由调用方收集后按逆拓扑序（子先父后）统一执行。
    scope = set(local_map) if deletable is None else (set(local_map) & set(deletable))
    doomed = scope - set(merged_map)
    # 规范序返回（与 table_rows 同序：跨轮 sha1 稳定，推送跳过判定才可靠）
    ordered = sorted(actual.values(), key=lambda r: str(r.get(pk, "")))
    return ordered, inserted, updated, doomed


def _repoint_children(db: Session, parent_name: str, old_pk: str, new_pk: str) -> None:
    """自然键撞车 pk 收敛的子表外键改指（defer FK 事务内调用，末态自洽）。

    子行 updated_at 会被触发器盖 now——子行确实变了（归属换了主键），语义
    成立；同步链把它们作为正常变更推给对端。"""
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_keys:
            if fk.column.table.name == parent_name:
                db.execute(t.update().where(
                    getattr(t.c, fk.parent.name) == old_pk
                ).values({fk.parent.name: new_pk}))


def find_by_natural_key(db: Session, name: str, row: dict,
                        *, keys=None) -> dict | None:
    """按自然键唯一约束查本地行（撞车回退定位，2026-11-17 架构批 5 公有化——
    sync_merge 的最后一个私有名伸手退役）。

    keys 缺省取 NATURAL_KEYS[name]（策略常量单源）；显式传入供未来消费方
    覆盖键域。"""
    if keys is None:
        keys = NATURAL_KEYS.get(name)
    if not keys:
        return None
    table, _ = _table_meta(name)
    conds = [getattr(table.c, k) == _revive_value(table.columns[k], row[k]) for k in keys]
    hit = db.execute(select(table).where(*conds).limit(1)).first()
    if hit is None:
        return None
    cols = table.columns.keys()
    return portable(name, {c: _ser(v) for c, v in zip(cols, hit)})


# 内部旧名兼容锚点（_write_local 撞车回退定位沿用）
_find_by_natural_key = find_by_natural_key


class _Writer:
    """writing(db) 的登记面：删除延迟到事务退出时逆拓扑统一执行。"""

    __slots__ = ("_deletes",)

    def __init__(self):
        self._deletes: list[tuple[str, str, set]] = []

    def delete(self, name: str, pk: str, keys: set) -> None:
        """登记待删键集（子先父后由 writing 退出序保证）。"""
        if keys:
            self._deletes.append((name, pk, keys))


@contextmanager
def writing(db: Session):
    """同步域写事务（2026-11-17 架构批 5：三处 PRAGMA + commit/rollback 收编单源）。

    进入执行 PRAGMA defer_foreign_keys=ON（immediate FK 语句级检查推迟到提交
    ——撞自然键 pk 收敛的中间态「子表先指新 pk、新父行后落」依赖它）；正常
    退出按登记逆序（子先父后）统一执行删除再 commit——父表删除时子表的对端
    删除行必须已清（2026-11-17 真机批教训：每表内联删在父先处理时撞 FK、
    defer_foreign_keys 与后台线程共用连接池时可能不落在执行连接——逆拓扑
    统一执行 + 本上下文的确定性时序根治）；异常 rollback + 重抛。
    事务内零网络 IO 由调用方保证（写库顺序铁律）。
    """
    w = _Writer()
    db.execute(text("PRAGMA defer_foreign_keys=ON"))
    try:
        yield w
        for name, pk, doomed in reversed(w._deletes):
            table, _ = _table_meta(name)
            db.execute(delete(table).where(getattr(table.c, pk).in_(doomed)))
        db.commit()
    except Exception:
        db.rollback()
        raise


# —— 主流程 ——————————————————————————————————————————————————————————

# 互斥锁（手动 run / 循环 / merge 后调度共用；try_acquire 防重入）
_sync_lock = threading.Lock()
# 主事件循环引用（broadcast 线程桥用；测试环境无循环则跳过广播）
_main_loop: asyncio.AbstractEventLoop | None = None
# 防抖唤醒信号（PATCH 开启开关 / merge-apply 后立即调度）
_wake = threading.Event()


def schedule_now() -> None:
    """请求尽快同步（防抖通道：loop 侧等 DEBOUNCE_S 后执行）。"""
    _wake.set()


def is_syncing() -> bool:
    return _sync_lock.locked()


class SyncBusy(Exception):
    """同步进行中，危险操作（重置基线/恢复/合并导入）被拒。"""


@contextmanager
def exclusive():
    """危险操作与同步互斥（2026-11-16 优化批 3）：reset-base / merge-apply /
    restore 在同步进行时直接拒绝，避免 rmtree 与 _save_base 并发、或合并写库
    撞长事务。拿不到锁抛 SyncBusy（router 翻 409）。"""
    if not _sync_lock.acquire(blocking=False):
        raise SyncBusy("同步进行中，请稍后再试")
    try:
        yield
    finally:
        _sync_lock.release()


def _client_from_config(cfg) -> WebDavClient:
    return WebDavClient(cfg.webdav_url, cfg.webdav_username, cfg.webdav_password)


def _broadcast_sync(ok: bool, error: str | None, tables_changed: int) -> None:
    """同步完成事件（线程安全桥接：workflows 的 asyncio.Queue 非线程安全，
    同 ai_coach 的 call_soon_threadsafe 惯例；loop 不可用静默跳过）。"""
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    try:
        from services import workflows

        event = {"type": "sync", "ok": ok, "error": error,
                 "at": datetime.now().isoformat(timespec="seconds"),
                 "tables_changed": tables_changed}
        loop.call_soon_threadsafe(workflows.broadcast, event)
    except Exception:
        logger.debug("同步事件广播失败（无人订阅是常态）", exc_info=True)


def run_sync(db: Session, client: WebDavClient, cfg) -> dict:
    """完整同步一轮。返回统计 dict；守卫中止以 SyncAbort 抛出。异常
    （WebDavError/SyncAbort/OSError）由调用方统一落 last_sync_error。

    冷启动（无 Base）**全自动**（2026-11-15 迭代四，用户拍板免手动确认）：
    远端空 → Force Push；远端非空 → union+同键取新（本地空即纯 Pull——union
    公式天然覆盖，不做特判）。个人多设备场景 union+LWW 几乎总正确：冷启动无
    Base 不做删除传播、行不丢；「覆盖式迁移」诉求由覆盖恢复功能承担。

    请求量纪律（2026-11-16 真机批，坚果云限频治理）：ensure_dirs 仅首同步；
    纯拉取轮（合并结果与远端一致）不回推该表；全表未推时 manifest 也不推——
    「对端改→我拉」的轮次从 23+ 请求降到 ~1。"""
    manifest = client.fetch_manifest()
    base_exists = has_base()

    # 守卫 a（远端方向）：非首同步时远端 manifest 意外缺失 = 远端被清/损坏——
    # 中止（防被误判为「对端全删」传播清空远端）。用户确属重置远端请用重置基线。
    if base_exists and manifest is None:
        raise SyncAbort("远端同步数据意外缺失（曾被同步过）。如确认远端已重置，"
                        "请先点击「重置同步基线」再同步")

    # 首推竞态守卫：远端空（从未同步）时先以 If-None-Match 探测拿首推权——
    # 双端同时首同步，后到者收 412 → 重拉 manifest 直接走 union 合并（自动）
    if manifest is None:
        client.ensure_dirs()  # 仅首同步建目录（原每轮 2 个 MKCOL 白发，限频元凶之一）
        probe = {"app": "clara-sync", "format": 1, "device_id": cfg.device_id,
                 "updated_at": datetime.now().isoformat(timespec="seconds"),
                 "tables": {}}
        try:
            client.push_manifest(probe, if_none_match=True)
            client.put_readme(_README)  # 云端自说明（仅首同步一次）
        except WebDavError as e:
            if "__PRECONDITION_412__" not in str(e):
                raise
            manifest = client.fetch_manifest()  # 对端抢先写入：转 union 合并

    # —— 逐表：双 hash 比对 → 跳过 / 合并 ——
    remote_tables = (manifest or {}).get("tables") or {}
    # 拉到的远端 manifest 原始形态（纯拉取轮的探测记忆基准，见尾部注释）
    remote_hashes_at_fetch = {t: v.get("sha1") for t, v in remote_tables.items()}
    cleared_state = _read_cleared_marker()  # 合法清空豁免（/clear 类）
    cleared_tables = set(cleared_state["tables"])
    cleared_version = cleared_state["version"]
    if base_exists:
        # 表级守卫：Base 有的表 manifest 缺（远端文件被手动删）→ 远端异常中止。
        # **Base 快照为空的表不算缺失**（真机二轮实测）：运行中新增的空表在
        # manifest 收录前会先落空 Base 快照——若拦，推送代码永远没机会执行、
        # manifest 永远不收录，死锁；真被删的旧表 Base 非空照拦
        missing = [t for t in SYNC_TABLES
                   if t not in remote_tables and _base_path(t).is_file()
                   and len(_load_base(t) or []) > 0]
        if missing:
            raise SyncAbort(f"远端缺失表文件：{missing[:3]}（可能被手动删除）。"
                            "如确认远端已重置，请先「重置同步基线」")

    stats = {"tables_changed": 0, "inserted": 0, "updated": 0,
             "bytes_pushed": 0, "bytes_pulled": 0}
    pushed_tables: dict[str, dict] = {}
    snapshots: dict[str, list[dict]] = {}  # 与推送同源的 Base 落盘内容
    any_pushed = False  # 是否发生过表文件推送（False 且 manifest 未变 → 也不推）
    plans: list[dict] = []  # 阶段 1 产物：每表合并决策（写库/推送都消费它）

    # —— 阶段 1：只读规划（全部网络 GET 在此，不持写事务）———————————————
    for name in SYNC_TABLES:  # sorted_tables 序：父表在前
        table, pk = _table_meta(name)
        base = _load_base(name) if base_exists else None
        base_hash = _hash_rows(base) if base is not None else None
        remote_hash_hint = remote_tables.get(name, {}).get("sha1")

        local = table_rows(db, name)
        local_hash = _hash_rows(local)

        # 增量跳过：双未变（远端 manifest.hash == Base.hash 且本地未变）
        if base_exists and base is not None and remote_hash_hint == base_hash \
                and local_hash == base_hash:
            pushed_tables[name] = {"sha1": base_hash, "rows": len(local)}
            continue

        clear_from = None
        remote_missing = False
        if base_exists and base is not None and remote_hash_hint == base_hash:
            remote, remote_hash = base, base_hash  # 远端未变：省 GET，R=Base
        else:
            data = client.fetch_table(name)
            remote_missing = data is None
            remote = (data or {}).get("rows", [])
            clear_from = (data or {}).get("cleared_from")
            remote_hash = _hash_rows(remote)
            remote = [portable(name, row) for row in remote]
            stats["bytes_pulled"] += len(json.dumps(remote, ensure_ascii=False))

        # 表级守卫 a：Base 有记录但远端文件 404（manifest 撒谎/文件被删）
        if base_exists and remote_missing and base:
            raise SyncAbort(f"远端表文件缺失：{name}（manifest 有记录但拉取 404）")

        result = _merge_table(name, local, base, remote)
        merged, del_remote, del_local = result["merged"], result["del_remote"], result["del_local"]

        # 守卫 b（推方向）：将删远端大量行且远端自身未变 → 疑本地删库重建。
        # 合法清空豁免（/clear 类整表删空是正常操作，拦截+「重置基线」指引
        # 在此场景反而会把清空的数据 union 拉回来——真机二轮实测误伤）
        if base_exists and base is not None and remote_hash == base_hash \
                and name not in cleared_tables:
            if len(del_remote) > max(DELETE_GUARD_MIN, len(remote) // 2):
                raise SyncAbort(
                    f"本地 {name} 表比上次同步少了 {len(del_remote)} 行（常见于删库重建/恢复备份），"
                    "已中止以防连带清空云端。点「重置基线」后重新同步 = 云端数据与当前本地"
                    "数据合并保留（双方都不丢）；若想用本地完全替换云端，请先清空 WebDAV "
                    "上的 clara-sync 目录再重置基线")
        # 守卫 c（本地清空式）：将删本地全部行且本地未变 → 竞态残留/远端被清。
        # 2026-11-16 优化批 3：取消 >100 行下限——小表（model_providers /
        # app_settings / chat_state / projects）被远端写成空数组时同样需要
        # 保护（此前整表删除无任何提示）。
        if base_exists and base is not None and local_hash == base_hash \
                and len(local) > 0 and del_local \
                and len(del_local) == len(local) \
                and not (not remote and clear_from == base_hash
                         and remote_hash_hint == remote_hash) \
                and not (remote_hash_hint == remote_hash and _canonical_rekeys(name, pk, local, remote)):
            raise SyncAbort(
                f"远端 {name} 表已被清空而本地未动过，疑似云端数据异常，已中止同步"
                "防止本地被连带清空。如云端确已清空，请点「重置同步基线」后重新同步")

        plans.append({
            "name": name, "pk": pk, "merged": merged,
            "del_local": del_local, "remote_hash": remote_hash,
            # 写锁内复核规划版本，避免旧计划覆盖用户删改。
            "local_hash": local_hash,
            "clear_from": remote_hash if name in cleared_tables and not local and remote else None,
        })

    # 只读阶段结束：结束隐式读事务（否则写事务会从第一次 SELECT 起一直持有
    # 写锁——阶段 1 的网络等待会变成整库锁等待）
    db.rollback()

    # —— 阶段 2：单事务写本地（零网络 IO，短事务；写序铁律收编 writing）—————
    # 插入/更新按父前子后；删除经 writer 登记后由 writing 退出时逆拓扑（子先
    # 父后）统一执行——2026-11-17 真机批教训的结构化吸收（详见 writing docstring）
    if plans:
        db.execute(text("BEGIN IMMEDIATE"))  # 锁内复核，读后至提交不再容许插入业务写
    with writing(db) as w:
        for p in plans:
            if _hash_rows(table_rows(db, p["name"])) != p["local_hash"]:
                raise SyncAbort(f"规划期间本地 {p['name']} 已变化，已保留编辑；请重试同步")
        for p in plans:
            name, pk = p["name"], p["pk"]
            fresh = {r[pk]: r for r in table_rows(db, name)}
            actual, ins, upd, doomed = _write_local(db, name, p["merged"], fresh)
            p["actual"] = actual
            w.delete(name, pk, doomed)
            stats["inserted"] += ins
            stats["updated"] += upd
            if ins or upd or doomed:
                stats["tables_changed"] += 1

    # —— 阶段 3：推送（网络；本地已提交——推送失败不回滚用户数据）—————————
    for p in plans:
        name, actual = p["name"], p["actual"]
        actual_hash = _hash_rows(actual)
        if actual_hash == p["remote_hash"] and name in remote_tables:
            # 纯拉取轮：远端 manifest 已收录该表且合并结果与远端一致（本地
            # 只是应用了远端变更）——不回推同内容（2026-11-16 真机批：对端
            # 改→我拉的轮次曾全量回推 22 表 + manifest，请求翻倍撞限频）。
            # 「name in remote_tables」前提不可省：**运行中新增的空表**内容
            # 与远端（无文件=空）一致，若跳过则 manifest 永不收录该表，而
            # Base 已落快照 → 下轮表级守卫误拦「远端缺失表文件」（真机二
            # 轮实测抓到）——新表必须推一次让对端知晓
            snapshots[name] = actual
            pushed_tables[name] = {"sha1": actual_hash, "rows": len(actual),
                                   "updated_at": datetime.now().isoformat(timespec="seconds")}
            continue
        payload = {"table": name, "rows": actual}
        if not actual and p["clear_from"]:
            payload["cleared_from"] = p["clear_from"]
        client.push_table(name, payload)
        any_pushed = True
        stats["bytes_pushed"] += len(json.dumps(payload, ensure_ascii=False))
        snapshots[name] = actual  # Base 与推送字节级同源（hash 恒一致）
        pushed_tables[name] = {"sha1": actual_hash, "rows": len(actual),
                               "updated_at": datetime.now().isoformat(timespec="seconds")}

    # 提交点：最后推 manifest（滞后于表文件；首推的探测 manifest 已在前面落地）。
    # 纯拉取轮全表未推 → manifest 内容无变化也不推（updated_at 不值得一个请求）。
    # 自愈（2026-11-16 优化批 3）：上一轮表文件 PUT 成功但 manifest PUT 失败
    # （503/超时/退出冲刷）后，后续轮 pure-pull 会让 any_pushed 恒 False → manifest
    # 永不重推，其他设备永远跳过新数据。只要本轮期望表集/hash 与远端 manifest
    # 不一致就补推（正常一致时零额外请求）。
    known_remote = {t: remote_tables.get(t) or {} for t in SYNC_TABLES}
    manifest_diverges = (
        set(known_remote) != set(pushed_tables)
        or any(known_remote[t].get("sha1") != pushed_tables[t].get("sha1")
               for t in pushed_tables)
        # 远端残留已排除/已回撤表的条目（二轮批 1）：2026-11-16 前同步过的目录
        # 必带 workflow_runs 条目——旧版永不清理（远端文件与条目双残留），且每
        # 次进程重启后 30s 探测发现远端 manifest（含多余键）≠ 记忆，白跑一轮
        # 完整同步才稳定。重推 manifest 自然剔除多余条目（远端表文件不动）
        or bool(set(remote_tables) - set(SYNC_TABLES))
    )
    pushed_manifest = any_pushed or manifest is None or manifest_diverges
    if pushed_manifest:
        manifest_out = {
            "app": "clara-sync", "format": 1, "device_id": cfg.device_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "tables": pushed_tables,
        }
        client.push_manifest(manifest_out)

    # 快照落地（与推送同源的 actual——manifest.sha1 与 Base 内容 hash 恒一致，
    # 「远端未变」判定才可靠；跳过的表 Base 本就未变不重写。失败 = 整轮失败，
    # TabDav 教训：否则下轮无法检测删除）
    for name, rows in snapshots.items():
        _save_base(name, rows)
    _save_base_meta(cfg.device_id or "")
    # 远端探测记忆（2026-11-15 迭代四）：30s 轻探测的比对基准 = **远端 manifest
    # 现状**——推了 manifest 记推送形态；纯拉取轮（manifest 未推）记拉到的原
    # manifest（若记实际内容 hash，而远端 manifest 停在旧值（如对端写过后被
    # 判纯拉取），探测会每轮误判「有变化」白跑同步——2026-11-16 真机批踩坑）
    global _last_seen_hashes
    if pushed_manifest:
        _last_seen_hashes = {t: v.get("sha1") for t, v in pushed_tables.items()}
    else:
        _last_seen_hashes = remote_hashes_at_fetch
    _consume_cleared_marker(cleared_version)  # 全链成功才消耗（版本不符则保留下轮）
    return stats


def _sync_once(manual: bool = False, *, lock_timeout: float = 0) -> dict:
    """一轮同步的完整编排（锁/配置/状态回写/广播；循环、手动与退出冲刷共用）。
    manual=True 跳过 webdav_enabled 检查（2026-11-16 真机批用户拍板：「立即
    同步」是手动一次性操作，不受自动同步开关约束——开关只管自动；地址仍必须
    已配置）。返回 run_sync 的结果或错误态 dict（不抛——调用方零异常处理）；
    限频失败（429/503）附 rate_limited=True 供 sync_loop 快速进入长退避。"""
    if not _sync_lock.acquire(timeout=lock_timeout):
        return {"busy": True}
    # 取走脏表（2026-11-16 优化批 3）：此前只 peek 不清空——任何一次业务写后
    # 脏集合永久非空，sync_loop 每 3 秒跑一轮全表扫描 + manifest GET + SSE 广播，
    # 持续消耗 WebDAV 额度并让前端反复重拉。同步期间的新写会重新入集合。
    dirty = take_dirty_tables()
    try:
        with database.SessionLocal() as db:
            cfg = get_sync_config(db)
            if not cfg.webdav_url:
                return {"error": "未配置 WebDAV 服务地址"}
            if not manual and not cfg.webdav_enabled:
                return {"error": "同步未启用（可点「立即同步」手动执行一次）"}
            client = _client_from_config(cfg)
            try:
                result = run_sync(db, client, cfg)
                cfg.last_sync_at = datetime.now()
                cfg.last_sync_ok = True
                cfg.last_sync_error = None
                db.commit()
                logger.info("同步完成：%s", result)
                _broadcast_sync(True, None, result.get("tables_changed", 0))
                return result
            except (WebDavError, SyncAbort, OSError) as e:
                mark_tables_dirty(dirty)  # 失败恢复标脏：下轮重试不丢这次变更
                cfg.last_sync_at = datetime.now()
                cfg.last_sync_ok = False
                cfg.last_sync_error = str(e)[:400]
                db.commit()
                rate_limited = isinstance(e, WebDavError) and e.status in (429, 503)
                if rate_limited:
                    logger.warning("同步撞限频（HTTP %s），进入长退避", e.status)
                else:
                    logger.warning("同步失败：%s", e)
                _broadcast_sync(False, str(e)[:200], 0)
                out = {"error": str(e)[:400]}
                if rate_limited:
                    out["rate_limited"] = True
                return out
            except Exception as e:
                # 非预期异常（IntegrityError / 坏远端行 / KeyError 等）也必须
                # 落 last_sync_error（优化批 3）：此前直接逸出，_sync_once 的
                # 调用方读到的还是上一轮状态，用户看到「已同步」但实际失败。
                mark_tables_dirty(dirty)
                logger.exception("同步非预期异常")
                try:
                    db.rollback()
                    with database.SessionLocal() as db2:
                        cfg2 = get_sync_config(db2)
                        cfg2.last_sync_at = datetime.now()
                        cfg2.last_sync_ok = False
                        cfg2.last_sync_error = f"同步异常：{type(e).__name__}（详见后端日志）"
                        db2.commit()
                except Exception:
                    logger.debug("同步失败状态回写失败", exc_info=True)
                _broadcast_sync(False, f"同步异常：{type(e).__name__}", 0)
                return {"error": f"同步异常：{type(e).__name__}"}
            finally:
                client.close()  # 复用连接随轮关闭（二轮批 1）
    finally:
        _sync_lock.release()


def flush_on_exit() -> dict:
    """等待当前轮结束后再做一次最终对齐；不得绕开启用状态。"""
    return _sync_once(lock_timeout=15)


def sync_now() -> dict:
    """手动「立即同步」入口（POST /api/sync/run 经 to_thread 调用；manual 语义）。"""
    return _sync_once(manual=True)


# 远端探测记忆（run_sync 成功尾部回写；None = 进程启动后未同步过 → 首次探测
# 恒判「有变化」触发一轮完整同步，无害）
_last_seen_hashes: dict | None = None


def _probe_remote_changed(client: WebDavClient | None = None) -> bool:
    """30s 远端轻探测（迭代四）：只 GET manifest（几 KB），per-table sha1 与
    上次同步记忆比对——变了才触发完整同步，把双端在线的收敛延迟从周期 300s
    压到 ~30s。锁占用/未启用/网络失败一律返 False（静默，30s 后再探，不计
    失败退避——探测请求极轻，坚果云限频内）。manifest None 且有记忆 = 远端
    被清 → 返 True 触发同步（守卫 a 会正确拦截并落提示）。client 可注入
    （测试替身）；缺省自建。"""
    if _sync_lock.locked() or _last_seen_hashes is None:
        return False
    if client is None:
        with database.SessionLocal() as db:
            cfg = get_sync_config(db)
            if not cfg.webdav_enabled or not cfg.webdav_url:
                return False
            client = _client_from_config(cfg)
    try:
        manifest = client.fetch_manifest()
    except WebDavError:
        return False
    if manifest is None:
        return True  # 远端被清：触发同步走守卫 a 的正确路径
    hashes = {t: v.get("sha1") for t, v in (manifest.get("tables") or {}).items()}
    return hashes != _last_seen_hashes


async def sync_loop() -> None:
    """后台同步循环（main.lifespan 挂 create_task）：启动延迟 → 每秒检查三级
    触发——①防抖（脏表/唤醒 ≥3s）②30s 远端轻探测（manifest hash 变才拉全量）
    ③周期兜底 300s（防脏表漏标，失败退避至 1800s）。同步体走 to_thread
    （事件循环零阻塞，workflows 铁律）。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    await asyncio.sleep(START_DELAY_S)
    register_dirty_hook()
    # 启动即满足一次周期（优化批 3）：time.monotonic() 在 Windows 是开机时长，
    # 初值 0 时开机不足 300s 启动的应用首次同步会被拖到开机满 300s。显式回退
    # 一个周期 → 首 tick 立即做一轮全量（远端有变化即收敛）。
    last_attempt = time.monotonic() - PERIODIC_S
    last_probe = time.monotonic()
    fail_streak = 0
    logger.info("同步循环已启动（防抖 %ds / 探测 %ds / 周期 %ds / 退避上限 %ds）",
                DEBOUNCE_S, PROBE_S, PERIODIC_S, MAX_PERIOD_S)
    while True:
        try:
            woke = await asyncio.to_thread(_wake.wait, 1.0)
            _wake.clear()
            now = time.monotonic()
            period = min(PERIODIC_S * (2 ** min(fail_streak, 3)), MAX_PERIOD_S)
            dirty = peek_dirty_tables()
            since_last = now - last_attempt
            if not ((dirty or woke) and since_last >= DEBOUNCE_S) and since_last < period:
                # 未到完整同步条件：30s 轻探测远端（对端新数据 ~30s 可见）。
                # 退避期（fail_streak>0）不探测（优化批 3）：探测基准在失败轮
                # 不更新，会每 30s 判「有变化」重复触发整轮失败。
                if fail_streak == 0 and now - last_probe >= PROBE_S:
                    last_probe = now
                    changed = await asyncio.to_thread(_probe_remote_changed)
                    if not changed:
                        continue
                else:
                    continue
            last_attempt = now
            result = await asyncio.to_thread(_sync_once)
            if "error" in result:
                # 限频（429/503）窗口是分钟级：fail_streak 跳升 3 直接进
                # 600s/1200s 长退避，短退避反复撞窗只会持续失败（真机批六根因）
                fail_streak += 3 if result.get("rate_limited") else 1
            else:
                fail_streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            fail_streak += 1
            logger.exception("同步循环异常（退避后重试）")
            await asyncio.sleep(DEBOUNCE_S)
