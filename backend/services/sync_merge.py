"""合并导入（2026-11-14 同步备份）：把备份 zip 的数据合并进当前库（两阶段：
analyze dry-run → 用户选冲突策略 → apply），支持双 zip 格式。

格式判定（与 prepare_restore 的双格式分流同一矩阵）：
- 备份格式（manifest.app=="clara"）：zip 内含整库 clara.db——解 tmp 后 sqlite3
  只读连接按 SYNC_TABLES dump（复用 backup.py 的快照形态）；
- 同步格式（manifest.app=="clara-sync"）：tables/<t>.json 散文件——用户手动把
  WebDAV 同步目录压成 zip 也能直接导入。
两种来源都滤掉 sync_settings（WebDAV 凭证/备份目录是设备本地配置）。

合并语义（两路合并，无 Base）：
- 新行（本地无此主键）→ INSERT；
- 同主键内容不同 → 按 strategy：overwrite 备份覆盖本地 / skip 保留本地；
- **不删除**本地行——两路合并无法区分「备份没有 = 新数据」与「曾存在已被删」，
  删除语义属覆盖恢复（prepare_restore 整库替换），UI 文案明示两者差异；
- 写入行的 updated_at 一律盖当前时刻（不保留备份时间戳）——合并导入的数据
  视为本地最新修改，后续 WebDAV 同步按 LWW 正常传播而非被远端新行覆盖；
- 完成后 mark_tables_dirty + schedule_now（防抖推上远端）。
"""

import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.sync_engine import SYNC_TABLES

logger = logging.getLogger(__name__)


def load_zip_tables(path: str) -> dict[str, list[dict]]:
    """双格式统一出口：{表名: [行 dict, ...]}（滤 sync_settings；只含 SYNC_TABLES
    内的表）。非法 zip / 缺 manifest / 异应用包抛 ValueError（router 翻 422）。
    成员名经 backup._normalize_members 归一——手压 WebDAV 目录多包一层
    `clara-sync/` 前缀也识别。"""
    from services.backup import _normalize_members

    archive = Path(path)
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ValueError("所选文件不是有效的备份包")
    with zipfile.ZipFile(archive) as zf:
        norm = _normalize_members(zf.namelist())
        if not norm:
            raise ValueError("备份包缺少 manifest.json，无法识别格式")
        try:
            manifest = json.loads(zf.read(norm["manifest.json"]))
        except (ValueError, OSError) as e:
            raise ValueError(f"备份包 manifest 损坏：{e}") from e
        app = manifest.get("app")

        tables: dict[str, list[dict]] = {}
        if app == "clara-sync":
            # 同步格式：tables/<t>.json 散文件
            member_map = {}
            for norm_name, orig in norm.items():
                if norm_name.startswith("tables/") and norm_name.endswith(".json"):
                    member_map[norm_name[len("tables/"):-len(".json")]] = orig
            unknown = set(member_map) - set(SYNC_TABLES)
            if unknown:
                raise ValueError(f"备份包含未知表文件：{sorted(unknown)[:3]}")
            for name, orig in member_map.items():
                try:
                    data = json.loads(zf.read(orig))
                except ValueError as e:
                    raise ValueError(f"表文件损坏（{name}）：{e}") from e
                rows = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError(f"表文件格式异常（{name}）")
                tables[name] = rows
        elif app == "clara":
            # 备份格式：zip 内整库 clara.db → tmp 解包只读 dump
            if "clara.db" not in norm:
                raise ValueError("备份包缺少数据库文件，无法导入")
            tmp_dir = Path(tempfile.mkdtemp(prefix="clara-merge-"))
            try:
                (tmp_dir / "clara.db").write_bytes(zf.read(norm["clara.db"]))
                tables = _dump_sqlite_tables(tmp_dir / "clara.db")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            raise ValueError("这不是 Clara 的备份包")
    tables.pop("sync_settings", None)
    return tables


