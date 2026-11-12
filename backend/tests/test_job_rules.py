"""services/job_rules.py 测试（定时任务识别规则层，2026-10-20 增）。

is_important_meeting / hard_task_score 纯判定直喂 SimpleNamespace 替身
（鸭子类型，不建库）；pick_important_meetings / pick_hard_task 走真表
（conftest.TestingSessionLocal）。核心回归：重复系列主行基准在上周时
窗口查询会漏——pick_important_meetings 必须经 expand_occurrences 选中。
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from conftest import TestingSessionLocal
from models import Task
from services.job_rules import (
    hard_task_score,
    is_important_meeting,
    pick_hard_task,
    pick_important_meetings,
)


def _clone(**kw) -> SimpleNamespace:
    """任务替身：字段名与 ORM 对齐，未给的取"普通未命中"默认值。"""
    base = dict(
        id="t1",
        title="普通任务",
        priority=3,
        start_time=None,
        end_time=None,
        duration_mins=None,
        recurrence_rule=None,
        notes=None,
        created_at=datetime.now(),
        reschedule_count=0,
        ever_scheduled=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _add_task(title="任务", **kw) -> str:
    with TestingSessionLocal() as db:
        row = Task(title=title, **kw)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


# —— is_important_meeting：三分支或，边界逐支验证 ————————————————————————


def test_meeting_priority_branch():
    # priority≤1 直接命中，其余全普通
    assert is_important_meeting(_clone(priority=1)) is True
    assert is_important_meeting(_clone(priority=2, duration_mins=30)) is False


def test_meeting_duration_branch_two_paths():
    # 89 分钟差 1 分钟不命中；90 分钟经 duration_mins 与 end-start 两路径都命中
    assert is_important_meeting(_clone(duration_mins=89)) is False
    assert is_important_meeting(_clone(duration_mins=90)) is True
    start = datetime(2026, 10, 21, 14, 0)
    assert is_important_meeting(
        _clone(start_time=start, end_time=start + timedelta(minutes=90))
    ) is True


def test_meeting_keyword_branch():
    # 关键词第三支：中文 / 英文大小写 / 半角 1:1；"写周报"不落会议词表
    assert is_important_meeting(_clone(title="客户评审会")) is True
    assert is_important_meeting(_clone(title="Quarterly Review")) is True
    assert is_important_meeting(_clone(title="1:1 sync")) is True
    assert is_important_meeting(_clone(title="写周报")) is False


# —— hard_task_score：资格 + 权重 ————————————————————————————————————————————


def test_hard_score_requires_unscheduled():
    # 已排期无资格；未排期且不命中三分支也 None
    now = datetime.now()
    assert hard_task_score(_clone(start_time=now), now) is None
    assert hard_task_score(_clone(), now) is None


def test_hard_score_three_hit_branches():
    now = datetime.now()
    assert hard_task_score(_clone(duration_mins=90), now) is not None
    assert hard_task_score(_clone(priority=1), now) is not None
    assert hard_task_score(_clone(title="写调研报告"), now) is not None


def test_hard_score_weights():
    # 权重 = 滞留天数 + reschedule_count，ever_scheduled 加 0.5
    now = datetime.now()
    t = _clone(title="方案", created_at=now - timedelta(days=3), reschedule_count=2)
    assert hard_task_score(t, now) == 5
    t2 = _clone(title="方案", created_at=now - timedelta(days=3),
                reschedule_count=2, ever_scheduled=True)
    assert hard_task_score(t2, now) == 5.5


# —— pick_important_meetings：两路合流 + 系列缺口回归 ————————————————————————


def test_pick_meetings_single_task_window():
    day = date.today() + timedelta(days=1)
    noon = datetime.combine(day, datetime.min.time()) + timedelta(hours=10)
    _add_task("明日体检", start_time=noon, priority=1)
    # 目标日之外的普通任务（今天）不干扰
    _add_task("今天的会", start_time=datetime.now())
    with TestingSessionLocal() as db:
        picked = pick_important_meetings(db, day)
    assert len(picked) == 1
    assert picked[0][0].title == "明日体检"
    assert picked[0][1] == noon


def test_pick_meetings_series_base_in_past_not_missed():
    # 缺口回归：系列主行基准在上周一，目标日本周一——窗口查询按 start_time 查
    # 不到主行，必须经 expand_occurrences 展开仍被选中
    this_monday = date.today() - timedelta(days=date.today().isoweekday() - 1)
    last_monday = this_monday - timedelta(days=7)
    assert last_monday.isoweekday() == 1
    base = datetime.combine(last_monday, datetime.min.time()) + timedelta(hours=9)
    target = last_monday + timedelta(days=7)
    _add_task("每周一站会", start_time=base, end_time=base + timedelta(minutes=30),
              recurrence_rule="FREQ=WEEKLY;BYDAY=MO", priority=1)
    with TestingSessionLocal() as db:
        picked = pick_important_meetings(db, target)
    assert len(picked) == 1
    assert picked[0][0].title == "每周一站会"
    assert picked[0][1].date() == target


def test_pick_meetings_cancelled_occurrence_skipped():
    # 墓碑取消的实例不选中（cancelled_dates 挖空）
    monday = date.today() + timedelta(days=(1 - date.today().isoweekday()) % 7 or 7)
    base = datetime.combine(monday, datetime.min.time()) + timedelta(hours=9)
    tid = _add_task("周一评审", start_time=base, recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
                    priority=1)
    with TestingSessionLocal() as db:
        db.add(Task(title="周一评审", parent_id=tid, original_time=base, is_deleted=True))
        db.commit()
    with TestingSessionLocal() as db:
        assert pick_important_meetings(db, monday) == []


def test_pick_meetings_sorted_by_occurrence():
    # 多会议按实例开始时刻升序（准备清单时间轴阅读序）
    day = date.today() + timedelta(days=1)
    lo = datetime.combine(day, datetime.min.time())
    _add_task(title="下午会", start_time=lo + timedelta(hours=15), priority=1)
    _add_task(title="上午会", start_time=lo + timedelta(hours=9), priority=1)
    with TestingSessionLocal() as db:
        picked = pick_important_meetings(db, day)
    assert [t.title for t, _ in picked] == ["上午会", "下午会"]


# —— pick_hard_task：权重最高 1 条 ————————————————————————————————————————————


def test_pick_hard_task_picks_best_one():
    now = datetime.now()
    _add_task(title="刚建的难事", duration_mins=120,
              created_at=now - timedelta(days=1))  # 权重 1
    _add_task(title="拖最久的方案", created_at=now - timedelta(days=5),
              reschedule_count=2)  # 权重 7
    _add_task("普通待办")  # 不命中
    with TestingSessionLocal() as db:
        best = pick_hard_task(db, now)
    assert best is not None and best.title == "拖最久的方案"


def test_pick_hard_task_tie_earlier_created():
    # 同分取创建早（越早创建越先滞留）
    now = datetime.now()
    _add_task(title="晚建的方案", created_at=now - timedelta(days=1))
    _add_task(title="早建的方案", created_at=now - timedelta(days=3))
    with TestingSessionLocal() as db:
        assert pick_hard_task(db, now).title == "早建的方案"


def test_pick_hard_task_none_when_no_candidate():
    now = datetime.now()
    _add_task("普通待办")
    _add_task(title="已排期的难事方案", start_time=now + timedelta(days=1))
    with TestingSessionLocal() as db:
        assert pick_hard_task(db, now) is None


def test_pick_stalled_tasks_overdue_single():
    """滞留点名单次形态（优化批 7）：排期已过 ≥3 天未完成上榜；昨天/已完成
    不上榜。"""
    from datetime import date, datetime, timedelta

    from models import Task
    from services.job_rules import pick_stalled_tasks

    today = date.today()
    with TestingSessionLocal() as db:
        db.add(Task(title="三天前的活", duration_mins=30,
                    start_time=datetime.combine(today - timedelta(days=3),
                                                datetime.min.time()) + timedelta(hours=9)))
        db.add(Task(title="昨天的活", duration_mins=30,
                    start_time=datetime.combine(today - timedelta(days=1),
                                                datetime.min.time()) + timedelta(hours=9)))
        db.add(Task(title="做完的旧活", duration_mins=30,
                    start_time=datetime.combine(today - timedelta(days=4),
                                                datetime.min.time()) + timedelta(hours=9),
                    done_at=datetime.now() - timedelta(days=3)))
        db.commit()
        out = pick_stalled_tasks(db)
    assert [s["title"] for s in out] == ["三天前的活"]
    assert "未完成" in out[0]["note"]
