"""Node3 硬约束初筛矩阵测试（批 5，2026-10-21）：十道约束各至少 1 例 +
纯函数单测。finder_node 直调（不走全图——节点级隔离，context 手工构造）；
时间基准 2026-10-17 08:00（周六——工作日/雨日用例的日期推断锚点）。
"""

from datetime import date, datetime, timedelta

from agents.scheduler.context import _long_chain_ends
from agents.scheduler.finder import (
    BUFFER_MIN,
    _commute_needed,
    _eff_duration,
    _extend_busy_buffers,
    _needs_weekday,
    _rainy_dates,
    finder_node,
)

NOW = datetime(2026, 10, 17, 8, 0)  # 周六 08:00


def _entry(title="写周报", duration=60, *, task_id="t1", _from=None, profile=None,
           **extra):
    e = {"task_id": task_id, "title": title, "duration": duration, "start": None}
    if _from is not None:
        e["_from"] = _from.isoformat() if isinstance(_from, datetime) else _from
    if profile is not None:
        e["profile"] = profile
    e.update(extra)
    return e


def _finder_state(agent_entries=None, conflict_queue=None, *, busy=None,
                  lunch=(690, 810), day=(480, 1320), budget=480, weather=None,
                  last_medical=None, philosophy=(), long_chain_ends=None):
    return {
        "now": NOW, "philosophy_ids": list(philosophy),
        "context": {
            "busy": busy or [], "candidates": [], "valid_ids": set(),
            "prio_by_id": {}, "scheduled": [], "settings": {},
            "lunch": lunch, "day_start_min": day[0], "day_end_min": day[1],
            "day_budget": budget, "load_summary": [], "recent_chat": [],
            "weather": weather, "similar_pool": [],
            "long_chain_ends": long_chain_ends or [],
            "last_medical": last_medical,
        },
        "agent_entries": agent_entries or [],
        "conflict_queue": conflict_queue or [],
    }


# —— 纯函数 ————————————————————————————————————————————————————————————————


def test_long_chain_ends_merges_and_filters():
    # gap≤10min 三段连成 ≥3h 链 → 链尾 12:00；独立短会不入清单
    busy = [(datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 10, 30)),
            (datetime(2026, 10, 17, 10, 35), datetime(2026, 10, 17, 11, 0)),
            (datetime(2026, 10, 17, 11, 5), datetime(2026, 10, 17, 12, 0)),
            (datetime(2026, 10, 17, 15, 0), datetime(2026, 10, 17, 16, 0))]
    assert _long_chain_ends(busy) == [datetime(2026, 10, 17, 12, 0)]


def test_long_chain_ends_gap_breaks_chain():
    # gap 20min 断链——两段各 <3h 都不入清单
    busy = [(datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 10, 0)),
            (datetime(2026, 10, 17, 10, 20), datetime(2026, 10, 17, 11, 30))]
    assert _long_chain_ends(busy) == []


def test_extend_busy_buffers_appends_tail_block():
    ends = [datetime(2026, 10, 17, 12, 0)]
    out = _extend_busy_buffers([], ends)
    assert out == [(datetime(2026, 10, 17, 12, 0),
                    datetime(2026, 10, 17, 12, 0 + BUFFER_MIN))]


def test_extend_busy_buffers_meeting_tail_15min():
    # 批 5 泛化：busy_meta 标题命中会议词 → 尾 +15min；非会议词不加；meta 缺省
    # 不炸（旧签名兼容）；会议同时是链尾时 30min 与 15min 并存（重叠无差）
    end = datetime(2026, 10, 17, 10, 0)
    busy = [(datetime(2026, 10, 17, 9, 0), end)]
    meta = [{"title": "季度评审会", "end": end},
            {"title": "写作时间", "end": end}]
    out = _extend_busy_buffers(busy, [], meta)
    assert (end, end.replace(minute=15)) in out
    assert len(out) == 2  # 原区间 + 会议尾（"写作时间"不加）
    assert _extend_busy_buffers(busy, []) == busy


