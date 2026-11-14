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
import hashlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path

from sqlalchemy import DateTime, Date, Time, delete, select, text
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
SYNC_TABLES: list[str] = [
    t.name for t in Base.metadata.sorted_tables if t.name != "sync_settings"
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
# 冷启动「本地空」判定表（业务数据特征；配置表不算——B 机配好模型再配 WebDAV
# 是正常路径，不应被误判为「本地有数据」而要求 Case C 确认）
_BUSINESS_TABLES = ("tasks", "projects", "messages", "subtasks", "proposals")

# —— 触发节奏 —————————————————————————————————————————————————————
DEBOUNCE_S = 3        # 变更防抖
PERIODIC_S = 300      # 周期兜底
START_DELAY_S = 15    # 启动延迟（避开启动期建库/seed/恢复）
MAX_PERIOD_S = 1800   # 失败退避上限（300→600→1200→1800，成功复位）

# —— 守卫阈值 —————————————————————————————————————————————————————
DELETE_GUARD_MIN = 100  # 删除行数绝对下限


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
    锁确保释放（异常路径残留会锁死后用例）。"""
    with _dirty_lock:
        _dirty_tables.clear()
    _wake.clear()
    global _main_loop
    _main_loop = None
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


# —— 序列化与哈希（口径复用 config_backup._ser：datetime 族 isoformat）———————


def _ser(value):
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    return value


def _table_meta(name: str):
    """表对象与主键列名（cli_tools 主键是 adapter_id，动态取不硬编码）。"""
    table = Base.metadata.tables[name]
    pk = table.primary_key.columns[0].name
    return table, pk


def table_rows(db: Session, name: str) -> list[dict]:
    """全表 dump 为行 dict 列表（datetime → isoformat）。**行按主键排序**——
    规范序保证「同一份数据两次读 / push 与快照」的 sha1 恒一致（增量判定的
    命脉；无 ORDER BY 的 SQLite 返回序是 rowid 序，与合并输出的键序不同）。"""
    table, pk = _table_meta(name)
    cols = table.columns.keys()
    rows = [
        {c: _ser(v) for c, v in zip(cols, row)}
        for row in db.execute(select(table)).all()
    ]
    rows.sort(key=lambda r: str(r.get(pk, "")))
    return rows


def _hash_rows(rows: list[dict]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                         default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _row_ts(row: dict) -> datetime | None:
    """行级 LWW 时间判据：updated_at → created_at 回退；解析失败/缺失返 None。
    调用方平局规则恒本地优先（>= 判定）。"""
    for key in ("updated_at", "created_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            continue
    return None


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
        return data if isinstance(data, list) else None
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


def clear_snapshot() -> None:
    """清空快照（重置基线 / 覆盖恢复后调用——数据回到过去，Base 失效，下轮退化
    冷启动语义；守卫拦截后的用户出路）。"""
    import shutil

    d = snapshot_dir()
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        logger.info("同步快照已清空（基线重置）")


# —— 三路合并（纯函数，直测友好）————————————————————————————————————————


def _merge_table(name: str, local: list[dict], base: list[dict] | None,
                 remote: list[dict] | None) -> dict:
    """行级三路合并。返回 {merged, del_remote, del_local}（del_* 为相对 R/L 的
    删除主键集，供守卫判定；merged 为最终行集）。

    真值表（键=主键；ts 判据 _row_ts，平局本地优先；单行表同键自然整行 LWW）：
      L-  S-  R+ → 取 R（对端新增）
      L+  S-  R- → 取 L（本地新增）
      L-  S+  R+ → 删（本地删除传播到远端）
      L+  S+  R- → 删（对端删除落地本地）
      其余（双方都有）→ ts 新者
    base=None（冷启动）：union + 同键取新（Case A/B/C 的统一公式——L 空则纯
    Pull、R 空则纯 Force Push、双有则并集）。
    """
    _, pk = _table_meta(name)
    l_map = {r[pk]: r for r in local}
    r_map = {r[pk]: r for r in (remote or [])}
    s_map = {r[pk]: r for r in (base or [])} if base is not None else None

    merged: dict = {}

    def newer(local_row: dict, remote_row: dict) -> dict:
        lt, rt = _row_ts(local_row), _row_ts(remote_row)
        if lt is None:
            return remote_row
        if rt is None:
            return local_row
        return local_row if lt >= rt else remote_row  # 平局本地优先

    for key in set(l_map) | set(r_map):
        l_row, r_row = l_map.get(key), r_map.get(key)
        if s_map is None:
            # 冷启动：union + 同键取新
            if l_row is None:
                merged[key] = r_row
            elif r_row is None:
                merged[key] = l_row
            else:
                merged[key] = newer(l_row, r_row)
            continue
        s_row = s_map.get(key)
        if l_row is not None and r_row is not None:
            merged[key] = newer(l_row, r_row)
        elif l_row is not None:
            if s_row is None:
                merged[key] = l_row            # 本地新增
            # else：S 有 L 有 R 无 → 对端删 → 本地删（不进 merged）
        elif r_row is not None:
            if s_row is None:
                merged[key] = r_row            # 对端新增
            # else：S 有 R 有 L 无 → 本地删 → 远端删（不进 merged）
        # 双无：不可能（键来自 L∪R）

    ordered = sorted(merged.values(), key=lambda r: str(r.get(pk, "")))  # 规范序
    return {"merged": ordered,
            "del_remote": set(r_map) - set(merged),
            "del_local": set(l_map) - set(merged)}


# —— 写库（单事务 + defer FK；返回实际落库行集）———————————————————————————


def _revive_value(col, value):
    """单值 → 可写值：datetime 族字符串按列类型转回对象（SQLite DateTime 方言
    只认对象）；非空布尔列的 None 归 False（config_backup._revive 同款防御）。"""
    from sqlalchemy import Boolean

    if value is None:
        return False if (isinstance(col.type, Boolean) and not col.nullable) else None
    if isinstance(col.type, DateTime):
        return datetime.fromisoformat(str(value))
    if isinstance(col.type, Date):
        return date.fromisoformat(str(value))
    if isinstance(col.type, Time):
        return dt_time.fromisoformat(str(value))
    return value


def _revive_row(table, row: dict) -> dict:
    """行 dict → 可写值（按目标表列集过滤：远端多列/少列均安全——少列时
    NOT NULL 列缺省由列 default 兜不住会炸，旧版本数据场景由 schema 兼容性
    兜底：同版本包正常，跨版本记 tech-debt）。"""
    out: dict = {}
    for col_name, value in row.items():
        col = table.columns.get(col_name)
        if col is None:
            continue  # 远端多列（本表已无）：忽略
        out[col_name] = _revive_value(col, value)
    return out


def _write_local(db: Session, name: str, merged: list[dict],
                 local_map: dict) -> tuple[list[dict], int, int]:
    """把合并行集写进本地（单事务由调用方统一 commit；本函数只 execute）。
    返回 (实际落库行集, 插入数, 更新数)。

    - 键不在本地 → INSERT；撞自然键唯一约束 → 按自然键定位本地行，LWW 决定
      UPDATE（用本地 pk 承载远端内容）或跳过（本地赢）；
    - 键在本地且内容不同 → UPDATE（显式带 updated_at——触发器 WHEN 守卫不覆盖）；
    - 本地有而 merged 无 → DELETE（调用方逆拓扑序处理跨表）。
    实际落库行集 = merged 经撞车替换后的形态（保 Base=本地=远端一致）。
    """
    table, pk = _table_meta(name)
    cols = table.columns.keys()
    actual: list[dict] = []
    inserted = updated = 0

    merged_map = {r[pk]: r for r in merged}
    for key, row in merged_map.items():
        local_row = local_map.get(key)
        if local_row is None:
            values = _revive_row(table, row)
            try:
                db.execute(table.insert().values(**values))
                inserted += 1
                actual.append(row)
            except IntegrityError:
                # 自然键 add-add 撞车（UUID 主键不撞 pk）：定位本地行走 LWW
                hit = _find_by_natural_key(db, name, row)
                if hit is None:
                    logger.warning("同步插入撞约束且无法按自然键定位（%s/%s），跳过",
                                   name, key)
                    actual.append(row)  # 本地没进去但保留在推送集（下轮再收敛）
                    continue
                local_row = hit
                local_map[hit[pk]] = hit
                key = hit[pk]
        if local_row is not None:
            if _rows_equal(row, local_row, cols):
                actual.append(local_row)  # 内容相同：不写，推送本地形态
                continue
            lt, rt = _row_ts(local_row), _row_ts(row)
            if lt is not None and rt is not None and lt >= rt:
                actual.append(local_row)  # 本地新（撞车场景 LWW）：保留本地
                continue
            values = _revive_row(table, row)
            values[pk] = key  # 撞车场景：远端内容落在本地 pk 上
            db.execute(table.update().where(
                getattr(table.c, pk) == key).values(**values))
            row = {**row, pk: key}
            updated += 1
            actual.append(row)

    # 删除（本地有而 merged 无）
    doomed = set(local_map) - set(merged_map)
    if doomed:
        db.execute(delete(table).where(getattr(table.c, pk).in_(doomed)))
    return actual, inserted, updated


def _rows_equal(a: dict, b: dict, cols) -> bool:
    return all(a.get(c) == b.get(c) for c in cols)


def _find_by_natural_key(db: Session, name: str, row: dict) -> dict | None:
    """按自然键唯一约束查本地行（撞车回退定位）。"""
    keys = NATURAL_KEYS.get(name)
    if not keys:
        return None
    table, _ = _table_meta(name)
    conds = [getattr(table.c, k) == _revive_value(table.columns[k], row[k]) for k in keys]
    hit = db.execute(select(table).where(*conds).limit(1)).first()
    if hit is None:
        return None
    cols = table.columns.keys()
    return {c: _ser(v) for c, v in zip(cols, hit)}


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


def _local_business_empty(db: Session) -> bool:
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    return all(
        db.execute(select(tables[n]).limit(1)).first() is None
        for n in _BUSINESS_TABLES)


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


def run_sync(db: Session, client: WebDavClient, cfg, *, force_confirm: bool = False) -> dict:
    """完整同步一轮。返回统计 dict；needs_confirm / 守卫中止以 SyncAbort 或
    返回值表达（needs_confirm 时零写操作）。异常（WebDavError/SyncAbort/OSError）
    由调用方统一落 last_sync_error。"""
    client.ensure_dirs()
    manifest = client.fetch_manifest()
    base_exists = has_base()

    # 守卫 a（远端方向）：非首同步时远端 manifest 意外缺失 = 远端被清/损坏——
    # 中止（防被误判为「对端全删」传播清空远端）。用户确属重置远端请用重置基线。
    if base_exists and manifest is None:
        raise SyncAbort("远端同步数据意外缺失（曾被同步过）。如确认远端已重置，"
                        "请先点击「重置同步基线」再同步")

    # 冷启动细分：真首同步（远端空）/ 快照丢失（远端有、本地 Base 无）
    if not base_exists and manifest is not None and not force_confirm:
        if _local_business_empty(db):
            pass  # Pull（Case B）：本地空，直接拉远端
        else:
            local_rows = sum(len(table_rows(db, t)) for t in _BUSINESS_TABLES)
            remote_rows = sum(
                (manifest.get("tables") or {}).get(t, {}).get("rows", 0)
                for t in _BUSINESS_TABLES)
            reason = "snapshot_missing" if (snapshot_dir().is_dir()) else "first_sync"
            return {"needs_confirm": True, "reason": reason,
                    "local_rows": local_rows, "remote_rows": int(remote_rows)}

    # 首推竞态守卫：远端空（从未同步）时先以 If-None-Match 探测拿首推权——
    # 双端同时首同步，后到者收 412 转冷启动确认路径（union 合并）
    first_push = manifest is None
    if first_push:
        probe = {"app": "clara-sync", "format": 1, "device_id": cfg.device_id,
                 "updated_at": datetime.now().isoformat(timespec="seconds"),
                 "tables": {}}
        try:
            client.push_manifest(probe, if_none_match=True)
        except WebDavError as e:
            if "__PRECONDITION_412__" in str(e):
                # 对端抢先写入：重拉 manifest，按首同步确认语义重新判定
                # （force_confirm 已含则直接走下方 union 合并）
                manifest = client.fetch_manifest()
                if not force_confirm and not _local_business_empty(db):
                    return {"needs_confirm": True, "reason": "first_sync",
                            "local_rows": sum(len(table_rows(db, t)) for t in _BUSINESS_TABLES),
                            "remote_rows": sum((manifest.get("tables") or {}).get(t, {}).get("rows", 0)
                                               for t in _BUSINESS_TABLES)}
            else:
                raise

    # —— 逐表：双 hash 比对 → 跳过 / 合并 ——
    remote_tables = (manifest or {}).get("tables") or {}
    if base_exists:
        # 表级守卫：Base 有的表 manifest 缺（远端文件被手动删）→ 远端异常中止
        missing = [t for t in SYNC_TABLES
                   if t not in remote_tables and _base_path(t).is_file()]
        if missing:
            raise SyncAbort(f"远端缺失表文件：{missing[:3]}（可能被手动删除）。"
                            "如确认远端已重置，请先「重置同步基线」")

    stats = {"tables_changed": 0, "inserted": 0, "updated": 0,
             "bytes_pushed": 0, "bytes_pulled": 0}
    pushed_tables: dict[str, dict] = {}
    snapshots: dict[str, list[dict]] = {}  # 与推送同源的 Base 落盘内容
    try:
        db.execute(text("PRAGMA defer_foreign_keys=ON"))
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

            remote_missing = False
            if base_exists and base is not None and remote_hash_hint == base_hash:
                remote, remote_hash = base, base_hash  # 远端未变：省 GET，R=Base
            else:
                data = client.fetch_table(name)
                remote_missing = data is None
                remote = (data or {}).get("rows", [])
                remote_hash = _hash_rows(remote)
                stats["bytes_pulled"] += len(json.dumps(remote, ensure_ascii=False))

            # 表级守卫 a：Base 有记录但远端文件 404（manifest 撒谎/文件被删）
            if base_exists and remote_missing and base:
                raise SyncAbort(f"远端表文件缺失：{name}（manifest 有记录但拉取 404）")

            result = _merge_table(name, local, base, remote)
            merged, del_remote, del_local = result["merged"], result["del_remote"], result["del_local"]

            # 守卫 b（推方向）：将删远端大量行且远端自身未变 → 疑本地删库重建
            if base_exists and base is not None and remote_hash == base_hash:
                if len(del_remote) > max(DELETE_GUARD_MIN, len(remote) // 2):
                    raise SyncAbort(
                        f"检测到本地大量删除（{name} 表 {len(del_remote)} 行）且远端未变，"
                        "已中止同步防止误传播。如删除确属故意，请点击「重置同步基线」后重新同步")
            # 守卫 c（本地清空式）：将删本地全部行且本地未变 → 竞态残留/远端被清
            if base_exists and base is not None and local_hash == base_hash \
                    and len(local) > DELETE_GUARD_MIN and del_local \
                    and len(del_local) == len(local):
                raise SyncAbort(
                    f"远端数据异常（{name} 表将被整体清空且本地未变），已中止同步。"
                    "如远端确已清空，请点击「重置同步基线」后重新同步")

            actual, ins, upd = _write_local(db, name, merged,
                                            {r[pk]: r for r in local})
            stats["inserted"] += ins
            stats["updated"] += upd
            if ins or upd or del_local:
                stats["tables_changed"] += 1
            payload = {"table": name, "rows": actual}
            client.push_table(name, payload)
            stats["bytes_pushed"] += len(json.dumps(payload, ensure_ascii=False))
            snapshots[name] = actual  # Base 与推送字节级同源（hash 恒一致）
            pushed_tables[name] = {"sha1": _hash_rows(actual), "rows": len(actual),
                                   "updated_at": datetime.now().isoformat(timespec="seconds")}
        db.commit()
    except Exception:
        db.rollback()
        raise

    # 提交点：最后推 manifest（滞后于表文件；首推的探测 manifest 已在前面落地）
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
    return stats


def _sync_once(confirm: bool = False) -> dict:
    """一轮同步的完整编排（锁/配置/状态回写/广播；循环与手动共用）。
    返回 run_sync 的结果或错误态 dict（不抛——调用方零异常处理）。"""
    if not _sync_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        with database.SessionLocal() as db:
            cfg = get_sync_config(db)
            if not cfg.webdav_enabled or not cfg.webdav_url:
                return {"error": "同步未启用或未配置 WebDAV 地址"}
            client = _client_from_config(cfg)
            try:
                result = run_sync(db, client, cfg, force_confirm=confirm)
                cfg.last_sync_at = datetime.now()
                cfg.last_sync_ok = True
                cfg.last_sync_error = None
                db.commit()
                logger.info("同步完成：%s", result)
                _broadcast_sync(True, None, result.get("tables_changed", 0))
                return result
            except (WebDavError, SyncAbort, OSError) as e:
                cfg.last_sync_at = datetime.now()
                cfg.last_sync_ok = False
                cfg.last_sync_error = str(e)[:400]
                db.commit()
                logger.warning("同步失败：%s", e)
                _broadcast_sync(False, str(e)[:200], 0)
                return {"error": str(e)[:400]}
    finally:
        _sync_lock.release()


def sync_now(confirm: bool = False) -> dict:
    """手动「立即同步」入口（POST /api/sync/run 经 to_thread 调用）。"""
    return _sync_once(confirm=confirm)


def probe_run() -> dict | None:
    """手动同步前的快速预检（无锁只读）：判 needs_confirm（本地与远端均有数据
    的首同步/快照丢失），None = 可直接跑。network 只拉一次 manifest。"""
    with database.SessionLocal() as db:
        cfg = get_sync_config(db)
        if not cfg.webdav_enabled or not cfg.webdav_url:
            return {"error": "同步未启用或未配置 WebDAV 地址"}
        if has_base():
            return None
        try:
            client = _client_from_config(cfg)
            client.ensure_dirs()
            manifest = client.fetch_manifest()
        except WebDavError as e:
            return {"error": str(e)[:400]}
        if manifest is None:
            return None  # 远端空：Force Push 无需确认
        if _local_business_empty(db):
            return None  # 本地空：Pull 无需确认
        reason = "snapshot_missing" if snapshot_dir().is_dir() else "first_sync"
        return {"needs_confirm": True, "reason": reason,
                "local_rows": sum(len(table_rows(db, t)) for t in _BUSINESS_TABLES),
                "remote_rows": sum((manifest.get("tables") or {}).get(t, {}).get("rows", 0)
                                   for t in _BUSINESS_TABLES)}


async def sync_loop() -> None:
    """后台同步循环（main.lifespan 挂 create_task）：启动延迟 → 每秒检查
    （防抖唤醒 / 脏表 / 周期兜底带失败退避）。同步体走 to_thread（事件循环
    零阻塞，workflows 铁律）。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    await asyncio.sleep(START_DELAY_S)
    register_dirty_hook()
    last_attempt = 0.0  # monotonic
    fail_streak = 0
    logger.info("同步循环已启动（防抖 %ds / 周期 %ds / 退避上限 %ds）",
                DEBOUNCE_S, PERIODIC_S, MAX_PERIOD_S)
    while True:
        try:
            woke = await asyncio.to_thread(_wake.wait, 1.0)
            _wake.clear()
            now = time.monotonic()
            period = min(PERIODIC_S * (2 ** min(fail_streak, 3)), MAX_PERIOD_S)
            dirty = peek_dirty_tables()
            since_last = now - last_attempt
            due = ((dirty or woke) and since_last >= DEBOUNCE_S) or since_last >= period
            if not due:
                continue
            last_attempt = now
            result = await asyncio.to_thread(_sync_once)
            if "error" in result or "needs_confirm" in result:
                if "needs_confirm" in result:
                    # 高危冷启动留给用户手动确认；落状态行提示
                    with database.SessionLocal() as db:
                        cfg = get_sync_config(db)
                        cfg.last_sync_at = datetime.now()
                        cfg.last_sync_ok = False
                        cfg.last_sync_error = (
                            "本地与远端均有数据（首次同步或快照丢失），请在设置中手动点击「立即同步」确认合并")
                        db.commit()
                fail_streak += 1
            else:
                fail_streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            fail_streak += 1
            logger.exception("同步循环异常（退避后重试）")
            await asyncio.sleep(DEBOUNCE_S)
