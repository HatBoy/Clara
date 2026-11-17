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
from datetime import date, datetime, time
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
    from services.backup import _check_member_sizes, _normalize_members

    archive = Path(path)
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ValueError("所选文件不是有效的备份包")
    with zipfile.ZipFile(archive) as zf:
        norm = _normalize_members(zf.namelist())
        if not norm:
            raise ValueError("备份包缺少 manifest.json，无法识别格式")
        _check_member_sizes(zf, norm)  # 解压炸弹防线（二轮批 1，恢复路同款）
        try:
            manifest = json.loads(zf.read(norm["manifest.json"]))
        except (ValueError, OSError) as e:
            raise ValueError(f"备份包 manifest 损坏：{e}") from e
        app = manifest.get("app")

        tables: dict[str, list[dict]] = {}
        if app == "clara-sync":
            # 同步格式：tables/<t>.json 散文件
            from services.sync_engine import SYNC_EXCLUDED_TABLES

            member_map = {}
            for norm_name, orig in norm.items():
                if norm_name.startswith("tables/") and norm_name.endswith(".json"):
                    member_map[norm_name[len("tables/"):-len(".json")]] = orig
            # 排除表成员放行、数据丢弃（2026-11-17 二轮批 1）：2026-11-16 前同步过的
            # 远端目录必含 workflow_runs.json（当时在同步范围、排除后远端文件无人
            # 清理）——README 指引的「下载目录压 zip 合并导入」灾备路径此前直接
            # 422。与覆盖恢复 _materialize_sync_zip 的白名单口径对齐（真未知表仍拒）。
            unknown = set(member_map) - set(SYNC_TABLES) - set(SYNC_EXCLUDED_TABLES)
            if unknown:
                raise ValueError(f"备份包含未知表文件：{sorted(unknown)[:3]}")
            for name, orig in member_map.items():
                if name in SYNC_EXCLUDED_TABLES:
                    continue  # 设备本地表账本：不解析不导入（省大文件解析开销）
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
    from services.sync_engine import SYNC_EXCLUDED_TABLES

    for name in SYNC_EXCLUDED_TABLES:  # 设备本地配置/执行账本：导入时忽略
        tables.pop(name, None)
    return tables


def _dump_sqlite_tables(db_path: Path) -> dict[str, list[dict]]:
    """SQLite 库文件 → {表名: 规范行集}（sqlite3 原生只读游标；行口径归一
    收敛 row_codec.canonical_rows——裸读文本 datetime / JSON 字符串 / 0-1
    布尔与 table_rows 同口径，2026-11-16「冲突虚高」事故的结构性单源）。
    列取「metadata 列 ∩ 库内实有列」交集——旧版本备份列少时取共有列
    （缺表整表跳过）。"""
    from database import Base  # 列类型/主键元数据单源
    from services import row_codec

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
            out[name] = row_codec.canonical_rows(name, rows)
        return out
    finally:
        conn.close()


def analyze_merge(db: Session, path: str) -> dict:
    """第一阶段（dry-run，零写入）：统计新增/更新/冲突明细（前 50 条）。

    自然键预检（2026-11-17 二轮批 1）：本地无此 pk 但撞自然键（备份来自另一
    设备、同名项目/规则）且内容不同的行计入 conflicts 而非 new——「无冲突
    直通」以 conflicts 空为准，此前撞键行藏进 new_count 让 skip 直通静默
    覆盖本地新改动。撞键同内容 = 合并零操作，两侧都不计。"""
    from services import row_codec
    from services.sync_engine import NATURAL_KEYS, _find_by_natural_key

    tables = load_zip_tables(path)
    new_count = updated_count = 0
    per_table: dict[str, dict] = {}
    conflicts: list[dict] = []
    for name in SYNC_TABLES:
        rows = tables.get(name) or []
        if not rows:
            continue
        table, pk = row_codec.table_meta(name)
        cols = table.columns.keys()
        body_cols = [c for c in cols if c != pk]  # 内容比对剔除主键：撞键两行 pk 必不同
        local = {r[pk]: r for r in row_codec.dump_table(db, name)}
        t_new = t_upd = 0
        for row in rows:
            key = row.get(pk)
            local_row = local.get(key)
            if local_row is None and name in NATURAL_KEYS:
                hit = _find_by_natural_key(db, name, row)
                if hit is not None:
                    if row_codec.rows_equal(row, hit, body_cols):
                        continue
                    local_row = hit  # 撞键视同本地已有行（apply 同口径）
            if local_row is None:
                t_new += 1
            elif not row_codec.rows_equal(row, local_row, cols):
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
    不删除本地行（模块头语义说明）。

    撞键 LWW（2026-11-17 二轮批 1）：此前撞自然键后直接落入无条件 UPDATE——
    strategy 检查与 LWW 比较全部旁路（跳过 pk 相等路径的 elif 链），备份内容
    必然覆盖且 updated_at=now 让它在后续同步强制胜出；「无冲突直通」（skip）
    会静默覆盖本地新改动。现与 sync_engine._write_local 同款：撞键视同本地
    已有行，过 strategy 与 LWW 再写。"""
    from services import row_codec
    from services.sync_engine import _find_by_natural_key, mark_tables_dirty, schedule_now

    tables = load_zip_tables(path)
    inserted = updated = skipped = 0
    try:
        db.execute(text("PRAGMA defer_foreign_keys=ON"))
        now = datetime.now()
        for name in SYNC_TABLES:  # 父表在前
            rows = tables.get(name) or []
            if not rows:
                continue
            table, pk = row_codec.table_meta(name)
            cols = table.columns.keys()
            body_cols = [c for c in cols if c != pk]  # 撞键两行 pk 必不同，内容比对剔除
            local = {r[pk]: r for r in row_codec.dump_table(db, name)}
            for row in rows:
                key = row.get(pk)
                local_row = local.get(key)
                collided = False
                if local_row is None:
                    values = row_codec.writable_row(table, row)
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
                        key, local_row, collided = hit[pk], hit, True
                if row_codec.rows_equal(row, local_row, body_cols):
                    continue
                if strategy != "overwrite":
                    skipped += 1
                    continue
                if collided:
                    lt, rt = row_codec.row_ts(local_row), row_codec.row_ts(row)
                    if lt is not None and rt is not None and lt >= rt:
                        skipped += 1  # 本地较新：备份让位（LWW）
                        continue
                # overwrite：备份内容落本地（自然键撞车场景落在本地 pk 上）
                values = row_codec.writable_row(table, row)
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
