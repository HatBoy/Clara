"""偏好硬闸与 est_hint 个性化测试（2026-10-26 提速+秘书化批 5）。

覆盖四件：preference_slots 解析三闸正负例 + hits_window 区间重叠口径；
引擎三函数 avoid_windows 闸（suggest_slot / is_valid_slot / candidate_slots
共享 _gates_ok 单点）；similar_done_durations 同类历史耗时均值（profiler
est_hint 数据源）。finder 候选过滤与 planner 引擎继承在同模块测试文件
（test_scheduler_finder / test_strategist_dispatcher 各追加一例）。
"""

from datetime import datetime, timedelta

from conftest import TestingSessionLocal
from models import Task
from services.preference_slots import hits_window, parse_avoid_windows
from services.tasks import (
    candidate_slots,
    is_valid_slot,
    similar_done_durations,
    suggest_slot,
)

# —— 解析器：正例 ————————————————————————————————————————————————————————


def test_parse_weekday_afternoon():
    w = parse_avoid_windows(["[排期偏好·用户设定] 周五下午别排事"])
    assert w == [{"weekday": 4, "start_min": 720, "end_min": 1080,
                  "rule": "[排期偏好·用户设定] 周五下午别排事"}]


def test_parse_every_day_evening_without_weekday():
    # 无星期词 = 每天；动词在句尾（泛指宾语闸通过）
    assert parse_avoid_windows(["[作息规律·用户设定] 每天晚上不排"]) == \
        [{"weekday": None, "start_min": 1080, "end_min": 1440,
          "rule": "[作息规律·用户设定] 每天晚上不排"}]


def test_parse_variants_and_full_day():
    # 星期六/礼拜天变体；无时段词 = 全天；「留空/空着」动词
    assert parse_avoid_windows(["星期六空着"]) == \
        [{"weekday": 5, "start_min": 0, "end_min": 1440, "rule": "星期六空着"}]
    assert parse_avoid_windows(["礼拜天上午留空"]) == \
        [{"weekday": 6, "start_min": 0, "end_min": 720, "rule": "礼拜天上午留空"}]


# —— 解析器：三闸负例（宁漏勿误）———————————————————————————————————————


def test_parse_three_guards_reject():
    assert parse_avoid_windows([
        # 闸①：星期/时段全缺的纯动词条件句（哲学人格文本误报防线）
        "深度工作：高价值任务需要整块无打断的时间，深度时段不排会议与碎片事务。",
        # 闸②：钟点表达（「10 点前」的窗是 (0,600) 而非整个上午，不解析）
        "[排期偏好·AI学习·置信0.55] 上午10点前不排会议",
        # 闸③：实义限定宾语（语义非全窗禁排）
        "周五下午别排太满",
        # 无避让动词（维持文本注入现状）
        "不喜欢晚上开会",
    ]) == []


def test_parse_cache_signature_invalidation():
    a = parse_avoid_windows(["周五下午别排事"])
    assert parse_avoid_windows(["周五下午别排事"]) is a  # 同签名命中缓存
    parse_avoid_windows(["周六上午别排事"])  # 换签名重解析
    assert parse_avoid_windows(["周五下午别排事"]) == a  # 缓存挤出后重解析等值


# —— hits_window：区间重叠口径 ——————————————————————————————————————————


def test_hits_window_overlap_semantics():
    w = [{"weekday": 4, "start_min": 720, "end_min": 1080, "rule": "周五下午"}]
    assert hits_window(datetime(2026, 10, 16, 14, 0), 60, w) is w[0]  # 窗内
    # 起点在窗外但跨进窗内（11:30-12:30 与 12:00 起的窗重叠）同样命中
    assert hits_window(datetime(2026, 10, 16, 11, 30), 60, w) is w[0]
    assert hits_window(datetime(2026, 10, 16, 11, 0), 60, w) is None  # 不沾窗
    assert hits_window(datetime(2026, 10, 17, 14, 0), 60, w) is None  # 周六不判
    assert hits_window(datetime(2026, 10, 16, 14, 0), 60, []) is None  # 空表


# —— 引擎三函数 avoid_windows 闸（_gates_ok 单点）——————————————————————


def test_suggest_slot_defers_out_of_window():
    # 周五 12:00-18:00 禁排：same_clock_first=False 网格顺延 → 首槽 18:00
    w = [{"weekday": 4, "start_min": 720, "end_min": 1080, "rule": "周五下午"}]
    slot = suggest_slot(datetime(2026, 10, 16, 12, 0), 60, [],
                        day_start_min=480, day_end_min=1320, lunch=None,
                        same_clock_first=False, avoid_windows=w)
    assert slot == datetime(2026, 10, 16, 18, 0)


