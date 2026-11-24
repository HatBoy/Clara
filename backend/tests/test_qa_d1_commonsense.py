"""QA 维度一：Track A 排期引擎常识与环境推理（七维度端到端测试批 2026-11-23）。

断言口径经读码核实（见 docs/qa-bug-checklist.md 薄弱环节对账）：
- 医疗上午闸 = MORNING_KEYWORDS 标题子串 + 医疗画像常识「上午」（finder agent
  路径硬闸；明确空腹体检指定下午时单独问询，不覆盖用户钟点）；
- 频率防呆 = FREQ_GUARD_DAYS {体检365, 洗牙180, 疫苗90} + last_similar_occurrence
  （真库历史行 + 相似度 0.6）→ frequency 问询；
- 天气 = RAINY_PRECIP=60 雨日剔日（外出体力类）→ no_slot / 改日落无雨日；
- 精力匹配 = deep-work 哲学 ∧ 深度脑力 ∧ 高能耗 → 120min 整块抬升（上午黄金段
  最早优先）；机械杂事/低耗 ≤30min → 垃圾时间锚定（午休末 810 优先）；
- 恢复窗 = 爬山|徒步|马拉松|越野|晚宴|团建|聚餐 次日 0:00-10:00 并 busy；
- 缓冲 = 会议尾 15 / 会前 10 / 链尾 30；用户明示钟点撞 busy → conflict 绝不静默挪。

时间锚 2026-11-25（周三）09:00；日历窗 08:00-22:00，午休 11:30-13:30。
"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm

from agents.scheduler import scheduler_app
from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    MultiKeyLLMCapture,
    patch_scheduler_inputs,
)

ANCHOR = datetime(2026, 11, 25, 9, 0)  # 周三
ADJ_KEEP = json.dumps({"selections": [], "explanation": "维持引擎主荐"},
                      ensure_ascii=False)


def _prof(entries, reply="好的。"):
    return json.dumps({"entries": entries, "reply": reply}, ensure_ascii=False)


def _agent(title, duration=60, **profile):
    """引擎待排条目（无用户钟点 → finder 找槽出草案）。"""
    return {"task_id": None, "origin": "agent", "title": title,
            "duration": duration, "profile": profile or {}}


def _user_at(title, start, explicit):
    """用户明示钟点条目（过三重复核 → 直落 applied；撞 busy → conflict）。"""
    return {"task_id": None, "origin": "user", "title": title,
            "duration": 60, "start": start.isoformat(),
            "explicit_time": explicit}


def _db_task(title, start, end, *, priority=2):
    with TestingSessionLocal() as db:
        db.add(Task(title=title[:30], start_time=start, end_time=end,
                    duration_mins=int((end - start).total_seconds() // 60),
                    priority=priority))
        db.commit()


def _weather(days_ahead_precip: dict[int, int], base: datetime = ANCHOR):
    """构造 forecast_for_city 返回行（date/precip 两键是消费面）。"""
    return [{"date": (base + timedelta(days=d)).date().isoformat(),
             "text": "雨" if p else "晴", "tmax": 20, "tmin": 10, "precip": p}
            for d, p in days_ahead_precip.items()]


def _invoke(monkeypatch, user_input, profiler_entries, *, philosophy=None,
            weather=None, anchor=ANCHOR, settings_overrides=None):
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof(profiler_entries),
        KEY_ADJUDICATOR: ADJ_KEEP,
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch, philosophy=philosophy, weather=weather,
                           anchor=anchor, settings_overrides=settings_overrides)
    out = scheduler_app.invoke({"user_input": user_input, "memory_rules": []})
    return out, cap


# —— 医学常识：空腹体检 ———————————————————————————————————————————————————


def test_medical_morning_gate_forces_am_slot(monkeypatch):
    """无明示钟点的「空腹体检」→ 医疗上午闸：草案只落上午，绝不落下午。"""
    out, _ = _invoke(monkeypatch, "安排一下常规空腹体检", [
        _agent("空腹体检", 60, category="医疗健康", energy_drain="中",
               common_sense_rules=["体检需空腹，宜上午安排"])])
    drafts = out.get("draft_plan") or []
    assert drafts and drafts[0]["title"] == "空腹体检"
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.hour < 12, f"上午闸失守：落到了 {start.isoformat()}"
    assert not out.get("question_plan")


def test_medical_title_keyword_hits_gate_without_profile(monkeypatch):
    """画像缺省时标题关键词兜底：「体检」∈ MORNING_KEYWORDS 仍走上午闸。"""
    out, _ = _invoke(monkeypatch, "帮我约个体检", [_agent("年度体检", 60)])
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]).hour < 12


MEDICAL_NOW = datetime(2026, 11, 24, 9)
MEDICAL_PM = datetime(2026, 11, 25, 14)


@pytest.mark.parametrize(("reply", "action", "expected"), [
    ("保留下午", "confirmed", MEDICAL_PM),
    ("坚持原时间", "insisted", MEDICAL_PM),
    ("改到明天上午9点", "rescheduled", datetime(2026, 11, 25, 9)),
    ("好的", "rescheduled", None),  # 即使模型擅自补 9 点也不写入
])
def test_medical_afternoon_requires_explicit_choice(monkeypatch, reply, action, expected):
    out, _ = _invoke(monkeypatch, "明天下午2点安排常规空腹体检", [
        _user_at("体检", MEDICAL_PM, "明天下午2点")], anchor=MEDICAL_NOW)
    assert not out.get("applied_plan") and not out.get("draft_plan")
    assert "上午" in out["final_reply"] and "体检机构" in out["final_reply"]
    pending = {**out["question_plan"], "proposal_id": "q1"}
    response = {"resolution": {"action": action, "entries": [
        _user_at("体检", datetime(2026, 11, 25, 9), "明天上午9点")]}}
    monkeypatch.setattr(llm, "call_llm", MultiKeyLLMCapture({
        KEY_PROFILER: json.dumps(response, ensure_ascii=False)}))
    settled = scheduler_app.invoke({"user_input": reply, "pending": pending, "memory_rules": []})
    applied = settled.get("applied_plan") or []
    if expected is None:
        assert not applied and not settled.get("draft_plan") and settled.get("question_plan")
    else:
        assert [e["start"] for e in applied] == [expected.isoformat()]


@pytest.mark.parametrize("description", ["无需空腹的体检", "非空腹体检", "常规体检", "遵医嘱指定下午的空腹体检"])
def test_medical_exceptions_keep_requested_time(monkeypatch, description):
    out, _ = _invoke(monkeypatch, "明天下午2点安排" + description, [
        _user_at("体检", MEDICAL_PM, "明天下午2点")], anchor=MEDICAL_NOW)
    assert [e["start"] for e in out["applied_plan"]] == [MEDICAL_PM.isoformat()]
    assert not out.get("question_plan") and "上午" not in out["final_reply"]


def test_freq_guard_blocks_recent_checkup(monkeypatch):
    """90 天前做过「年度体检」→ 再排「体检」触发 frequency 防重问询，不出草案。"""
    last = datetime.now() - timedelta(days=90)
    _db_task("年度体检", last, last + timedelta(hours=1))
    out, _ = _invoke(monkeypatch, "安排一下体检", [_agent("体检", 60)])
    q = out.get("question_plan")
    assert q and q["kind"] == "frequency"
    assert q.get("interval_days") == 365
    assert not (out.get("draft_plan") or [])


def test_freq_guard_allows_after_interval(monkeypatch):
    """400 天前做过的体检 → 超出 365 天防呆窗，正常出草案不再拦。"""
    last = datetime.now() - timedelta(days=400)
    _db_task("年度体检", last, last + timedelta(hours=1))
    out, _ = _invoke(monkeypatch, "安排一下体检", [_agent("体检", 60)])
    drafts = out.get("draft_plan") or []
    assert drafts and not out.get("question_plan")


def test_freq_guard_vaccine_90d(monkeypatch):
    """疫苗 90 天防呆：60 天前打过 → 问询；防重不误伤洗牙窗。"""
    last = datetime.now() - timedelta(days=60)
    _db_task("疫苗接种", last, last + timedelta(minutes=30))
    out, _ = _invoke(monkeypatch, "安排打疫苗", [_agent("疫苗加强针", 30)])
    q = out.get("question_plan")
    assert q and q["kind"] == "frequency"
    assert q.get("interval_days") == 90


# —— 外部环境联动：天气 ———————————————————————————————————————————————————


def test_rain_blocks_outdoor_entire_window(monkeypatch):
    """未来 8 天全暴雨 + 户外体力类 → 雨日全剔 → no_slot 问询，绝不照排雨日。"""
    weather = _weather({d: 80 for d in range(8)})
    out, _ = _invoke(monkeypatch, "这周找个时间去爬山", [
        _agent("周末爬山", 120, category="外出体力", energy_drain="高",
               env_deps=["需要好天气"])], weather=weather)
    q = out.get("question_plan")
    assert q and q["kind"] == "no_slot"
    assert not (out.get("draft_plan") or [])


def test_rain_redirects_to_clear_day(monkeypatch):
    """今明暴雨、后天起放晴 → 草案跳过雨日落无雨日（户外类约束 3）。"""
    weather = _weather({0: 80, 1: 80, 2: 10, 3: 10, 4: 10})
    out, _ = _invoke(monkeypatch, "这周找个时间去爬山", [
        _agent("周末爬山", 120, category="外出体力", energy_drain="高",
               env_deps=["需要好天气"])], weather=weather)
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.date() >= (ANCHOR + timedelta(days=2)).date()


@pytest.mark.parametrize("rain, expected_day", [(0, 0), (90, 1)])
def test_same_hike_changes_day_only_when_forecast_changes(monkeypatch, rain, expected_day):
    out, _ = _invoke(monkeypatch, "找个时间去爬山", [
        _agent("爬山", 60, category="外出体力", env_deps=["需要好天气"])],
        weather=_weather({0: rain, 1: 0}))
    drafts = out.get("draft_plan") or []
    assert len(drafts) == 1 and not out.get("applied_plan")
    assert datetime.fromisoformat(drafts[0]['start']).date() == (ANCHOR + timedelta(days=expected_day)).date()


@pytest.mark.parametrize("category,wind,offset", [("外出体力", 10, 0), ("外出体力", 60, 1), ("深度脑力", 60, 0)])
def test_windy_dry_day(monkeypatch, category, wind, offset):
    weather = _weather({0: 0, 1: 0})
    weather[0]['wind_kmh'] = wind
    title = '爬山' if category == '外出体力' else '室内写作'
    out, _ = _invoke(monkeypatch, '安排' + title, [_agent(title, 60, category=category)], weather=weather)
    assert not out.get('applied_plan')
    assert datetime.fromisoformat(out['draft_plan'][0]['start']).date() == (ANCHOR + timedelta(days=offset)).date()


def test_rain_does_not_block_indoor(monkeypatch):
    """雨天只拦户外类：室内写作任务雨日照常安排（约束域不误伤）。"""
    weather = _weather({d: 80 for d in range(8)})
    out, _ = _invoke(monkeypatch, "这周找时间写季度总结", [
        _agent("写季度总结", 90, category="深度脑力", energy_drain="高")],
        weather=weather)
    drafts = out.get("draft_plan") or []
    assert drafts and not out.get("question_plan")


# —— 精力与生物钟匹配 ——————————————————————————————————————————————————————


def test_deep_work_lands_morning_golden_block(monkeypatch):
    """高能耗深度任务（2h）+ deep-work 哲学 → 120min 整块落上午黄金段，
    不落午休后/日末垃圾时间。"""
    out, _ = _invoke(monkeypatch, "安排写一份下季度的底层架构重构方案", [
        _agent("底层架构重构方案", 120, category="深度脑力",
               energy_drain="高")], philosophy="deep-work")
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start.hour < 12, f"深度任务未落上午黄金段：{start.isoformat()}"
    assert start.hour >= 8


def test_low_energy_task_anchors_garbage_time(monkeypatch):
    """低能耗 ≤30min 杂事 → 垃圾时间锚定：午休末（13:30）优先，不占黄金段。"""
    out, _ = _invoke(monkeypatch, "抽空整理一下报销单", [
        _agent("整理报销单", 15, category="机械杂事", energy_drain="低")])
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert (start.hour, start.minute) == (13, 30), start.isoformat()


def test_mixed_batch_energy_matching(monkeypatch):
    """混合批量：深任务 + 碎杂事同轮 → 各归其位（深=上午整块，碎=午休末），
    碎条目不抢深条目的黄金时段。"""
    out, _ = _invoke(monkeypatch, "安排写架构方案，顺便整理报销单", [
        _agent("底层架构重构方案", 120, category="深度脑力", energy_drain="高"),
        _agent("整理报销单", 15, category="机械杂事", energy_drain="低")],
        philosophy="deep-work")
    drafts = out.get("draft_plan") or []
    by_title = {d["title"]: datetime.fromisoformat(d["start"]) for d in drafts}
    assert len(by_title) == 2
    deep = by_title["底层架构重构方案"]
    chore = by_title["整理报销单"]
    assert deep.hour < 12
    assert (chore.hour, chore.minute) == (13, 30)
    assert deep < chore  # 碎条目不插队黄金段


# —— 政务工作日硬约束 ——————————————————————————————————————————————————————


def test_gov_weekday_gate_rejects_weekend_only_window(monkeypatch):
    """车管所办证常识（政务工作日）+ 全部工作日被占满 → 周末不可用 → no_slot。
    对照组（下例）无常识时周末可排，双例隔离证明闸生效。"""
    for offset in (0, 1, 2, 5, 6, 7):  # 窗内全部工作日全天占满（含午休）
        day = ANCHOR + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        _db_task("全天占用", day.replace(hour=8), day.replace(hour=22))
    out, _ = _invoke(monkeypatch, "找时间去车管所办证", [
        _agent("去车管所办证", 60,
               common_sense_rules=["车管所只在工作日办公"])],
        settings_overrides={"work_minutes_per_day": 1440})  # 关预算闸隔离约束 1
    q = out.get("question_plan")
    assert q and q["kind"] == "no_slot"
    assert not (out.get("draft_plan") or [])


def test_no_gate_weekend_is_usable(monkeypatch):
    """对照组：非政务任务同样满工作日 → 周六正常出草案（工作日闸非全局）。"""
    for offset in (0, 1, 2, 5, 6, 7):
        day = ANCHOR + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        _db_task("全天占用", day.replace(hour=8), day.replace(hour=22))
    out, _ = _invoke(monkeypatch, "找时间和朋友聚聚", [
        _agent("朋友聚会", 120, category="沟通协作")],
        settings_overrides={"work_minutes_per_day": 1440})
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]).weekday() >= 5


# —— 生理恢复窗 / 缓冲 / 用户意志 ———————————————————————————————————————————


def test_recovery_window_blocks_next_morning(monkeypatch):
    """周六「周末爬山」→ 次日（周日）0:00-10:00 恢复窗并 busy：周日 08 点起的
    条目被推到 10:00 后，恢复日早间不排活。"""
    sat = ANCHOR + timedelta(days=3)  # 11-28 周六
    _db_task("周末爬山", sat.replace(hour=10), sat.replace(hour=16))
    sun = sat + timedelta(days=1)
    out, _ = _invoke(monkeypatch, "周日上午安排个复盘会", [
        _agent("周日复盘会", 30)], anchor=sun.replace(hour=8))
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start >= sun.replace(hour=10), f"恢复窗失守：{start.isoformat()}"


def test_meeting_tail_buffer_respected(monkeypatch):
    """会议尾缓冲 15min：明日 14:00-15:00「产品评审会议」后新条目最早 15:15，
    不与会议贴身（上午占用块与会议拉开 1h gap——gap≤10 会连成链尾 30 缓冲）。"""
    tomorrow = ANCHOR + timedelta(days=1)
    _db_task("封闭开发", ANCHOR.replace(hour=9), ANCHOR.replace(hour=21, minute=30))
    _db_task("上午排满", tomorrow.replace(hour=8), tomorrow.replace(hour=13))
    _db_task("产品评审会议", tomorrow.replace(hour=14), tomorrow.replace(hour=15))
    out, _ = _invoke(monkeypatch, "记得写周报", [_agent("写周报", 30)])
    drafts = out.get("draft_plan") or []
    assert drafts
    assert datetime.fromisoformat(drafts[0]["start"]) == \
        tomorrow.replace(hour=15, minute=15)


def test_long_chain_tail_buffer_and_lunch(monkeypatch):
    """3h+ 背靠背会议链（gap≤10min）→ 链尾 +30min 缓冲，叠午休 → 链后首槽 13:30，
    不贴链尾。"""
    _db_task("周会", ANCHOR.replace(hour=9), ANCHOR.replace(hour=10))
    _db_task("评审会", ANCHOR.replace(hour=10, minute=5), ANCHOR.replace(hour=11, minute=5))
    _db_task("复盘会", ANCHOR.replace(hour=11, minute=10), ANCHOR.replace(hour=12, minute=10))
    out, _ = _invoke(monkeypatch, "抽空整理下会议纪要", [
        _agent("整理会议纪要", 30, category="机械杂事", energy_drain="中")])
    drafts = out.get("draft_plan") or []
    assert drafts
    start = datetime.fromisoformat(drafts[0]["start"])
    assert start >= ANCHOR.replace(hour=12, minute=40)
    assert start == ANCHOR.replace(hour=13, minute=30)


def test_user_clock_clash_conflict_not_silent_move(monkeypatch):
    """用户明示「明天下午3点开会」撞既有日程 → conflict 问询 + 改期建议，
    绝不静默挪槽、绝不静默直落重叠块。"""
    tomorrow = ANCHOR + timedelta(days=1)
    _db_task("季度汇报", tomorrow.replace(hour=15), tomorrow.replace(hour=16))
    out, _ = _invoke(monkeypatch, "明天下午3点开个会", [
        _user_at("项目对齐会", tomorrow.replace(hour=15), "明天下午3点")])
    q = out.get("question_plan")
    assert q and q["kind"] == "conflict"
    assert q.get("suggestion")  # 引擎给出改期主荐
    assert not (out.get("draft_plan") or [])
    assert not (out.get("applied_plan") or [])


@pytest.mark.parametrize('peak', [9, 17])
def test_deep_task_follows_observed_peak(monkeypatch, peak):
    from models import BehaviorEvent
    with TestingSessionLocal() as db:
        for day in range(1, 5):
            at = datetime.now() - timedelta(days=day)
            for kind, hour in [('done', peak), ('reschedule', 14)]:
                db.add(BehaviorEvent(kind=kind, created_at=at, prev_start=at.replace(hour=hour)))
        db.commit()
    out, _ = _invoke(monkeypatch, '写架构方案，同时安排报销', [
        _agent('写架构方案', 120, category='深度脑力', energy_drain='高'),
        _agent('整理报销', 15, category='机械杂事', energy_drain='低')], philosophy='deep-work')
    slots = {e['title']: datetime.fromisoformat(e['start']) for e in out['draft_plan']}
    assert slots['写架构方案'].hour == peak
    assert (slots['整理报销'].hour, slots['整理报销'].minute) == (13, 30)
