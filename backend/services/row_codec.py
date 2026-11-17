"""行口径编解码（2026-11-17 架构批 2）：「同步域规范行」的唯一定义点。

规范行（canonical row）是被三路合并、哈希、内容比对、落盘、上云的唯一行形态，
其不变量族原先散在四份实现里（sync_engine._ser/_revive_value/_writable_row、
sync_merge._normalize_dump_row/_iso_norm、backup._materialize_sync_zip 内联拍平、
config_backup._ser/_revive/_bindable），发生过两次真机口径漂移事故：
- af6daf2（2026-11-17）：JSON 列显式 None 落库成文本 'null'——三路同中招各修一遍；
- 2026-11-16：裸 sqlite3 读出的空格分隔 datetime / JSON 原始字符串与
  table_rows 口径分叉 → analyze_merge 逐行误判「不同」（冲突虚高）。

**规范形不变量**（本 module 对外承诺的全部语义）：
- I1 datetime 族：DateTime/Date/Time 列 → isoformat 字符串（T 分隔）；输入是
  对象则 isoformat、是文本则 parse 后重发（空格分隔旧形态归一）；parse 失败
  原样透传（读路径容忍坏数据，不炸整轮）。
- I2 JSON 列：值为字符串先 json.loads（失败透传——防双重编码）；值为对象原样。
- I3 布尔：非空 Boolean 列的 None → False（历史 ADD COLUMN 踩坑形态防御）；
  整数 0/1 → bool。
- I4 列集过滤：输出键 = 输入键 ∩ metadata 列集（远端/旧版多列忽略；少列由
  调用方 schema 兼容性兜底——跨版本记 tech-debt）。
- I5 主键规范序：行集输出按 str(row[pk]) 升序——「同一份数据两次读 / push 与
  快照」的 sha1 恒一致（增量判定命脉，docs/sync-backup.md「改任何 hash/序列化
  逻辑前先想这条」）。
- I6 冻结字节形态：hash_rows 的 payload = json.dumps(rows, ensure_ascii=False,
  separators=(",", ":"), default=str) → sha1。**不可变**——磁盘上全部
  sync_snapshot/*.json 与远端 manifest 的 hash 依赖此形态。

**decode 产物纪律**：writable_row 返回值含 null() SQL 构造，只可即时喂
execute；flat_row 返回裸 sqlite3 文本。两者绝不进 actual/Base/推送内容。

**策略不在此**：SYNC_TABLES / SYNC_EXCLUDED_TABLES / NATURAL_KEYS / 三守卫 /
合并真值表 / 写库编排是 sync 域策略，住 sync_engine（策略变更与口径变更的
速率、失败形态不同——策略漏配有三件套清单与启动断言防，口径漂移靠本 module
单源防）。本 module 零 services 依赖（只 database.Base + sqlalchemy + stdlib），
是依赖图叶子，任何未来消费方可零环引入。
"""

import hashlib
import json
from datetime import date, datetime, time as dt_time
from typing import Any, Iterable, Mapping

from sqlalchemy import JSON as SAJSON
from sqlalchemy import Boolean, Date, DateTime, Time, null as sa_null, select
from sqlalchemy.orm import Session

from database import Base


# —— 值级内核（config_backup 共享层）——————————————————————————————————


def ser_value(value: Any) -> Any:
    """编码单值：datetime/date/time 对象 → isoformat 字符串；其余原样。"""
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    return value


def revive_value(col, value: Any) -> Any:
    """规范形值 → SQLAlchemy 可写值：datetime 族文本按列类型转回对象
    （SQLite DateTime 方言只认对象）；非空布尔列的 None 归 False；JSON 列
    拿到字符串先 loads（失败透传——直接交 SQLAlchemy 会再 dumps 一次落成
    双重编码字符串）。datetime 解析失败 ValueError 上抛（写路径严格）。"""
    if value is None:
        return False if (isinstance(col.type, Boolean) and not col.nullable) else None
    if isinstance(col.type, DateTime):
        return datetime.fromisoformat(str(value))
    if isinstance(col.type, Date):
        return date.fromisoformat(str(value))
    if isinstance(col.type, Time):
        return dt_time.fromisoformat(str(value))
    if isinstance(col.type, SAJSON) and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def bindable_value(col, value: Any) -> Any:
    """revive 后：JSON 列的 None → SQL null() 构造。

    SQLAlchemy 2.0 对 JSON 列的**显式 None 绑定**会走 json.dumps 落成文本
    'null'（省略键才是真 NULL）——2026-11-17 双机真机对账实证 af6daf2 事故
    的机制化单源：null() 直接渲染 SQL NULL，绕过序列化器（INSERT / UPDATE /
    executemany 均可用）。"""
    v = revive_value(col, value)
    if v is None and isinstance(col.type, SAJSON):
        return sa_null()
    return v


# —— 元数据 ————————————————————————————————————————————————————————————