def test_finder_frequency_guard_vaccine_key():
    # 批 5：疫苗 90 天保守下限——30 天前打过 → frequency 问询（多针拆条目由
    # PROFILER 给明确日期，不落此键判定路径）
    state = _finder_state(agent_entries=[_entry(
        title="打疫苗", duration=30, profile={"category": "医疗健康"})],
        lunch=None, last_medical={"疫苗": (NOW - timedelta(days=30)).isoformat()})
    out = finder_node(state)
    assert out["question"]["kind"] == "frequency"
    assert out["question"]["entries"][0]["title"] == "打疫苗"


def test_rainy_dates_real_shape():
    # 真实形态 = forecast_for_city 裸 list[dict]（行含 date/precip %）
    rows = [{"date": "2026-10-17", "precip": 90}, {"date": "2026-10-18", "precip": 30}]
    assert _rainy_dates(rows) == {date(2026, 10, 17)}
    assert _rainy_dates(None) == set()
    assert _rainy_dates({"rows": rows}) == set()  # 非 list 形态恒空集不炸


def test_needs_weekday_and_commute_predicates():
    assert _needs_weekday({"common_sense_rules": ["政务中心需工作日办理"]})
    assert not _needs_weekday({"common_sense_rules": ["户外注意防晒"]})
    assert _commute_needed({"category": "差旅通勤"})
    assert _commute_needed({"env_deps": ["需要查交通"]})
    assert not _commute_needed({"category": "深度脑力"})
    # 约束域拆分（2026-11-02 真机三轮）：夜跑「外出体力」曾误出「路上」条目
    # ——户外（避雨）≠ 差旅（通勤块），两类约束不再共用一个域
    assert not _commute_needed({"category": "外出体力",
                                "env_deps": ["需要好天气"]})


def test_eff_duration_deep_work_floor():
    deep = {"category": "深度脑力", "energy_drain": "高"}
    assert _eff_duration({"duration": 90, "profile": deep}, {"deep-work"}) == 120
    assert _eff_duration({"duration": 150, "profile": deep}, {"deep-work"}) == 150
    assert _eff_duration({"duration": 90, "profile": deep}, set()) == 90  # 哲学关不抬


# —— 节点级：十道硬约束 —————————————————————————————————————————————————————


def test_finder_weekday_constraint_skips_weekend():
    # 约束 1：政务常识 → 周六/周日全剔，首槽落周一 2026-10-19
    out = finder_node(_finder_state(agent_entries=[_entry(
        title="政务中心办证", duration=60,
        profile={"category": "机械杂事", "common_sense_rules": ["需工作日办理"]})],
        lunch=None))
    assert out["clean"][0]["start"].startswith("2026-10-19")
    assert date.fromisoformat(out["clean"][0]["start"][:10]).weekday() == 0


def test_finder_morning_only_profile_rule():
    # 约束 2：画像常识"需上午"（标题不含就医关键词）→ 起点偏午后时剔到次日晨
    state = _finder_state(agent_entries=[_entry(
        title="诊疗咨询", duration=60, _from=datetime(2026, 10, 17, 13, 0),
        profile={"category": "医疗健康", "common_sense_rules": ["复查需上午"]})],
        lunch=None)
    out = finder_node(state)
    slot = datetime.fromisoformat(out["clean"][0]["start"])
    assert slot == datetime(2026, 10, 18, 8, 0)  # 当天 12:00 后全剔 → 次日时段头


def test_finder_rainy_day_excluded_for_outdoor():
    # 约束 3：户外 + 降水 ≥60% 剔日——今明两日雨、周一放晴 → 首槽周一
    weather = [
        {"date": "2026-10-17", "precip": 90}, {"date": "2026-10-18", "precip": 95},
        {"date": "2026-10-19", "precip": 10}]
    out = finder_node(_finder_state(agent_entries=[_entry(
        title="爬山", duration=240,
        profile={"category": "外出体力", "env_deps": ["需要好天气"]})],
        lunch=None, weather=weather))
    assert out["clean"][0]["start"].startswith("2026-10-19")


