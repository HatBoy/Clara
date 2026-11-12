"""排期子图 e2e 测试（批 2 骨架，2026-10-21）：scheduler_app.invoke 全管道
贯通，出口断言与旧 schedule_planner 三出口口径一致（行为等价的子图级证明）。

替身口径与 test_agents_nodes 相同：monkeypatch services.llm.call_llm 按子串
分发（profiler 与引擎 SLOT_REFINE 双调用面都经此拦截）；读库 monkeypatch
intake.SessionLocal（scheduler 包内唯一直连库的节点）+ services.tasks.list_tasks。
时间基准冻结 2026-10-17 08:00（与旧 planner 测试同款口径）。
"""

import json
from datetime import datetime
from types import SimpleNamespace

import agents.scheduler.context as context_mod
import agents.scheduler.intake as intake_mod
from agents.scheduler.prompts import PROFILER_PROMPT as PLANNER_PROMPT
from agents.scheduler import scheduler_app
from conftest import TestingSessionLocal
from services import ai_data
from services import llm
from services import settings as settings_svc
from services import tasks as tasks_svc


class _LLMCapture:
    """call_llm 替身：按 system prompt 子串分发预设应答并记录调用
    （test_agents_nodes 同款；子图不涉流式，无双协议需求）。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        self.calls.append((system, messages[-1]["content"]))
        for prompt, reply in self.replies.items():
            if prompt in system:
                return reply
        return self.default


def _task(id_="t1", title="周报评审", prio=1, start=None, end=None, notes=None,
          dur=90, rrule=None):
    """任务替身（对齐 list_tasks 返回的节点消费面）。"""
    return SimpleNamespace(id=id_, title=title, priority=prio, start_time=start,
                           end_time=end, notes=notes, duration_mins=dur, reschedule_count=0,
                           recurrence_rule=rrule)


class _FrozenDT(datetime):
    """冻结 now（2026-10-17 08:00）——硬编码 10-17 日期的用例防跨日漂移。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 17, 8, 0)


def _patch_inputs(monkeypatch, *, pending, scheduled=None, philosophy=None):
    """intake 读库/设置替身（_patch_planner_inputs 的 scheduler 版：SessionLocal
    patch 点从 nodes 迁到 intake，形参不叫 unscheduled 的教训同源）。"""
    monkeypatch.setattr(intake_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(context_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(intake_mod, "datetime", _FrozenDT)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        if unscheduled:
            return pending
        return scheduled or []

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    monkeypatch.setattr(tasks_svc, "calendar_load_summary", lambda db, **kw: [])
    monkeypatch.setattr(settings_svc, "get_settings", lambda db: SimpleNamespace(
        work_minutes_per_day=480, calendar_start_min=480, calendar_end_min=1320,
        lunch_enabled=True, lunch_start_min=690, lunch_end_min=810, reminder_lead_min=15,
        weather_city=None, ai_philosophy=philosophy))


# —— 三出口：快录直落 / 引擎草案 / 问询 ————————————————————————————————


def test_subgraph_fast_track_applies_user_origin(monkeypatch):
    # 快录口语：三重复核全过 → 直落 applied_plan（无卡无按钮），回复确定性拼装
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "已排。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检",
                                "memory_rules": []})
    assert out["applied_plan"] == [{"task_id": None, "title": "体检",
                                    "start": "2026-10-17T10:00:00", "duration": 60,
                                    "explicit_time": "上午10点", "origin": "user"}]
    assert "已安排" in out["final_reply"] and "体检" in out["final_reply"]
    assert out["draft_plan"] == []
    assert "question_plan" not in out


def test_short_confirmation_applies_from_history(monkeypatch):
    """省略式确认直落（2026-10-23 死循环修复批）：本轮"安排一下"零时间词，
    profiler 从 recent_chat 提取历史原话条目（explicit_time 摘录"明天下午5点"），
    三重复核扩围认可历史用户原话——explicit_time 子串 + title 子串关系 +
    钟点一致（下午5点 → 17:00）三条都过 → 直落 applied_plan 而非降级出卡。"""
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "user", "title": "跑步",
                     "start": "2026-10-18T17:00:00", "duration": 60,
                     "explicit_time": "明天下午5点"}],
        "reply": "安排上了。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="明天下午5点我要去跑步一小时")
        ai_data.append_message(db, role="assistant", content="跑步计划不错呀～")
    out = scheduler_app.invoke({"user_input": "安排一下", "memory_rules": []})
    assert [e["title"] for e in out["applied_plan"]] == ["跑步"]
    assert out["applied_plan"][0]["start"] == "2026-10-18T17:00:00"
    assert out["draft_plan"] == []
    assert "question_plan" not in out


