"""搜索端点用例（60 分档，2026-11-06 搜索批）：关键词命中 / 软删滤 /
notes 命中 / 英文大小写 / 空 q / 通配符转义 / project_name 组装。

结构照 test_tasks.py 的造数模式（TestingSessionLocal 直加 ORM 行 +
client.get 断言），seeded 基线用「周计划」类宽词只验非空不锁具体条数。
"""

from datetime import datetime, timedelta

from conftest import TestingSessionLocal
from models import Project, Task


def _add(db, **kw):
    """造数薄壳：默认活跃未排期任务，覆盖测试关注的差异字段"""
    db.add(Task(title=kw.pop("title"), sort_order=kw.pop("sort_order", 0), **kw))
    db.commit()


def test_search_hits_title(client):
    """标题子串命中（中文），日程/待办整包返回由前端分组。"""
    with TestingSessionLocal() as db:
        _add(db, title="和 HR 对齐 OKR", sort_order=1)
        _add(db, title="健身房", sort_order=2)
    res = client.get("/api/search", params={"q": "OKR"})
    tasks = res.json()["tasks"]
    assert [t["title"] for t in tasks] == ["和 HR 对齐 OKR"]


def test_search_hits_notes(client):
    """备注命中与标题同级（or 条件）。"""
    with TestingSessionLocal() as db:
        _add(db, title="神秘任务", notes="记得带发票", sort_order=1)
    res = client.get("/api/search", params={"q": "发票"})
    assert len(res.json()["tasks"]) == 1


def test_search_case_insensitive_ascii(client):
    """SQLite LIKE 对 ASCII 默认大小写不敏感——无需 LOWER 折叠。"""
    with TestingSessionLocal() as db:
        _add(db, title="Review Q3 Report", sort_order=1)
    res = client.get("/api/search", params={"q": "q3 report"})
    assert len(res.json()["tasks"]) == 1


def test_search_filters_soft_deleted(client):
    """软删行不可见（与列表口径一致）。"""
    with TestingSessionLocal() as db:
        _add(db, title="已删的会议", sort_order=1)
        row = db.query(Task).filter(Task.title == "已删的会议").one()
        row.is_deleted = True
        db.commit()
    res = client.get("/api/search", params={"q": "会议"})
    assert res.json()["tasks"] == []


def test_search_empty_q_returns_empty(client, seeded_db):
    """空 q / 纯空白返回空结果（不报错、不整表）。"""
    assert client.get("/api/search", params={"q": ""}).json()["tasks"] == []
    assert client.get("/api/search", params={"q": "   "}).json()["tasks"] == []


def test_search_escapes_like_wildcards(client):
    """用户输入的 %/_ 按字面匹配（不放大命中面）。"""
    with TestingSessionLocal() as db:
        _add(db, title="100%_完成度", sort_order=1)
        _add(db, title="普通任务", sort_order=2)
    assert len(client.get("/api/search", params={"q": "%"}).json()["tasks"]) == 1
    assert len(client.get("/api/search", params={"q": "_完"}).json()["tasks"]) == 1


def test_search_carries_project_name(client):
    """结果行带 project_name（免前端二次查表）。"""
    with TestingSessionLocal() as db:
        proj = Project(name="Clara 建设", color="#3b82f6", sort_order=0)
        db.add(proj)
        db.flush()
        _add(db, title="搜索功能开发", project_id=proj.id, sort_order=1)
    row = client.get("/api/search", params={"q": "搜索功能"}).json()["tasks"][0]
    assert row["project_name"] == "Clara 建设"