def test_finder_deep_work_block_inflates_occupation():
    # 约束 4：deep-work 哲学 ∧ 深度脑力∧高 → 90 分钟按 120 占位（15:30 小会
    # 把 14:00 起的 120 块顶到 16:00；草案 duration 仍报原值 90）
    state = _finder_state(agent_entries=[_entry(
        title="写方案", duration=90, _from=datetime(2026, 10, 17, 14, 0),
        profile={"category": "深度脑力", "energy_drain": "高"})],
        lunch=None, philosophy=("deep-work",),
        busy=[(datetime(2026, 10, 17, 15, 30), datetime(2026, 10, 17, 16, 0))])
    out = finder_node(state)
    assert out["clean"][0]["start"] == "2026-10-17T16:00:00"
    assert out["clean"][0]["duration"] == 90  # 报用户原值，抬升只影响占用


def test_finder_buffer_after_long_chain():
    # 约束 5：≥3h 链尾 12:00 后 30 分钟缓冲 → 首槽 12:30（无缓冲本可 12:00）
    state = _finder_state(agent_entries=[_entry(_from=datetime(2026, 10, 17, 12, 0))],
                          lunch=None,
                          busy=[(datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 12, 0))],
                          long_chain_ends=[datetime(2026, 10, 17, 12, 0)])
    out = finder_node(state)
    assert out["clean"][0]["start"] == "2026-10-17T12:30:00"


def test_finder_commute_lead_time_pushes_slot():
    # 约束 6：差旅条目槽前 30 分钟须空闲——13:45 的小会撞通勤窗把 14:00 顶到
    # 14:30（对照：非通勤类同输入槽 14:00）
    state = _finder_state(agent_entries=[_entry(
        title="拜访客户", duration=60, _from=datetime(2026, 10, 17, 14, 0),
        profile={"category": "差旅通勤"})],
        lunch=None,
        busy=[(datetime(2026, 10, 17, 13, 45), datetime(2026, 10, 17, 13, 55))])
    out = finder_node(state)
    assert out["clean"][0]["start"] == "2026-10-17T14:30:00"
    assert out["clean"][0]["_commute"] == 30  # 通勤条目标记（裁决器出"路上"块）

    plain = _finder_state(agent_entries=[_entry(_from=datetime(2026, 10, 17, 14, 0))],
                          lunch=None,
                          busy=[(datetime(2026, 10, 17, 13, 45),
                                 datetime(2026, 10, 17, 13, 55))])
    assert finder_node(plain)["clean"][0]["start"] == "2026-10-17T14:00:00"


def test_finder_garbage_time_anchor():
    # 约束 8：低能耗短任务锚定午休末 13:30 优先（非锚定对照首槽 08:00）
    low = {"category": "机械杂事", "energy_drain": "低"}
    out = finder_node(_finder_state(agent_entries=[_entry(title="报销贴票", duration=30,
                                                          profile=low)]))
    assert out["clean"][0]["start"] == "2026-10-17T13:30:00"
    hi = dict(low, energy_drain="高")
    out2 = finder_node(_finder_state(agent_entries=[_entry(title="报销贴票", duration=30,
                                                           profile=hi)]))
    assert out2["clean"][0]["start"] == "2026-10-17T08:00:00"


def test_finder_frequency_guard_raises_question():
    # 约束 9：30 天前洗过牙（<180d）→ frequency 问询、条目剔出待排
    state = _finder_state(agent_entries=[_entry(
        title="洗牙", duration=60, profile={"category": "医疗健康"})],
        lunch=None, last_medical={"洗牙": "2026-09-17T10:00:00"})
    out = finder_node(state)
    assert out["question"]["kind"] == "frequency"
    assert out["question"]["entries"][0]["title"] == "洗牙"
    assert out["clean"] == []
    assert state["agent_entries"][0]["_freq_blocked"] is True


def test_finder_frequency_ok_flag_skips_guard():
    # resolution 确认轮置 _freq_ok → 二次防呆跳过（防确认死循环），照常出草案
    state = _finder_state(agent_entries=[_entry(
        title="洗牙", duration=60, profile={"category": "医疗健康"}, _freq_ok=True)],
        lunch=None, last_medical={"洗牙": "2026-09-17T10:00:00"})
    out = finder_node(state)
    assert "question" not in out or out["question"] is None
    assert out["clean"][0]["title"] == "洗牙"