def test_subgraph_agent_draft_via_engine(monkeypatch):
    # agent 草案时间经统一引擎校验重申：LLM 起点 14:00 本就合法 → 引擎原样保留；
    # payload 形状与旧 planner 逐键一致（取数段移植的等价证明）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": "t1", "title": "写周报",
                        "start": "2026-10-17T14:00:00", "duration": 90}],
        "reply": "下午整块时间写周报。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    out = scheduler_app.invoke({"user_input": "把周报排到明天下午",
                                "memory_rules": ["[作息] 午休不排会"]})
    assert out["draft_plan"] == [{"task_id": "t1", "title": "写周报",
                                  "start": "2026-10-17T14:00:00", "duration": 90}]
    assert out["candidate_slots"][0] == {"key": "a0", "title": "写周报",
                                         "slot": "2026-10-17T14:00:00"}
    assert "已出草案" in out["final_reply"] and "写周报" in out["final_reply"]
    _, payload = fake.calls[0]  # calls[0] 是 profiler（引擎 SLOT_REFINE 走 default）
    data = json.loads(payload)
    assert data["tasks_unscheduled"][0]["id"] == "t1"
    assert data["tasks_unscheduled"][0]["intensity"] == "high"  # 90 分钟
    assert data["memory_rules"] == ["[作息] 午休不排会"]
    assert data["settings"]["lunch"] == [690, 810]
    assert data["settings"]["day_start"] == "08:00"


def test_subgraph_conflict_question_with_engine_suggestion(monkeypatch):
    # 撞期：origin=user 10:00 撞 09:30-11:00 既有 → conflict 问询 + 引擎"明天
    # 同钟点"改期建议（体检就医上午闸内 10:00 合法）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "已排。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], scheduled=[
        _task(id_="s1", title="评审", prio=1,
              start=datetime(2026, 10, 17, 9, 30), end=datetime(2026, 10, 17, 11, 0))])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检",
                                "memory_rules": []})
    q = out["question_plan"]
    assert q["kind"] == "conflict"
    assert q["suggestion"] == "2026-10-18T10:00:00"  # same_clock_first 明天同钟点
    assert q["entries"][0]["title"] == "体检"
    assert out["draft_plan"] == [] and "applied_plan" not in out
    assert "体检" in out["final_reply"]


def test_subgraph_fuzzy_no_time_question(monkeypatch):
    # 只有日期缺钟点 → no_time 问询（date_hint 透传），零草案
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"origin": "fuzzy", "title": "爬山", "duration": 240,
                        "date_hint": "明天"}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    out = scheduler_app.invoke({"user_input": "明天去爬山", "memory_rules": []})
    q = out["question_plan"]
    assert q["kind"] == "no_time"
    assert q["entries"][0]["title"] == "爬山"
    assert q["date_hint"] == "明天"
    assert out["draft_plan"] == []


def test_subgraph_pending_confirmed_uses_suggestion(monkeypatch):
    # pending 消费：confirmed 同意建议 → conflict 快照起点换主荐时间直落 + 结算回写
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "confirmed"}, "draft_plan": [], "reply": "好"},
        ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    pending = {"proposal_id": "q1", "kind": "conflict",
               "entries": [{"task_id": None, "title": "体检",
                            "start": "2026-10-17T10:00:00", "duration": 60}],
               "suggestion": "2026-10-17T14:00:00"}
    out = scheduler_app.invoke({"user_input": "可以", "memory_rules": [],
                                "pending": pending})
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "accepted"}
    assert out["applied_plan"][0]["start"] == "2026-10-17T14:00:00"
    assert "已安排" in out["final_reply"]


def test_subgraph_garbage_output_degrades(monkeypatch):
    # 垃圾输出 → 零草案降级文案（profiler 写终态，下游透传）
    monkeypatch.setattr(llm, "call_llm",
                        _LLMCapture({PLANNER_PROMPT: "抱歉我不会"}))
    _patch_inputs(monkeypatch, pending=[])
    out = scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    assert out["draft_plan"] == []
    assert "排期推理失败" in out["final_reply"]


# —— 规格五键贯穿与拓扑 ————————————————————————————————————————————————


def test_subgraph_state_spec_keys_present(monkeypatch):
    # 五键中 raw_input/task_profile/context 全程落值；final_decision 批 6 才落。
    # 空草案轮触发待排清零早退（批 4 三岔）——finder 不跑，candidate_slots
    # 键不在 state；agent 草案轮才产（见 test_subgraph_agent_draft_via_engine）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"draft_plan": [], "reply": "无"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1")])
    out = scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    assert out["raw_input"] == "排一下"
    assert out["task_profile"]["reply"] == "无"
    assert "busy" in out["context"] and "candidates" in out["context"]
    assert "candidate_slots" not in out  # 早退路由生效（finder 空转跳过）
    assert out["final_decision"] == {}  # 规格⑤批 6 起全程落值（空轮零 selections）


