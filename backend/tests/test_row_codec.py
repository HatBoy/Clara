"""row_codec 接口契约测试（2026-11-17 架构批 2）。

规范形（canonical row）不变量的可执行文档：三载体等价 / round-trip 律 /
冻结字节形态 / 值级边缘（JSON null、非空布尔 None）。这些测试是 seam 的
合同，也是未来第五消费方的接入说明书（替换而非叠加——消费方旧散装实现
的等价性由 sync 簇 112 个既有用例守着，不在此重复）。
"""

import json as json_lib

from conftest import TestingSessionLocal
from models import Project
from services import row_codec as rc


def _project(title: str, *, color: str = "#1a73e8") -> str:
    with TestingSessionLocal() as db:
        row = Project(name=title, color=color)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


# —— 规范形：三载体等价（远端 JSON 文件载体与 ORM 载体逐字节一致）————————


def test_canonical_rows_json_roundtrip_equals_dump():
    """json 往返（远端表文件的真实形态）→ canonical_rows 与 dump_table 同 hash。

    这是「远端 hash == Base hash 跳过判定」成立的结构前提：同一份数据不管
    以对象形态（本地 dump）还是文本形态（远端 JSON）出现，规范形唯一。"""
    _project("等价性验证")
    with TestingSessionLocal() as db:
        dumped = rc.dump_table(db, "projects")
        via_json = json_lib.loads(json_lib.dumps(dumped, ensure_ascii=False))
        assert rc.hash_rows(rc.canonical_rows("projects", via_json)) == \
            rc.hash_rows(dumped)


def test_canonical_rows_orders_by_pk_and_filters_columns():
    """pk 规范序（I5）+ 列集过滤（I4：远端多列忽略）。"""
    a = _project("甲")
    b = _project("乙")
    rows = [{"id": b, "name": "乙", "ghost_column": "远端多余列"},
            {"id": a, "name": "甲"}]
    out = rc.canonical_rows("projects", rows)
    assert [r["id"] for r in out] == sorted([a, b])
    assert all("ghost_column" not in r for r in out)


# —— round-trip 律：dump → writable → 落库 → dump 恒等（I3 抽象形态）————————


def test_writable_roundtrip_restores_hash():
    """规范行经 writable_row 写入后重读，hash 复原——双机物理形态不分叉
    （af6daf2 的抽象形态回归锚）。"""
    pid = _project("往返验证", color="#abc123")
    with TestingSessionLocal() as db:
        src = rc.dump_table(db, "projects")
        # 删掉重插（同库模拟对端写入）
        from sqlalchemy import delete

        db.execute(delete(Project).where(Project.id == pid))
        db.execute(Project.__table__.insert().values(
            **rc.writable_row(Project.__table__, src[0])))
        db.commit()
        assert rc.hash_rows(rc.dump_table(db, "projects")) == rc.hash_rows(src)


# —— 冻结字节形态（I6）：黄金样本 ————————————————————————————————————


def test_hash_rows_frozen_payload():
    """sha1 载荷形态冻结：磁盘上全部快照与远端 manifest 依赖此字节序。"""
    rows = [{"id": "a", "t": "中文", "n": None}]
    import hashlib

    payload = json_lib.dumps(rows, ensure_ascii=False,
                             separators=(",", ":"), default=str)
    assert rc.hash_rows(rows) == hashlib.sha1(payload.encode("utf-8")).hexdigest()


# —— 值级边缘（I1-I3）——————————————————————————————————————————————————


def test_bindable_json_none_renders_null_construct():
    """JSON 列显式 None → null() 构造（绕过 SQLAlchemy 的 'null' 文本坑）。"""
    from sqlalchemy import null as sa_null

    from models import StatsReport
    col = StatsReport.__table__.columns["payload"]
    v = rc.bindable_value(col, None)
    assert isinstance(v, sa_null().__class__)


def test_revive_value_edges():
    """非空布尔 None→False；datetime 文本→对象；JSON 字符串→loads（坏值透传）。"""
    from datetime import datetime

    from models import AppSettings, Project, StatsReport
    bool_col = AppSettings.__table__.columns["auto_start"]
    assert rc.revive_value(bool_col, None) is False
    ts_col = Project.__table__.columns["updated_at"]
    assert rc.revive_value(ts_col, "2026-11-17T10:00:00") == \
        datetime(2026, 11, 17, 10, 0)
    json_col = StatsReport.__table__.columns["payload"]
    assert rc.revive_value(json_col, '{"k": 1}') == {"k": 1}
    assert rc.revive_value(json_col, "不是JSON") == "不是JSON"


def test_flat_row_text_dialect():
    """flat_row：datetime → iso 文本、JSON 对象 → dumps 文本、None → None。"""
    from models import Project

    table = Project.__table__
    row = {"id": "x", "name": "拍平", "color": "#fff",
           "updated_at": "2026-11-17T09:30:00"}
    flat = rc.flat_row(table, row)
    assert flat["updated_at"] == "2026-11-17T09:30:00"  # 已是文本，幂等
    assert flat["name"] == "拍平"


def test_rows_equal_body_cols_semantics():
    """rows_equal 按给定列集比对（撞键场景传剔 pk 列表——478243c 语义）。"""
    a = {"id": "1", "name": "同名", "color": "#111"}
    b = {"id": "2", "name": "同名", "color": "#111"}
    assert rc.rows_equal(a, b, ["name", "color"])
    assert not rc.rows_equal(a, b, ["id", "name", "color"])
