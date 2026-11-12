"""统一排期引擎测试（批 1：agents/engine.py 三层架构 + services 规则层提升）。

替身口径：AI 微调层 monkeypatch services.llm.call_llm（engine._refine_with_llm
函数内 import 源模块，patch 源模块即拦截；漏 patch 会真探激活模型开 dev 库）；
库走 TestingSessionLocal 真内存库（fresh_db autouse 建表）。

时间口径：纯函数用例（suggest_slot / is_valid_slot / plan_slots）的 now 是
显式参数，用固定钟点可复现；库形态用例（auto_schedule_run / apply_slot_
decisions）内部取真实 now，slot 一律用"明天"派生保证恒为未来。
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import agents.engine as engine_mod
from agents.prompts import SLOT_REFINE_PROMPT
from conftest import TestingSessionLocal
from schemas import TaskCreate
from services import llm
from services.llm import NoActiveModelError
from services.settings import get_settings
from services.tasks import (
    AlreadyScheduledError,
    NoSlotError,
    apply_slot_decisions,
    create_task,
    day_busy_minutes,
    get_task,
    is_valid_slot,
    suggest_slot,
    title_morning_only,
)

# 纯函数用例的固定钟点：2026-10-20（周二）10:00
NOW = datetime(2026, 10, 20, 10, 0)
# 默认设置的排期轴：8:00-22:00 可排、11:30-13:30 午休
AXIS = dict(day_start_min=480, day_end_min=1320, lunch=(690, 810))


def _tomorrow(hour: int, minute: int = 0) -> datetime:
    """库形态用例的时刻派生：明天 hour:minute（恒为真实 now 的未来）。"""
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)


def _entry(key="e1", title="写周报", duration=60, **kw):
    """plan_slots 条目替身（同批最早优先模式）。"""
    base = {"key": key, "title": title, "priority": 2, "duration": duration,
            "from": None, "same_clock_first": False}
    base.update(kw)
    return base


class _RefineStub:
    """AI 微调层替身：只认 SLOT_REFINE_PROMPT 身份（引擎不该调别的提示词），
    返回预设 reply 或抛预设异常；记录调用次数供断言。"""

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, 0

    def __call__(self, messages, *, json_mode=False, max_tokens=None):
        assert SLOT_REFINE_PROMPT in messages[0]["content"]
        assert json_mode, "微调层必须走 json_mode"
        self.calls += 1
        self.max_tokens = max_tokens
        if self.exc is not None:
            raise self.exc
        return self.reply if self.reply is not None else "{}"


# —— 规则层（services.tasks.suggest_slot 公开版） ————————————————————————————


def test_suggest_slot_earliest_first_when_not_same_clock():
    # same_clock_first=False：当日 10:00 起 60 分钟不撞午休 → 当天 10:00
    got = suggest_slot(NOW, 60, [], same_clock_first=False, **AXIS)
    assert got == NOW


def test_suggest_slot_same_clock_next_day_first():
    # same_clock_first=True：次日同钟点 10:00 优先于当日网格（conflict 语感）
    got = suggest_slot(NOW, 60, [], same_clock_first=True, **AXIS)
    assert got == NOW + timedelta(days=1)


def test_suggest_slot_morning_gate_moves_to_next_am():
    # 就医类（morning_only）：起点 14:00 超上午上界 → 网格从次日 8:00 起
    got = suggest_slot(datetime(2026, 10, 20, 14, 0), 60, [],
                       morning_only=True, same_clock_first=False, **AXIS)
    assert got == datetime(2026, 10, 21, 8, 0)


def test_suggest_slot_none_when_days_full():
    # 7 天每天 8:00-22:00 全被占 → 无空档 None（duration 也放不进任何缝隙）
    busy = [(datetime(2026, 10, 20).replace(hour=0) + timedelta(days=i,
            minutes=480), datetime(2026, 10, 20).replace(hour=0) + timedelta(
            days=i, minutes=1320)) for i in range(8)]
    assert suggest_slot(NOW, 60, busy, same_clock_first=False, **AXIS) is None


def test_title_morning_only_matches_keywords():
    assert title_morning_only("年度体检")
    assert title_morning_only("空腹抽血复查")
    assert not title_morning_only("写周报")


# —— 每日工时预算（2026-10-20 增：批量排期不再全堆同一天的规则层硬保证） ————————


def test_day_busy_minutes_splits_midnight():
    # 跨午夜区间按天拆分：22:00-次日 8:00 → 前日 120 分钟 + 次日 480 分钟
    loads = day_busy_minutes([
        (datetime(2026, 10, 20, 22, 0), datetime(2026, 10, 21, 8, 0))])
    assert loads == {date(2026, 10, 20): 120, date(2026, 10, 21): 480}


def test_suggest_slot_daily_budget_spills_to_next_day():
    # 今天已被 busy 占 480 分钟（= 工时预算）→ 60 分钟任务顺延明天 8:00
    busy = [(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 16, 0))]
    got = suggest_slot(NOW, 60, busy, same_clock_first=False, **AXIS,
                       day_load=day_busy_minutes(busy), day_budget=480)
    assert got == datetime(2026, 10, 21, 8, 0)


def test_suggest_slot_daily_budget_empty_day_exemption():
    # 空日豁免：10 小时任务超预算，但当日尚无占用 → 放行（超大任务不至于
    # 无处可排）；lunch=None 排除午休轴干扰（默认午休下 600 分钟本就放不进）
    got = suggest_slot(NOW, 600, [], same_clock_first=False,
                       day_start_min=480, day_end_min=1320, lunch=None,
                       day_load={}, day_budget=480)
    assert got == NOW


def test_is_valid_slot_daily_budget():
    # 校验层与规则层同口径：当日占用 480 + 本条 60 > 预算 480 → 非法；放宽到 540 → 合法
    busy = [(datetime(2026, 10, 21, 8, 0), datetime(2026, 10, 21, 16, 0))]
    load = day_busy_minutes(busy)
    assert not is_valid_slot(datetime(2026, 10, 21, 16, 0), 60, busy, now=NOW,
                             **AXIS, day_load=load, day_budget=480)
    assert is_valid_slot(datetime(2026, 10, 21, 16, 0), 60, busy, now=NOW,
                         **AXIS, day_load=load, day_budget=540)


# —— 校验层（is_valid_slot：AI 微调结果的统一后验闸） ————————————————————————


def test_is_valid_slot_accepts_clean_future_slot():
    assert is_valid_slot(NOW + timedelta(days=1), 60, [], now=NOW, **AXIS)


def test_is_valid_slot_rejects_past_tz_lunch_and_busy():
    past = NOW - timedelta(hours=1)
    assert not is_valid_slot(past, 60, [], now=NOW, **AXIS)  # 过去
    aware = NOW.replace(tzinfo=timezone(timedelta(hours=8)))
    assert not is_valid_slot(aware, 60, [], now=NOW, **AXIS)  # 带时区
    lunch = datetime(2026, 10, 21, 12, 0)  # 12:00-13:00 全在午休 11:30-13:30 内
    assert not is_valid_slot(lunch, 60, [], now=NOW, **AXIS)
    busy = [(datetime(2026, 10, 21, 10, 0), datetime(2026, 10, 21, 11, 0))]
    assert not is_valid_slot(datetime(2026, 10, 21, 10, 30), 60, busy, now=NOW, **AXIS)


def test_is_valid_slot_morning_gate_rejects_pm():
    # 就医类 14:00 即使不撞任何占用也非法——常识硬闸（体检没有下午做的）
    pm = datetime(2026, 10, 21, 14, 0)
    assert not is_valid_slot(pm, 60, [], now=NOW, morning_only=True, **AXIS)
    assert is_valid_slot(datetime(2026, 10, 21, 10, 0), 60, [], now=NOW,
                         morning_only=True, **AXIS)


# —— plan_slots：三层架构 ———————————————————————————————————————————————————


def test_plan_slots_rule_only_and_same_batch_exclusive():
    # use_ai=False：纯规则层，同批条目互不重叠（第二条避开第一条与午休）
    finals = engine_mod.plan_slots(
        entries=[_entry("e1"), _entry("e2")], busy=[], now=NOW,
        use_ai=False, **AXIS)
    assert finals == {"e1": NOW, "e2": datetime(2026, 10, 20, 13, 30)}


def test_plan_slots_ai_adjust_applied(monkeypatch):
    # AI 微调合法（明天 20:00，不撞占用）→ 采纳并覆盖规则槽
    stub = _RefineStub(reply=json.dumps(
        {"slots": [{"id": "e1", "start": "2026-10-21T20:00:00"}]}))
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1")], busy=[], now=NOW, **AXIS)
    assert finals == {"e1": datetime(2026, 10, 21, 20, 0)}
    assert stub.calls == 1


def test_plan_slots_ai_invalid_falls_back_to_rule(monkeypatch):
    # AI 微调非法（早于 now）→ 该条回落规则槽，引擎不空手
    stub = _RefineStub(reply=json.dumps(
        {"slots": [{"id": "e1", "start": "2026-10-20T08:00:00"}]}))
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1")], busy=[], now=NOW, **AXIS)
    assert finals == {"e1": NOW}


def test_plan_slots_ai_error_skips_whole_layer(monkeypatch):
    # AI 层异常（零激活态 / 网络炸）→ 整层静默跳过，全批规则槽，不 raise
    stub = _RefineStub(exc=NoActiveModelError())
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1"), _entry("e2")], busy=[], now=NOW, **AXIS)
    assert finals == {"e1": NOW, "e2": datetime(2026, 10, 20, 13, 30)}


def test_plan_slots_ai_hallucinated_id_and_garbage_ignored(monkeypatch):
    # 幻觉 id 与非法时刻（带时区）逐条丢弃，合法部分照常采纳
    stub = _RefineStub(reply=json.dumps({"slots": [
        {"id": "ghost", "start": "2026-10-21T20:00:00"},
        {"id": "e1", "start": "2026-10-21T20:00:00+08:00"},  # 带时区 → 丢
        {"id": "e1", "start": "not-a-time"},  # 解析失败 → 丢
        {"id": "e1", "start": "2026-10-21T20:00:00"},  # 合法 → 采纳
    ]}))
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1")], busy=[], now=NOW, **AXIS)
    assert finals == {"e1": datetime(2026, 10, 21, 20, 0)}


def test_plan_slots_ai_refine_respects_same_batch_exclusive(monkeypatch):
    # AI 想把两条都挪到同一空档：同批互斥只放行先到者，后者回落规则槽
    same = json.dumps({"slots": [{"id": "e1", "start": "2026-10-21T20:00:00"},
                                 {"id": "e2", "start": "2026-10-21T20:00:00"}]})
    monkeypatch.setattr(llm, "call_llm", _RefineStub(reply=same))
    finals = engine_mod.plan_slots(
        entries=[_entry("e1"), _entry("e2")], busy=[], now=NOW, **AXIS)
    assert finals["e1"] == datetime(2026, 10, 21, 20, 0)
    assert finals["e2"] == datetime(2026, 10, 20, 13, 30)  # 撞已采纳的 20:00 → 回落


def test_plan_slots_ai_refine_frees_rule_slot_for_later_entries(monkeypatch):
    # 微调挪走 e1（10:00→20:00）后旧规则槽必须退场（2026-10-21 复查批 A 修）：
    # 旧版只 append 新槽，旧槽残影在 busy_acc 里挡住 e2 挪进 10:00 空档
    stub = _RefineStub(reply=json.dumps({"slots": [
        {"id": "e1", "start": "2026-10-20T20:00:00"},
        {"id": "e2", "start": "2026-10-20T10:00:00"}]}))
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1", duration=60), _entry("e2", duration=60)],
        busy=[], now=NOW, **AXIS)
    assert finals["e1"] == datetime(2026, 10, 20, 20, 0)
    assert finals["e2"] == datetime(2026, 10, 20, 10, 0)  # e1 腾出的空档可进
    assert stub.calls == 1
    assert stub.max_tokens == 800  # 微调层显式上限（2026-10-21 补）


def test_plan_slots_morning_gate_overrides_ai(monkeypatch):
    # AI 把就医类挪到下午 → 校验层上午硬闸拒绝，回落规则槽（规则硬闸兜底 AI）；
    # 起点 now=10:00 当天上午仍有整空档 → 规则槽就是当天 10:00
    stub = _RefineStub(reply=json.dumps(
        {"slots": [{"id": "e1", "start": "2026-10-21T14:00:00"}]}))
    monkeypatch.setattr(llm, "call_llm", stub)
    finals = engine_mod.plan_slots(
        entries=[_entry("e1", title="体检")], busy=[], now=NOW, **AXIS)
    assert finals["e1"] == NOW


def test_plan_slots_daily_budget_spreads_batch():
    # 预算 480 分钟 + 10 条 60 分钟：今天装 8 条（10:00-11:00 与 13:30-20:30，
    # 中间避午休）即塞满工时，第 9、10 条自动顺延明天 8:00 / 9:00——分散生效
    entries = [_entry(f"e{i}", duration=60) for i in range(10)]
    finals = engine_mod.plan_slots(
        entries=entries, busy=[], now=NOW, use_ai=False, **AXIS, day_budget=480)
    today = [v for v in finals.values() if v is not None and v.date() == NOW.date()]
    assert len(today) == 8
    assert finals["e8"] == datetime(2026, 10, 21, 8, 0)
    assert finals["e9"] == datetime(2026, 10, 21, 9, 0)


# —— auto_schedule_run：收件箱入口编排 ——————————————————————————————————————


def _mk_task(db, title, **kw):
    t = create_task(db, TaskCreate(title=title, **kw))
    db.commit()
    return t


def test_auto_schedule_single_pure_rule(monkeypatch):
    # 单卡纯规则直落（2026-10-20 决策：毫秒级响应，AI 微调退役）：替身一被
    # 调用即炸 AssertionError——哨兵证明全链路零 LLM
    with TestingSessionLocal() as db:
        t = _mk_task(db, "写周报")
        monkeypatch.setattr(
            llm, "call_llm", _RefineStub(exc=AssertionError("单卡纯规则不得调 LLM")))
        d = engine_mod.auto_schedule_run(db, t.id)
        assert d["task_id"] == t.id and d["slot"] is not None
        assert d["slot"] >= datetime.now().replace(hour=0, minute=0)


def test_auto_schedule_unknown_task_raises_keyerror():
    with TestingSessionLocal() as db:
        with pytest.raises(KeyError):
            engine_mod.auto_schedule_run(db, "no-such-id")


def test_auto_schedule_already_scheduled_raises():
    with TestingSessionLocal() as db:
        t = _mk_task(db, "已排期的日程", duration_mins=60,
                     start_time=_tomorrow(9), end_time=_tomorrow(10))
        with pytest.raises(AlreadyScheduledError):
            engine_mod.auto_schedule_run(db, t.id)


def test_auto_schedule_single_no_slot_raises():
    # 时长超过单日可排时段（840 分钟）→ 7 天都无空档 → 单卡严格 NoSlotError
    with TestingSessionLocal() as db:
        t = _mk_task(db, "超大任务", duration_mins=850)
        with pytest.raises(NoSlotError):
            engine_mod.auto_schedule_run(db, t.id)


# —— apply_slot_decisions：顺序落库 ——————————————————————————————————————————


def test_apply_slot_decisions_persists_and_isolates_none():
    with TestingSessionLocal() as db:
        t_ok = _mk_task(db, "能安排的待办")
        t_no = _mk_task(db, "没空档的待办")
        slot = _tomorrow(9)
        scheduled, failed = apply_slot_decisions(db, [
            {"task_id": t_ok.id, "title": t_ok.title, "slot": slot},
            {"task_id": t_no.id, "title": t_no.title, "slot": None},
        ])
        assert [t.id for t in scheduled] == [t_ok.id]
        got = get_task(db, t_ok.id)
        assert got.start_time == slot and got.end_time == slot + timedelta(minutes=60)
        assert got.ever_scheduled is True
        assert len(failed) == 1
        assert failed[0]["id"] == t_no.id and "空档" in failed[0]["reason"]


def test_apply_slot_decisions_rejects_stale_slot():
    # 决策与落库之间 slot 被占（并发纵深防御）：fresh busy 重验不过 → failed
    with TestingSessionLocal() as db:
        _mk_task(db, "占位日程", duration_mins=60,
                 start_time=_tomorrow(9), end_time=_tomorrow(10))
        t = _mk_task(db, "目标待办")
        scheduled, failed = apply_slot_decisions(db, [
            {"task_id": t.id, "title": t.title, "slot": _tomorrow(9)}])
        assert scheduled == [] and len(failed) == 1
        assert get_task(db, t.id).start_time is None


def test_apply_slot_decisions_skips_already_scheduled():
    # 落库层双保险：目标在决策后已被他处排期 → failed 隔离不中断整批
    with TestingSessionLocal() as db:
        t1 = _mk_task(db, "待办一")
        t2 = _mk_task(db, "待办二")
        slot = _tomorrow(9)
        # 模拟决策后 t2 被并发排期
        scheduled, failed = apply_slot_decisions(db, [
            {"task_id": t1.id, "title": t1.title, "slot": slot},
            {"task_id": t2.id, "title": t2.title, "slot": slot},
        ])
        # t1 落库成功后 t2 的同槽 fresh busy 撞 t1 → failed（顺序避让）
        assert [t.id for t in scheduled] == [t1.id]
        assert failed[0]["id"] == t2.id


def test_plan_slots_tail_buffer_for_long_tasks():
    """尾随缓冲收口引擎（优化批 6）：≥90 分钟条目后 +15 分钟虚拟延长——同批
    下一条不再贴身排；短条目无缓冲；缓冲不进日预算（休息不是工时）。"""
    from datetime import datetime

    from agents.engine import plan_slots

    day0 = datetime(2026, 11, 2, 8, 0)
    out = plan_slots(
        entries=[{"key": "long", "title": "深度评审", "duration": 120},
                 {"key": "short", "title": "回复消息", "duration": 30}],
        busy=[], now=day0, day_start_min=480, day_end_min=1320,
        lunch=(690, 810), use_ai=False)
    long_end = out["long"] + timedelta(minutes=120)
    assert out["short"] >= long_end + timedelta(minutes=15)  # 缓冲让位

    out2 = plan_slots(  # 两条短任务：无缓冲照常贴身
        entries=[{"key": "a", "title": "甲", "duration": 30},
                 {"key": "b", "title": "乙", "duration": 30}],
        busy=[], now=day0, day_start_min=480, day_end_min=1320,
        lunch=(690, 810), use_ai=False)
    assert out2["b"] == out2["a"] + timedelta(minutes=30)