# —— Node1 剖析正式化（批 4：entries schema / profile 清洗 / 两分钟短路） ——————


def test_profiler_entries_schema_with_profile(monkeypatch):
    # 新 schema：entries 键 + profile 六类画像 → 草案照走管道、出口剥离画像
    # （九键契约零漂移），task_profile 留剖析档案
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": "t1", "origin": "agent", "title": "写周报",
                     "start": "2026-10-17T14:00:00", "duration": 90,
                     "profile": {"category": "深度脑力", "energy_drain": "高",
                                 "env_deps": ["需要好天气"],
                                 "common_sense_rules": ["整块专注，防打断"],
                                 "est_minutes": 90}}],
        "reply": "收到"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    out = scheduler_app.invoke({"user_input": "把周报排了", "memory_rules": []})
    assert out["task_profile"]["entries"][0]["profile"]["category"] == "深度脑力"
    assert out["draft_plan"][0] == {"task_id": "t1", "title": "写周报",
                                    "start": "2026-10-17T14:00:00", "duration": 90}
    assert all("profile" not in e for e in out["draft_plan"])  # 出口剥画像


def test_profiler_profile_garbage_falls_back(monkeypatch):
    # 画像形态错配 → 全字段丢弃不炸（条目照常分流 ≈ 旧行为）；非法枚举归 other
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": "t1", "origin": "agent", "title": "写周报",
                     "duration": 90,
                     "profile": {"category": "不知道", "energy_drain": "超耗能",
                                 "env_deps": "要晴天", "est_minutes": -5}}],
        "reply": "收到"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    out = scheduler_app.invoke({"user_input": "把周报排了", "memory_rules": []})
    assert out["draft_plan"][0]["title"] == "写周报"  # 照常出草案
    # 清洗画像附在分流产物上（task_profile 是 LLM 原始解析）：非法枚举归
    # other、形态错配字段（energy_drain/env_deps/est）全丢弃
    assert out["agent_entries"][0]["profile"] == {"category": "other"}
    assert "profile" not in out["draft_plan"][0]


def test_two_minute_shortcut_when_philosophy_on(monkeypatch):
    # 两分钟法则短路：全部新建条目 est≤2 + two-minute 哲学启用 → 劝导不落库
    # （无卡无 applied，终态条件边直达 END）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "user", "title": "回消息",
                     "start": "2026-10-17T10:00:00", "duration": 2,
                     "explicit_time": "上午10点",
                     "profile": {"category": "机械杂事", "energy_drain": "低",
                                 "env_deps": [], "common_sense_rules": [],
                                 "est_minutes": 2}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], philosophy="two-minute")
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点回消息",
                                "memory_rules": []})
    assert "两分钟" in out["final_reply"] and "立刻" in out["final_reply"]
    assert out["draft_plan"] == [] and "applied_plan" not in out


def test_two_minute_off_without_philosophy(monkeypatch):
    # 哲学未启用 → 同输入照常快录直落（两分钟短路是哲学开关的行为非硬编码）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "user", "title": "回消息",
                     "start": "2026-10-17T10:00:00", "duration": 2,
                     "explicit_time": "上午10点",
                     "profile": {"category": "机械杂事", "est_minutes": 2}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], philosophy=None)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点回消息",
                                "memory_rules": []})
    assert out["applied_plan"][0]["title"] == "回消息"
    assert "已安排" in out["final_reply"]


def test_two_minute_skipped_when_pending_active(monkeypatch):
    # pending 活跃轮不短路：回复语义是结算问询（"可以"≠新两分钟任务）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "cancelled"}}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], philosophy="two-minute")
    pending = {"proposal_id": "q1", "kind": "no_time",
               "entries": [{"task_id": None, "title": "爬山",
                            "start": None, "duration": 240}]}
    out = scheduler_app.invoke({"user_input": "算了", "memory_rules": [],
                                "pending": pending})
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "ignored"}
    assert "两分钟" not in out["final_reply"]


# —— 批量排期短路（2026-10-25 功能边界批）——————————————————————————————


def test_bulk_intent_word_groups():
    # 三组词全命中才判批量；改期动词在场即不判（误判弹窗打断改期语义代价大，
    # 漏判走常规管道行为不坏）；单条排期无范围词不命中；「组|项目」作范围词
    # （2026-10-25 真机修复）——"把X项目组的待办排期"隐含全量但无全量词，
    # 需待办名词同场才命中（"新项目排期"单实体不误吞）
    from agents.scheduler.profiler import _bulk_intent
    assert _bulk_intent("把所有待办都排了") is True
    assert _bulk_intent("帮我安排全部任务") is True
    assert _bulk_intent("没排期的事情都规划一下") is True
    assert _bulk_intent("把日常运营项目组的待办排期") is True
    assert _bulk_intent("明天下午3点开会") is False
    assert _bulk_intent("新项目排期") is False
    assert _bulk_intent("所有任务改排到周一") is False
    assert _bulk_intent("把所有会议挪到周五") is False