def test_search_scheduled_row_keeps_series_context(client):
    """系列命中行附带 cancelled_dates 聚合（前端展开虚拟实例需要）——
    造一个系列 + 一个墓碑例外，断言挖空日期带出。"""
    with TestingSessionLocal() as db:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        series = Task(
            title="每周复盘", start_time=base + timedelta(days=1, hours=10),
            recurrence_rule="FREQ=WEEKLY;BYDAY=FR", sort_order=1,
        )
        db.add(series)
        db.flush()
        db.add(Task(  # 挖空首个实例的墓碑行
            title="每周复盘", parent_id=series.id,
            original_time=base + timedelta(days=1, hours=10),
            is_deleted=True, sort_order=2,
        ))
        db.commit()
    row = client.get("/api/search", params={"q": "每周复盘"}).json()["tasks"][0]
    assert row["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert len(row["cancelled_dates"]) == 1


# ── 100 分档：系列实例 / 项目 / 建议条目 / 已完成过滤 ──


def test_search_expands_series_instances(client):
    """系列命中行展开为窗口内实例行（每实例独立 start_time 可跳转）。"""
    from services.tasks import expand_occurrences  # noqa: F401 —— 验证 import 面
    with TestingSessionLocal() as db:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        db.add(Task(
            title="每日站会", start_time=base + timedelta(days=1, hours=9),
            end_time=base + timedelta(days=1, hours=9, minutes=30),
            recurrence_rule="FREQ=DAILY", duration_mins=30, sort_order=1,
        ))
        db.commit()
    rows = client.get("/api/search", params={"q": "站会"}).json()["tasks"]
    assert len(rows) > 1  # 每日系列窗口内多实例
    starts = {r["start_time"] for r in rows}
    assert len(starts) == len(rows)  # 每实例独立时刻
    assert all(r["recurrence_rule"] == "FREQ=DAILY" for r in rows)


def test_search_hits_project_name(client):
    """项目名命中进 projects 组。"""
    with TestingSessionLocal() as db:
        db.add(Project(name="Clara 建设", color="#3b82f6", sort_order=0))
        db.commit()
    res = client.get("/api/search", params={"q": "Clara"})
    names = [p["name"] for p in res.json()["projects"]]
    assert "Clara 建设" in names


def test_search_hits_pending_ingest_proposal_item(client):
    """未决 from_ingest 卡的条目标题命中（已决卡与普通卡不进）。"""
    from services.ai_data import create_proposal
    with TestingSessionLocal() as db:
        create_proposal(db, kind="inbox",
                        payload={"from_ingest": True, "items": [{"title": "带发票报销"}]})
        create_proposal(db, kind="inbox",
                        payload={"from_ingest": True, "items": [{"title": "无关条目"}]})
        db.commit()
    items = client.get("/api/search", params={"q": "发票"}).json()["proposal_items"]
    assert [i["title"] for i in items] == ["带发票报销"]
    assert items[0]["kind"] == "inbox"


def test_search_hides_done_by_default(client):
    """已完成默认隐藏，include_done=true 保留。"""
    with TestingSessionLocal() as db:
        _add(db, title="已完成的复盘", done_at=datetime.now(), sort_order=1)
    assert client.get("/api/search", params={"q": "复盘"}).json()["tasks"] == []
    rows = client.get("/api/search", params={"q": "复盘", "include_done": "true"}).json()["tasks"]
    assert len(rows) == 1


# ── 120 分档：拼音首字母 / 时间词解析 / 快速创建口径 ──


def test_search_pinyin_full_and_initials(client):
    """纯字母 q 走拼音分支：全拼（ruguitang）与首字母（rgt/rh 类）都能命中。"""
    with TestingSessionLocal() as db:
        _add(db, title="乳鸽汤局", sort_order=1)
        _add(db, title="普通任务", sort_order=2)
    assert len(client.get("/api/search", params={"q": "ruge"}).json()["tasks"]) == 1
    assert len(client.get("/api/search", params={"q": "rgt"}).json()["tasks"]) == 1


def test_search_time_word_filters_and_strips(client):
    """「明天 体检」= 明明窗口内搜"体检"；时间词剥离后关键词单独命中。"""
    with TestingSessionLocal() as db:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        _add(db, title="明天体检", start_time=base + timedelta(days=1, hours=8), sort_order=1)
        _add(db, title="昨天体检", start_time=base - timedelta(days=1, hours=8), sort_order=2)
    res = client.get("/api/search", params={"q": "明天 体检"}).json()
    assert res["parsed_range"]["label"] == "明天"
    assert [t["title"] for t in res["tasks"]] == ["明天体检"]


def test_search_time_word_alone_lists_window(client):
    """纯时间词 = 该窗口任务清单（无关键词面）。"""
    with TestingSessionLocal() as db:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        _add(db, title="窗口内", start_time=base + timedelta(days=1, hours=9), sort_order=1)
        _add(db, title="窗口外", start_time=base + timedelta(days=9, hours=9), sort_order=2)
    res = client.get("/api/search", params={"q": "明天"}).json()
    assert [t["title"] for t in res["tasks"]] == ["窗口内"]


def test_search_weekday_word(client):
    """「周三」= 本周三单日窗（周一锚定）；上周三跨周正确。"""
    from services.search import _parse_time_range
    lo, hi, word = _parse_time_range("上周三 开会")
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = day0 - timedelta(days=day0.weekday())
    assert word == "上周三"
    assert lo == monday - timedelta(weeks=1) + timedelta(days=2)
    assert (hi - lo) == timedelta(days=1)
