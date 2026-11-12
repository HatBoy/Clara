"""日程提醒调度测试（基建见 conftest.py）。

service 层直测为主（scan/claim 是纯时刻语义，绕开 HTTP）；提醒提前量变更清表
联动走 client（PATCH 全链路）。注意：client fixture 无 lifespan，调度循环在
测试中不启动——这里只测循环体调用的两个函数本身。
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TestingSessionLocal
from models import Project, Reminder, Task
from services.reminders import _BYDAY_TO_ISO, claim_due, scan_due
from services.settings import get_settings

# 默认提前量（DEFAULTS 同源）：入列时刻 = 实例开始 − 15 分钟
LEAD = 15


def _add_task(**kw) -> str:
    """直插一行任务并返回 id（默认存活、非重复）。"""
    kw.setdefault("title", "测试任务")
    with TestingSessionLocal() as db:
        t = Task(**kw)
        db.add(t)
        db.commit()
        return t.id


def _set_lead(lead: int | None) -> None:
    with TestingSessionLocal() as db:
        get_settings(db).reminder_lead_min = lead
        db.commit()


def _set_sound(sound: str) -> None:
    with TestingSessionLocal() as db:
        get_settings(db).sound = sound
        db.commit()


def _queue_rows() -> list[Reminder]:
    with TestingSessionLocal() as db:
        return db.scalars(select(Reminder)).all()


# —— scan：单次任务 ——


def test_scan_enqueues_task_in_window():
    now = datetime.now()
    # 实例开始 = now + lead + 20s → fire_at = now + 20s，落在 (now, now+40s] 窗口内
    start = now + timedelta(minutes=LEAD, seconds=20)
    tid = _add_task(start_time=start)
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 1
    rows = _queue_rows()
    assert len(rows) == 1
    assert rows[0].task_id == tid
    assert rows[0].fire_at == start - timedelta(minutes=LEAD)


def test_scan_lead_zero_on_time():
    _set_lead(0)  # "准时"档
    now = datetime.now()
    _add_task(start_time=now + timedelta(seconds=20))
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 1


def test_scan_skips_when_lead_none_and_muted():
    # "不提醒"且提示音关闭：无任何输出通道，零调度
    _set_lead(None)
    _set_sound("off")
    now = datetime.now()
    _add_task(start_time=now + timedelta(seconds=20))
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 0
    assert _queue_rows() == []


def test_scan_lead_none_with_sound_enqueue_on_time():
    # "不提醒" + 提示音开启：按"准时"（0 提前量）入列——事件到点由前端只播声音
    _set_lead(None)
    _set_sound("chime")
    now = datetime.now()
    start = now + timedelta(seconds=20)
    _add_task(start_time=start)
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 1
    rows = _queue_rows()
    assert len(rows) == 1
    assert rows[0].fire_at == start


def test_scan_skips_soft_deleted():
    now = datetime.now()
    _add_task(start_time=now + timedelta(minutes=LEAD, seconds=20), is_deleted=True)
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 0


def test_scan_idempotent():
    # (task_id, fire_at) 唯一约束 + on_conflict_do_nothing：重叠窗口反复扫描不重复入列
    now = datetime.now()
    _add_task(start_time=now + timedelta(minutes=LEAD, seconds=20))
    for _ in range(2):
        with TestingSessionLocal() as db:
            scan_due(db, now)
    assert len(_queue_rows()) == 1


# —— scan：重复系列 ——


def test_series_weekly_match_and_mismatch():
    now = datetime.now()
    # 实例时刻取"未来 15min20s"的绝对时间（跨天也自然落在今明两天的枚举范围内）
    start = now + timedelta(minutes=LEAD, seconds=20)
    hit_code = next(c for c, i in _BYDAY_TO_ISO.items() if i == start.isoweekday())
    miss_code = next(c for c, i in _BYDAY_TO_ISO.items() if i != start.isoweekday())
    tid_hit = _add_task(start_time=start, recurrence_rule=f"FREQ=WEEKLY;BYDAY={hit_code}")
    tid_miss = _add_task(start_time=start, recurrence_rule=f"FREQ=WEEKLY;BYDAY={miss_code}")
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 1
    ids = {r.task_id for r in _queue_rows()}
    assert tid_hit in ids
    assert tid_miss not in ids


def test_series_until_dead_not_enqueued():
    # UNTIL 截断到昨天 = 死系列，今天的实例不提醒
    now = datetime.now()
    start = now + timedelta(minutes=LEAD, seconds=20)
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    _add_task(start_time=start, recurrence_rule=f"FREQ=DAILY;UNTIL={yesterday}T235959")
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 0


def test_series_tombstone_day_skipped():
    # "仅删本次"的墓碑行挖空该日实例——虚拟实例不提醒
    now = datetime.now()
    start = now + timedelta(minutes=LEAD, seconds=20)
    tid = _add_task(start_time=start, recurrence_rule="FREQ=DAILY")
    with TestingSessionLocal() as db:
        db.add(Task(title="墓碑", parent_id=tid, original_time=start, is_deleted=True))
        db.commit()
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 0


# —— claim：认领与放弃 ——


def _seed_claimable(fire: datetime, **task_kw) -> str:
    """直插一条已到点的待发提醒（任务 start = fire + 有效提前量保持口径一致——
    2026-10-22 分级后"评审会"等标题命中会议词会抬到 60，按 _effective_lead_min
    单源对齐，别让 helper 与分级口径脱节）。"""
    from services.reminders import _effective_lead_min

    with TestingSessionLocal() as db:
        t = Task(title=task_kw.pop("title", "认领任务"), **task_kw)
        t.start_time = fire + timedelta(minutes=_effective_lead_min(t, LEAD) or 0)
        db.add(t)
        db.flush()
        db.add(Reminder(task_id=t.id, fire_at=fire))
        db.commit()
        return t.id


def test_claim_returns_payload_and_deletes():
    now = datetime.now()
    fire = now - timedelta(seconds=1)  # 已到点、未超宽限
    with TestingSessionLocal() as db:
        proj = Project(name="提醒项目", color="#3b82f6")
        db.add(proj)
        db.flush()
        tid = _seed_claimable(fire, title="带项目", project_id=proj.id)
    with TestingSessionLocal() as db:
        events = claim_due(db, now)
    assert len(events) == 1
    ev = events[0]
    assert ev["task_id"] == tid
    assert ev["title"] == "带项目"
    assert ev["project_name"] == "提醒项目"
    assert ev["lead_min"] == LEAD
    assert datetime.fromisoformat(ev["start_at"]) == fire + timedelta(minutes=LEAD)
    assert _queue_rows() == []  # 触发即删行
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []  # 二次认领空（防双发）


def test_claim_stale_dropped():
    # 过期超宽限（120s）：启动补账风暴防线——行删但不发
    now = datetime.now()
    _seed_claimable(now - timedelta(seconds=121))
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []
    assert _queue_rows() == []


def test_claim_deleted_task_dropped():
    now = datetime.now()
    tid = _seed_claimable(now - timedelta(seconds=1), is_deleted=True)
    assert tid
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []
    assert _queue_rows() == []


def test_claim_rescheduled_task_dropped():
    # 单次任务已改期：fire_at 与当前 start−lead 不匹配 → 幽灵提醒放弃
    # （改期后的新时刻由后续扫描重新入列）
    now = datetime.now()
    fire = now - timedelta(seconds=1)
    tid = _add_task(start_time=now + timedelta(hours=2))  # start 已被挪走
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []
    assert _queue_rows() == []


def test_claim_sound_only_payload():
    # "不提醒"档事件载荷：lead_min=None（前端只播声音），start_at = 实例开始时刻
    _set_lead(None)
    _set_sound("chime")
    now = datetime.now()
    fire = now - timedelta(seconds=1)  # 已到点、未超宽限
    tid = _add_task(start_time=fire)  # 0 提前量：start 即 fire
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    with TestingSessionLocal() as db:
        events = claim_due(db, now)
    assert len(events) == 1
    assert events[0]["lead_min"] is None
    assert datetime.fromisoformat(events[0]["start_at"]) == fire
    assert _queue_rows() == []


# —— 设置联动：提前量/提示音变更清表（HTTP 全链路） ——


def test_patch_lead_change_clears_queue(client: TestClient):
    client.get("/api/settings")  # 落默认行（lead=15）
    fire = datetime.now() + timedelta(seconds=10)
    tid = _add_task(start_time=fire + timedelta(minutes=LEAD))
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()

    resp = client.patch("/api/settings", json={"reminder_lead_min": 0})
    assert resp.status_code == 200
    assert _queue_rows() == []  # 口径已变，旧队列整体清除

    # 值未变的 PATCH 不清队列（下轮扫描按同口径幂等续用）
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    client.patch("/api/settings", json={"reminder_lead_min": 0})
    assert len(_queue_rows()) == 1


def test_patch_sound_change_clears_queue(client: TestClient):
    # 提示音变更同样清表："不提醒"档的零调度开关翻转，旧口径队列作废
    client.get("/api/settings")
    _set_lead(None)
    _set_sound("chime")
    fire = datetime.now() + timedelta(seconds=10)
    tid = _add_task(start_time=fire)
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()

    resp = client.patch("/api/settings", json={"sound": "off"})
    assert resp.status_code == 200
    assert _queue_rows() == []

    # 值未变的 PATCH 不清队列
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    client.patch("/api/settings", json={"sound": "off"})
    assert len(_queue_rows()) == 1


# —— 会前重推标记（prep_proposal_id，2026-10-20 增）——————————————————————


def _seed_prep_card(task_id: str, *, resolve: str | None = None) -> str:
    """直插一张未决（或已决）prep 建议卡，返回 proposal_id。"""
    from services.ai_data import create_proposal, record_proposal_outcome

    with TestingSessionLocal() as db:
        p = create_proposal(db, kind="prep", payload={
            "meetings": [{"task_id": task_id, "title": "评审会",
                          "start": "2026-10-21T14:00:00", "headline": "备会",
                          "checklist": ["过文档"]}]})
        if resolve is not None:
            record_proposal_outcome(db, p.id, resolve)
        return p.id


def test_claim_marks_pending_prep():
    # 有未决 prep 卡 → 事件附 prep_proposal_id
    fire = datetime.now() - timedelta(seconds=1)
    tid = _seed_claimable(fire, title="评审会")
    pid = _seed_prep_card(tid)
    with TestingSessionLocal() as db:
        events = claim_due(db, datetime.now())
    assert len(events) == 1
    assert events[0]["prep_proposal_id"] == pid


def test_claim_no_mark_when_card_resolved():
    # 卡已决（accepted）→ 无标记，提醒回归普通文案
    fire = datetime.now() - timedelta(seconds=1)
    tid = _seed_claimable(fire, title="评审会")
    _seed_prep_card(tid, resolve="accepted")
    with TestingSessionLocal() as db:
        events = claim_due(db, datetime.now())
    assert len(events) == 1
    assert "prep_proposal_id" not in events[0]


def test_claim_no_mark_without_card():
    # 无 prep 卡 → 无标记
    fire = datetime.now() - timedelta(seconds=1)
    _seed_claimable(fire, title="普通会议")
    with TestingSessionLocal() as db:
        events = claim_due(db, datetime.now())
    assert len(events) == 1
    assert "prep_proposal_id" not in events[0]


def test_claim_prep_mark_latest_card_wins():
    # 同任务两张未决卡（重开一轮）：取最新一条
    fire = datetime.now() - timedelta(seconds=1)
    tid = _seed_claimable(fire, title="评审会")
    _seed_prep_card(tid)
    latest = _seed_prep_card(tid)
    with TestingSessionLocal() as db:
        events = claim_due(db, datetime.now())
    assert events[0]["prep_proposal_id"] == latest


# —— 完成态（done_at，2026-10-22 增）：已完成任务不提醒 ——————————————————


def test_scan_skips_done_task():
    # 完成 ≠ 删除，但已完成的事不再提醒：scan 不入列
    now = datetime.now()
    _add_task(start_time=now + timedelta(minutes=LEAD, seconds=20),
              done_at=now - timedelta(minutes=1))
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 0


def test_claim_done_task_dropped():
    # 完成时恰好压在触发点上的提醒：claim 幽灵判定放弃（行删不发）
    now = datetime.now()
    fire = now - timedelta(seconds=1)
    tid = _add_task(start_time=fire + timedelta(minutes=LEAD), done_at=now)
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []
    assert _queue_rows() == []


# —— 分级提前量（2026-10-22 批 2）——————————————————————————————————————


def _mk_task(**kw):
    """纯对象任务（_effective_lead_min 吃鸭子类型，直测不落库）。"""
    from types import SimpleNamespace
    kw.setdefault("title", "普通事项")
    kw.setdefault("priority", 2)
    kw.setdefault("duration_mins", 30)
    return SimpleNamespace(**kw)


def test_effective_lead_pure_tiers():
    from services.reminders import (
        LEAD_IMPORTANT_MEETING,
        LEAD_TRAVEL,
        _effective_lead_min,
    )
    # None（仅声音档）恒 None——分级不作用于声音通道
    assert _effective_lead_min(_mk_task(title="评审会"), None) is None
    # 普通事项：全局档直通
    assert _effective_lead_min(_mk_task(), 15) == 15
    # 重要会议三判据：关键词 / 时长 ≥90 / priority ≤1，均抬到 60
    assert _effective_lead_min(_mk_task(title="季度汇报"), 15) == LEAD_IMPORTANT_MEETING
    assert _effective_lead_min(_mk_task(duration_mins=120), 0) == LEAD_IMPORTANT_MEETING
    assert _effective_lead_min(_mk_task(priority=1), 30) == LEAD_IMPORTANT_MEETING
    # 差旅正则抬到 40（在会议判据之前——差旅类标题即命中）
    assert _effective_lead_min(_mk_task(title="去上海出差"), 15) == LEAD_TRAVEL
    assert _effective_lead_min(_mk_task(title="高铁 G102 复盘会"), 15) == LEAD_IMPORTANT_MEETING
    # max 语义：全局档更大时尊重用户（不降级）
    assert _effective_lead_min(_mk_task(title="评审会"), 30) == LEAD_IMPORTANT_MEETING
    assert _effective_lead_min(_mk_task(), 30) == 30


def test_scan_important_meeting_60min_window():
    # 全局档 15：重要会议实例开始 = now+60+20s（fire=now+20s 落窗）应入列；
    # 同时刻的普通任务不该入列（其自身窗 now+15，now+60:20 不在）
    now = datetime.now()
    _add_task(title="评审会", start_time=now + timedelta(minutes=60, seconds=20))
    _add_task(title="取快递", start_time=now + timedelta(minutes=60, seconds=20))
    with TestingSessionLocal() as db:
        assert scan_due(db, now) == 1
    rows = _queue_rows()
    assert len(rows) == 1
    assert rows[0].fire_at == now + timedelta(seconds=20)


def test_claim_payload_reports_effective_lead_and_end():
    # 载荷 lead_min 报任务级有效提前量（非全局档）；end_at / recurrence 供前端按钮
    _set_lead(15)
    now = datetime.now()
    fire = now - timedelta(seconds=1)
    with TestingSessionLocal() as db:
        t = Task(title="融资路演", start_time=fire + timedelta(minutes=60),
                 end_time=fire + timedelta(minutes=150), duration_mins=90)
        db.add(t)
        db.flush()
        db.add(Reminder(task_id=t.id, fire_at=fire))
        db.commit()
    with TestingSessionLocal() as db:
        events = claim_due(db, now)
    assert len(events) == 1
    ev = events[0]
    assert ev["lead_min"] == 60  # 会议档覆盖全局 15
    assert ev["end_at"] == (fire + timedelta(minutes=150)).isoformat()
    assert ev["recurrence"] is False


def test_claim_tier_drift_drops_stale_row():
    # 入列时按 15 档（普通事项）；用户随后把标题改成会议名（档变 60）→ 旧行
    # fire ≠ start−60 被弃，新时刻由后续扫描按新档重入列
    now = datetime.now()
    fire = now - timedelta(seconds=1)
    tid = _add_task(title="普通事项", start_time=fire + timedelta(minutes=LEAD))
    with TestingSessionLocal() as db:
        db.add(Reminder(task_id=tid, fire_at=fire))
        db.commit()
    with TestingSessionLocal() as db:
        db.get(Task, tid).title = "评审会"
        db.commit()
    with TestingSessionLocal() as db:
        assert claim_due(db, now) == []
    assert _queue_rows() == []