def test_bulk_shortcut_engine_drafts_zero_profiler_llm(monkeypatch):
    # 批量意图 + ≥2 候选 → 零剖析 LLM，全量直交统一引擎出 bulk 草案（条目全
    # 带 task_id——改既有任务排期，前端弹窗确认走改期路径）+ 弹窗文案终态
    fake = _LLMCapture()  # 无预设：剖析被误调即拿空串降级，断言可辨
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task("t1", "写周报", 2, dur=60),
                                        _task("t2", "回信张三", 2, dur=30)])
    out = scheduler_app.invoke({"user_input": "把所有待办都排了", "memory_rules": []})
    assert out.get("bulk") is True
    assert len(out["draft_plan"]) == 2
    assert {d["task_id"] for d in out["draft_plan"]} == {"t1", "t2"}
    assert all(d["start"] for d in out["draft_plan"])
    assert "弹窗" in out["final_reply"]
    # 剖析 LLM 零调用（calls 里至多引擎 SLOT_REFINE 面——default 空串解析失败
    # 静默回落规则槽，草案不空手）
    assert not any(PLANNER_PROMPT in c[0] for c in fake.calls)


def test_bulk_intent_single_candidate_goes_normal(monkeypatch):
    # 候选 <2 条：批量语句走常规管道（单条弹窗无意义，出常规草案卡）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": "t1", "origin": "agent", "title": "写周报",
                     "start": None, "duration": 60}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task("t1", "写周报", 2, dur=60)])
    out = scheduler_app.invoke({"user_input": "把所有待办都排了", "memory_rules": []})
    assert "bulk" not in out  # 未短路，bulk 键不出现


def test_bulk_shortcut_settles_strategist_pending(monkeypatch):
    # 真机死循环回归（2026-10-25）：批量请求被 LLM 误判 coach 挂起 clarify 后，
    # 批量语句要能换意图进排期管道——strategist pending 放行短路 + 旧行结算
    # ignored（不结算会继续抢锚点，下轮"可以"误消费旧 clarify）
    fake = _LLMCapture()  # 无预设：剖析被误调即拿空串降级，断言可辨
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task("t1", "写周报", 2, dur=60),
                                        _task("t2", "回信张三", 2, dur=30)])
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(
            db, kind="question",
            payload={"kind": "strategist_clarify", "ask_text": "把具体待办发给我",
                     "original_input": "把所有待办排期"})
        ai_data.append_message(db, role="assistant", content="问询",
                               proposal_id=row.id)
        stale_id = row.id
    out = scheduler_app.invoke({
        "user_input": "把日常运营项目组的待办排期", "memory_rules": [],
        "pending": {"proposal_id": stale_id, "kind": "strategist_clarify",
                    "origin": "strategist", "ask_text": "把具体待办发给我"}})
    assert out.get("bulk") is True  # 短路照走（未被 pending 挡）
    with TestingSessionLocal() as db:
        stale = ai_data.get_proposal(db, stale_id)
    assert stale.outcome == "ignored"


# —— 批 6 裁决器：selections 换槽 / adjust 回落 / conflict 精修 —————————————