def table_meta(name: str):
    """表对象与主键列名（cli_tools 主键是 adapter_id，动态取不硬编码）。
    未知表 KeyError（metadata 单源，fail fast）。"""
    table = Base.metadata.tables[name]
    pk = table.primary_key.columns[0].name
    return table, pk


# —— 编码：任意载体 → 规范形 ————————————————————————————————————————


def _row_to_dict(row) -> dict:
    """三种行载体统一为 dict：SQLAlchemy Row（._mapping）/ sqlite3.Row /
    普通 Mapping。载体差异在 implementation 内消化，interface 不见。"""
    if hasattr(row, "_mapping"):  # SQLAlchemy Row
        return dict(row._mapping)
    return dict(row)


def canonical_value(col, value: Any) -> Any:
    """单值 → 规范形值（按列类型分派；吸收 sync_merge._normalize_dump_row
    的文本对齐语义）。坏 datetime 文本原样透传（读路径容忍）。"""
    if value is None or col is None:
        return value
    t = col.type
    if isinstance(t, (DateTime, Date, Time)):
        if isinstance(value, str):
            try:
                return revive_value(col, value).isoformat()
            except ValueError:
                return value
        return ser_value(value)
    if isinstance(t, SAJSON):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    if isinstance(t, Boolean):
        return value if isinstance(value, bool) else bool(value)
    return value


def canonical_rows(name: str, rows: Iterable) -> list[dict]:
    """任意来源行集 → 规范行集（按 str(pk) 规范序，I1-I5 全量适用）。

    覆盖三种载体：ORM Core 查询结果（对象值）、裸 sqlite3 游标行（文本值 +
    0/1 布尔）、JSON 载入的 dict（通常已规范，幂等归一）。列集按 metadata
    过滤（远端多列忽略）。"""
    table, pk = table_meta(name)
    columns = table.columns
    out = [
        {c: canonical_value(columns[c], v) for c, v in _row_to_dict(row).items()
         if c in columns}
        for row in rows
    ]
    out.sort(key=lambda r: str(r.get(pk, "")))
    return out


def dump_table(db: Session, name: str) -> list[dict]:
    """全表读为规范行集（原 sync_engine.table_rows，签名与行为逐字兼容）。"""
    table, _ = table_meta(name)
    return canonical_rows(name, db.execute(select(table)).all())


# —— 解码：规范形 → 两种落库方言 ———————————————————————————————————————


def revive_row(table, row: Mapping) -> dict:
    """行 dict → SQLAlchemy 可写值（按目标表列集过滤：远端多列/少列均安全
    ——少列时 NOT NULL 列缺省由列 default 兜不住会炸，旧版本数据场景由
    schema 兼容性兜底）。"""
    out: dict = {}
    for col_name, value in row.items():
        col = table.columns.get(col_name)
        if col is None:
            continue  # 远端多列（本表已无）：忽略
        out[col_name] = revive_value(col, value)
    return out


def writable_row(table, row: Mapping) -> dict:
    """规范行 → SQLAlchemy execute 专用值（JSON 列 None → null()，I2 单源）。
    返回值不可再 json.dumps / 落盘 / 进 actual。"""
    return {c: bindable_value(table.columns[c], v)
            for c, v in revive_row(table, row).items()}


def flat_row(table, row: Mapping) -> dict:
    """规范行 → 裸 sqlite3 驱动直插文本（datetime→iso 文本、JSON→dumps 文本；
    None→None——裸驱动无 SQLAlchemy 的 JSON None 坑）。原 backup.
    _materialize_sync_zip 的 12 行内联拍平单源（先 revive 再拍平的必要性：
    JSON 列已是文本时直接交裸驱动会二次 dumps）。"""
    out: dict = {}
    for c, v in revive_row(table, row).items():
        if v is None:
            out[c] = None
        elif hasattr(v, "isoformat"):
            out[c] = v.isoformat()
        elif isinstance(table.columns[c].type, SAJSON):
            out[c] = json.dumps(v, ensure_ascii=False)
        else:
            out[c] = v
    return out


# —— 规范形上的判据 —————————————————————————————————————————————————————


def hash_rows(rows: list[dict]) -> str:
    """规范行集 → sha1（I6 冻结字节形态；**不排序**——输入须已是规范序
    （dump_table/canonical_rows 产物），单行比较场景序无意义）。"""
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                         default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def row_ts(row: Mapping) -> datetime | None:
    """行级 LWW 时间判据：updated_at → created_at 回退；解析失败/缺失返
    None。比较与平局规则（>= 本地优先）归调用方。"""
    for key in ("updated_at", "created_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            continue
    return None


def rows_equal(a: Mapping, b: Mapping, cols) -> bool:
    """两行按给定列集全等比对（撞键场景调用方传剔 pk 的 body_cols——
    478243c 语义：撞自然键两行 pk 必不同，含 pk 恒判不等）。"""
    return all(a.get(c) == b.get(c) for c in cols)