def _dump_sqlite_tables(db_path: Path) -> dict[str, list[dict]]:
    """SQLite 库文件 → {表名: 行 dict}（sqlite3 原生只读游标；datetime 以文本
    读出，口径与 table_rows 的 isoformat 一致）。列取「metadata 列 ∩ 库内实有
    列」交集——旧版本备份列少时取共有列（缺表整表跳过），与 _revive_row 的
    列集过滤口径一致。"""
    from database import Base  # 列类型/主键元数据单源

    meta = {t.name: t for t in Base.metadata.sorted_tables}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        out: dict[str, list[dict]] = {}
        for name in SYNC_TABLES:
            try:
                actual = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
            except sqlite3.OperationalError:
                continue
            if not actual:
                continue  # 旧版本备份表更少：缺表跳过
            cols = [c for c in meta[name].columns.keys() if c in actual]
            rows = conn.execute(f"SELECT {','.join(cols)} FROM {name}").fetchall()
            out[name] = [dict(r) for r in rows]
        return out
    finally:
        conn.close()


def analyze_merge(db: Session, path: str) -> dict:
    """第一阶段（dry-run，零写入）：统计新增/更新/冲突明细（前 50 条）。"""
    from services.sync_engine import _rows_equal, _table_meta, table_rows

    tables = load_zip_tables(path)
    new_count = updated_count = 0
    per_table: dict[str, dict] = {}
    conflicts: list[dict] = []
    for name in SYNC_TABLES:
        rows = tables.get(name) or []
        if not rows:
            continue
        _, pk = _table_meta(name)
        local = {r[pk]: r for r in table_rows(db, name)}
        t_new = t_upd = 0
        for row in rows:
            key = row.get(pk)
            local_row = local.get(key)
            if local_row is None:
                t_new += 1
            elif not _rows_equal(row, local_row, _table_meta(name)[0].columns.keys()):
                t_upd += 1
                if len(conflicts) < 50:
                    conflicts.append({
                        "table": name,
                        "pk": str(key)[:12],
                        "local_updated_at": local_row.get("updated_at") or local_row.get("created_at"),
                        "backup_updated_at": row.get("updated_at") or row.get("created_at"),
                    })
        if t_new or t_upd:
            per_table[name] = {"new": t_new, "updated": t_upd}
            new_count += t_new
            updated_count += t_upd
    return {"ok": True, "new_count": new_count, "updated_count": updated_count,
            "tables": per_table, "conflicts": conflicts}


def apply_merge(db: Session, path: str, strategy: str) -> dict:
    """第二阶段：按 strategy（overwrite=冲突用备份覆盖 / skip=保留本地）合并写入。
    单事务 + defer FK；插入撞自然键唯一约束回退定位（同 sync_engine 撞车语义）；
    不删除本地行（模块头语义说明）。"""
    from services.sync_engine import (
        _find_by_natural_key, _revive_row, _rows_equal, _table_meta,
        mark_tables_dirty, schedule_now, table_rows,
    )

    tables = load_zip_tables(path)
    inserted = updated = skipped = 0
    try:
        db.execute(text("PRAGMA defer_foreign_keys=ON"))
        now = datetime.now()
        for name in SYNC_TABLES:  # 父表在前
            rows = tables.get(name) or []
            if not rows:
                continue
            table, pk = _table_meta(name)
            cols = table.columns.keys()
            local = {r[pk]: r for r in table_rows(db, name)}
            for row in rows:
                key = row.get(pk)
                local_row = local.get(key)
                if local_row is None:
                    values = _revive_row(table, row)
                    if "updated_at" in cols:
                        values["updated_at"] = now  # 盖当前时刻：视为本地最新修改
                    try:
                        db.execute(table.insert().values(**values))
                        inserted += 1
                        continue
                    except IntegrityError:
                        hit = _find_by_natural_key(db, name, row)
                        if hit is None:
                            skipped += 1
                            continue
                        key = hit[pk]
                elif _rows_equal(row, local_row, cols):
                    continue
                elif strategy != "overwrite":
                    skipped += 1
                    continue
                # overwrite：备份内容落本地（自然键撞车场景落在本地 pk 上）
                values = _revive_row(table, row)
                values[pk] = key
                if "updated_at" in cols:
                    values["updated_at"] = now
                db.execute(table.update().where(
                    getattr(table.c, pk) == key).values(**values))
                updated += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    if inserted or updated:
        mark_tables_dirty([n for n in tables if tables[n]])
        schedule_now()  # 合并结果防抖推上远端
    logger.info("合并导入完成：新增 %d / 更新 %d / 跳过 %d（strategy=%s）",
                inserted, updated, skipped, strategy)
    return {"ok": True, "inserted": inserted, "updated": updated, "skipped": skipped,
            "message": f"合并导入完成：新增 {inserted} 项、更新 {updated} 项、跳过 {skipped} 项"}