def test_adjudicator_selection_moves_slot_and_explains(monkeypatch):
    # 裁决器选中 a0 并微调 10:30（同日 ±90min 内）→ 草案换槽 + reply 附
    # 秘书口吻 explanation + final_decision 落键（规格⑤）
    fake = _LLMCapture({
        PLANNER_PROMPT: json.dumps({
            "entries": [{"task_id": "t1", "origin": "agent", "title": "写方案",
                         "start": "2026-10-17T14:00:00", "duration": 90,
                         "profile": {"category": "深度脑力", "energy_drain": "高"}}],
            "reply": "好"}, ensure_ascii=False),
        "时间裁决官": json.dumps({
            "selections": [{"key": "a0", "adjust_start": "2026-10-17T15:00:00",
                            "reason": "午后安静时段"}],
            "explanation": "午后安静整块留给方案。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写方案", prio=2)])
    out = scheduler_app.invoke({"user_input": "把方案排了", "memory_rules": []})
    assert out["draft_plan"][0]["start"] == "2026-10-17T15:00:00"
    assert "午后安静整块留给方案" in out["final_reply"]
    assert out["final_decision"]["selections"] == [
        {"key": "a0", "slot": "2026-10-17T15:00:00"}]


def test_adjudicator_adjust_crossday_falls_back(monkeypatch):
    # adjust 跨日（候选 10-19，调到 10-20）→ 越界丢弃回落候选主荐
    fake = _LLMCapture({
        PLANNER_PROMPT: json.dumps({
            "entries": [{"task_id": "t1", "origin": "agent", "title": "写方案",
                         "start": "2026-10-19T14:00:00", "duration": 60}],
            "reply": "好"}, ensure_ascii=False),
        "时间裁决官": json.dumps({
            "selections": [{"key": "a0", "adjust_start": "2026-10-20T09:00:00"}]},
            ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写方案", prio=2)])
    out = scheduler_app.invoke({"user_input": "排方案", "memory_rules": []})
    assert out["draft_plan"][0]["start"] == "2026-10-19T14:00:00"  # 主荐不动


def test_adjudicator_adjust_into_lunch_falls_back(monkeypatch):
    # adjust 合法窗内但落进午休（12:00-13:30）→ is_valid_slot 后验拒 → 回落主荐
    fake = _LLMCapture({
        PLANNER_PROMPT: json.dumps({
            "entries": [{"task_id": "t1", "origin": "agent", "title": "写周报",
                         "start": "2026-10-17T14:00:00", "duration": 60}],
            "reply": "好"}, ensure_ascii=False),
        "时间裁决官": json.dumps({
            "selections": [{"key": "a0", "adjust_start": "2026-10-17T12:30:00"}]},
            ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    out = scheduler_app.invoke({"user_input": "排周报", "memory_rules": []})
    assert out["draft_plan"][0]["start"] == "2026-10-17T14:00:00"


def test_adjudicator_refines_conflict_suggestion(monkeypatch):
    # conflict 轮裁决器精修 c0（明天同钟点 10:00 微调 09:15，就医上午闸内
    # 合法）→ question 卡 suggestion 换为裁决槽
    fake = _LLMCapture({
        PLANNER_PROMPT: json.dumps({
            "entries": [{"task_id": None, "origin": "user", "title": "体检",
                         "start": "2026-10-17T10:00:00", "duration": 60,
                         "explicit_time": "上午10点"}],
            "reply": "已排。"}, ensure_ascii=False),
        "时间裁决官": json.dumps({
            "selections": [{"key": "c0", "adjust_start": "2026-10-18T09:15:00",
                            "reason": "上午更早空腹"}]}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], scheduled=[
        _task(id_="s1", title="评审", prio=1,
              start=datetime(2026, 10, 17, 9, 30), end=datetime(2026, 10, 17, 11, 0))])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检",
                                "memory_rules": []})
    q = out["question_plan"]
    assert q["kind"] == "conflict"
    assert q["suggestion"] == "2026-10-18T09:15:00"  # 裁决精修生效





# —— 批 7 通勤块出卡 / 批 5 新问询闭环：frequency 防呆 · no_slot 豁免 ——————————


def test_commute_block_rides_draft_card(monkeypatch):
    # 差旅条目草案出两条：主体 + "路上"块（最终槽前 30 分钟，采纳即落库）；
    # 裁决器无回复（default 空串）走保底路径同样出块；出口无内部键
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": "t1", "origin": "agent", "title": "拜访客户",
                     "start": "2026-10-17T14:00:00", "duration": 60,
                     "profile": {"category": "差旅通勤", "energy_drain": "中"}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="拜访客户", prio=2)])
    out = scheduler_app.invoke({"user_input": "把拜访排了", "memory_rules": []})
    assert [d["title"] for d in out["draft_plan"]] == ["拜访客户", "去「拜访客户」路上"]
    assert out["draft_plan"][1]["start"] < out["draft_plan"][0]["start"]
    assert out["draft_plan"][1]["duration"] == 30
    assert all(not k.startswith("_") for d in out["draft_plan"] for k in d)


def test_frequency_question_then_confirmed_refinds(monkeypatch):
    # 第一轮：洗牙 30 天前做过（<180d）→ frequency 问询（context 查历史、
    # finder 防呆剔出待排）；第二轮：confirmed → 条目带 _freq_ok 重进管道
    # 找槽（不再拦——防死循环）→ 草案卡 + accepted 结算
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "agent", "title": "洗牙",
                     "duration": 60,
                     "profile": {"category": "医疗健康", "energy_drain": "低"}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    monkeypatch.setattr(tasks_svc, "last_similar_occurrence",
                        lambda db, title, **kw: datetime(2026, 9, 17, 10, 0))
    out = scheduler_app.invoke({"user_input": "安排洗牙", "memory_rules": []})
    q = out["question_plan"]
    assert q["kind"] == "frequency"
    assert q["entries"][0]["title"] == "洗牙"
    assert out["draft_plan"] == []
    assert "确认" in out["final_reply"]

    fake2 = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "confirmed"}, "entries": [], "reply": "好"},
        ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake2)
    out2 = scheduler_app.invoke({"user_input": "确认安排", "memory_rules": [],
                                 "pending": {"proposal_id": "q1",
                                             **{k: v for k, v in q.items()
                                                if k != "original_input"}}})
    assert out2["resolved_proposal"] == {"proposal_id": "q1", "outcome": "accepted"}
    assert out2["draft_plan"][0]["title"] == "洗牙"
    assert "question_plan" not in out2 or out2.get("question_plan") is None