def test_finder_no_slot_on_full_rain_week():
    # 约束 10：全窗雨日 + 户外 → 零候选 no_slot 问询 + dropped 附注
    weather = [{"date": f"2026-10-{d:02d}", "precip": 90} for d in range(17, 25)]
    state = _finder_state(agent_entries=[_entry(
        title="户外团建", duration=120,
        profile={"category": "外出体力", "env_deps": ["需要好天气"]})],
        lunch=None, weather=weather)
    out = finder_node(state)
    assert out["question"]["kind"] == "no_slot"
    assert out["clean"] == []
    assert any("户外团建" in d for d in out["dropped"])


def test_finder_no_budget_waiver_finds_slot():
    # 约束 7+10 联动：当日预算占满 → 顺延次日（2026-10-20 工时分散口径）；
    # _no_budget（"坚持安排"豁免）同状态当天 16:00 尾槽——insisted 重进的
    # 回落通路（no_slot 触发面见全雨用例）
    busy = [(datetime(2026, 10, 17, 8, 0), datetime(2026, 10, 17, 16, 0))]
    blocked = _finder_state(agent_entries=[_entry(title="赶方案", duration=60)],
                            lunch=None, busy=busy)
    out = finder_node(blocked)
    assert out["clean"][0]["start"] == "2026-10-18T08:00:00"

    insisted = _finder_state(agent_entries=[_entry(title="赶方案", duration=60,
                                                   _no_budget=True)],
                             lunch=None, busy=busy)
    out2 = finder_node(insisted)
    assert out2["clean"][0]["start"] == "2026-10-17T16:00:00"


def test_preference_window_filters_candidates():
    """批 5 偏好硬闸：memory_rules「周日下午别排事」→ 候选避开周日 12-18 点
    （首候选 18:00 起，窗内网格点全被滤除——引擎 _gates_ok 单点同口径）。"""
    import services.preference_slots as ps
    ps._CACHE = None  # 解析缓存隔离（防同签名残留，确定性等值本无碍）
    st = _finder_state([_entry(_from=datetime(2026, 10, 18, 13, 0))])
    st["context"]["memory_rules"] = ["[排期偏好·用户设定] 周日下午别排事"]
    out = finder_node(st)
    slots = [datetime.fromisoformat(s["slot"]) for s in out["candidate_slots"]]
    assert slots and slots[0] == datetime(2026, 10, 18, 18, 0)  # 窗内 13-18 全滤
    assert all(s.weekday() != 6 or s.hour >= 18 for s in slots)


def test_from_user_clock_conflict_not_silently_moved():
    """2026-11-05 真机批：用户明示钟点降级条目（_from_user）撞 busy → 转
    conflict 问询点名挡路者（绝不静默挪槽——真机「明天下午三点开个会」曾被
    挪到后天早上 8 点）。"""
    busy = [(datetime(2026, 10, 18, 15, 0), datetime(2026, 10, 18, 16, 0))]
    st = _finder_state(agent_entries=[_entry(title="和运营对方案", duration=60,
                                             _from=datetime(2026, 10, 18, 15, 0),
                                             _from_user=True)], busy=busy)
    st["context"]["busy_meta"] = [{"title": "既定写作块", "start": busy[0][0],
                                   "end": busy[0][1]}]
    out = finder_node(st)
    assert out["question"]["kind"] == "conflict"
    assert out["question"]["clash_titles"] == ["既定写作块"]
    assert not out["clean"]


def test_from_user_clock_bypasses_budget():
    """不撞 busy 的 _from_user 条目：日预算占满也照排原钟点（用户直落同口径
    豁免——软闸不拦意志）；无 _from_user 的 agent 条目同状态顺延次日。"""
    busy = [(datetime(2026, 10, 19, 8, 0), datetime(2026, 10, 19, 16, 0))]  # 周一 480 分钟占满
    st = _finder_state(agent_entries=[_entry(title="开会", duration=60,
                                             _from=datetime(2026, 10, 19, 17, 0),
                                             _from_user=True)], busy=busy)
    out = finder_node(st)
    assert out["clean"] and out["clean"][0]["start"] == "2026-10-19T17:00:00"

    st2 = _finder_state(agent_entries=[_entry(title="开会", duration=60,
                                              _from=datetime(2026, 10, 19, 17, 0))],
                        busy=busy)
    out2 = finder_node(st2)
    assert out2["clean"][0]["start"] != "2026-10-19T17:00:00"  # 预算闸生效顺延
