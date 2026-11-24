"""QA 维度二：管理哲学与个性化设置的强制约束力（七维度端到端测试批）。

断言口径经读码核实：
- 两分钟短路四条件（profiler.py:103-121, 256-262）：two-minute 哲学启用 ∧ 全部
  条目新建 ∧ 每条 est≤2 ∧ pending 非活跃轮；判定先于一切分流（用户明说时间也
  劝导）；输出 = 劝导 final_reply，不落库不出卡；
- 深度块防碎片（finder._eff_duration）：deep-work ∧ 深度脑力 ∧ 高能耗 → 连续块
  下限 120min 抬升（草案 duration 仍报原值）；今天碎片放不下 → 7 天窗内找整块
  日（拒绝拆散的行为形态）；
- 哲学红线硬闸（preference_slots）：可解析形态「周五下午别排事」产硬避让窗；
  严格的自定义哲学可产周五 16:00 至午夜禁窗；普通参考语句不升级。
- 张弛有度缓冲单源 engine.py:52-55：引擎层长/高优条目尾随 15min。
"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm
from services.preference_slots import parse_avoid_windows

from agents.engine import plan_slots
from agents.scheduler import scheduler_app
from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    MultiKeyLLMCapture,
    patch_scheduler_inputs,
)

ANCHOR = datetime(2026, 11, 25, 9, 0)  # 周三
ADJ_KEEP = json.dumps({"selections": [], "explanation": ""}, ensure_ascii=False)


def _prof(entries, reply="好的。"):
    return json.dumps({"entries": entries, "reply": reply}, ensure_ascii=False)


def _entry(title, duration, **profile):
    return {"task_id": None, "origin": "agent", "title": title,
            "duration": duration, "profile": profile or {}}


def _db_task(title, start, end):
    with TestingSessionLocal() as db:
        db.add(Task(title=title[:30], start_time=start, end_time=end,
                    duration_mins=int((end - start).total_seconds() // 60)))
        db.commit()


def _db_todo(title, duration=30):
    """未排期待办行（task_id 进 intake candidates/valid_ids）。"""
    with TestingSessionLocal() as db:
        t = Task(title=title[:30], duration_mins=duration)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


def _invoke(monkeypatch, user_input, entries, *, philosophy=None,
            memory_rules=None, pending=None, settings_overrides=None):
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof(entries),
        KEY_ADJUDICATOR: ADJ_KEEP,
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch, philosophy=philosophy,
                           settings_overrides=settings_overrides)
    state = {"user_input": user_input, "memory_rules": memory_rules or []}
    if pending is not None:
        state["pending"] = pending
    return scheduler_app.invoke(state), cap


# —— 两分钟法则 ————————————————————————————————————————————————————————————


def test_two_minute_short_circuits_all_tiny(monkeypatch):
    """「回复张总一句收到」+ two-minute 哲学 → 劝导立即执行：不排期、不落库、
    不出任何卡。"""
    out, cap = _invoke(monkeypatch, "回复张总一句收到", [
        _entry("回复张总收到", 2, est_minutes=1)], philosophy="two-minute")
    assert len(cap.calls) == cap.count(KEY_PROFILER) == 1
    assert "两分钟" in out["final_reply"] and "立刻" in out["final_reply"]
    assert out.get("draft_plan") == []
    assert out.get("applied_plan") is None
    assert out.get("question_plan") is None


def test_two_minute_includes_explicit_time(monkeypatch):
    """用户指定的执行时刻优先于两分钟法则。"""
    out, _ = _invoke(monkeypatch, "明天上午9点回复张总收到", [
        {"task_id": None, "origin": "user", "title": "回复张总收到",
         "duration": 2, "start": (ANCHOR + timedelta(days=1, hours=0, minutes=0))
         .isoformat(), "explicit_time": "明天上午9点",
         "profile": {"est_minutes": 1}}], philosophy="two-minute")
    assert "已安排" in out["final_reply"]
    assert datetime.fromisoformat(out["applied_plan"][0]["start"]) == ANCHOR + timedelta(days=1)
    assert out["applied_plan"][0]["title"] == "回复张总收到"


def test_two_minute_requires_philosophy_enabled(monkeypatch):
    """未启用 two-minute 哲学 → 同样输入正常出草案（设置开关真实介入）。"""
    out, _ = _invoke(monkeypatch, "回复张总一句收到", [
        _entry("回复张总收到", 2, est_minutes=1)], philosophy=None)
    assert "两分钟" not in out["final_reply"]
    assert (out.get("draft_plan") or []) or (out.get("applied_plan") or [])


def test_two_minute_mixed_entries_no_shortcut(monkeypatch):
    """混合条目（1 条 90min + 1 条 1min）→ 不短路，常规管道。"""
    out, _ = _invoke(monkeypatch, "写方案，顺手回复张总收到", [
        _entry("写方案", 90, category="深度脑力"),
        _entry("回复张总收到", 1, est_minutes=1)], philosophy="two-minute")
    assert "两分钟" not in out["final_reply"]
    assert len(out.get("draft_plan") or []) == 2


def test_two_minute_skips_existing_task(monkeypatch):
    """条目带 task_id（改既有任务排期）→ 不短路（短路只管新建）。"""
    tid = _db_todo("回复张总收到", 2)
    out, _ = _invoke(monkeypatch, "把回复张总这事排一下", [
        {"task_id": tid, "origin": "agent", "title": "回复张总收到",
         "duration": 2, "profile": {"est_minutes": 1}}],
        philosophy="two-minute")
    assert "两分钟" not in out["final_reply"]
    assert len(out.get("draft_plan") or []) == 1


def test_two_minute_pending_round_no_shortcut(monkeypatch):
    """pending 活跃轮（问询结算轮）→ 消费优先，不触发劝导短路。"""
    out, _ = _invoke(monkeypatch, "好的就这样", [
        _entry("回复张总收到", 2, est_minutes=1)], philosophy="two-minute",
        pending={"proposal_id": "p1", "kind": "question", "entries": [],
                 "ask_text": "要排明天吗？", "origin": "scheduler"})
    assert "两分钟" not in out["final_reply"]


# —— 深度工作防碎片化 ——————————————————————————————————————————————————————


def _scatter_today_gaps():
    """今天（锚日）铺 3×40min 空档：09:00-09:40 / 14:10-14:50 / 16:00-16:40。"""
    d = ANCHOR
    _db_task("块A", d.replace(hour=8), d.replace(hour=9))
    _db_task("块B", d.replace(hour=9, minute=40), d.replace(hour=11, minute=30))
    _db_task("块C", d.replace(hour=13, minute=30), d.replace(hour=14, minute=10))
    _db_task("块D", d.replace(hour=14, minute=50), d.replace(hour=16))
    _db_task("块E", d.replace(hour=16, minute=40), d.replace(hour=22))


def test_deep_work_refuses_fragmenting(monkeypatch):
    """deep-work + 2h 深度任务 vs 3×40min 碎片 → 拒绝拆散：今天零候选，
    7 天窗内整块日（明天）出草案，绝不塞进 40min 碎片。"""
    _scatter_today_gaps()
    out, _ = _invoke(monkeypatch, "安排写一份下季度的底层架构重构方案", [
        _entry("底层架构重构方案", 120, category="深度脑力", energy_drain="高")],
        philosophy="deep-work")
    drafts = out.get("draft_plan") or []
    assert len(drafts) == 1 and drafts[0]["duration"] == 120, drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.date() > ANCHOR.date(), f"碎片化失守：落进了今天 {start}"
    assert start.hour == 8  # 明天日首整块


def test_deep_lift_requires_all_conditions(monkeypatch):
    """对照：30min 中能耗杂事可入今天 40min 空档（120min 抬升只在 deep-work
    ∧ 深度脑力 ∧ 高能耗三条件齐备时发生；低能耗会触发垃圾锚定是另一约束）。"""
    _scatter_today_gaps()
    out, _ = _invoke(monkeypatch, "抽空整理下报销单", [
        _entry("整理报销单", 30, category="机械杂事", energy_drain="中")],
        philosophy="deep-work",
        settings_overrides={"work_minutes_per_day": 1440})
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]).date() == ANCHOR.date()


# —— 自定义哲学红线 ————————————————————————————————————————————————————————


def test_custom_philosophy_hardgate_parseable_form(monkeypatch):
    """可解析红线「周五下午别排事」→ 硬避让窗：周三/周四占满，周五只留
    12:00-18:00 空档 → 红线剔午后唯一候选消失，草案挪周六日首。"""
    fri = ANCHOR + timedelta(days=2)  # 11-27 周五
    for d in (ANCHOR, ANCHOR + timedelta(days=1)):
        _db_task("全天占用", d.replace(hour=8), d.replace(hour=22))
    _db_task("周五上午满", fri.replace(hour=8), fri.replace(hour=12))
    _db_task("周五晚间满", fri.replace(hour=18), fri.replace(hour=22))
    out, _ = _invoke(monkeypatch, "这两天安排个产品评审", [
        _entry("产品评审", 60)], memory_rules=["周五下午别排事"])
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    in_window = (start.weekday() == 4 and 12 <= start.hour < 18)
    assert not in_window, f"红线失守：草案落在周五下午 {start}"
    assert start.date() == (fri + timedelta(days=1)).date()  # 挪到周六


def test_custom_philosophy_without_rule_fills_friday_pm(monkeypatch):
    """对照组：无红线 → 同一日历布局下周五 13:30 正常出草案（证明上例是
    规则闸介入而非日历巧合）。"""
    fri = ANCHOR + timedelta(days=2)
    for d in (ANCHOR, ANCHOR + timedelta(days=1)):
        _db_task("全天占用", d.replace(hour=8), d.replace(hour=22))
    _db_task("周五上午满", fri.replace(hour=8), fri.replace(hour=12))
    _db_task("周五晚间满", fri.replace(hour=18), fri.replace(hour=22))
    out, _ = _invoke(monkeypatch, "这两天安排个产品评审", [
        _entry("产品评审", 60)],
        settings_overrides={"work_minutes_per_day": 1440})
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]) == \
        fri.replace(hour=13, minute=30)


@pytest.mark.parametrize(("clock", "duration", "blocked"), [("15:00", 60, False), ("15:30", 60, True), ("16:00", 30, True)])
def test_custom_friday_explicit_boundary(monkeypatch, clock, duration, blocked):
    from agents.prompts import personality_rules

    presets = "two-minute,deep-work"
    rules = personality_rules(None, presets, "周五下午4点后绝不安排任何工作。")
    start = datetime.fromisoformat("2026-11-27T" + clock)
    out, _ = _invoke(monkeypatch, f"周五{clock}安排产品评审", [{
        "title": "产品评审", "origin": "user", "start": start.isoformat(),
        "duration": duration, "explicit_time": clock}], philosophy=presets, memory_rules=rules)
    if blocked:
        assert not out.get("applied_plan") and not out.get("draft_plan")
        assert out.get("question_plan") and "16:00" in out["final_reply"]
    else:
        assert [e["start"] for e in out["applied_plan"]] == [start.isoformat()]


@pytest.mark.parametrize("rule_enabled", [False, True])
def test_custom_friday_finder_only_late_slot(monkeypatch, rule_enabled):
    from agents.prompts import personality_rules

    friday = ANCHOR + timedelta(days=2)
    for d in (ANCHOR, ANCHOR + timedelta(days=1)):
        _db_task("全天占用", d.replace(hour=8), d.replace(hour=22))
    _db_task("周五已有工作", friday.replace(hour=8), friday.replace(hour=15, minute=30))
    presets = "two-minute,deep-work"
    rules = personality_rules(None, presets, "周五16:00后绝不安排任何工作" if rule_enabled else None)
    out, _ = _invoke(monkeypatch, "找个时间做产品评审", [_entry("产品评审", 60)],
                     philosophy=presets, memory_rules=rules, settings_overrides={"work_minutes_per_day": 1440})
    start = datetime.fromisoformat(out["draft_plan"][0]["start"])
    assert start.date() == (friday + timedelta(days=int(rule_enabled))).date()
    if not rule_enabled:
        assert start + timedelta(hours=1) > friday.replace(hour=16)


def test_custom_friday_only_strict_user_rule_is_hard():
    hard = "[自定义哲学] 周五16:00后绝不安排任何工作"
    assert [(w["weekday"], w["start_min"], w["end_min"]) for w in parse_avoid_windows([hard])] == [(4, 960, 1440)]
    for text in (hard.replace("[自定义哲学] ", ""), hard.replace("绝不", "尽量不"), hard + "，除非有急事"):
        assert parse_avoid_windows([text]) == []


def test_clock_level_red_line_reaches_prompts(monkeypatch):
    """软约束面：钟点原话规则文本必须进 PROFILER payload（模型层仍可见），
    硬闸缺席时这是唯一防线——文本缺达即连软约束也没有。"""
    rule = "[排期偏好·用户设定] 周五下午4点后绝不安排任何工作"
    out, cap = _invoke(monkeypatch, "这两天安排个产品评审", [
        _entry("产品评审", 60)], memory_rules=[rule])
    profiler_users = cap.users(KEY_PROFILER)
    assert profiler_users and "绝不安排任何工作" in profiler_users[0]


# —— 张弛有度（work-rest-balance）缓冲 —————————————————————————————————————


def test_engine_tail_buffer_after_long_or_high_prio():
    """引擎层尾随缓冲：≥90min 或高优条目后 +15min，同批后续不贴身
    （夜间预排/单卡/批量全入口继承的张弛有度语义）。"""
    slots = plan_slots(
        entries=[
            {"key": "k1", "title": "深度评审会", "priority": 1, "duration": 90},
            {"key": "k2", "title": "整理纪要", "priority": 2, "duration": 30},
        ],
        busy=[], now=ANCHOR, day_start_min=480, day_end_min=1320,
        lunch=(690, 810), use_ai=False)
    s1, s2 = slots["k1"], slots["k2"]
    assert s1 == ANCHOR
    assert s2 >= s1 + timedelta(minutes=90 + 15), (s1, s2)


def test_engine_no_tail_after_short_mid_prio():
    """短且中优条目无尾随缓冲（缓冲只跟长/高优——张弛有度不误伤紧凑节奏）。"""
    slots = plan_slots(
        entries=[
            {"key": "k1", "title": "站会", "priority": 2, "duration": 30},
            {"key": "k2", "title": "回邮件", "priority": 2, "duration": 30},
        ],
        busy=[], now=ANCHOR, day_start_min=480, day_end_min=1320,
        lunch=(690, 810), use_ai=False)
    assert slots["k2"] == slots["k1"] + timedelta(minutes=30)