def test_no_slot_insisted_marks_budget_waiver(monkeypatch):
    # no_slot 问询 + insisted（"坚持安排"）→ 条目回 agent_entries 且带
    # _freq_ok/_no_budget 标记（finder 预算豁免重找的回落通路，节点级断言
    # 见 test_scheduler_finder）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "insisted"}, "entries": [], "reply": "好"},
        ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    from agents.scheduler.profiler import profiler_node
    ctx = {"busy": [], "candidates": [], "valid_ids": set(), "prio_by_id": {},
           "scheduled": [], "settings": {}, "lunch": [690, 810],
           "day_start_min": 480, "day_end_min": 1320, "day_budget": 480,
           "load_summary": [], "recent_chat": [], "weather": None,
           "similar_pool": []}
    state = {"user_input": "坚持安排", "memory_rules": [], "now": datetime(2026, 10, 17, 8, 0),
             "philosophy_ids": [], "context": ctx,
             "pending": {"proposal_id": "q1", "kind": "no_slot",
                         "entries": [{"task_id": None, "title": "赶方案",
                                      "start": None, "duration": 120}]}}
    out = profiler_node(state)
    e = out["agent_entries"][0]
    assert e["_no_budget"] is True and e["_freq_ok"] is True
    assert out["resolved"] == {"proposal_id": "q1", "outcome": "accepted"}


# —— 冲突让位（2026-10-22）：问询点名挡路者 + resolution 第七形态 displace —————


def test_conflict_question_names_clashing_task(monkeypatch):
    # 撞期问询点名挡路者："已安排「评审」"（旧文案只有"已有安排"，用户无从
    # 判断让不让位）；clash_titles 进 question 快照、_clash 内部键出口剥离
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "已排。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], scheduled=[
        _task(id_="s1", title="评审", prio=1,
              start=datetime(2026, 10, 17, 9, 30), end=datetime(2026, 10, 17, 11, 0))])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检",
                                "memory_rules": []})
    q = out["question_plan"]
    assert q["clash_titles"] == ["评审"]
    assert "已安排「评审」" in out["final_reply"]
    assert "_clash" not in q["entries"][0]  # 内部键不进快照


def test_displace_moves_blocked_task(monkeypatch):
    # displace：新任务原时间直落 + 挡路者引擎找新槽出改期卡（task_id → 前端
    # 改期路径）+ 回复口吻前置 + accepted 结算；全轮零裁决 LLM（slots 空）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "displace"}, "entries": [],
         "reply": "明白，启动会优先～"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], scheduled=[
        _task(id_="s1", title="写作时间", prio=2, dur=60,
              start=datetime(2026, 10, 17, 15, 0), end=datetime(2026, 10, 17, 16, 0))])
    pending = {"proposal_id": "q1", "kind": "conflict",
               "entries": [{"task_id": None, "title": "项目启动会",
                            "start": "2026-10-17T15:30:00", "duration": 60}],
               "suggestion": "2026-10-18T15:30:00"}
    out = scheduler_app.invoke({"user_input": "项目启动会比较重要，把冲突的日程改到其他时间",
                                "memory_rules": [], "pending": pending})
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "accepted"}
    assert out["applied_plan"][0]["start"] == "2026-10-17T15:30:00"  # 原时间照排
    # 挡路者从新任务结束（16:30）顺延找槽：写作 60min → 16:30-17:30 空闲即得
    assert out["draft_plan"] == [{"task_id": "s1", "title": "写作时间",
                                  "start": "2026-10-17T16:30:00", "duration": 60}]
    assert "明白，启动会优先～" in out["final_reply"]  # 口吻前置（displace 专属）
    assert "调整到" in out["final_reply"] and "写作时间" in out["final_reply"]
    assert len(fake.calls) == 1  # 直达 adjudicator 且 slots 空 → 零裁决调用


