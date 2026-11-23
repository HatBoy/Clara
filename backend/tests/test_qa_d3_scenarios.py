"""QA 维度三：Track B 赛博幕僚长七战略场景输出质量（七维度端到端测试批）。

断言口径经读码核实：
- classifier 五步短路栈：cron 直打 → memory pending → clarify pending → 危机
  正则（救命|全乱了|崩了|搞砸|来不及|紧急，无否定豁免——负向 xfail 锁定）→
  LLM 七分类（ask 非空 → 追问挂起）；
- brain 钳制（brain._clamp_out）：strategy 硬截 1600 / checklist ≤6×40 字 /
  action_items ≤6 + duration 钳 [15,240] 归整 15 倍数 + task_id 白名单越界置
  None + action 越枚举丢条 + memory evidence 空整条丢；
- dispatcher：action_items → planner 规则直排（schedule+duration → plan_slots；
  无槽降 inbox）；chat 轮仅拆解/会前/危机三场景出卡；memory_updates 确认制
  first-wins；危机重组 = LLM 建议制 + 用户确认，无引擎级自动砍会议；
- 晨报 run_preflight：cron trigger 直打，morning 场景取数含 inbox_high（高优
  收件箱）+ rhythm + stalled_items——「挑高能耗硬骨头」方法论在场景段。
"""

import json
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm

from agents.graph import run_preflight
from agents.strategist import strategist_app
from agents.strategist.brain import _clamp_out
from agents.strategist.classifier import classifier_node
from qa_helpers import (
    KEY_CLASSIFIER,
    KEY_PENDING_JUDGE,
    KEY_ROUTER,
    KEY_SCENARIO,
    MultiKeyLLMCapture,
    activate_model,
    chat_events,
    events_by_type,
    first_of,
    patch_strategist_reads,
    settle_draft,
)


def _brain(scenario, strategy="方案如下。", checklist=None, actions=None,
           memories=None):
    return json.dumps({"strategy": strategy, "checklist": checklist or [],
                       "action_items": actions or [],
                       "memory_updates": memories or []}, ensure_ascii=False)


