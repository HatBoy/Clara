"""排期子图 services 基础件测试（批1，2026-10-21）：candidate_slots 多候选
泛化 + normalize/similar/last_similar_occurrence 历史频率取数。

对拍纪律：不带新筛维度时 candidate_slots 首选必须与 suggest_slot 同值——
多候选层不改变引擎主荐口径（主荐漂移风险的锁定用例）。基准日期
2026-10-22（周四），全部固定时刻无时钟冻结依赖。
"""

from datetime import date, datetime, timedelta

from conftest import TestingSessionLocal
from schemas import TaskCreate
from services import tasks as tasks_svc

BASE = datetime(2026, 10, 22, 10, 0)  # 周四 10:00


def _cands(busy=None, duration=60, **kw):
    args = dict(day_start_min=480, day_end_min=1320, lunch=None)
    args.update(kw)
    return tasks_svc.candidate_slots(BASE, duration, busy or [], **args)


# —— candidate_slots：对拍 / 截断 / 筛维度 ————————————————————————


def test_candidate_slots_first_matches_suggest_slot():
    """对拍锁定：不带 allowed_dates/anchor_mins 时首选 == suggest_slot 返回值
    （same_clock_first 两形态各验一次）——多候选层不得漂移主荐口径。"""
    for same_clock in (True, False):
        kwargs = dict(day_start_min=480, day_end_min=1320, lunch=None,
                      same_clock_first=same_clock)
        expect = tasks_svc.suggest_slot(BASE, 60, [], **kwargs)
        got = tasks_svc.candidate_slots(BASE, 60, [], k=5, **kwargs)
        assert got and got[0] == expect


def test_candidate_slots_k_cap_and_avoid_busy():
    # 当日上午被占满 → 候选全部落在 13:00 后或后续天，k 截断生效且时序递增
    busy = [(BASE.replace(hour=8), BASE.replace(hour=13))]
    out = _cands(busy=busy, same_clock_first=False, k=5)
    assert len(out) == 5
    assert out == sorted(out)
    for c in out:
        assert all(not tasks_svc.overlaps(c, c + timedelta(minutes=60), b0, b1)
                   for b0, b1 in busy)


def test_candidate_slots_allowed_dates_whitelist():
    # 日期白名单（政务限工作日 / 户外剔雨天的下游形态）：候选只出自白名单日
    out = _cands(allowed_dates={date(2026, 10, 23)}, same_clock_first=False, k=5)
    assert out and all(c.date() == date(2026, 10, 23) for c in out)


def test_candidate_slots_morning_only_cap():
    # 就医上午闸：所有候选结束 ≤ 12:00（上界由 cap_min 收紧实现）
    out = _cands(morning_only=True, same_clock_first=False, k=5)
    assert out and all(c + timedelta(minutes=60) <= c.replace(hour=12, minute=0)
                       for c in out)


def test_candidate_slots_day_budget_skips_loaded_day():
    # 当日已占 460 分钟 + 本条 60 > 预算 480 → 顺延次日；未占用日不受预算拦
    load = {date(2026, 10, 22): 460}
    out = _cands(day_load=load, day_budget=480, same_clock_first=False, k=1)
    assert out and out[0].date() == date(2026, 10, 23)


def test_candidate_slots_anchor_prefers_garbage_time():
    # 垃圾时间锚定：锚点 13:30 → 首选 13:30 而非当日最早 10:00（低能耗短任务
    # 不占上午黄金脑力时段的规则层实现）
    start = BASE.replace(hour=8)
    out = tasks_svc.candidate_slots(start, 30, [], day_start_min=480, day_end_min=1320,
                                    lunch=None, same_clock_first=False,
                                    anchor_mins=[810], k=5)
    assert out and out[0] == start.replace(hour=13, minute=30)


def test_candidate_slots_empty_when_window_full():
    # 8 天全占满 → 空列表（Node3 零候选 → no_slot 问询的触发口径）
    busy = [(BASE.replace(hour=8) + timedelta(days=i),
             BASE.replace(hour=8) + timedelta(days=i, hours=14)) for i in range(8)]
    assert _cands(busy=busy, same_clock_first=False, k=5) == []


# —— normalize / similar / 历史频率 ————————————————————————————


def test_normalize_title_strips_punct():
    assert tasks_svc.normalize_title("  年度体检！ ") == "年度体检"
    assert tasks_svc.normalize_title("Write report.") == "writereport"


def test_similar_title_threshold():
    assert tasks_svc.similar_title("体检", [("年度体检", None)]) is not None
    assert tasks_svc.similar_title("洗牙", [("团建", None)]) is None
    assert tasks_svc.similar_title("", [("体检", None)]) is None


def test_last_similar_occurrence_latest_past_only():
    # 两次历史同类取最近；未来已排不算"做过"；无命中 None
    now = datetime.now()
    with TestingSessionLocal() as db:
        def mk(title, start):
            return tasks_svc.create_task(db, TaskCreate(
                title=title, start_time=start, end_time=start + timedelta(minutes=30),
                duration_mins=30))

        old = (now - timedelta(days=60)).replace(hour=10, minute=0, second=0, microsecond=0)
        near = (now - timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)
        future = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        mk("洗牙", old)
        mk("洗牙", near)
        mk("洗牙", future)
        assert tasks_svc.last_similar_occurrence(db, "洗牙") == near
        assert tasks_svc.last_similar_occurrence(db, "团建") is None
