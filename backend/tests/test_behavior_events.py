"""记忆重构批 5 测试（2026-11-03）：行为遥测——update_task 单点四事件 + delete
abandon + prune。前端零改动（拖拽/Toast/编辑全汇 PATCH /api/tasks 单点）。
"""

from datetime import datetime, timedelta

from conftest import TestingSessionLocal
from sqlalchemy import select
from models import BehaviorEvent
from schemas import TaskCreate, TaskUpdate
from services.ai_data import prune_behavior_events
from services.tasks import create_task, delete_task, update_task


def _events() -> list[BehaviorEvent]:
    with TestingSessionLocal() as db:
        return list(db.scalars(select(BehaviorEvent)).all())


def _scheduled_task(title="写文档", start=None):
    with TestingSessionLocal() as db:
        t = create_task(db, TaskCreate(
            title=title,
            start_time=start or datetime.now() + timedelta(days=1),
            end_time=(start or datetime.now() + timedelta(days=1)) + timedelta(minutes=60)))
        return t


def _ev(event: BehaviorEvent) -> BehaviorEvent:
    with TestingSessionLocal() as db:
        return db.get(BehaviorEvent, event.id)


# —— 四类事件 ——————————————————————————————————————————————————————


def test_reschedule_event_with_context():
    """改期 → reschedule 事件：有符号分钟差 + 环境快照（周几/时段）。"""
    base = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    t = _scheduled_task(start=base)
    new = base + timedelta(hours=2)
    with TestingSessionLocal() as db:
        update_task(db, t.id, TaskUpdate(start_time=new, end_time=new + timedelta(minutes=60)))
    evs = _events()
    assert len(evs) == 1 and evs[0].kind == "reschedule"
    assert evs[0].delta_min == 120
    assert evs[0].prev_start == base and evs[0].new_start == new
    ctx = evs[0].context
    assert ctx["weekday"] == base.weekday() and ctx["period"] in ("上午", "清晨", "下午")


def test_unschedule_event_on_start_cleared():
    """取消排期（值→None）→ unschedule 事件（放弃近似口径）。"""
    t = _scheduled_task()
    with TestingSessionLocal() as db:
        update_task(db, t.id, TaskUpdate(start_time=None))
    evs = _events()
    assert len(evs) == 1 and evs[0].kind == "unschedule"
    assert evs[0].prev_start is not None and evs[0].new_start is None


def test_resize_event_on_duration_change():
    """改时长（end 变 start 不变）→ resize 事件（估时偏差信号）。"""
    base = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    t = _scheduled_task(start=base)
    with TestingSessionLocal() as db:
        update_task(db, t.id, TaskUpdate(end_time=base + timedelta(minutes=120)))
    evs = _events()
    assert len(evs) == 1 and evs[0].kind == "resize"
    assert evs[0].delta_min == 60  # 60min → 120min


def test_done_event_with_planned_and_delta():
    """勾完成 → done 事件：planned_min = 计划时长，delta = 完成-开始历时 - 计划。"""
    base = (datetime.now() - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    t = _scheduled_task(title="开会", start=base)
    with TestingSessionLocal() as db:
        update_task(db, t.id, TaskUpdate(done=True))
    evs = _events()
    assert len(evs) == 1 and evs[0].kind == "done"
    assert evs[0].planned_min == 60
    assert evs[0].delta_min is not None and evs[0].delta_min >= 59  # 历时≈120min − 60


def test_first_scheduling_records_no_event():
    """首排（None→值）零事件——apply_slot_decisions 经 update_task 天然静默。"""
    with TestingSessionLocal() as db:
        t = create_task(db, TaskCreate(title="收件箱任务"))
        update_task(db, t.id, TaskUpdate(
            start_time=datetime.now() + timedelta(days=1),
            end_time=datetime.now() + timedelta(days=1, minutes=30)))
    assert _events() == []


# —— 系列口径（对齐 reschedule_count 既有拍板）———————————————————————————


def test_series_this_done_records_but_reschedule_not():
    """系列 mode=this：done 记事件、改期不记（例外实体化非改期，同 count 口径）。"""
    from schemas import TaskCreate as _TC

    _WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    _base = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    _byday = _WEEKDAYS[_base.weekday()]
    _next_occ = _base + timedelta(days=7)  # 基准实例的下一个同星期实例
    with TestingSessionLocal() as db:
        t = create_task(db, _TC(
            title="周会", start_time=_base,
            end_time=_base + timedelta(minutes=30),
            recurrence_rule=f"FREQ=WEEKLY;BYDAY={_byday}"))
        occ_date = _next_occ.date().isoformat()
        # 改期单实例（时间挪动）→ 不记 reschedule
        update_task(db, t.id, TaskUpdate(
            start_time=_next_occ.replace(hour=_next_occ.hour),
            end_time=_next_occ + timedelta(minutes=30)),
            mode="this", date=occ_date)
        assert _events() == []
        # 完成单实例 → 记 done（落在例外行上）
        update_task(db, t.id, TaskUpdate(done=True), mode="this", date=occ_date)
        evs = _events()
        assert len(evs) == 1 and evs[0].kind == "done"


# —— abandon（delete_task）———————————————————————————————————————————


def test_delete_records_abandon_except_flash():
    """老任务删除记 abandon；闪建闪删（<FLASH 秒且从未排期）是输入噪音不记。"""
    t1 = _scheduled_task(title="认真的任务")
    with TestingSessionLocal() as db:
        delete_task(db, t1.id)
    evs = _events()
    assert len(evs) == 1 and evs[0].kind == "abandon"
    assert evs[0].prev_start is not None

    with TestingSessionLocal() as db:
        flash = create_task(db, TaskCreate(title="刚建就删"))
        delete_task(db, flash.id)
    assert len(_events()) == 1  # 闪删不新增


def test_prune_behavior_events():
    """90 天过期清理，新鲜保留。"""
    with TestingSessionLocal() as db:
        db.add(BehaviorEvent(task_id=None, kind="reschedule",
                             created_at=datetime.now() - timedelta(days=91)))
        db.add(BehaviorEvent(task_id=None, kind="done",
                             created_at=datetime.now() - timedelta(days=10)))
        db.commit()
        assert prune_behavior_events(db) == 1
        rest = _events()
        assert len(rest) == 1 and rest[0].kind == "done"
