"""上班族日常场景矩阵（2026-10-20 私人秘书化批 5）。

定位：跨模块场景串联——晨间简报秘书视野（天气/负荷/高优未排期）/ 前夜综述
过载日 / coach 双模式三型 / QA 负荷与天气视角 / 对话偏好→反思→次日晨间简报
注入闭环。单件契约细节（解析钳制、垃圾降级、替身口径）在 test_agents_nodes /
test_run_preflight / test_reflection 各分文件，不在此重复。

替身口径同各分文件：LLM 打 services.llm.call_llm 按提示词常量分发；读库打
services.tasks.list_tasks / calendar_load_summary；天气打 services.weather.
forecast_for_city——run_preflight / qa_answerer 均函数内 import
源模块属性，monkeypatch 源模块即可（两会话陷阱先例）。
"""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import agents.graph as graph_mod
import agents.nodes as nodes_mod
from agents import reflection
from agents.prompts import (
    QA_PROMPT,
    REFLECT_PROMPT,
    RESOLVE_PROMPT,
    ROUTER_PROMPT,
)
from conftest import TestingSessionLocal
from services import ai_data, llm
import services.tasks as tasks_svc
import services.weather as weather_svc


class _LLMCapture:
    """call_llm / call_llm_stream 双协议替身：按 system prompt 子串分发预设应答
    并记录 (system, user) 调用（流式形态 on_chunk 忽略不模拟增量，返回全文）。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        self.calls.append((system, messages[-1]["content"]))
        for prompt, reply in self.replies.items():
            if prompt in system:
                return reply
        return self.default


def _task(id_="t1", title="评审会", prio=2, start=None, notes=None, rrule=None):
    """任务替身（对齐 ORM 消费面：calendar_load_summary / _task_dur /
    _source_context 溯源键读的字段齐全）。"""
    return SimpleNamespace(id=id_, title=title, priority=prio, start_time=start,
                           end_time=None, notes=notes, duration_mins=90, reschedule_count=0,
                           recurrence_rule=rrule, sort_order=0,
                           done_at=None, source_key=None, source_evidence=None)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def _load(count=3, mins=240, high=1, day=None):
    """按日负荷替身（calendar_load_summary 同形状）。"""
    return [{"date": (day or date.today()).isoformat(), "scheduled_count": count,
             "total_mins": mins, "high_count": high}]


def _forecast(text="中雨", precip=80, day=None):
    return [{"date": (day or date.today()).isoformat(), "text": text,
             "tmax": 18, "tmin": 12, "precip": precip}]


def _patch_reads(monkeypatch, *, scheduled=None, inbox=None, load=None):
    """读库替身：list_tasks 两口径（窗口/收件箱）+ 按日负荷；节点级 SessionLocal
    （qa 走 nodes，晨/夜简报经 strategist 子图走 cf/disp）一并替到
    内存库——tasks_svc 层替身对 context_fetcher/dispatcher 的函数内 import
    同样生效。"""
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    import agents.strategist.context_fetcher as cf
    import agents.strategist.dispatcher as disp
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        return (inbox or []) if unscheduled else (scheduled or [])

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    if load is not None:
        monkeypatch.setattr(tasks_svc, "calendar_load_summary", lambda db: load)


def _patch_weather(monkeypatch, forecast):
    monkeypatch.setattr(weather_svc, "forecast_for_city", lambda city: forecast)


# —— 晨间简报：秘书视野三键注入 ————————————————————————————————————————


def test_morning_brief_rainy_checkup_day_sees_full_picture(monkeypatch):
    """周一站会+深度工作+14:00 就医+雨天+高优未排期：天气/负荷/高优未排期
    三键全量进输入（Track B 批 6 后经 morning 场景 deep_context），简报照常出。"""
    day = date.today()
    fake = _LLMCapture({"【场景：morning": json.dumps(
        {"strategy": "## 上午留白写方案，午后空腹体检\n上午是产出的窗口。",
         "checklist": ["体检前 4 小时不进食"], "action_items": [],
         "memory_updates": []}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_reads(
        monkeypatch,
        scheduled=[
            _task(id_="t1", title="站会", prio=2, start=_at(day, 9, 30)),
            _task(id_="t2", title="写方案", prio=1, start=_at(day, 10, 30)),
            _task(id_="t3", title="体检", prio=1, start=_at(day, 14)),
        ],
        inbox=[_task(id_="t4", title="季度汇报", prio=1)],
        load=_load(count=3, mins=270, high=2))
    _patch_weather(monkeypatch, _forecast("中雨", 80))
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, day)
    system, payload = fake.calls[0]
    assert "【场景：morning" in system
    data = json.loads(payload)["上下文"]
    assert data["weather"][0]["text"] == "中雨"
    assert data["load_summary"][0]["total_mins"] == 270
    assert data["inbox_high"] == [{"id": "t4", "title": "季度汇报",
                                   "duration": 90, "priority": 1}]
    assert "上午是产出的窗口。" in out["final_reply"]
    assert out["preflight"]["checklist"] == ["体检前 4 小时不进食"]


def test_morning_brief_without_city_weather_null_reply_intact(monkeypatch):
    """未配置城市：weather 恒 null 不炸，final_reply 恒非空（晨间壳不变式）。"""
    fake = _LLMCapture({"【场景：morning": json.dumps(
        {"strategy": "## 两件事\n安排好了。", "checklist": [], "action_items": [],
         "memory_updates": []}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_reads(monkeypatch, scheduled=[_task(id_="t1", title="周会", prio=1)],
                 load=_load(count=1, mins=60))
    _patch_weather(monkeypatch, None)  # 未配置城市/失败 → None
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, date.today())
    data = json.loads(fake.calls[0][1])["上下文"]
    assert data["weather"] is None
    assert out["final_reply"]


# —— 前夜综述：明日过载 + 注入裁剪 ——————————————————————————————————————


def test_eve_brief_overloaded_tomorrow_omits_pending_high(monkeypatch):
    """明日 8 项（高优 3）：nightly 场景段 + 明日负荷注入；键集不含 inbox_high
    （明日待办次日晨间简报再看，前夜聚焦"今晚要准备什么"）。"""
    tomorrow = date.today() + timedelta(days=1)
    fake = _LLMCapture({"【场景：nightly": json.dumps(
        {"strategy": "## 明天满负荷\n明天很满，今晚备好材料。",
         "checklist": ["今晚材料过一遍"], "action_items": [],
         "memory_updates": []}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_reads(
        monkeypatch,
        scheduled=[_task(id_=f"e{i}", title=f"事项{i}", prio=1 if i < 3 else 3)
                   for i in range(8)],
        inbox=[_task(id_="p1", title="下季度规划", prio=1)],  # 高优未排期也不进高优键
        load=_load(count=8, mins=480, high=3, day=tomorrow))
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, tomorrow)
    system, payload = fake.calls[0]
    assert "【场景：nightly" in system
    data = json.loads(payload)["上下文"]
    assert "inbox_high" not in data  # nightly 键集无此键（前夜不看未排期高优）
    assert data["load_summary"][0]["scheduled_count"] == 8
    assert out["preflight"]["checklist"] == ["今晚材料过一遍"]
    assert "夜间复盘" in out["preflight"]["headline"]


def test_chat_graph_guidance_mode_end_to_end(monkeypatch):
    """真图链路（Track B 批 5 翻转后）：router=coach → strategist 子图 →
    execution 场景 checklist 空 → 仅 final_reply 零卡（指导形态语义保持）。"""
    import agents.strategist.context_fetcher as cf
    import agents.strategist.dispatcher as disp

    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    md = "两件事都重要。\n\n1. **上午写方案**（精力好）\n2. 午后雨停再健身\n\n先写方案？"
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "coach", "reason": "求取舍"},
                                  ensure_ascii=False),
        "幕僚长·场景嗅探官": json.dumps(
            {"scenario": "execution", "ask": None}, ensure_ascii=False),
        "【场景：execution": json.dumps(
            {"strategy": md, "checklist": [], "action_items": [],
             "memory_updates": []}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_reads(monkeypatch, scheduled=[_task(id_="t1", title="写方案", prio=1)])
    final = graph_mod.build_graph().invoke(
        {"user_input": "先健身还是先写方案", "memory_rules": []})
    assert final["intent"] == "coach"
    assert final["final_reply"] == md
    assert "preflight" not in final


# —— QA 秘书视角 —————————————————————————————————————————————————————————


def test_qa_busy_week_gets_load_summary(monkeypatch):
    """「这周忙不忙」：负荷摘要进输入（量化口径的依据），路由归 qa 非 coach。"""
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "qa", "reason": "查负荷"},
                                  ensure_ascii=False),
        QA_PROMPT: "周三最满，周四周五空。",
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    monkeypatch.setattr(llm, "call_llm_stream", fake)  # qa 走流式入口
    _patch_reads(monkeypatch,
                 scheduled=[_task(id_="t1", title="评审会", prio=1)],
                 load=_load(count=5, mins=420, high=2))
    final = graph_mod.build_graph().invoke(
        {"user_input": "这周忙不忙", "memory_rules": []})
    assert final["intent"] == "qa"
    data = json.loads(fake.calls[1][1])
    assert data["load_summary"][0]["scheduled_count"] == 5
    assert final["final_reply"] == "周三最满，周四周五空。"


def test_qa_running_question_gets_weather(monkeypatch):
    """「明天适合跑步吗」：天气进输入，路由归 qa（查事实对照）。"""
    fake = _LLMCapture({QA_PROMPT: "明天小雨，清晨还行，午后别跑。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    _patch_reads(monkeypatch, scheduled=[])
    _patch_weather(monkeypatch, _forecast("小雨", 60))
    out = nodes_mod.qa_answerer({"user_input": "明天适合跑步吗", "memory_rules": []})
    data = json.loads(fake.calls[0][1])
    assert data["weather"][0]["text"] == "小雨"
    assert out["final_reply"].startswith("明天小雨")


def test_qa_empty_day_honest_placeholder(monkeypatch):
    """空清单日：任务清单注入如实的空占位行，不编造。"""
    fake = _LLMCapture({QA_PROMPT: "今天没有安排。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    _patch_reads(monkeypatch, scheduled=[], inbox=[])
    nodes_mod.qa_answerer({"user_input": "今天有什么安排", "memory_rules": []})
    data = json.loads(fake.calls[0][1])
    assert data["tasks"] == ["（当前没有已排期日程，也没有待办任务）"]


# —— 习惯闭环：对话偏好 → 反思 → 次日晨间简报注入 ————————————————————————


def test_preference_chat_to_next_morning_brief(monkeypatch):
    """闭环端到端：当日 4 条同向偏好原话（无裁决）→ 夜间反思落库（chat 信号
    confidence ≤0.55）→ 次日晨间简报 system 注入该规则——秘书越用越懂你。"""
    for text in ["周五下午别排事，我要接孩子", "这周五下午也别安排会",
                 "上周五下午的会挪一下", "以后周五下午留给我"]:
        with TestingSessionLocal() as db:
            ai_data.append_message(db, role="user", content=text)
    monkeypatch.setattr(llm, "call_llm", _LLMCapture({
        REFLECT_PROMPT: json.dumps({"candidates": [{
            "category": "排期偏好", "content": "周五下午不排事",
            "evidence": "用户原话：周五下午别排事", "confidence": 0.5,
            "signal": "chat"}]}, ensure_ascii=False),
        RESOLVE_PROMPT: json.dumps({"actions": [{
            "candidate_idx": 0, "action": "new", "rule_id": None,
            "category": "排期偏好", "content": "周五下午不排事",
            "evidence": "用户原话：周五下午别排事", "confidence": 0.5}]},
            ensure_ascii=False),
    }))
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        assert report.created == 1 and not report.skipped
    fake = _LLMCapture({"【场景：morning": json.dumps(
        {"strategy": "## 周五留白\n周五留白。", "checklist": [], "action_items": [],
         "memory_updates": []}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_reads(monkeypatch, scheduled=[_task(id_="t1", title="周会", prio=1)])
    with TestingSessionLocal() as db:
        graph_mod.run_preflight(db, date.today())
    payload = fake.calls[0][1]
    # 2026-11-03 批 3 起双段单源分级格式（反思候选置信 0.5 归参考段）
    assert "[排期偏好·AI学习·置信0.5] 周五下午不排事" in payload  # 学到的偏好注入次日简报


# —— 边界防御 ———————————————————————————————————————————————————————————


def test_morning_brief_garbage_with_context_still_degrades(monkeypatch):
    """秘书视野全量注入下模型输出垃圾 → 确定性 headline 兜底卡 + 大脑兜底
    文案，不 raise（降级路径对新增输入键免疫）。"""
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(default="抱歉我不会"))
    _patch_reads(monkeypatch, scheduled=[_task(id_="t1", title="评审会", prio=1)],
                 load=_load())
    _patch_weather(monkeypatch, _forecast())
    day = date.today()
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, day)
    assert out["preflight"]["headline"] == f"{day.month}月{day.day}日 晨间启航"
    assert out["preflight"]["checklist"] == []
    assert "没能成形" in out["final_reply"]


# —— 批 5 场景用例（2026-10-22）：高强度活动次晨恢复保护 ——————————————————


def test_marathon_blocks_next_morning_slots(monkeypatch):
    """「周六跑马拉松」→ 周日 0:00-10:00 并入 busy：排期真图链路（intake
    busy_meta → context 恢复窗 → finder 避让）把"周日晨间深度工作"挪出保护窗。
    LLM 替身只出 PROFILER agent 条目（start 恒 null——时刻归引擎）。"""
    import agents.scheduler.context as sched_context
    import agents.scheduler.intake as sched_intake
    from agents.scheduler.prompts import PROFILER_PROMPT

    for m in (sched_intake, sched_context):
        monkeypatch.setattr(m, "SessionLocal", TestingSessionLocal)

    # 时间锚：周六 08:00（跑马拉松当日），马拉松 7:00-12:00 已在日历
    sat = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7)
    sat = sat.replace(hour=7, minute=0, second=0, microsecond=0)
    marathon = _task(id_="m1", title="城市马拉松", prio=2, start=sat)
    monkeypatch.setattr(tasks_svc, "list_tasks",
                        lambda db, **kw: [marathon] if not kw.get("unscheduled") else [])
    monkeypatch.setattr(tasks_svc, "calendar_load_summary", lambda db, **kw: [])
    import services.settings as settings_svc

    monkeypatch.setattr(settings_svc, "get_settings", lambda db: SimpleNamespace(
        work_minutes_per_day=480, calendar_start_min=480, calendar_end_min=1320,
        lunch_enabled=True, lunch_start_min=690, lunch_end_min=810, reminder_lead_min=15,
        weather_city=None, ai_philosophy=None))
    fake = _LLMCapture({
        PROFILER_PROMPT: json.dumps({
            "entries": [{"task_id": None, "origin": "agent", "title": "深度工作",
                         "start": None, "duration": 120, "not_before": f"{(sat + timedelta(days=1)).date().isoformat()}"}],
            "reply": "收到"}, ensure_ascii=False),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", fake)
    from agents.scheduler import scheduler_app

    out = scheduler_app.invoke({"user_input": "周日上午安排两小时深度工作", "memory_rules": []})
    starts = [d["start"] for d in out.get("draft_plan") or []]
    assert starts, "应产出草案"
    # 草案不得落在马拉松次晨保护窗（周日 0:00-10:00）内
    sun = sat + timedelta(days=1)
    for s in starts:
        assert not (sun <= datetime.fromisoformat(s) < sun.replace(hour=10)), s