def test_displace_series_blocker_notes_manual(monkeypatch):
    # 系列挡路者不出改期卡（this/following 语义雷区）——新任务照排 + 附注
    # 指引手动挪；多实例同 id 去重只提醒一次
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"resolution": {"action": "displace"}, "entries": [], "reply": "好"},
        ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[], scheduled=[
        _task(id_="s1", title="每周例会", prio=2, rrule="FREQ=WEEKLY",
              start=datetime(2026, 10, 17, 9, 30), end=datetime(2026, 10, 17, 11, 0))])
    pending = {"proposal_id": "q1", "kind": "conflict",
               "entries": [{"task_id": None, "title": "项目启动会",
                            "start": "2026-10-17T10:00:00", "duration": 60}]}
    out = scheduler_app.invoke({"user_input": "启动会比较重要，把冲突的改到其他时间",
                                "memory_rules": [], "pending": pending})
    assert out["draft_plan"] == []  # 不出改期卡
    assert out["applied_plan"][0]["start"] == "2026-10-17T10:00:00"  # 新任务照排
    assert "重复日程" in out["final_reply"] and "每周例会" in out["final_reply"]


def test_displace_mismatch_non_conflict_pending(monkeypatch):
    # 非 conflict 问询（no_time）给 displace → 形态错配不消费：问询不结算，
    # 回复按新请求正常处理（原话给了完整时间 → 三重复核直落）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "displace"},
        "entries": [{"task_id": None, "origin": "user", "title": "爬山",
                     "start": "2026-10-17T14:00:00", "duration": 120,
                     "explicit_time": "下午2点"}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    pending = {"proposal_id": "q1", "kind": "no_time",
               "entries": [{"task_id": None, "title": "爬山", "start": None,
                            "duration": 240}]}
    out = scheduler_app.invoke({"user_input": "下午2点去爬山，冲突的挪走",
                                "memory_rules": [], "pending": pending})
    assert "resolved_proposal" not in out  # 未结算（错配不消费）
    assert out["applied_plan"][0]["title"] == "爬山"  # 新请求照排


# —— 优先级直判（2026-10-22）：用户明示/记忆画像明示 → priority=1 ———————————


def test_explicit_importance_marks_priority_one(monkeypatch):
    # profile.important=true（用户原话明示/记忆画像明示）→ 条目带 priority=1
    # 出口透传（applied 直落轮与 agent 草案轮同判）；important 缺省不带键
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [
            {"task_id": None, "origin": "user", "title": "启动会",
             "start": "2026-10-17T10:00:00", "duration": 60,
             "explicit_time": "上午10点",
             "profile": {"category": "沟通协作", "energy_drain": "中",
                         "important": True}},
            {"task_id": "t1", "origin": "agent", "title": "写周报", "duration": 90,
             "profile": {"category": "深度脑力", "energy_drain": "高"}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点启动会，很重要，再把周报排了",
                                "memory_rules": []})
    assert out["applied_plan"][0]["priority"] == 1       # 明示重要 → 高优
    assert "priority" not in out["draft_plan"][0]        # 未明示不带键
    assert "profile" not in out["applied_plan"][0]       # 画像照常出口剥离


# —— 顺修回归（2026-10-22）：记忆规则透传 ————————————————————————————————


def test_intake_snapshot_carries_memory_rules(monkeypatch):
    # 裁决器提示词声明的 memory_rules 输入此前在 intake 快照里恒缺（恒空数组）——
    # 习惯偏好在 Node4 从不可见。快照键在，裁决 payload 的组段自然可消费
    _patch_inputs(monkeypatch, pending=[])
    out = intake_mod.intake_node(
        {"user_input": "排一下", "memory_rules": ["[作息] 午休不排会"]})
    assert out["context"]["memory_rules"] == ["[作息] 午休不排会"]


def test_intake_snapshot_carries_chat_summary(monkeypatch):
    # 滚动摘要透传（2026-10-26）：chat_context 带出的摘要进快照（此前被 _ 丢弃）
    # ——profiler 载荷经 ctx["chat_summary"] 消费，跨天偏好对剖析可见
    _patch_inputs(monkeypatch, pending=[])
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="用户提过周五下午别排事", count=2)
    out = intake_mod.intake_node({"user_input": "排一下", "memory_rules": []})
    assert out["context"]["chat_summary"] == "用户提过周五下午别排事"


# —— 过程反馈（2026-10-26 提速批 2）：status 帧序列 ——————————————————————


def test_subgraph_emits_status_frames_in_order(monkeypatch):
    # stream 驱动下 custom 帧按节点推进顺序携带 status 阶段文案——非流式排期
    # 轮不再黑屏干等（直调 / invoke 形态退化 no-op，行为不变；壳 SSE additive）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "title": "写方案", "duration": 60}],
        "reply": "排好了。"}, ensure_ascii=False)})  # adjudicator 替身走 default
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    frames = [p for mode, p in scheduler_app.stream(
        {"user_input": "找个时间写方案", "memory_rules": []},
        stream_mode=["custom", "values"]) if mode == "custom"]
    statuses = [p["text"] for p in frames
                if isinstance(p, dict) and p.get("type") == "status"]
    assert statuses == ["正在剖析任务画像…", "正在检索日历与空档…", "正在权衡最佳时刻…"]