def _invoke(monkeypatch, user_input, *, classify=None, brain_reply=None,
            pending=None, judge=None):
    """幕僚子图 E2E invoke（chat 轮）。classify: (scenario, ask)；judge: 消费
    轮裁决应答。"""
    replies = {}
    if classify is not None:
        replies[KEY_CLASSIFIER] = json.dumps(
            {"scenario": classify[0], "ask": classify[1]}, ensure_ascii=False)
    for name in ("task_breakdown", "meeting_prep", "execution", "morning",
                 "nightly", "crisis", "stakeholder"):
        if brain_reply is not None:
            replies[KEY_SCENARIO[name]] = brain_reply
    if judge is not None:
        replies[KEY_PENDING_JUDGE] = json.dumps({"action": judge})
    cap = MultiKeyLLMCapture(replies, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    state = {"user_input": user_input, "memory_rules": [], "pending": pending,
             "trigger": "chat"}
    return strategist_app.invoke(state), cap


def _db_task(title, start=None, end=None, *, duration=60, priority=2):
    with TestingSessionLocal() as db:
        mins = duration
        if start and end:
            mins = int((end - start).total_seconds() // 60)
        t = Task(title=title[:30], start_time=start, end_time=end,
                 duration_mins=mins, priority=priority)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


# —— 场景 1：复杂任务拆解与委派 ——————————————————————————————————————————————


def test_breakdown_wbs_to_track_a(monkeypatch):
    """「规划产品发布会」→ task_breakdown → brain 出 WBS 子任务（schedule+估时）
    → dispatcher 规则直排 → draft_plan 草案条目（Track A 引擎定钟点）→ 出卡。"""
    actions = [
        {"title": "敲定场地与物料", "duration": 60, "action": "schedule"},
        {"title": "确认嘉宾名单", "duration": 45, "action": "schedule"},
        {"title": "撰写发布会通稿", "duration": 120, "action": "schedule"},
        {"title": "排练彩排流程", "duration": 90, "action": "schedule"},
    ]
    out, cap = _invoke(
        monkeypatch, "规划一下这周五的产品发布会",
        classify=("task_breakdown", None),
        brain_reply=_brain("task_breakdown",
                           strategy="## 发布会拆解\n从交付物倒推…",
                           checklist=["定场地", "锁嘉宾", "写通稿"],
                           actions=actions))
    assert cap.count(KEY_SCENARIO["task_breakdown"]) == 1  # 分类后进大脑
    drafts = out.get("draft_plan") or []
    assert len(drafts) == 4
    assert all(d.get("start") for d in drafts)  # Track A 已定钟点
    assert all(datetime.fromisoformat(d["start"]) >= datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0) for d in drafts)
    assert out.get("preflight")  # 拆解场景 checklist+strategy 非空 → 出卡
    assert "发布会" in (out.get("final_reply") or "")


def test_breakdown_actions_clamped(monkeypatch):
    """brain 输出防御钳制：9 条截 6 / 超长标题截 30 / duration 500→240 /
    非法 action 丢条 / task_id 白名单越界置 None。"""
    actions = [{"title": f"子任务{i}", "duration": 30, "action": "schedule"}
               for i in range(9)]
    actions.append({"title": "x" * 50, "duration": 500, "action": "reschedule",
                    "task_id": "fake-id"})
    actions.append({"title": "合法条目", "action": "fantasy"})  # 越枚举丢
    out = _clamp_out({"strategy": "s", "checklist": ["c"] * 8,
                      "action_items": actions,
                      "memory_updates": [{"category": "作息", "content": "x"}]},
                     valid_ids=set())
    assert len(out["checklist"]) == 6
    assert len(out["action_items"]) == 6
    assert all(len(a["title"]) <= 30 for a in out["action_items"])
    durs = [a["duration"] for a in out["action_items"] if "duration" in a]
    assert all(d <= 240 for d in durs)
    assert all(a.get("task_id") is None for a in out["action_items"])
    assert out["memory_updates"] == []  # evidence 空 → 整条丢


def test_breakdown_no_slot_degrades_inbox(monkeypatch):
    """7 天窗全满 → schedule 条目降级 inbox 待确认（绝不丢弃、绝不硬塞）。"""
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    for i in range(8):  # 未来 8 天每天 08:00-22:00 全占（含日首空档）
        d = now + timedelta(days=i)
        _db_task("满档", d.replace(hour=8), d.replace(hour=22))
    out, _ = _invoke(
        monkeypatch, "规划产品发布会", classify=("task_breakdown", None),
        brain_reply=_brain("task_breakdown", actions=[
            {"title": "敲定场地", "duration": 60, "action": "schedule"}]))
    assert not (out.get("draft_plan") or [])
    inbox = out.get("inbox_items") or []
    assert inbox and inbox[0]["title"] == "敲定场地"


# —— 场景 2：晨间前瞻 / 夜间复盘 ——————————————————————————————————————————————


def _today_tasks():
    """今天 1 件高优深度硬骨头 + 3 件流水杂事（晨报挑骨头的原料）。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _db_task("架构重构方案", today + timedelta(hours=9),
             today + timedelta(hours=11), priority=1, duration=120)
    _db_task("回复邮件", today + timedelta(hours=11, minutes=30),
             today + timedelta(hours=12), duration=30)
    _db_task("站会", today + timedelta(hours=14), today + timedelta(hours=14, minutes=30),
             duration=30)
    _db_task("整理工位", today + timedelta(hours=15), today + timedelta(hours=15, minutes=30),
             duration=30)


def test_morning_brief_picks_hard_bone(monkeypatch):
    """晨报：morning 场景 payload 必须带高优任务原料（inbox_high/today），
    brain 策略点名硬骨头 → 非流水账形态；headline 确定性合成。"""
    _today_tasks()
    cap = MultiKeyLLMCapture({
        KEY_SCENARIO["morning"]: _brain(
            "morning", strategy="今天最值得保护的是「架构重构方案」，建议上午黄金段"
            "整块推进，午后批量处理邮件与会务。今日背靠背较多，站会前后各留十分钟缓冲。",
            checklist=["上午整块写方案", "午后批量回邮件"])})
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    with TestingSessionLocal() as db:
        out = run_preflight(db, datetime.now().date())
    assert cap.count(KEY_SCENARIO["morning"]) == 1
    payload = cap.users(KEY_SCENARIO["morning"])[0]
    assert "架构重构方案" in payload  # 高能耗硬骨头进入大脑视野
    assert "inbox_high" in payload and "rhythm" in payload  # 场景键集齐
    pre = out["preflight"]
    assert pre["headline"].endswith("晨间启航")
    assert len(pre["checklist"]) == 2
    assert "架构重构方案" in out["final_reply"]
    assert len(out["final_reply"]) >= 60  # 非一句话流水账


def test_morning_brief_garbage_fallback(monkeypatch):
    """brain 垃圾输出 → 确定性 headline 卡（空清单）零崩溃，消息恒落。"""
    _today_tasks()
    cap = MultiKeyLLMCapture({}, default="这不是 JSON")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    with TestingSessionLocal() as db:
        out = run_preflight(db, datetime.now().date())
    assert out["preflight"]["headline"].endswith("晨间启航")
    assert out["preflight"]["checklist"] == []
    assert out["final_reply"]  # 恒有意义（坑 ⑦）


def test_morning_overload_signal_in_payload(monkeypatch):
    """过载日（今日 10h+ 排满）→ load_summary 进 payload（减负建议的原料）。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(10):
        _db_task(f"会议{h}", today + timedelta(hours=8 + h),
                 today + timedelta(hours=9 + h))
    cap = MultiKeyLLMCapture({
        KEY_SCENARIO["morning"]: _brain("morning", strategy="今天满档…")})
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    with TestingSessionLocal() as db:
        run_preflight(db, datetime.now().date())
    payload = cap.users(KEY_SCENARIO["morning"])[0]
    assert "load_summary" in payload


def test_nightly_review_scenario(monkeypatch):
    """夜间复盘：cron 直打 nightly → 场景键集（today_past/rescheduled/rhythm）
    齐 + memory_updates 确认制问询出卡。复盘日（明天）有任务避免静好早退。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _db_task("晨会", today + timedelta(hours=9), today + timedelta(hours=10))
    _db_task("明日评审", today + timedelta(days=1, hours=10),
             today + timedelta(days=1, hours=11))
    cap = MultiKeyLLMCapture({
        KEY_SCENARIO["nightly"]: _brain(
            "nightly", strategy="今天执行偏差主要在…",
            memories=[{"category": "精力节律", "content": "上午开会效率低",
                       "evidence": "今天晨会后的三件事全部改期"}])})
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    with TestingSessionLocal() as db:
        out = run_preflight(db, datetime.now().date() + timedelta(days=1))
    assert cap.count(KEY_SCENARIO["nightly"]) == 1
    payload = cap.users(KEY_SCENARIO["nightly"])[0]
    assert "today_past" in payload and "rescheduled" in payload
    # memory 确认制问询由 run_preflight 承接落 question 行（返回锚点 id，
    # 子图 question_plan 键不透传）
    assert out.get("question_proposal_id")
    assert "同意写入长期记忆" in out["final_reply"]


# —— 场景 3：危机干预与盘面重组 ——————————————————————————————————————————————


def test_crisis_regex_zero_llm():
    """危机正则命中「全乱了」→ 零 LLM 直判 crisis（短路栈第④步）。"""
    cap = MultiKeyLLMCapture()
    out = classifier_node({"user_input": "全乱了，三个会都撞了，救命", "pending": None})
    assert out["scenario_type"] == "crisis"


def test_crisis_reorganize_e2e(monkeypatch):
    """危机 E2E：紧急报告插入满档下午 → brain 出重组方案（今日放不下的
    reschedule + 新紧急 schedule）→ dispatcher 全部出草案卡，确认制——
    断言无引擎级自动砍会议（原日程行零改动，一切经用户点卡）。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    tid_meeting = _db_task("季度汇报会", tomorrow + timedelta(hours=14),
                           tomorrow + timedelta(hours=15), priority=1)
    tid_review = _db_task("代码评审", tomorrow + timedelta(hours=15),
                          tomorrow + timedelta(hours=16), priority=2)
    brain_reply = _brain(
        "crisis", strategy="先别慌。四象限止血：紧急报告优先……"
        "拒绝话术：「评审能否顺延到明早十点？」",
        actions=[
            {"title": "季度汇报会改到明天", "action": "reschedule",
             "task_id": tid_meeting, "duration": 60},
            {"title": "今晚写紧急报告", "action": "schedule", "duration": 120},
        ])
    out, cap = _invoke(
        monkeypatch, "全乱了，今晚必须交的紧急报告，下午还全满了",
        classify=None,  # 危机正则零 LLM
        brain_reply=brain_reply)
    assert cap.count(KEY_CLASSIFIER) == 0
    drafts = out.get("draft_plan") or []
    assert len(drafts) == 2
    by_title = {d["title"]: d for d in drafts}
    rescheduled = [d for d in drafts if d.get("task_id")]
    assert rescheduled and rescheduled[0]["task_id"] == tid_meeting
    # 原日程未被引擎擅动（真库行原时间不变——重组只是草案待确认）
    with TestingSessionLocal() as db:
        row = db.get(Task, tid_review)
        assert row.start_time == tomorrow + timedelta(hours=15)
    assert "拒绝话术" in out["final_reply"]  # 得体的改期/请假话术随策略输出


def test_crisis_negative_sentence_still_hits_regex():
    """负向锁定：「不紧急」也命中危机正则（无否定豁免）——误路由已知取舍，
    修复方向（否定前置豁免）登记 docs/qa-bug-checklist.md 待拍板。"""
    out = classifier_node({"user_input": "这事不紧急，下周再说", "pending": None})
    assert out["scenario_type"] == "crisis"


# —— 场景 4：信息不足追问挂起 ——————————————————————————————————————————————


def test_insufficient_info_asks_not_invents(monkeypatch):
    """「帮我准备下周的会」信息不足 → ask 追问挂起（question_plan+
    strategist_clarify），零编造零落库。"""
    out, cap = _invoke(
        monkeypatch, "帮我准备下周的会",
        classify=("meeting_prep", "是哪场会？告诉我会的主题和时间"),
        brain_reply=None)
    assert cap.count(KEY_SCENARIO["meeting_prep"]) == 0  # 追问轮零大脑调用
    q = out.get("question_plan")
    assert q and q["kind"] == "strategist_clarify"
    assert q["original_input"] == "帮我准备下周的会"
    assert not (out.get("draft_plan") or [])
    assert "是哪场会" in out["final_reply"]


def _clarify_pending():
    return {"proposal_id": "pq1", "kind": "strategist_clarify", "entries": [],
            "original_input": "帮我准备下周的会",
            "ask_text": "是哪场会？", "scenario_type": "meeting_prep",
            "origin": "strategist"}


def test_clarify_consume_resume_zero_classify(monkeypatch):
    """补充信息消费挂起：agree 裁决 → 恢复轮零分类 LLM（挂起快照续场景）
    → meeting_prep 大脑照常出策略。"""
    out, cap = _invoke(
        monkeypatch, "下周三的融资路演，20 个投资人",
        pending=_clarify_pending(), judge="agree",
        brain_reply=_brain("meeting_prep", strategy="路演准备三步走…"))
    assert cap.count(KEY_CLASSIFIER) == 0  # 恢复轮零分类
    assert cap.count(KEY_PENDING_JUDGE) == 1
    payload = cap.users(KEY_SCENARIO["meeting_prep"])[0]
    assert "融资路演" in payload and "帮我准备下周的会" in payload  # 原话+补充合并
    assert not out.get("question_plan")
    assert out.get("final_reply")


def test_clarify_unrelated_second_guide(monkeypatch):
    """答非所问（≤12 字无动作词）→ 二次引导，问询保留、零 LLM。"""
    out, cap = _invoke(
        monkeypatch, "嗯", pending=_clarify_pending())
    assert cap.count(KEY_PENDING_JUDGE) == 1  # judge 先判 unrelated（垃圾输出兜底）
    assert "没说清楚" in out["final_reply"]  # 二次引导：换个角度重问
    assert not out.get("question_plan")  # 挂起保留在 proposals 层，本轮零新卡


# —— 其余场景补齐 ———————————————————————————————————————————————————————————


def test_stakeholder_talk_tracks_no_cards(monkeypatch):
    """stakeholder：话术即产物（三档话术进 strategy），零草案零简报卡。"""
    out, _ = _invoke(
        monkeypatch, "怎么跟老板汇报项目延期",
        classify=("stakeholder", None),
        brain_reply=_brain("stakeholder",
                           strategy="**温和档**：「老板，项目因X延期…」\n"
                                    "**正式档**：…\n**升级档**：…"))
    assert not (out.get("draft_plan") or [])
    assert not out.get("preflight")  # 非拆解/会前/危机不出卡
    assert "温和" in out["final_reply"]


def test_execution_memory_update_confirm_flow(monkeypatch):
    """execution 场景 memory_updates → 确认制问询（kind=memory），不直接落库。"""
    out, _ = _invoke(
        monkeypatch, "周报写不动怎么办",
        classify=("execution", None),
        brain_reply=_brain(
            "execution", strategy="卡点是启动门槛…",
            actions=[], memories=[{"category": "精力节律",
                                   "content": "下午4点后写作效率高",
                                   "evidence": "近两周周报都在傍晚完成"}]))
    q = out.get("question_plan")
    assert q and q["kind"] == "memory"
    assert q["rule"]["content"] == "下午4点后写作效率高"
    assert not (out.get("draft_plan") or [])


# —— chat SSE 全链 E2E（HTTP 入口 → 主图路由 → 幕僚子图 → 结算落库）————————


def _chat_breakdown_stubs(monkeypatch, strategy="## 发布会拆解：从交付物倒推"):
    actions = [
        {"title": "敲定场地与物料", "duration": 60, "action": "schedule"},
        {"title": "确认嘉宾名单", "duration": 45, "action": "schedule"},
    ]
    cap = MultiKeyLLMCapture({
        KEY_ROUTER: '{"intent": "coach", "reason": "拆解规划诉求"}',
        KEY_CLASSIFIER: '{"scenario": "task_breakdown", "ask": null}',
        KEY_SCENARIO["task_breakdown"]: _brain(
            "task_breakdown", strategy=strategy, checklist=["定场地"],
            actions=actions),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    return cap


def test_chat_breakdown_draft_frame_and_settle(monkeypatch, client):
    """全链 E2E：chat SSE → draft 帧 → 模拟前端两步结算 → 任务真落日历
    （起止与草案一致）→ proposal 回写 accepted。"""
    activate_model()
    _chat_breakdown_stubs(monkeypatch)
    events = chat_events(client, "产品发布会筹备千头万绪，帮我理一理思路")
    types = [e["type"] for e in events]
    assert "draft" in types and "reply" in types and "done" in types
    draft = first_of(events, "draft")
    assert draft["proposal_id"] and len(draft["draft_plan"]) == 2
    created = settle_draft(client, draft["proposal_id"], draft["draft_plan"])
    assert len(created) == 2
    from datetime import datetime as _dt
    row0, entry0 = created[0], draft["draft_plan"][0]
    assert row0["title"] == entry0["title"][:30]
    assert _dt.fromisoformat(row0["start_time"]) ==         _dt.fromisoformat(entry0["start"])


def test_chat_breakdown_modified_reflow(monkeypatch, client):
    """modified 回流：用户编辑草案时间后采纳 → payload.modified_diff 非空
    （反思链学习「用户改成了什么」的原料）。"""
    activate_model()
    _chat_breakdown_stubs(monkeypatch)
    events = chat_events(client, "产品发布会筹备千头万绪，帮我理一理思路")
    draft = first_of(events, "draft")
    from datetime import datetime as _dt, timedelta as _td
    edited = [dict(e) for e in draft["draft_plan"]]
    e0 = _dt.fromisoformat(edited[0]["start"]) + _td(hours=2)
    edited[0]["start"] = e0.isoformat(timespec="minutes")
    settle_draft(client, draft["proposal_id"], edited, outcome="modified",
                 modified_entries=edited)
    with TestingSessionLocal() as db:
        row = ai_data_get(db, draft["proposal_id"])
    assert row["payload"].get("modified_diff")
    assert row["outcome"] == "modified"


def ai_data_get(db, proposal_id):
    from services.ai_data import list_proposals

    for p in list_proposals(db):
        if p.id == proposal_id:
            return {"payload": p.payload or {}, "outcome": p.outcome}
    raise AssertionError("proposal 不存在")


def test_chat_crisis_e2e_reschedule_confirm_only(monkeypatch, client):
    """危机全链：chat SSE 危机正则直判（router LLM coach）→ 重组草案卡
    （reschedule 带原任务 id）→ settle 走 PATCH 改期——确认制，原任务本体
    只被用户点卡的动作改动。"""
    activate_model()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    tid = _db_task("季度汇报会", tomorrow + timedelta(hours=14),
                   tomorrow + timedelta(hours=15), priority=1)
    cap = MultiKeyLLMCapture({
        KEY_ROUTER: '{"intent": "coach", "reason": "危机情绪"}',
        KEY_SCENARIO["crisis"]: _brain(
            "crisis", strategy="先别慌。四象限止血……拒绝话术：「评审顺延可吗？」",
            actions=[{"title": "季度汇报会改到明天上午", "action": "reschedule",
                      "task_id": tid, "duration": 60}]),
    }, default="")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_strategist_reads(monkeypatch)
    events = chat_events(client, "全乱了，今晚必须交的紧急报告，明天下午还满着")
    draft = first_of(events, "draft")
    assert draft and draft["draft_plan"][0]["task_id"] == tid
    settle_draft(client, draft["proposal_id"], draft["draft_plan"])
    with TestingSessionLocal() as db:
        row = db.get(Task, tid)
    # 改期落库日期无关断言（hint 含「今晚/明天」相对词——跨午夜运行时
    # 「今晚」钳 now 后最早槽落今天日首，引擎行为正确；绝不允许过去）
    assert row.start_time.date() >= datetime.now().date()
    assert row.start_time.date() <= tomorrow.date()


def test_reschedule_past_not_before_clamped():
    """QA 批发现修复回归（Bug#1）：「今晚…改期」经 _hint_to_not_before 产过去
    起点（今天 0 点 < now）→ plan_slots 必须钳到 now，绝不产出已过时刻草案。"""
    from agents.engine import plan_slots

    now = datetime.now().replace(second=0, microsecond=0)
    slots = plan_slots(
        entries=[{"key": "k", "title": "季度汇报会", "duration": 60,
                  "from": now.replace(hour=0) - timedelta(days=3)}],
        busy=[], now=now, day_start_min=480, day_end_min=1320,
        lunch=(690, 810), use_ai=False)
    assert slots["k"] is not None and slots["k"] >= now
