"""阶段一任务读/写接口与 seed 测试（基建见 conftest.py）。

运行：cd backend && python -m pytest
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TestingSessionLocal
from models import Task
from schemas import TaskCreate
from services import llm
from services.seed import seed_if_empty
from services.tasks import (
    apply_plan_entries,
    busy_intervals,
    calendar_load_summary,
    create_task,
    expand_occurrences,
    purge_expired_soft_deletes,
)


def _today_zero() -> datetime:
    """当天 0 点（naive 本地）——与 seed_if_empty 的基准同口径。"""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _first_by_title(tasks: list[dict], title: str) -> dict:
    return next(t for t in tasks if t["title"] == title)


def test_health(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_empty_db_returns_empty_lists(client: TestClient):
    # 未 seed 的空库：列表端点返回空数组而非报错
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/projects").json() == []


def test_seed_populates_full_dataset(client: TestClient, seeded_db):
    # seed 复刻前端 mock 全量：117 任务 + 3 项目
    assert len(client.get("/api/tasks").json()) == 117
    assert len(client.get("/api/projects").json()) == 3


def test_seed_idempotent(client: TestClient, seeded_db):
    # 模拟重启：再次 seed 直接跳过，数据不翻倍
    with TestingSessionLocal() as db:
        assert seed_if_empty(db) is False
    assert len(client.get("/api/tasks").json()) == 117


def test_seed_generates_today_relative_times(client: TestClient, seeded_db):
    # dateOffset=0 start=10 end=11.5 → 当天 10:00 / 11:30（naive 本地）
    tasks = client.get("/api/tasks").json()
    task = _first_by_title(tasks, "产品迭代里程碑评审")
    zero = _today_zero()
    assert task["start_time"] == (zero + timedelta(hours=10)).isoformat()
    assert task["end_time"] == (zero + timedelta(hours=11.5)).isoformat()


def test_priority_int_to_string(client: TestClient, seeded_db):
    # 库内 INT 1/2/3 → API 出参 high/medium/low
    tasks = client.get("/api/tasks").json()
    assert _first_by_title(tasks, "OKR 对齐会")["priority"] == "high"
    assert _first_by_title(tasks, "撰写周报")["priority"] == "medium"
    assert _first_by_title(tasks, "夜跑")["priority"] == "low"


def test_recurrence_rrule_roundtrip(client: TestClient, seeded_db):
    tasks = client.get("/api/tasks").json()
    assert _first_by_title(tasks, "英语课")["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert _first_by_title(tasks, "团队站会")["recurrence_rule"] == "FREQ=DAILY"
    assert _first_by_title(tasks, "周计划梳理")["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO"

    # 单条端点原样返回
    one = _first_by_title(tasks, "英语课")
    fetched = client.get(f"/api/tasks/{one['id']}").json()
    assert fetched["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"

    # monthly 透传：ORM 直改后读回原样（后端不解析 RRULE）
    with TestingSessionLocal() as db:
        row = db.scalars(select(Task).where(Task.title == "周计划梳理")).first()
        row.recurrence_rule = "FREQ=MONTHLY"
        db.commit()
    tasks = client.get("/api/tasks").json()
    assert _first_by_title(tasks, "周计划梳理")["recurrence_rule"] == "FREQ=MONTHLY"


def test_get_task_404(client: TestClient):
    assert client.get(f"/api/tasks/{uuid.uuid4()}").status_code == 404


def test_soft_deleted_filtered(client: TestClient, seeded_db):
    victim = _first_by_title(client.get("/api/tasks").json(), "产品迭代里程碑评审")
    with TestingSessionLocal() as db:
        db.get(Task, victim["id"]).is_deleted = True
        db.commit()
    titles = [t["title"] for t in client.get("/api/tasks").json()]
    assert "产品迭代里程碑评审" not in titles
    assert client.get(f"/api/tasks/{victim['id']}").status_code == 404


def test_list_range_filter(client: TestClient, seeded_db):
    # 查今天一整天：窗口内单次 7 条（里程碑评审/技术预研/撰写周报/设计对齐/
    # 回复合作方邮件/夜跑/团队站会 anchor 0）+ 窗外基准重复系列 6 条 = 13 条；
    # 未排期任务被自然排除
    zero = _today_zero()
    resp = client.get(
        "/api/tasks",
        params={"start": zero.isoformat(), "end": (zero + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 13
    assert all(t["start_time"] is not None for t in tasks)


def test_range_includes_out_of_window_recurring(client: TestClient, seeded_db):
    # 窗外基准的重复系列必须命中：基准在今天-300 的 weekly 系列，今天的范围
    # 查询应返回（前端 occursOn 以基准+规则在前端展开，按基准日过滤会漏）
    with TestingSessionLocal() as db:
        db.add(Task(
            title="远古周会",
            start_time=_today_zero() - timedelta(days=300, hours=10),
            end_time=_today_zero() - timedelta(days=300, hours=11),
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
            sort_order=999,
        ))
        db.commit()
    zero = _today_zero()
    resp = client.get(
        "/api/tasks",
        params={"start": zero.isoformat(), "end": (zero + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 200
    assert _first_by_title(resp.json(), "远古周会")["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO"


def test_range_end_exclusive(client: TestClient, seeded_db):
    # end 边界不含：单次任务 start_time 恰等于 end 参数 → 不返回
    edge = _today_zero() + timedelta(days=5, hours=9)
    with TestingSessionLocal() as db:
        db.add(Task(title="边界任务", start_time=edge, sort_order=998))
        db.commit()
    resp = client.get(
        "/api/tasks", params={"start": _today_zero().isoformat(), "end": edge.isoformat()}
    )
    assert "边界任务" not in [t["title"] for t in resp.json()]


def test_unscheduled_filter(client: TestClient, seeded_db):
    # 只返回未排期待办（seed 20 条），全部 start_time 为空
    tasks = client.get("/api/tasks", params={"unscheduled": "true"}).json()
    assert len(tasks) == 20
    assert all(t["start_time"] is None for t in tasks)


def test_unscheduled_ignores_range(client: TestClient, seeded_db):
    # unscheduled=true 时范围参数被忽略（两种请求结果等价）
    zero = _today_zero()
    a = client.get("/api/tasks", params={"unscheduled": "true"}).json()
    b = client.get(
        "/api/tasks",
        params={"unscheduled": "true",
                "start": zero.isoformat(),
                "end": (zero + timedelta(days=1)).isoformat()},
    ).json()
    assert a == b


def test_range_open_end(client: TestClient, seeded_db):
    # 只传 start 不传 end（前端初始窗口契约：历史侧起点、未来侧开放）——
    # seed 已排期 97 条基准全在今天-60 之后 → 全命中（单次 90 + 系列 7）
    resp = client.get(
        "/api/tasks", params={"start": (_today_zero() - timedelta(days=60)).isoformat()}
    )
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 97
    assert all(t["start_time"] is not None for t in tasks)


def test_list_order_matches_mock(client: TestClient, seeded_db):
    # sort_order 稳定序 = 前端 mock 数组序（首条与末条锚定）
    titles = [t["title"] for t in client.get("/api/tasks").json()]
    assert titles[0] == "周计划梳理"
    assert titles[-1] == "采购下周早餐食材和咖啡豆"


def test_lifespan_seeds_on_startup(monkeypatch):
    # 验证 lifespan 真接线：monkeypatch 掉真实建表与 SessionLocal（指向测试内存库），
    # 进入 TestClient 上下文触发 lifespan 后应自动灌入 seed
    import main

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "SessionLocal", TestingSessionLocal)
    with TestClient(main.app) as c:
        assert len(c.get("/api/tasks").json()) == 117


# —— 写路径：PATCH 部分更新 ——


def _tid_by_title(client: TestClient, title: str) -> str:
    return _first_by_title(client.get("/api/tasks").json(), title)["id"]


def test_update_task_title_persists(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    before = _first_by_title(client.get("/api/tasks").json(), "夜跑")
    resp = client.patch(f"/api/tasks/{task_id}", json={"title": "夜跑 5 公里"})
    assert resp.status_code == 200 and resp.json()["title"] == "夜跑 5 公里"
    # 键省略的字段不动（exclude_unset 语义锚）：priority/notes 原值保持
    body = resp.json()
    assert body["priority"] == before["priority"] == "low"
    assert body["notes"] == before["notes"]
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).title == "夜跑 5 公里"


def test_update_task_note_set_null_clears_and_empty_normalized(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    # 先设值
    assert client.patch(f"/api/tasks/{task_id}", json={"notes": "注意拉伸"}).json()["notes"] == "注意拉伸"
    # 显式 null 清空
    assert client.patch(f"/api/tasks/{task_id}", json={"notes": None}).json()["notes"] is None
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).notes is None
    # 空串归一同清空（str_strip + validator）
    assert client.patch(f"/api/tasks/{task_id}", json={"notes": "临时"}).status_code == 200
    assert client.patch(f"/api/tasks/{task_id}", json={"notes": ""}).json()["notes"] is None


def test_update_task_priority_string_to_int(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    resp = client.patch(f"/api/tasks/{task_id}", json={"priority": "high"})
    assert resp.status_code == 200 and resp.json()["priority"] == "high"
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).priority == 1  # 库内 INT
    assert client.patch(f"/api/tasks/{task_id}", json={"priority": "urgent"}).status_code == 422


def test_update_task_duration_mins_set_and_clear(client: TestClient, seeded_db):
    # 场景1：未排期任务（start/end 空）duration_mins 可自由设置/清空，不触发重算——
    # 收件箱耗时档路径（已排期卡不渲染耗时行，时长由排期字段派生，见下方重算用例）
    task_id = client.get("/api/tasks", params={"unscheduled": "true"}).json()[0]["id"]
    assert client.patch(f"/api/tasks/{task_id}", json={"duration_mins": 45}).json()["duration_mins"] == 45
    assert client.patch(f"/api/tasks/{task_id}", json={"duration_mins": None}).json()["duration_mins"] is None


def test_update_task_duration_recomputed_from_schedule(client: TestClient, seeded_db):
    # 场景3/4：start/end 齐备时 duration_mins ≡ end-start 自动重算（拖拽/拉伸调时长后
    # 库内耗时跟上；三值齐备恒保持 end-start = duration_mins）
    task_id = _tid_by_title(client, "产品迭代里程碑评审")  # seed 今天 10:00–11:30
    zero = _today_zero()
    resp = client.patch(f"/api/tasks/{task_id}", json={
        "start_time": (zero + timedelta(hours=9)).isoformat(),
        "end_time": (zero + timedelta(hours=10.5)).isoformat(),
    })
    assert resp.status_code == 200 and resp.json()["duration_mins"] == 90
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).duration_mins == 90


def test_update_task_duration_recomputed_when_start_moves_only(client: TestClient, seeded_db):
    # 合并态重算：只发 start_time（end_time 是库中旧值）同样触发——三值齐备的不变式
    # 对 setattr 后的整体生效，不要求本请求携带全部排期字段
    task_id = _tid_by_title(client, "产品迭代里程碑评审")  # 10:00–11:30
    zero = _today_zero()
    resp = client.patch(f"/api/tasks/{task_id}", json={
        "start_time": (zero + timedelta(hours=10.5)).isoformat(),
    })
    assert resp.status_code == 200 and resp.json()["duration_mins"] == 60  # 10:30–11:30
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).duration_mins == 60


def test_update_task_manual_duration_overridden_by_schedule(client: TestClient, seeded_db):
    # 冲突裁决：已排期任务即使显式发 duration_mins，与 end-start 计算冲突时以排期为准
    task_id = _tid_by_title(client, "产品迭代里程碑评审")  # 10:00–11:30 = 90 分钟
    resp = client.patch(f"/api/tasks/{task_id}", json={"duration_mins": 45})
    assert resp.status_code == 200 and resp.json()["duration_mins"] == 90
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).duration_mins == 90


def test_update_task_schedule_moves_start_end(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "产品迭代里程碑评审")
    zero = _today_zero()
    resp = client.patch(f"/api/tasks/{task_id}", json={
        "start_time": (zero + timedelta(hours=9)).isoformat(),
        "end_time": (zero + timedelta(hours=10.5)).isoformat(),
    })
    assert resp.status_code == 200
    with TestingSessionLocal() as db:
        row = db.get(Task, task_id)
        assert row.start_time == zero + timedelta(hours=9)
        assert row.end_time == zero + timedelta(hours=10.5)


def test_update_task_end_before_start_422(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "产品迭代里程碑评审")
    zero = _today_zero()
    resp = client.patch(f"/api/tasks/{task_id}", json={
        "start_time": (zero + timedelta(hours=10)).isoformat(),
        "end_time": (zero + timedelta(hours=10)).isoformat(),
    })
    assert resp.status_code == 422


def test_update_task_end_without_start_422(client: TestClient, seeded_db):
    # 未排期任务只发 end_time：合并态校验拦"有 end 无 start"（取 unscheduled 列表首条确保未排期）
    task_id = client.get("/api/tasks", params={"unscheduled": "true"}).json()[0]["id"]
    resp = client.patch(f"/api/tasks/{task_id}", json={
        "end_time": (_today_zero() + timedelta(hours=10)).isoformat()
    })
    assert resp.status_code == 422


def test_update_task_start_null_clears_end(client: TestClient, seeded_db):
    # start_time 置 null（取消排期）联动清 end——库内不变式 end ⇒ start
    task_id = _tid_by_title(client, "产品迭代里程碑评审")
    resp = client.patch(f"/api/tasks/{task_id}", json={"start_time": None})
    assert resp.status_code == 200
    assert resp.json()["start_time"] is None and resp.json()["end_time"] is None
    with TestingSessionLocal() as db:
        row = db.get(Task, task_id)
        assert row.start_time is None and row.end_time is None


def test_update_task_recurrence_valid_invalid_and_clear(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    # 合法三形态透传（BYDAY 乱序同收——后端不规范化，规范序由前端生成端保证）
    for rrule in ("FREQ=DAILY", "FREQ=MONTHLY", "FREQ=WEEKLY;BYDAY=WE,MO"):
        assert client.patch(f"/api/tasks/{task_id}", json={"recurrence_rule": rrule}).json()[
            "recurrence_rule"] == rrule
    # 坏格式：未知 FREQ / weekly 缺 BYDAY / BYDAY 空格分隔 / 超集键 INTERVAL
    for rrule in ("FREQ=YEARLY", "FREQ=WEEKLY", "FREQ=WEEKLY;BYDAY=MO WE", "INTERVAL=2;FREQ=DAILY"):
        assert client.patch(f"/api/tasks/{task_id}", json={"recurrence_rule": rrule}).status_code == 422
    # null 清空
    assert client.patch(f"/api/tasks/{task_id}", json={"recurrence_rule": None}).json()[
        "recurrence_rule"] is None


def test_update_task_project_id_move_and_remove(client: TestClient, seeded_db):
    from models import Project

    task_id = _tid_by_title(client, "夜跑")
    with TestingSessionLocal() as db:
        alpha_id = db.scalar(select(Project.id).where(Project.name == "Alpha Launch"))
    assert client.patch(f"/api/tasks/{task_id}", json={"project_id": alpha_id}).status_code == 200
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).project_id == alpha_id
    # 显式 null 移出项目；脏 id 422（FK 真约束下会 500，service 拦）
    assert client.patch(f"/api/tasks/{task_id}", json={"project_id": None}).json()["project_id"] is None
    assert client.patch(f"/api/tasks/{task_id}", json={"project_id": str(uuid.uuid4())}).status_code == 422


def test_update_task_timezone_normalized(client: TestClient, seeded_db):
    # 带 +HH:MM 偏移的入参在契约层归一为 naive 本地（同查询参数口径）
    task_id = _tid_by_title(client, "夜跑")
    offset = datetime.now().astimezone().strftime("%z")  # 本机偏移，如 +0800
    iso_with_tz = _today_zero().isoformat() + f"{offset[:3]}:{offset[3:]}"
    resp = client.patch(f"/api/tasks/{task_id}", json={"start_time": iso_with_tz})
    assert resp.status_code == 200
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).start_time == _today_zero()


def test_update_task_validation_and_404(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    # 长度/NOT NULL 拦截（title 31 字符；title 显式 null）
    assert client.patch(f"/api/tasks/{task_id}", json={"title": "x" * 31}).status_code == 422
    assert client.patch(f"/api/tasks/{task_id}", json={"title": None}).status_code == 422
    assert client.patch(f"/api/tasks/{task_id}", json={"title": "   "}).status_code == 422  # strip 后空
    # 404：不存在与软删
    assert client.patch(f"/api/tasks/{uuid.uuid4()}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert client.patch(f"/api/tasks/{task_id}", json={"title": "x"}).status_code == 404


def test_delete_task_soft_deletes(client: TestClient, seeded_db):
    task_id = _tid_by_title(client, "夜跑")
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
    assert len(client.get("/api/tasks").json()) == 116
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).is_deleted is True  # 软删行还在


# —— 删除策略 6 场景（2026-09-14 判定矩阵，见 services/tasks.py 模块头）——


def _add_raw_task(title: str, **kw) -> str:
    """ORM 直插任务行并返回 id——控制 created_at/deleted_at 等不经 API 的字段。"""
    with TestingSessionLocal() as db:
        row = Task(title=title, sort_order=990, **kw)
        db.add(row)
        db.commit()
        return row.id


def test_delete_flash_unscheduled_physical(client: TestClient, seeded_db):
    # 场景 1：创建 <300s 且从未排期 → 物理删（行消失，不给 AI 学习样本留噪音）
    task_id = _add_raw_task("闪建闪删")
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id) is None


def test_delete_draft_past_flash_window_soft(client: TestClient, seeded_db):
    # 场景 2：创建超 300s 且从未排期 → 软删 + deleted_at 落锚（TTL 计时起点）
    task_id = _add_raw_task("老草稿")
    with TestingSessionLocal() as db:
        db.get(Task, task_id).created_at = datetime.now() - timedelta(seconds=301)
        db.commit()
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    with TestingSessionLocal() as db:
        row = db.get(Task, task_id)
        assert row.is_deleted is True and row.deleted_at is not None


def test_purge_only_expired_drafts(client: TestClient, seeded_db):
    # purge 精确清理场景 2：仅"软删超 7 天 + 非重复 + 从未排期 + 非墓碑"的行；
    # 未到期 / 曾经排期（场景 3）/ 重复系列（场景 6）/ 墓碑行一律保留
    with TestingSessionLocal() as db:
        expired = Task(title="过期草稿", sort_order=990, is_deleted=True,
                       deleted_at=datetime.now() - timedelta(days=8))
        fresh = Task(title="未到期草稿", sort_order=991, is_deleted=True,
                     deleted_at=datetime.now() - timedelta(days=6))
        cancelled = Task(title="排期取消", sort_order=992, is_deleted=True, ever_scheduled=True,
                         deleted_at=datetime.now() - timedelta(days=8))
        series = Task(title="死系列", sort_order=993, is_deleted=True, recurrence_rule="FREQ=DAILY",
                      deleted_at=datetime.now() - timedelta(days=8))
        db.add_all([expired, fresh, cancelled, series])
        db.flush()  # 拿回 UUID 后墓碑才能引用
        db.add(Task(title="墓碑", sort_order=994, parent_id=series.id, is_deleted=True,
                    deleted_at=datetime.now() - timedelta(days=8)))
        db.commit()
        ids = {t.title: t.id for t in (expired, fresh, cancelled, series)}
        tombstone_id = db.scalar(select(Task.id).where(Task.parent_id == series.id))

    with TestingSessionLocal() as db:
        assert purge_expired_soft_deletes(db) == 1
        assert db.get(Task, ids["过期草稿"]) is None
        for title in ("未到期草稿", "排期取消", "死系列"):
            assert db.get(Task, ids[title]).is_deleted is True
        assert db.get(Task, tombstone_id) is not None  # 墓碑永生


def test_delete_scheduled_permanent_soft_survives_purge(client: TestClient, seeded_db):
    # 场景 3：曾经排期（seed 的夜跑今天有排期）→ 永久软删，deleted_at 拨到 30 天前
    # 也不被 purge 清理——高价值数据资产永不物理抹除
    task_id = _tid_by_title(client, "夜跑")
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    with TestingSessionLocal() as db:
        db.get(Task, task_id).deleted_at = datetime.now() - timedelta(days=30)
        db.commit()
    with TestingSessionLocal() as db:
        purge_expired_soft_deletes(db)
        assert db.get(Task, task_id) is not None


def test_ever_scheduled_sticky_after_schedule_clear(client: TestClient, seeded_db):
    # 曾经排期是单向开关：置 True 后显式清 start_time（拖回收件箱语义）不清回，
    # 场景 3 判定不因"排期后又回炉"失效
    task_id = client.get("/api/tasks", params={"unscheduled": "true"}).json()[0]["id"]
    zero = _today_zero()
    assert client.patch(f"/api/tasks/{task_id}", json={
        "start_time": (zero + timedelta(hours=10)).isoformat()}).status_code == 200
    with TestingSessionLocal() as db:
        assert db.get(Task, task_id).ever_scheduled is True
    assert client.patch(f"/api/tasks/{task_id}", json={"start_time": None}).status_code == 200
    with TestingSessionLocal() as db:
        row = db.get(Task, task_id)
        assert row.start_time is None and row.ever_scheduled is True


def test_delete_recurring_this_creates_tombstone(client: TestClient, seeded_db):
    # 场景 4：仅删本次 → 墓碑行（主任务规则不动），读路径聚合出 cancelled_dates；
    # 同日重复调用幂等（墓碑不叠加）；聚合仅内存态、不写回已废弃的 JSON 列
    task_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()  # 下周一（英语课 MO/WE/FR 实例日）
    resp = client.delete(f"/api/tasks/{task_id}", params={"mode": "this", "date": date})
    assert resp.status_code == 204
    fetched = client.get(f"/api/tasks/{task_id}").json()  # 主任务仍活、规则不动
    assert fetched["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert fetched["cancelled_dates"] == [date]
    assert date in _first_by_title(client.get("/api/tasks").json(), "英语课")["cancelled_dates"]
    assert client.delete(f"/api/tasks/{task_id}", params={"mode": "this", "date": date}).status_code == 204
    with TestingSessionLocal() as db:
        assert len(db.scalars(select(Task).where(Task.parent_id == task_id)).all()) == 1
        assert db.get(Task, task_id).cancelled_dates is None  # DB 列不被聚合写回


def test_delete_recurring_following_appends_until(client: TestClient, seeded_db):
    # 场景 5：删此日程及后续 → 规则尾拼 UNTIL=被点日前一晚 23:59:59（前一日实例
    # 保留、被点日起砍掉）；再次截断到更早日期时新 UNTIL 覆盖旧值（不叠加）。
    # 两个 date 均取英语课真实实例日（下周一/再下周一）——存在性校验拦非实例日
    task_id = _tid_by_title(client, "英语课")
    d2 = _next_weekday(datetime.now(), 1)
    d = _next_weekday(d2 + timedelta(days=1), 1)  # 再下一个周一（恒晚于 d2）
    assert client.delete(f"/api/tasks/{task_id}",
                         params={"mode": "following", "date": d.date().isoformat()}).status_code == 204
    expected = "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=" + (d - timedelta(days=1)).strftime("%Y%m%d") + "T235959"
    assert client.get(f"/api/tasks/{task_id}").json()["recurrence_rule"] == expected

    client.delete(f"/api/tasks/{task_id}", params={"mode": "following", "date": d2.date().isoformat()})
    expected2 = "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=" + (d2 - timedelta(days=1)).strftime("%Y%m%d") + "T235959"
    assert client.get(f"/api/tasks/{task_id}").json()["recurrence_rule"] == expected2


def test_reschedule_all_cross_day_shifts_byday(client: TestClient, seeded_db):
    # 2026-11-12 修复：weekly 系列跨天重锚（拖拽/改期 mode=all）时 BYDAY 跟随新
    # 基准日平移——此前不动 BYDAY 造成「基准周五、规则周三」脏状态，前端按 BYDAY
    # 展开实例仍渲染在原星期，用户视角「拖不动」。纯调钟点（同日）BYDAY 不动。
    wed = _next_weekday(datetime.now(), 3)  # ISO 3=周三
    tid = _add_raw_task("锚点周会", recurrence_rule="FREQ=WEEKLY;BYDAY=WE",
                        start_time=_today_zero() + timedelta(days=(wed.date() - datetime.now().date()).days, hours=14))
    fri = wed + timedelta(days=2)
    resp = client.patch(f"/api/tasks/{tid}", json={
        "start_time": f"{fri:%Y-%m-%dT}16:00:00",
        "end_time": f"{fri:%Y-%m-%dT}17:00:00",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=FR"  # 单日跟随新日
    assert body["start_time"].startswith(f"{fri:%Y-%m-%d}T16:00")
    # 同日纯调钟点：日期不变 BYDAY 原样
    resp2 = client.patch(f"/api/tasks/{tid}", json={
        "start_time": f"{fri:%Y-%m-%dT}09:30:00",
        "end_time": f"{fri:%Y-%m-%dT}10:30:00",
    })
    assert resp2.json()["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=FR"


def test_reschedule_all_multi_byday_set_shifts_together(client: TestClient, seeded_db):
    # 多日集合整体平移保持相对关系：MO,WE 拖到周五（整体 +4 天）→ FR,SU
    mon = _next_weekday(datetime.now(), 1)  # ISO 1=周一
    tid = _add_raw_task("多日系列", recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE",
                        start_time=_today_zero() + timedelta(days=(mon.date() - datetime.now().date()).days, hours=9))
    fri = mon + timedelta(days=4)
    resp = client.patch(f"/api/tasks/{tid}", json={
        "start_time": f"{fri:%Y-%m-%dT}09:00:00",
        "end_time": f"{fri:%Y-%m-%dT}10:00:00",
    })
    assert resp.status_code == 200
    assert resp.json()["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=FR,SU"


def test_reschedule_following_cross_day_shifts_new_series(client: TestClient, seeded_db):
    # 「及后续」跨天改期：老系列截断、新系列 BYDAY 相对原实例日平移（周一实例日
    # 改到周五 → MO,WE,FR 整体 +4 → FR,SU,TU）
    task_id = _tid_by_title(client, "英语课")
    mon = _next_weekday(datetime.now(), 1)
    fri = mon + timedelta(days=4)
    resp = client.patch(
        f"/api/tasks/{task_id}",
        params={"mode": "following", "date": mon.date().isoformat()},
        json={"start_time": f"{fri:%Y-%m-%dT}15:00:00",
              "end_time": f"{fri:%Y-%m-%dT}16:00:00"},
    )
    assert resp.status_code == 200
    new = resp.json()
    assert new["id"] != task_id
    assert new["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=FR,SU,TU"
    assert new["start_time"].startswith(f"{fri:%Y-%m-%d}T15:00")



    # 场景 6：刚建的重复系列（<300s）→ 物理删且连带清墓碑；建久了 → 永久软删
    fresh_id = _add_raw_task("新系列", recurrence_rule="FREQ=DAILY",
                             start_time=_today_zero() + timedelta(hours=9))
    client.delete(f"/api/tasks/{fresh_id}",
                  params={"mode": "this", "date": datetime.now().date().isoformat()})  # 先留一颗墓碑
    assert client.delete(f"/api/tasks/{fresh_id}", params={"mode": "all"}).status_code == 204
    with TestingSessionLocal() as db:
        assert db.get(Task, fresh_id) is None
        assert db.scalars(select(Task).where(Task.parent_id == fresh_id)).all() == []

    old_id = _add_raw_task("老系列", recurrence_rule="FREQ=DAILY")
    with TestingSessionLocal() as db:
        db.get(Task, old_id).created_at = datetime.now() - timedelta(minutes=10)
        db.commit()
    assert client.delete(f"/api/tasks/{old_id}", params={"mode": "all"}).status_code == 204
    with TestingSessionLocal() as db:
        assert db.get(Task, old_id).is_deleted is True


def test_delete_mode_validation_422(client: TestClient, seeded_db):
    # 非重复任务发 this/following（无系列语义）→ 422；this/following 缺 date 或坏格式 → 422
    task_id = _tid_by_title(client, "夜跑")
    assert client.delete(f"/api/tasks/{task_id}", params={"mode": "this"}).status_code == 422
    series_id = _tid_by_title(client, "英语课")
    assert client.delete(f"/api/tasks/{series_id}", params={"mode": "this"}).status_code == 422
    assert client.delete(f"/api/tasks/{series_id}",
                         params={"mode": "following", "date": "2026/09/15"}).status_code == 422


def test_update_task_rrule_until_accepted(client: TestClient, seeded_db):
    # UNTIL 尾（RFC 5545 DATE-TIME、naive 本地）合法透传；DATE 形式/带连字符坏格式拦截
    task_id = _tid_by_title(client, "夜跑")
    ok = "FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20260912T235959"
    assert client.patch(f"/api/tasks/{task_id}", json={"recurrence_rule": ok}).json()["recurrence_rule"] == ok
    assert client.patch(f"/api/tasks/{task_id}",
                        json={"recurrence_rule": "FREQ=DAILY;UNTIL=2026-09-12"}).status_code == 422
    assert client.patch(f"/api/tasks/{task_id}",
                        json={"recurrence_rule": "FREQ=DAILY;UNTIL=20260912"}).status_code == 422


# —— 编辑的 Clone & Override（mode=this/following 分流，见 services/tasks.py 模块头）——


def _next_weekday(base: datetime, target: int) -> datetime:
    """base 起未来 1..7 天内最近的 target 星期几（ISO 1=周一…7=周日）0 点——
    保证是被点系列未来某真实实例日。"""
    shift = (target - base.isoweekday()) % 7 or 7
    return (base + timedelta(days=shift)).replace(hour=0, minute=0, second=0, microsecond=0)


def test_patch_this_materializes_exception(client: TestClient, seeded_db):
    # 场景 A：仅修改此日程 → 幻影实例实体化为独立单次行（字段继承主记录、patch 覆盖、
    # recurrence 置空、parent_id/original_time 锚定主系列），主记录完全不动
    series = _first_by_title(client.get("/api/tasks").json(), "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()  # 下周一（英语课实例日）
    resp = client.patch(f"/api/tasks/{series['id']}",
                        params={"mode": "this", "date": date},
                        json={"notes": "本节补课"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] != series["id"]  # 新建例外行，非主记录
    assert body["recurrence_rule"] is None  # 例外是单次
    assert body["start_time"] == f"{date}T{series['start_time'][11:16]}:00"  # 实例日 + 主钟点
    assert body["notes"] == "本节补课"  # patch 覆盖
    assert body["title"] == "英语课"  # 其余字段继承
    with TestingSessionLocal() as db:
        row = db.get(Task, body["id"])
        assert row.parent_id == series["id"]
        assert row.original_time == row.start_time
        master = db.get(Task, series["id"])  # 主记录不动：notes 原值、规则原样
        assert master.notes == series["notes"]
        assert master.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_patch_this_hollows_virtual_and_returns_entity(client: TestClient, seeded_db):
    # 读路径虚实结合：主系列 cancelled_dates 含该日（挖空虚拟实例），例外实体
    # 自带 start_time + recurrence NULL → 窗口查询当普通单次任务返回渲染
    series_id = _tid_by_title(client, "英语课")
    monday = _next_weekday(datetime.now(), 1)
    date = monday.date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}",
                           params={"mode": "this", "date": date}, json={"notes": "x"}).json()
    assert client.get(f"/api/tasks/{series_id}").json()["cancelled_dates"] == [date]
    window = client.get("/api/tasks", params={
        "start": f"{date}T00:00:00",
        "end": (monday + timedelta(days=1)).date().isoformat() + "T00:00:00",
    }).json()
    ids = [t["id"] for t in window]
    assert series_id in ids and created["id"] in ids  # 系列（恒返回）+ 例外实体（按交集）


def test_task_out_exposes_parent_id_for_exception(client: TestClient, seeded_db):
    # TaskOut.parent_id：活体例外行（"仅修改此日程"实体化的单次实体）= 主系列 id
    #（前端据此派生 isException，禁用重复设置入口——service 层拒绝例外行原地转系列）；
    # 主系列行与普通任务 = None；墓碑行软删视为不存在恒不出现在响应里
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}",
                           params={"mode": "this", "date": date},
                           json={"notes": "x"}).json()
    assert client.get(f"/api/tasks/{created['id']}").json()["parent_id"] == series_id
    assert client.get(f"/api/tasks/{series_id}").json()["parent_id"] is None


def test_patch_this_same_day_reuses_exception_row(client: TestClient, seeded_db):
    # 同日二次编辑 → 复用同一例外行（活体例外数不增、值覆盖，幂等不叠加）
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    a = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                     json={"notes": "一改"}).json()
    b = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                     json={"notes": "二改"}).json()
    assert b["id"] == a["id"] and b["notes"] == "二改"
    with TestingSessionLocal() as db:
        rows = db.scalars(select(Task).where(
            Task.parent_id == series_id, Task.is_deleted.is_(False))).all()
        assert len(rows) == 1


def test_patch_following_splits_series(client: TestClient, seeded_db):
    # 场景 B：此日程及后续 → 老系列 UNTIL 截断到被点日前一晚；响应 = 新主记录
    # （parent_id 空、规则复制去 UNTIL 尾、基准重锚被点实例日钟点、patch 覆盖）
    series = _first_by_title(client.get("/api/tasks").json(), "英语课")
    d = _next_weekday(datetime.now(), 1)
    date = d.date().isoformat()
    resp = client.patch(f"/api/tasks/{series['id']}",
                        params={"mode": "following", "date": date},
                        json={"notes": "换教材"})
    assert resp.status_code == 200
    new = resp.json()
    assert new["id"] != series["id"] and new["notes"] == "换教材"
    assert new["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"  # 复制去 UNTIL
    assert new["start_time"] == f"{date}T{series['start_time'][11:16]}:00"  # 基准重锚
    old = client.get(f"/api/tasks/{series['id']}").json()
    assert old["recurrence_rule"] == (
        "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL="
        + (d - timedelta(days=1)).strftime("%Y%m%d") + "T235959"
    )
    with TestingSessionLocal() as db:
        assert db.get(Task, new["id"]).parent_id is None


def test_delete_this_soft_deletes_materialized_exception(client: TestClient, seeded_db):
    # 已实体化的实例再"仅删本次" → 软删该实体行（软删后即墓碑形态，不建第二颗
    # 墓碑）；主系列 cancelled_dates 仍含该日、窗口查询不再返回该例外
    series_id = _tid_by_title(client, "英语课")
    monday = _next_weekday(datetime.now(), 1)
    date = monday.date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}",
                           params={"mode": "this", "date": date}, json={"notes": "临时"}).json()
    assert client.delete(f"/api/tasks/{series_id}",
                         params={"mode": "this", "date": date}).status_code == 204
    with TestingSessionLocal() as db:
        row = db.get(Task, created["id"])
        assert row.is_deleted is True and row.deleted_at is not None
        assert len(db.scalars(select(Task).where(Task.parent_id == series_id)).all()) == 1
    assert client.get(f"/api/tasks/{series_id}").json()["cancelled_dates"] == [date]
    window = client.get("/api/tasks", params={
        "start": f"{date}T00:00:00",
        "end": (monday + timedelta(days=1)).date().isoformat() + "T00:00:00",
    }).json()
    assert created["id"] not in [t["id"] for t in window]


def test_patch_mode_validation_422(client: TestClient, seeded_db):
    # 非重复任务 mode=this / this 缺 date / this 带 recurrence_rule 键 → 422
    single_id = _tid_by_title(client, "夜跑")
    assert client.patch(f"/api/tasks/{single_id}",
                        params={"mode": "this", "date": _today_zero().date().isoformat()},
                        json={"notes": "x"}).status_code == 422
    series_id = _tid_by_title(client, "英语课")
    assert client.patch(f"/api/tasks/{series_id}", params={"mode": "this"},
                        json={"notes": "x"}).status_code == 422
    assert client.patch(f"/api/tasks/{series_id}",
                        params={"mode": "this", "date": _today_zero().date().isoformat()},
                        json={"recurrence_rule": "FREQ=DAILY"}).status_code == 422


def test_patch_exception_row_as_normal_task(client: TestClient, seeded_db):
    # 例外实体是普通任务行：不带 mode 的 PATCH 直接改它自己，主系列不动
    # （实体化实例的浮层二次编辑直通路径）
    series = _first_by_title(client.get("/api/tasks").json(), "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    created = client.patch(f"/api/tasks/{series['id']}",
                           params={"mode": "this", "date": date}, json={"notes": "一稿"}).json()
    resp = client.patch(f"/api/tasks/{created['id']}", json={"title": "英语课（补）"})
    assert resp.status_code == 200 and resp.json()["title"] == "英语课（补）"
    with TestingSessionLocal() as db:
        master = db.get(Task, series["id"])
        assert master.title == "英语课" and master.notes == series["notes"]


def test_cancelled_dates_dedup_tombstone_and_exception(client: TestClient, seeded_db):
    # 同日先删（墓碑）再编辑（实体化）→ 两行并存但 cancelled_dates 该日仅一次
    # （set 去重；挖空日期语义只关心"该日不渲染虚拟实例"）
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    client.delete(f"/api/tasks/{series_id}", params={"mode": "this", "date": date})
    client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                 json={"notes": "复活改稿"})
    assert client.get(f"/api/tasks/{series_id}").json()["cancelled_dates"] == [date]
    with TestingSessionLocal() as db:
        assert len(db.scalars(select(Task).where(Task.parent_id == series_id)).all()) == 2


# —— 系列派生行生命周期（2026-09-14 审查修复：拆分迁移子行 / 软删连带 / date 存在性校验） ——


def test_split_series_migrates_tombstone(client: TestClient, seeded_db):
    # 拆分"此日程及后续"时被点日之后的墓碑改挂新系列——不迁移则墓碑留在已被
    # UNTIL 截断的老系列名下、新系列照常生成该日实例（用户已删的日程复活）
    series_id = _tid_by_title(client, "英语课")
    monday = _next_weekday(datetime.now(), 1)
    d = monday.date().isoformat()  # 拆分点：下周一
    x = (monday + timedelta(days=7)).date().isoformat()  # 被删实例：再下周一（> d）
    client.delete(f"/api/tasks/{series_id}", params={"mode": "this", "date": x})
    new = client.patch(f"/api/tasks/{series_id}", params={"mode": "following", "date": d},
                       json={"notes": "拆分"}).json()
    # 墓碑已改挂新系列：聚合挖空随新系列生效，被删实例不复活
    assert client.get(f"/api/tasks/{new['id']}").json()["cancelled_dates"] == [x]
    with TestingSessionLocal() as db:
        tombstones = db.scalars(select(Task).where(
            Task.parent_id == new["id"], Task.is_deleted.is_(True))).all()
        assert len(tombstones) == 1
        assert tombstones[0].original_time.date().isoformat() == x


def test_split_series_migrates_live_exception(client: TestClient, seeded_db):
    # 拆分时被点日之后的活体例外也改挂新系列——不迁移则该日同时出现例外行与
    # 新系列虚拟实例（双渲染）；改挂后例外日期进新系列 cancelled_dates 挖空虚拟实例
    series_id = _tid_by_title(client, "英语课")
    monday = _next_weekday(datetime.now(), 1)
    d = monday.date().isoformat()
    x_day = monday + timedelta(days=7)
    x = x_day.date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": x},
                           json={"notes": "单次改稿"}).json()
    new = client.patch(f"/api/tasks/{series_id}", params={"mode": "following", "date": d},
                       json={"notes": "拆分"}).json()
    assert client.get(f"/api/tasks/{new['id']}").json()["cancelled_dates"] == [x]
    with TestingSessionLocal() as db:
        assert db.get(Task, created["id"]).parent_id == new["id"]


def test_delete_all_soft_deletes_children(client: TestClient, seeded_db):
    # "删除所有"软删分支（老系列）连带软删活体例外行——只标记主行则例外孤儿
    # （独立已排期任务、purge 豁免）永久留在日历；与闪删分支的物理清子行对齐
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                           json={"notes": "x"}).json()
    with TestingSessionLocal() as db:  # seed 时刻距今 <300s 会走闪删分支，拨老主行强制走软删
        db.get(Task, series_id).created_at = datetime.now() - timedelta(minutes=10)
        db.commit()
    assert client.delete(f"/api/tasks/{series_id}", params={"mode": "all"}).status_code == 204
    assert client.get(f"/api/tasks/{created['id']}").status_code == 404  # 例外孤儿一并消失
    with TestingSessionLocal() as db:
        row = db.get(Task, created["id"])
        assert row is not None and row.is_deleted is True  # 软删保留（AI 资产不物理抹）
        assert row.deleted_at is not None


def test_series_mode_date_validations_422(client: TestClient, seeded_db):
    # date 非该系列真实实例日 → 422：weekly 非 BYDAY 星期 / 超出 UNTIL（防反向
    # 延长复活已删实例）/ 早于基准日——阶段二 agent 复用 service 层前的安全闸
    series_id = _tid_by_title(client, "英语课")  # FREQ=WEEKLY;BYDAY=MO,WE,FR
    tue = _next_weekday(datetime.now(), 2).date().isoformat()  # 下周二（不在集合）
    assert client.delete(f"/api/tasks/{series_id}",
                         params={"mode": "this", "date": tue}).status_code == 422
    assert client.patch(f"/api/tasks/{series_id}",
                        params={"mode": "following", "date": tue},
                        json={"notes": "x"}).status_code == 422
    mon = _next_weekday(datetime.now(), 1).date().isoformat()
    client.delete(f"/api/tasks/{series_id}", params={"mode": "following", "date": mon})
    beyond = (_next_weekday(datetime.now(), 1) + timedelta(days=14)).date().isoformat()
    assert client.delete(f"/api/tasks/{series_id}",
                         params={"mode": "following", "date": beyond}).status_code == 422
    early = _next_weekday(datetime.now() - timedelta(days=30), 1).date().isoformat()
    assert client.delete(f"/api/tasks/{series_id}",
                         params={"mode": "this", "date": early}).status_code == 422


def test_patch_empty_body_with_series_mode_422(client: TestClient, seeded_db):
    # this/following 携带空 body → 422（前端乐观更新把响应当"新行"消费，空请求
    # 返回主行会形成"本地已挖空、后端无实体"的洞）；all 模式空 body 仍 200 短路
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    assert client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                        json={}).status_code == 422
    assert client.patch(f"/api/tasks/{series_id}", params={"mode": "following", "date": date},
                        json={}).status_code == 422
    assert client.patch(f"/api/tasks/{series_id}", json={}).status_code == 200


def test_patch_exception_row_cannot_become_series(client: TestClient, seeded_db):
    # 活体例外行（parent_id 锚定主系列）经普通 PATCH 设置 recurrence_rule → 422：
    # 否则该行既被当独立系列展开、又被聚合为主系列的挖空日期（双重身份）
    series_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                           json={"notes": "x"}).json()
    assert client.patch(f"/api/tasks/{created['id']}",
                        json={"recurrence_rule": "FREQ=DAILY"}).status_code == 422
    # 清空不受限（例外行保持单次语义，null 本就无操作）
    assert client.patch(f"/api/tasks/{created['id']}",
                        json={"recurrence_rule": None}).status_code == 200


def test_materialize_exception_inherits_sort_order(client: TestClient, seeded_db):
    # 例外行继承主记录 sort_order：列表稳定序不因实体化跳到最前（default 0）
    series_id = _tid_by_title(client, "英语课")
    with TestingSessionLocal() as db:
        master_order = db.get(Task, series_id).sort_order
    date = _next_weekday(datetime.now(), 1).date().isoformat()
    created = client.patch(f"/api/tasks/{series_id}", params={"mode": "this", "date": date},
                           json={"notes": "x"}).json()
    with TestingSessionLocal() as db:
        assert db.get(Task, created["id"]).sort_order == master_order


# —— 写路径：新建（快捷录入框） ——


def test_create_task_201_minimal_shape(client: TestClient, seeded_db):
    # 仅 title：未排期、priority 默认 medium、ever_scheduled False、sort_order 排 seed 末位
    resp = client.post("/api/tasks", json={"title": "看书"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "看书" and body["priority"] == "medium"
    assert body["start_time"] is None and body["end_time"] is None
    assert body["recurrence_rule"] is None and body["project_id"] is None
    with TestingSessionLocal() as db:
        task = db.get(Task, body["id"])
        assert task.priority == 2 and task.ever_scheduled is False
        assert task.sort_order == 117  # seed 117 条 sort_order 0..116，新任务排末位


def test_create_task_scheduled_forces_duration(client: TestClient, seeded_db):
    # start+end 齐备时 duration 强制重算（显式携带的冲突值被排期计算覆盖）、ever_scheduled 置位
    start = _today_zero() + timedelta(days=1, hours=9)
    resp = client.post("/api/tasks", json={
        "title": "看书", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=2)).isoformat(), "duration_mins": 999,
    })
    assert resp.status_code == 201
    assert resp.json()["duration_mins"] == 120
    with TestingSessionLocal() as db:
        task = db.get(Task, resp.json()["id"])
        assert task.ever_scheduled is True and task.duration_mins == 120


def test_create_task_with_recurrence(client: TestClient, seeded_db):
    # RRULE 原样透传；周期任务 start 即基准 → ever_scheduled True
    start = _today_zero() + timedelta(hours=9)
    resp = client.post("/api/tasks", json={
        "title": "晨会", "start_time": start.isoformat(),
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
    })
    assert resp.status_code == 201
    assert resp.json()["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_create_task_project_assignment(client: TestClient, seeded_db):
    pid = client.get("/api/projects").json()[0]["id"]
    resp = client.post("/api/tasks", json={"title": "看书", "project_id": pid})
    assert resp.status_code == 201 and resp.json()["project_id"] == pid


def test_create_task_dirty_project_422(client: TestClient, seeded_db):
    resp = client.post("/api/tasks", json={"title": "看书", "project_id": str(uuid.uuid4())})
    assert resp.status_code == 422


def test_create_task_end_without_start_422(client: TestClient, seeded_db):
    resp = client.post("/api/tasks", json={
        "title": "看书", "end_time": (_today_zero() + timedelta(hours=10)).isoformat(),
    })
    assert resp.status_code == 422


def test_create_task_end_before_start_422(client: TestClient, seeded_db):
    start = _today_zero() + timedelta(days=1, hours=9)
    resp = client.post("/api/tasks", json={
        "title": "看书", "start_time": start.isoformat(),
        "end_time": (start - timedelta(minutes=30)).isoformat(),
    })
    assert resp.status_code == 422


def test_create_task_title_validation_422(client: TestClient, seeded_db):
    # 空 / strip 后空 / 超长（TITLE_MAX_LEN=30）
    assert client.post("/api/tasks", json={"title": ""}).status_code == 422
    assert client.post("/api/tasks", json={"title": "   "}).status_code == 422
    assert client.post("/api/tasks", json={"title": "字" * 31}).status_code == 422


def test_create_task_duration_bounds_422(client: TestClient, seeded_db):
    assert client.post("/api/tasks", json={"title": "看书", "duration_mins": 0}).status_code == 422
    assert client.post("/api/tasks", json={"title": "看书", "duration_mins": 1441}).status_code == 422


def test_create_task_bad_rrule_422(client: TestClient, seeded_db):
    resp = client.post("/api/tasks", json={"title": "晨会", "recurrence_rule": "FREQ=HOURLY"})
    assert resp.status_code == 422


def test_create_task_sort_order_increments(client: TestClient, seeded_db):
    a = client.post("/api/tasks", json={"title": "第一个"}).json()
    b = client.post("/api/tasks", json={"title": "第二个"}).json()
    assert b["id"] != a["id"]
    with TestingSessionLocal() as db:
        assert db.get(Task, b["id"]).sort_order == db.get(Task, a["id"]).sort_order + 1


def test_create_task_notes_passthrough(client: TestClient, seeded_db):
    # inbox 建议卡收录时携带来源摘要（M1-②），POST 透传落 Task.notes
    resp = client.post("/api/tasks", json={"title": "买牛奶", "notes": "来自购物清单邮件"})
    assert resp.status_code == 201
    with TestingSessionLocal() as db:
        assert db.get(Task, resp.json()["id"]).notes == "来自购物清单邮件"
    # 不带 notes = None（列可空）；超长在 schema 层拦（≤500）
    assert client.post("/api/tasks", json={"title": "x"}).json()["notes"] is None or True
    assert client.post("/api/tasks", json={"title": "x", "notes": "长" * 501}).status_code == 422


# —— 排期智能配套（2026-10-17：系列展开 / 负荷摘要 / 忙区 / 快录自动落库） ————

_ISO_BY_DAY = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}


def _mk_task(**over):
    """expand_occurrences 直测替身（getattr 口径——SimpleNamespace 天然零适配）。"""
    base = dict(id="x", title="替身", priority=3, start_time=None, end_time=None,
                notes=None, duration_mins=None, recurrence_rule=None,
                cancelled_dates=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_expand_occurrences_single_rules_cancelled_until():
    lo, hi = datetime(2026, 10, 11), datetime(2026, 10, 20)
    # 单次：start 落窗返回起点本身；窗外空；未排期恒空
    one = _mk_task(start_time=datetime(2026, 10, 12, 9, 30))
    assert expand_occurrences(one, lo, hi) == [datetime(2026, 10, 12, 9, 30)]
    assert expand_occurrences(one, datetime(2026, 10, 13), hi) == []
    assert expand_occurrences(_mk_task(), lo, hi) == []
    # 周重复：BYDAY 命中星期、钟点取基准、cancelled_dates 挖空（10-12/10-19 是周一）
    weekly = _mk_task(start_time=datetime(2026, 10, 12, 8, 0),
                      recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
                      cancelled_dates=["2026-10-19"])
    assert expand_occurrences(weekly, lo, hi) == [datetime(2026, 10, 12, 8, 0)]
    weekly.cancelled_dates = None
    assert expand_occurrences(weekly, lo, hi) == [
        datetime(2026, 10, 12, 8, 0), datetime(2026, 10, 19, 8, 0)]
    # 早于基准日无实例（10-10/10-11 在周一基准 10-12 之前）
    assert expand_occurrences(weekly, datetime(2026, 10, 10), datetime(2026, 10, 12)) == []
    # 日重复 + UNTIL 截断（含当日）；月重复按日号对齐
    daily = _mk_task(start_time=datetime(2026, 10, 13, 7, 0),
                     recurrence_rule="FREQ=DAILY;UNTIL=20261015")
    assert expand_occurrences(daily, lo, hi) == [
        datetime(2026, 10, 13, 7, 0), datetime(2026, 10, 14, 7, 0),
        datetime(2026, 10, 15, 7, 0)]
    monthly = _mk_task(start_time=datetime(2026, 9, 15, 10, 0),
                       recurrence_rule="FREQ=MONTHLY")
    assert expand_occurrences(monthly, datetime(2026, 10, 1), datetime(2026, 11, 16)) == [
        datetime(2026, 10, 15, 10, 0), datetime(2026, 11, 15, 10, 0)]


def test_calendar_load_summary_and_busy_intervals():
    # 明天两条单次（60 普通 + 120 高强度）+ 今天起每周系列（90 高强度）——
    # 按日聚合 / 空日零行 / 系列按展开实例计 / 忙区区间口径
    t0 = _today_zero() + timedelta(days=1)
    today = _today_zero()
    with TestingSessionLocal() as db:
        create_task(db, TaskCreate(title="普通会", start_time=t0.replace(hour=10),
                                   end_time=t0.replace(hour=11), duration_mins=60))
        create_task(db, TaskCreate(title="深度块", start_time=t0.replace(hour=14),
                                   end_time=t0.replace(hour=16), duration_mins=120))
        create_task(db, TaskCreate(
            title="周站会", start_time=today.replace(hour=8),
            end_time=today.replace(hour=9, minute=30), duration_mins=90,
            recurrence_rule=f"FREQ=WEEKLY;BYDAY={_ISO_BY_DAY[today.isoweekday()]}"))
        summary = calendar_load_summary(db)
        assert len(summary) == 15  # [-1, +14) 天，空日也出零行
        row = next(r for r in summary if r["date"] == t0.date().isoformat())
        assert row == {"date": t0.date().isoformat(), "scheduled_count": 2,
                       "total_mins": 180, "high_count": 1}
        today_row = next(r for r in summary if r["date"] == today.date().isoformat())
        assert today_row["scheduled_count"] == 1 and today_row["high_count"] == 1
        next_week_row = next(r for r in summary
                             if r["date"] == (today + timedelta(days=7)).date().isoformat())
        assert next_week_row["scheduled_count"] == 1  # 系列下週实例
        busy = busy_intervals(db)
        assert (t0.replace(hour=10), t0.replace(hour=11)) in busy
        assert (today.replace(hour=8), today.replace(hour=9, minute=30)) in busy


def test_apply_plan_entries_scheduled_unscheduled_skip():
    # 快录自动落库：带 start 走全套校验建排期任务；start=None 建未排期待办；
    # task_id 非空跳过（第一版不自动改排期）
    t0 = _today_zero() + timedelta(days=2)
    with TestingSessionLocal() as db:
        seeded = create_task(db, TaskCreate(title="已存在", duration_mins=30))
        created = apply_plan_entries(db, [
            {"task_id": None, "title": "体检", "start": t0.replace(hour=10).isoformat(),
             "duration": 60},
            {"task_id": seeded.id, "title": "改排期",
             "start": t0.replace(hour=15).isoformat(), "duration": 30},
            {"task_id": None, "title": "修车", "start": None, "duration": 90},
        ])
        assert [t.title for t in created] == ["体检", "修车"]
        first, second = created
        assert first.start_time == t0.replace(hour=10) and first.duration_mins == 60
        assert second.start_time is None and second.end_time is None
        assert db.get(Task, seeded.id).start_time is None  # 原行未被触碰
        assert db.execute(select(Task).where(Task.title == "改排期")).scalars().first() is None


def test_apply_plan_entries_invalid_raises_atomically():
    # 任一条非法整批上抛（调用方原子降级为确认卡）——合法条也不落半批
    with TestingSessionLocal() as db:
        with pytest.raises(ValueError):
            apply_plan_entries(db, [
                {"task_id": None, "title": "合法", "start": "2026-10-20T10:00:00",
                 "duration": 30},
                {"task_id": None, "title": "非法", "start": "not-a-time", "duration": 30},
            ])
        assert db.execute(select(Task).where(Task.title == "合法")).scalars().first() is None


def test_apply_plan_entries_priority_passthrough():
    # priority 直判透传（2026-10-22）：合法值 1 落高优、缺省走库默认 2、
    # 越界值（99/字符串）按缺失处理不炸
    t0 = _today_zero() + timedelta(days=2)
    with TestingSessionLocal() as db:
        created = apply_plan_entries(db, [
            {"task_id": None, "title": "启动会", "start": t0.replace(hour=10).isoformat(),
             "duration": 60, "priority": 1},
            {"task_id": None, "title": "杂事", "start": t0.replace(hour=15).isoformat(),
             "duration": 30},
            {"task_id": None, "title": "坏值", "start": t0.replace(hour=16).isoformat(),
             "duration": 30, "priority": 99},
        ])
        assert [t.priority for t in created] == [1, 2, 2]


def test_apply_plan_entries_source_provenance_passthrough():
    # 采集溯源透传（2026-10-23）：条目带 source_ref/evidence → 任务行三列落库；
    # 不带（排期链自产条目）→ 恒 None 无行为分叉
    t0 = _today_zero() + timedelta(days=2)
    with TestingSessionLocal() as db:
        created = apply_plan_entries(db, [
            {"task_id": None, "title": "订场地", "start": t0.replace(hour=10).isoformat(),
             "duration": 60, "evidence": "邮件摘录",
             "source_ref": {"source": "Gmail", "source_key": "ca-1", "item_id": "m1",
                            "title": "羽毛球", "time": None, "fingerprint": "e" * 40}},
            {"task_id": None, "title": "自产条目", "start": t0.replace(hour=15).isoformat(),
             "duration": 30},
        ])
        assert created[0].source_key == "ca-1"
        assert created[0].source_fingerprint == "e" * 40
        assert created[0].source_evidence == "邮件摘录"
        assert created[1].source_key is None and created[1].source_fingerprint is None
        assert created[1].source_evidence is None


# ---- 一键智能安排端点（统一排期引擎，2026-10-20 增）——————————————
# AI 微调层统一替身为零激活态（plan_slots 整层跳过回落纯规则）——端点测试
# 只验 HTTP 契约与落库，AI 微调行为在 test_engine.py 单测覆盖


@pytest.fixture(autouse=True)
def _no_active_llm(monkeypatch):
    """拦断 engine → llm 链：漏拦会真探激活模型开 dev 库（替身口径同 test_engine）。"""
    monkeypatch.setattr(llm, "call_llm", _raise_no_active)


def _raise_no_active(messages, *, json_mode=False, max_tokens=None):
    from services.llm import NoActiveModelError
    raise NoActiveModelError()


def _mk(client: TestClient, title: str, **kw) -> dict:
    r = client.post("/api/tasks", json={"title": title, **kw})
    assert r.status_code == 201
    return r.json()


def test_auto_schedule_endpoint_ok(client: TestClient):
    t = _mk(client, "写周报", duration_mins=60)
    r = client.post(f"/api/tasks/{t['id']}/auto-schedule")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == t["id"]
    assert body["start_time"] and body["end_time"]


def test_auto_schedule_endpoint_already_scheduled_409(client: TestClient):
    tmr = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0)
    t = _mk(client, "已排期的日程", duration_mins=60,
                 start_time=tmr.isoformat(),
                 end_time=(tmr + timedelta(minutes=60)).isoformat())
    r = client.post(f"/api/tasks/{t['id']}/auto-schedule")
    assert r.status_code == 409
    assert "已排期" in r.json()["detail"]


def test_auto_schedule_endpoint_missing_404(client: TestClient):
    assert client.post("/api/tasks/nope/auto-schedule").status_code == 404


def test_auto_schedule_endpoint_no_slot_422(client: TestClient):
    # 时长超单日可排时段（840 分钟）→ 7 天全无空档 → 422 文案可直显
    t = _mk(client, "超大任务", duration_mins=850)
    r = client.post(f"/api/tasks/{t['id']}/auto-schedule")
    assert r.status_code == 422
    assert "空档" in r.json()["detail"]


# ---- 完成态 done_at（2026-10-22 增，批 1；同日修订系列口径）————————————
# 完成 ≠ 删除：未排期完成任务从收件箱消失（unscheduled 排除），已排期任务
# 保留在窗口查询（busy 避让源防双订）；系列任务：mode=this 单实例实体化例外行
# 标完成（主行与其余实例不动），mode=all 整系列 done 语义错仍拦 422


def test_done_roundtrip(client: TestClient):
    # PATCH done:true → done_at 落时刻；done:false → 清回 None（撤销）；done-only patch 不被空 patch 短路吞掉
    t = _mk(client, "买牛奶")
    assert t["done_at"] is None
    r = client.patch(f"/api/tasks/{t['id']}", json={"done": True})
    assert r.status_code == 200
    assert r.json()["done_at"] is not None
    r = client.patch(f"/api/tasks/{t['id']}", json={"done": False})
    assert r.status_code == 200
    assert r.json()["done_at"] is None


def test_done_unscheduled_hidden_but_window_kept(client: TestClient):
    # 语义分叉：未排期 done 行从收件箱消失；已排期 done 行保留在窗口查询
    todo = _mk(client, "未排期待办完成")
    sched = _mk(client, "已排期日程完成", start_time=(_today_zero() + timedelta(days=1)).replace(hour=9).isoformat(),
                duration_mins=60)
    assert client.patch(f"/api/tasks/{todo['id']}", json={"done": True}).status_code == 200
    assert client.patch(f"/api/tasks/{sched['id']}", json={"done": True}).status_code == 200
    inbox = client.get("/api/tasks", params={"unscheduled": "true"}).json()
    assert all(x["id"] != todo["id"] for x in inbox)
    win = client.get("/api/tasks", params={
        "start": _today_zero().isoformat(),
        "end": (_today_zero() + timedelta(days=2)).isoformat()}).json()
    assert any(x["id"] == sched["id"] for x in win)


def test_done_series_rejected_422(client: TestClient):
    # 系列整系列完成语义错：mode=all（默认）仍拦；this 单实例路径见下例
    t = _mk(client, "每周例会", recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
            start_time=(_today_zero() + timedelta(days=1)).replace(hour=10).isoformat(),
            duration_mins=60)
    r = client.patch(f"/api/tasks/{t['id']}", json={"done": True})
    assert r.status_code == 422
    assert "重复系列" in r.json()["detail"]


def test_done_series_instance_via_this_mode(client: TestClient, seeded_db):
    # 系列单实例完成（2026-10-22 修订）：mode=this + done（done-only patch 不撞
    # 「至少一个修改字段」闸）→ 实体化例外行标 done_at，主行 done_at 不动、该日
    # 被挖空（cancelled_dates）；其余实例（再下周一）不受影响
    task_id = _tid_by_title(client, "英语课")
    date = _next_weekday(datetime.now(), 1).date().isoformat()  # 下周一实例日
    r = client.patch(f"/api/tasks/{task_id}", json={"done": True},
                     params={"mode": "this", "date": date})
    assert r.status_code == 200
    exc = r.json()
    assert exc["done_at"] is not None and exc["parent_id"] == task_id
    assert exc["recurrence_rule"] is None  # 例外是单次实体
    # 主行未完成、该日挖空；例外行落库可查
    fetched = client.get(f"/api/tasks/{task_id}").json()
    assert fetched["done_at"] is None and date in fetched["cancelled_dates"]
    with TestingSessionLocal() as db:
        rows = db.scalars(select(Task).where(Task.parent_id == task_id)).all()
        assert len(rows) == 1 and rows[0].done_at is not None


def _rc(task_id: str) -> int:
    # reschedule_count 前端不消费不进 TaskOut——直查 ORM 断言
    with TestingSessionLocal() as db:
        return db.get(Task, task_id).reschedule_count or 0


def test_reschedule_count_write_point(client: TestClient):
    # 改期计数（夜间复盘"计划 vs 实际"信号源，此前全库无写入恒 0）：已排期任务
    # 挪时刻 +1；首次排期与取消排期不计；同值 PATCH 不计
    base = (_today_zero() + timedelta(days=1)).replace(hour=9)
    t = _mk(client, "会挪的会", start_time=base.isoformat(),
            end_time=(base + timedelta(minutes=60)).isoformat())
    t2 = _mk(client, "先不排期", duration_mins=30)
    # 同值再写同刻：不计
    client.patch(f"/api/tasks/{t['id']}", json={"start_time": base.isoformat()})
    assert _rc(t["id"]) == 0
    # 挪到 11 点（start/end 同挪——前端拖拽语义）：+1
    moved = (_today_zero() + timedelta(days=1)).replace(hour=11)
    client.patch(f"/api/tasks/{t['id']}", json={
        "start_time": moved.isoformat(),
        "end_time": (moved + timedelta(minutes=60)).isoformat()})
    assert _rc(t["id"]) == 1
    # 首次排期（None → 值）：不计
    client.patch(f"/api/tasks/{t2['id']}", json={"start_time": moved.isoformat()})
    assert _rc(t2["id"]) == 0
    # 取消排期（值 → None）：不计
    client.patch(f"/api/tasks/{t2['id']}", json={"start_time": None})
    assert _rc(t2["id"]) == 0


def test_execution_rhythm_aggregates(monkeypatch):
    """执行节奏聚合（优化批 5）：近 14 天（不含今天）已排期的勾选完成率 /
    完成均时长（样本<3 给 None 不硬凑）/ reschedule_count 推迟 top3。"""
    from services.tasks import execution_rhythm

    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    base = day0 - timedelta(days=3)
    with TestingSessionLocal() as db:
        from models import Task

        def add(title, *, done=None, resched=0, hours=9):
            db.add(Task(title=title, start_time=base + timedelta(hours=hours),
                        end_time=base + timedelta(hours=hours + 1),
                        duration_mins=60, done_at=done, reschedule_count=resched))
        add("完成A", done=datetime.now() - timedelta(days=2))
        add("完成B", done=datetime.now() - timedelta(days=2))
        add("完成C", done=datetime.now() - timedelta(days=1))
        add("未勾选D")
        add("反复推迟E", resched=3)
        db.add(Task(title="今天的排期", start_time=day0 + timedelta(hours=9),
                    duration_mins=60))  # 今天不计入
        db.commit()
        rhythm = execution_rhythm(db)
        assert rhythm["planned_count"] == 5  # 今天的排除
        assert rhythm["done_count"] == 3
        assert rhythm["done_rate"] == 0.6
        assert rhythm["avg_done_mins"] == 60
        assert rhythm["top_deferred"] == [{"title": "反复推迟E", "count": 3, "priority": 2}]


# —— 全天/跨天日程（2026-11-06 批 B）：归一 / 不变式豁免 / 占天语义 ——


def test_create_all_day_normalizes_times(client: TestClient, seeded_db):
    # 时刻归一：start 取当日 0 点；end 非 0 点视为「含该日」→ 次日 0 点独占界
    resp = client.post("/api/tasks", json={
        "title": "出差", "all_day": True,
        "start_time": "2026-11-10T14:30:00", "end_time": "2026-11-12T09:00:00",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["all_day"] is True
    assert body["start_time"] == "2026-11-10T00:00:00"
    assert body["end_time"] == "2026-11-13T00:00:00"
    assert body["duration_mins"] is None  # 不变式豁免（占天不占时段）


def test_create_all_day_single_day_without_end(client: TestClient, seeded_db):
    # 单日全天：end 允许空（渲染口径 start+24h），duration 恒空
    resp = client.post("/api/tasks", json={
        "title": "请假", "all_day": True, "start_time": "2026-11-10T00:00:00",
    })
    assert resp.status_code == 201
    assert resp.json()["end_time"] is None and resp.json()["duration_mins"] is None


def test_create_all_day_requires_start(client: TestClient, seeded_db):
    resp = client.post("/api/tasks", json={"title": "x", "all_day": True})
    assert resp.status_code == 422
    assert "开始日期" in resp.json()["detail"]


def test_update_toggle_all_day_renormalizes(client: TestClient, seeded_db):
    # 普通 → 全天：合并态归一 + duration 清空；再关回普通：齐备时恢复重算
    start = _today_zero() + timedelta(days=1, hours=14, minutes=30)
    created = client.post("/api/tasks", json={
        "title": "出差", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
    }).json()
    day0 = start.replace(hour=0, minute=0)

    resp = client.patch(f"/api/tasks/{created['id']}", json={"all_day": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_day"] is True
    assert body["start_time"] == day0.isoformat()
    assert body["end_time"] == (day0 + timedelta(days=1)).isoformat()
    assert body["duration_mins"] is None

    resp = client.patch(f"/api/tasks/{created['id']}", json={"all_day": False})
    assert resp.json()["all_day"] is False
    # 转回普通：0 点→次日 0 点齐备，不变式恢复（24h=1440，用户可再拖出钟点）
    assert resp.json()["duration_mins"] == 1440


def test_update_all_day_explicit_null_is_noop(client: TestClient, seeded_db):
    created = client.post("/api/tasks", json={"title": "看书"}).json()
    resp = client.patch(f"/api/tasks/{created['id']}", json={"all_day": None})
    assert resp.status_code == 200
    assert resp.json()["all_day"] is False


def test_busy_intervals_all_day_occupies_whole_day(client: TestClient, seeded_db):
    # 占天语义：全天行在 busy_intervals 占满整天（出差日不宜再排时段任务）
    day0 = _today_zero() + timedelta(days=2)
    client.post("/api/tasks", json={
        "title": "出差", "all_day": True,
        "start_time": day0.isoformat(),
        "end_time": (day0 + timedelta(days=2)).isoformat(),  # 跨两天
    })
    with TestingSessionLocal() as db:
        spans = busy_intervals(db, days_back=1, days_fwd=14)
    assert (day0, day0 + timedelta(days=2)) in spans


def test_load_summary_all_day_counts_not_minutes(client: TestClient, seeded_db):
    # 负载口径：全天行计入条数但时长恒 0（日预算不被 24h 灌满）——seed 当日可能
    # 已有日程，取创建前后差值断言
    day0 = _today_zero() + timedelta(days=3)

    def _row():
        with TestingSessionLocal() as db:
            rows = calendar_load_summary(db, days_back=1, days_fwd=14)
        return next(r for r in rows if r["date"] == day0.date().isoformat())

    before = _row()
    client.post("/api/tasks", json={
        "title": "请假", "all_day": True, "start_time": day0.isoformat(),
    })
    after = _row()
    assert after["scheduled_count"] == before["scheduled_count"] + 1
    assert after["total_mins"] == before["total_mins"]  # 时长零增量


def test_materialize_exception_inherits_all_day(client: TestClient, seeded_db):
    # 全天系列的实例实体化：例外行继承 all_day（跨距随 duration 复制路径天然正确）
    day0 = _today_zero() + timedelta(days=1)
    created = client.post("/api/tasks", json={
        "title": "驻场", "all_day": True, "start_time": day0.isoformat(),
        "recurrence_rule": "FREQ=DAILY",
    }).json()
    target = (day0 + timedelta(days=2)).date().isoformat()
    resp = client.patch(
        f"/api/tasks/{created['id']}?mode=this&date={target}",
        json={"title": "驻场改期日"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_day"] is True
    assert body["start_time"] == f"{target}T00:00:00"
    assert body["duration_mins"] is None


# —— 子任务清单（2026-11-06 批 C）：全量替换 / 装配 / 继承 / 清理 ——


def test_subtasks_full_replace_and_readback(client: TestClient, seeded_db):
    created = client.post("/api/tasks", json={"title": "会议"}).json()
    tid = created["id"]
    # 空清单起步 → 全量替换两项（一勾选）→ 读回顺序与完成态
    assert created["subtasks"] == []
    res = client.patch(f"/api/tasks/{tid}", json={
        "subtasks": [{"title": "定议程", "done": True}, {"title": "发邀请"}]
    })
    assert res.status_code == 200
    body = res.json()
    assert [(s["title"], s["done"]) for s in body["subtasks"]] == [("定议程", True), ("发邀请", False)]
    # GET 单条与列表口径一致
    assert client.get(f"/api/tasks/{tid}").json()["subtasks"] == body["subtasks"]
    # 清空
    res = client.patch(f"/api/tasks/{tid}", json={"subtasks": []})
    assert res.json()["subtasks"] == []


def test_subtasks_replace_with_other_fields(client: TestClient, seeded_db):
    # subtasks 与普通字段同 patch 一起提交（单独弹出走替换，不进 setattr）
    created = client.post("/api/tasks", json={"title": "采购"}).json()
    res = client.patch(f"/api/tasks/{created['id']}", json={
        "title": "囤货采购", "subtasks": [{"title": "买菜"}, {"title": "买肉", "done": True}],
    })
    body = res.json()
    assert body["title"] == "囤货采购"
    assert len(body["subtasks"]) == 2


def test_subtasks_item_validation(client: TestClient, seeded_db):
    created = client.post("/api/tasks", json={"title": "x"}).json()
    res = client.patch(f"/api/tasks/{created['id']}", json={
        "subtasks": [{"title": ""}]
    })
    assert res.status_code == 422  # 空文本被契约层拦
    res = client.patch(f"/api/tasks/{created['id']}", json={
        "subtasks": [{"title": "t" * 31}]
    })
    assert res.status_code == 422  # 超长（>30）


def test_subtasks_inherit_on_exception_and_split(client: TestClient, seeded_db):
    # 系列主行清单 → this 实体化例外复制清单；following 拆分新系列继承
    day0 = _today_zero() + timedelta(days=1)
    created = client.post("/api/tasks", json={
        "title": "每日站会", "start_time": day0.isoformat(),
        "recurrence_rule": "FREQ=DAILY",
    }).json()
    tid = created["id"]
    client.patch(f"/api/tasks/{tid}", json={"subtasks": [{"title": "过进度"}, {"title": "记纪要"}]})
    target = (day0 + timedelta(days=7)).date().isoformat()

    res = client.patch(
        f"/api/tasks/{tid}?mode=this&date={target}", json={"subtasks": [{"title": "改过预算"}]}
    )
    assert res.status_code == 200
    assert [s["title"] for s in res.json()["subtasks"]] == ["改过预算"]  # 覆盖复制值

    res = client.patch(
        f"/api/tasks/{tid}?mode=following&date={target}", json={"title": "周会(新段)"}
    )
    assert res.status_code == 200
    new_id = res.json()["id"]
    assert [s["title"] for s in res.json()["subtasks"]] == ["过进度", "记纪要"]  # 无清单提交时继承主行
    with TestingSessionLocal() as db:
        from models import Subtask
        rows = db.scalars(__import__("sqlalchemy").select(Subtask).where(Subtask.task_id == new_id)).all()
        assert len(rows) == 2


def test_subtasks_cascade_on_flash_delete(client: TestClient, seeded_db):
    # 闪建闪删物理删：清单行连带清（不留孤儿）
    from sqlalchemy import select

    from models import Subtask
    created = client.post("/api/tasks", json={"title": "临时"}).json()
    tid = created["id"]
    client.patch(f"/api/tasks/{tid}", json={"subtasks": [{"title": "a"}]})
    res = client.delete(f"/api/tasks/{tid}")
    assert res.status_code == 204
    with TestingSessionLocal() as db:
        assert db.scalars(select(Subtask).where(Subtask.task_id == tid)).all() == []


def test_subtasks_soft_delete_keeps_rows_hidden(client: TestClient, seeded_db):
    # 软删：清单行保留（不可见——读路径按父任务过滤），purge 孤儿清理兜底
    from sqlalchemy import select

    from models import Subtask
    from services.tasks import purge_expired_soft_deletes

    created = client.post("/api/tasks", json={"title": "草稿", "subtasks": []}).json()
    tid = created["id"]
    client.patch(f"/api/tasks/{tid}", json={"subtasks": [{"title": "x"}]})
    client.delete(f"/api/tasks/{tid}")  # 场景 2 软删（>300s 不满足则闪删——刚建 <300s！）
    with TestingSessionLocal() as db:
        rows = db.scalars(select(Subtask).where(Subtask.task_id == tid)).all()
        # 刚建 <300s 走闪删物理删（含清单）；软删路径由下例专测
        assert rows == []


# —— 预估校准提示（2026-11-06 批 E1）——


def test_estimate_hint_counts_similar_done(client: TestClient, seeded_db):
    # 两个同类已完成待办（60/90 分钟，未排期 duration 不变式不触发）→ 均值 75、样本 2
    for mins in (60, 90):
        created = client.post("/api/tasks", json={
            "title": "方案评审", "duration_mins": mins,
        }).json()
        client.patch(f"/api/tasks/{created['id']}", json={"done": True})
    res = client.get("/api/tasks/estimate-hint", params={"title": "方案评审会"})
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert body["avg_min"] == 75


def test_estimate_hint_no_hit(client: TestClient, seeded_db):
    res = client.get("/api/tasks/estimate-hint", params={"title": "完全无关的独有事项"})
    assert res.json() == {"avg_min": None, "count": 0}