def test_intake_snapshot_carries_secretary_keys(monkeypatch):
    # 批 4 补键：candidates 带 reschedule_count、scheduled 带 priority、快照带
    # recent_done（近 7 天完成窗）——画像与产能证据进剖析载荷
    _patch_inputs(monkeypatch, pending=[_task(id_="p1", title="写方案", prio=2)],
                  scheduled=[_task(
                      id_="s1", title="项目评审", start=datetime(2026, 10, 17, 14, 0),
                      end=datetime(2026, 10, 17, 15, 0))])
    with TestingSessionLocal() as db:
        from datetime import datetime as _dt
        from models import Task
        done = Task(id="d1", title="整理周报", duration_mins=30, done_at=_dt.now())
        db.add(done)
        db.commit()
    out = intake_mod.intake_node({"user_input": "排一下", "memory_rules": []})
    assert out["context"]["scheduled"][0]["priority"] == 1
    assert out["context"]["candidates"][0]["reschedule_count"] == 0
    assert [e["title"] for e in out["context"]["recent_done"]] == ["整理周报"]


def test_fast_track_notes_preference_clash(monkeypatch):
    """批 5 偏好硬闸配套：直落撞偏好窗照落不拦（用户明给时间，意志优先），
    回复附注提醒——insisted 撞 busy 附注的同款先例（引擎/finder 才是硬闸）。"""
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "去打球",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "已排。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_inputs(monkeypatch, pending=[])
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点去打球",
                                "memory_rules": ["[排期偏好·用户设定] 周六上午留空"]})
    assert out["applied_plan"][0]["start"] == "2026-10-17T10:00:00"  # 照落
    assert "相冲" in out["final_reply"] and "周六上午留空" in out["final_reply"]


# —— 新建条目个性化耗时（2026-11-01 优化批 1）：est_hint 扩面 ——————————————


def _seed_done_task():
    """近 3 天完成的同类任务（真实 done_at，90 天窗内命中 similar_done_durations）。"""
    from datetime import timedelta

    from models import Task

    with TestingSessionLocal() as db:
        db.add(Task(title="季度汇报", duration_mins=90,
                    done_at=datetime.now() - timedelta(days=3)))
        db.commit()


def _profiler_state(user_input: str) -> dict:
    return {"user_input": user_input, "memory_rules": [],
            "now": datetime(2026, 10, 17, 8, 0), "philosophy_ids": [],
            "context": {"busy": [], "candidates": [], "valid_ids": set(),
                        "prio_by_id": {}, "scheduled": [], "settings": {},
                        "lunch": [690, 810], "day_start_min": 480,
                        "day_end_min": 1320, "day_budget": 480,
                        "load_summary": [], "recent_chat": [], "weather": None,
                        "similar_pool": []}}


def test_profiler_new_entry_uses_history_duration(monkeypatch):
    # 对话新建条目（task_id null）此前无 est_hint——按 LLM 提炼后的标题补查
    # 同类历史均值，覆盖 LLM 经验估值（duration 与 est_minutes 同步对齐；
    # PROFILER §四.5 优先级链：用户 > 历史 > 经验）。origin=agent 形态直达
    # agent_entries（fuzzy 走 no_time 问询不落草案，路径另测）
    from agents.scheduler.profiler import profiler_node

    _seed_done_task()
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "agent", "title": "季度汇报",
                     "duration": 30,
                     "profile": {"category": "沟通协作", "energy_drain": "中",
                                 "est_minutes": 30}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    out = profiler_node(_profiler_state("下周找时间做季度汇报"))
    e = out["agent_entries"][0]
    assert e["duration"] == 90  # 历史均值 90 分钟覆盖 LLM 经验估值 30


def test_profiler_explicit_user_duration_not_overridden(monkeypatch):
    # 用户明说时长（"两小时的季度汇报"）：用户设定优先于历史——不覆盖
    from agents.scheduler.profiler import profiler_node

    _seed_done_task()
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "entries": [{"task_id": None, "origin": "agent", "title": "季度汇报",
                     "duration": 120,
                     "profile": {"category": "沟通协作", "energy_drain": "中",
                                 "est_minutes": 120}}],
        "reply": "好"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    out = profiler_node(_profiler_state("安排两小时的季度汇报"))
    assert out["agent_entries"][0]["duration"] == 120  # 用户口径不被历史覆盖