def test_is_valid_slot_rejects_window():
    w = [{"weekday": None, "start_min": 1080, "end_min": 1440, "rule": "晚上"}]
    kw = dict(now=datetime(2026, 10, 16, 8, 0),
              day_start_min=480, day_end_min=1320, lunch=None)
    assert is_valid_slot(datetime(2026, 10, 16, 20, 0), 60, [],
                         avoid_windows=w, **kw) is False
    assert is_valid_slot(datetime(2026, 10, 16, 15, 0), 60, [],
                         avoid_windows=w, **kw) is True
    # 不传 avoid_windows 行为不变（既有调用方零改动口径）
    assert is_valid_slot(datetime(2026, 10, 16, 20, 0), 60, [], **kw) is True


def test_candidate_slots_filtered_by_window():
    w = [{"weekday": 4, "start_min": 0, "end_min": 720, "rule": "周五上午"}]
    cands = candidate_slots(datetime(2026, 10, 16, 8, 0), 60, [],
                            day_start_min=480, day_end_min=1320, lunch=None,
                            same_clock_first=False, avoid_windows=w, k=5)
    assert len(cands) == 5 and all(c.hour >= 12 for c in cands)  # 上午全滤


# —— similar_done_durations：est_hint 数据源 ————————————————————————————


def test_similar_done_durations_recent_three_average(fresh_db):
    now = datetime.now()
    with TestingSessionLocal() as db:
        # 4 个相似（新→旧 999/105/90/75，取新 3 均值 = (999+105+90)/3）+ 1 不相似
        for back, dur in [(40, 999), (50, 105), (60, 90), (70, 75)]:
            db.add(Task(title="季度方案评审", duration_mins=dur,
                        done_at=now - timedelta(days=back)))
        db.add(Task(title="取快递", duration_mins=10,
                    done_at=now - timedelta(days=1)))
        db.commit()
        assert similar_done_durations(db, "季度方案评审") == 398  # 只取近 3 次
        assert similar_done_durations(db, "写周报") is None       # 无相似降级 None


def test_similar_done_durations_empty_db(fresh_db):
    with TestingSessionLocal() as db:
        assert similar_done_durations(db, "任何任务") is None


# —— 优化批 6（2026-11-01）：复杂形态两式 + 跨午夜顺修 ————————————————————


def test_workday_period_form_parses():
    """复杂形态 A：工作日 + 时段——「工作日上午不排会」产 workdays 窗；
    无时段词的「工作日别排早会」宁漏勿误不产窗。"""
    from services.preference_slots import parse_avoid_windows

    (win,) = parse_avoid_windows(["工作日上午不排会"])
    assert win["workdays"] is True
    assert (win["start_min"], win["end_min"]) == (0, 720)

    assert parse_avoid_windows(["工作日别排早会"]) == []  # 无时段词不解析


def test_month_end_form_parses():
    """复杂形态 B：月末锚——「每月末留白」默认最后 2 天全天；带时段收窄。"""
    from datetime import date

    from services.preference_slots import parse_avoid_windows

    (win,) = parse_avoid_windows(["每月末留白"])
    assert win["month_last_days"] == 2
    assert (win["start_min"], win["end_min"]) == (0, 1440)
    # 31 天月的 30/31 号命中，29 号不命中
    from services.preference_slots import hits_window
    from datetime import datetime as _dt

    hit = hits_window(_dt(2026, 10, 30, 9), 60, [win])
    miss = hits_window(_dt(2026, 10, 29, 9), 60, [win])
    assert hit is not None and miss is None

    (win2,) = parse_avoid_windows(["每个月底的下午别排事"])
    assert win2["month_last_days"] == 2
    assert (win2["start_min"], win2["end_min"]) == (720, 1080)


def test_hits_window_cross_midnight_next_day():
    """跨午夜顺修（批 6）：周五 23:30-00:30 的槽撞「周六上午」窗——旧版只按
    起点 weekday 判漏拦；周五上午窗不拦该槽（起点不是周五上午）。"""
    from datetime import datetime

    from services.preference_slots import hits_window, parse_avoid_windows

    sat_am, *_ = parse_avoid_windows(["周六上午别排事"])
    slot = datetime(2026, 10, 30, 23, 30)  # 周五 23:30 → 跨到周六 00:30
    assert hits_window(slot, 60, [sat_am]) is not None
    fri_am, *_ = parse_avoid_windows(["周五上午别排事"])
    assert hits_window(slot, 60, [fri_am]) is None
