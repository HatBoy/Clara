"""agents.graph.run_preflight 测试（批 D 起步，2026-10-21 Track B 批 6 收编
strategist 子图 cron 直调后全量迁移）。

替身口径（docs/ai-agents.md §五 两会话陷阱）：
- run_preflight 自带 db 形参（壳传入，不开 SessionLocal）——无需 patch 会话；
- strategist 子图读库走 cf/disp 两模块级 SessionLocal——patch 到测试库；
- LLM：monkeypatch services.llm.call_llm 按「【场景：morning/nightly」分发
  （cron 直打 tag 零分类，子图唯一 LLM=大脑）；
- 读库：monkeypatch services.tasks.list_tasks 返回 SimpleNamespace 任务替身；
- 记忆规则走真表（TestingSessionLocal + ai_data.upsert_memory_rule 造行）。
"""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import agents.strategist.context_fetcher as cf
import agents.strategist.dispatcher as disp
import agents.graph as graph_mod
from conftest import TestingSessionLocal
from services import ai_data, llm
from services import tasks as tasks_svc


class _LLMCapture:
    """call_llm 替身：按 system prompt 子串分发预设应答并记录 (system, user) 调用。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None):
        system = messages[0]["content"]
        self.calls.append((system, messages[1]["content"]))
        for prompt, reply in self.replies.items():
            if prompt in system:
                return reply
        return self.default


def _task(id_="t1", title="评审会", prio=1, start=None, rrule=None):
    """任务替身（对齐 ORM Task 的消费面——_task_dur 读 duration_mins/end_time）。"""
    return SimpleNamespace(id=id_, title=title, priority=prio, start_time=start,
                           end_time=None, notes=None, duration_mins=90, reschedule_count=0,
                           recurrence_rule=rrule, sort_order=0,
                           done_at=None)


def _patch(monkeypatch, scheduled, inbox=None):
    """子图读库替身：cf/disp SessionLocal + list_tasks 两口径。"""
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        return (inbox or []) if unscheduled else scheduled

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)


def _brain_reply(scenario, headline="上午高压注意留白", reply="上午两场高优连轴。",
                 checklist=("会前打印材料",), memory=None):
    out = {"strategy": f"## {headline}\n{reply}", "checklist": list(checklist),
           "action_items": [],
           "memory_updates": [memory] if memory else []}
    return {f"【场景：{scenario}": json.dumps(out, ensure_ascii=False)}


def test_run_preflight_today_full_calendar_into_morning(monkeypatch):
    # 收编后旧「高优筛选」退役：今日全量进 deep_context（选择职责归场景段方法论）
    fake = _LLMCapture(_brain_reply("morning"))
    monkeypatch.setattr(llm, "call_llm", fake)
    day = date.today()
    _patch(monkeypatch, [
        _task(id_="t1", title="评审会", prio=1, start=datetime(day.year, day.month, day.day, 9)),
        _task(id_="t3", title="浇花", prio=3, start=datetime(day.year, day.month, day.day, 16)),
    ])
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, day)
    # cron 轮 headline 确定性合成（LLM headline 只进 strategy 正文）
    assert out["preflight"]["headline"] == f"{day.month}月{day.day}日 晨间启航"
    assert out["preflight"]["checklist"] == ["会前打印材料"]
    assert "上午两场高优连轴。" in out["final_reply"]
    system, payload = fake.calls[0]
    assert "【场景：morning" in system  # 今天 → 晨间场景段
    data = json.loads(payload)
    assert [t["id"] for t in data["上下文"]["today"]] == ["t1", "t3"]  # 全量视野无筛选
    assert data["上下文"]["now"]


def test_run_preflight_no_tasks_zero_llm(monkeypatch):
    # 无任务 → 零 LLM 调用，headline 如实 + final_reply 有意义（坑 ⑦）
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch(monkeypatch, [])
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, date.today())
    assert fake.calls == []
    assert "没有已排期日程" in out["preflight"]["headline"]
    assert out["preflight"]["checklist"] == []
    assert out["final_reply"]


def test_run_preflight_tomorrow_uses_nightly_scenario(monkeypatch):
    fake = _LLMCapture(_brain_reply("nightly", headline="明早带材料",
                                    reply="为明天做准备。", checklist=["今晚确认打印"]))
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch(monkeypatch, [_task(id_="t1", title="晨会", prio=1)])
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, date.today() + timedelta(days=1))
    assert out["preflight"]["checklist"] == ["今晚确认打印"]
    system, _ = fake.calls[0]
    assert "【场景：nightly" in system  # 明日 → 前夜综述场景段


def test_run_preflight_garbage_degrades(monkeypatch):
    # 垃圾输出 → 大脑兜底文案 + 确定性 headline 卡，不 raise（解析失败是数据）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(default="抱歉我不会"))
    day = date.today()
    _patch(monkeypatch, [_task(id_="t1", title="评审会", prio=1)])
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, day)
    assert out["preflight"]["headline"] == f"{day.month}月{day.day}日 晨间启航"
    assert "没能成形" in out["final_reply"]  # 大脑兜底文案


def test_run_preflight_memory_rules_injected_into_payload(monkeypatch):
    # 记忆规则经人格前置进 memory_rules → build_strategist_prompt 的「记忆画像」键
    fake = _LLMCapture(_brain_reply("morning", reply="照常。", checklist=()))
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch(monkeypatch, [_task(id_="t1", title="评审会", prio=1)])
    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="作息规律", content="上午10点前不排会",
                                   confidence=0.8)
        graph_mod.run_preflight(db, date.today())
    system, payload = fake.calls[0]
    assert "【场景：morning" in system
    # 2026-11-03 批 3 起双段单源分级格式（confidence=0.8 归铁律段，标签带置信）
    assert "[作息规律·AI学习·置信0.8] 上午10点前不排会" in json.loads(payload)["记忆画像"]


def test_run_preflight_prepends_personality_rules(monkeypatch):
    # 人格规则前置（2026-10-20）：设置页风格/哲学排在记忆规则之前
    fake = _LLMCapture(_brain_reply("morning", reply="好。", checklist=()))
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch(monkeypatch, [_task(id_="t1", title="评审会", prio=1)])
    from services.settings import get_settings
    with TestingSessionLocal() as db:
        ai_data.upsert_memory_rule(db, category="作息规律",
                                   content="上午10点前不排会", confidence=0.8)
        s = get_settings(db)
        s.ai_style = "concise"
        s.ai_philosophy = "mit-3"
        db.commit()
        graph_mod.run_preflight(db, date.today())
    _, payload = fake.calls[0]
    rules = json.loads(payload)["记忆画像"]
    assert rules[0].startswith("[回复风格]")  # 人格在前
    # 2026-11-03 批 3 起双段单源分级格式
    assert "[作息规律·AI学习·置信0.8] 上午10点前不排会" in rules[-1]


def test_run_preflight_memory_question_anchored(monkeypatch):
    # cron 轮 memory 问询承接（批 6）：question 行落库 + question_proposal_id
    # 返回键（消费端消息锚点挂它）+ ask 拼进 final_reply 尾部；消费端按锚点落
    # assistant 消息后 active_pending 快照激活（kind=memory/origin=strategist）
    fake = _LLMCapture(_brain_reply(
        "nightly", reply="今天执行阻力在下午。", checklist=["晚安"],
        memory={"category": "精力节律", "content": "下午3点不宜深度创作",
                "evidence": "近两周下午3点任务三次被推迟"}))
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch(monkeypatch, [_task(id_="t1", title="评审会", prio=1)])
    tomorrow = date.today() + timedelta(days=1)
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, tomorrow)
        assert "同意写入长期记忆吗" in out["final_reply"]  # ask 已拼进回复
        qid = out["question_proposal_id"]
        row = ai_data.get_proposal(db, qid)  # 问询行已落库未决
        assert row is not None and row.kind == "question"
        assert row.payload["kind"] == "memory"
        # 消费端（workflows）把 assistant 消息锚点挂问询行 → 快照激活
        ai_data.append_message(db, role="assistant", content=out["final_reply"],
                               proposal_id=qid)
        snap = ai_data.active_pending(db)
        assert snap is not None and snap["proposal_id"] == qid
        assert snap["kind"] == "memory" and snap["origin"] == "strategist"
        assert snap["rule"]["content"] == "下午3点不宜深度创作"


# —— 生日/纪念日前瞻（2026-10-22 批 5）：晨间零 LLM 扫描 → kind=inbox 卡 ————


def test_run_preflight_birthday_inbox_card(monkeypatch):
    # 未来 7 天窗口内命中「生日/纪念日」→ kind=inbox 礼物准备卡 + assistant
    # 消息锚点挂生日卡；简报回复带附注。list_tasks 双口径替身：窗口查询返回
    # 生日任务（前瞻扫描复用同一替身——start/end 参数区分当日/7 天窗）
    fake = _LLMCapture(_brain_reply("morning"))
    monkeypatch.setattr(llm, "call_llm", fake)
    day = date.today()
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    birthday = _task(id_="b1", title="妈妈生日", prio=2,
                     start=datetime(day.year, day.month, day.day, 9))
    daily = _task(id_="t1", title="评审会", prio=1,
                  start=datetime(day.year, day.month, day.day, 14))

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        if unscheduled:
            return []
        # 当日窗口（end-lo ≤ 1 天）返回当日任务；7 天前瞻窗返回生日任务
        span = (end - start).days
        return [daily, birthday] if span <= 1 else [birthday]

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, day)
    assert "礼物" in out["final_reply"]
    from models import Proposal
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        cards = db.scalars(select(Proposal).where(Proposal.kind == "inbox")).all()
        assert len(cards) == 1
        items = cards[0].payload["items"]
        assert items[0]["title"] == "为「妈妈生日」准备礼物"
        assert "妈妈生日" in items[0]["notes"]
        msgs = ai_data.list_messages(db)
        assert any("礼物" in (m.content or "") for m in msgs)


def test_run_preflight_birthday_dedup_within_lookahead(monkeypatch):
    # 同一周内晨跑重扫：近 7 天已出同名条目 → 不重出卡（连出 7 天卡会刷屏）
    fake = _LLMCapture(_brain_reply("morning"))
    monkeypatch.setattr(llm, "call_llm", fake)
    day = date.today()
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    birthday = _task(id_="b1", title="结婚纪念日", prio=2,
                     start=datetime(day.year, day.month, day.day, 9))

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        if unscheduled:
            return []
        return [birthday]  # 当日与前瞻窗都返回——静好分支不触发（当日有任务）

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    with TestingSessionLocal() as db:
        graph_mod.run_preflight(db, day)   # 第一次：出卡
        out2 = graph_mod.run_preflight(db, day)  # 第二次：去重不出
    assert "礼物" not in out2["final_reply"]
    from models import Proposal
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        cards = db.scalars(select(Proposal).where(Proposal.kind == "inbox")).all()
        assert len(cards) == 1


def test_run_preflight_birthday_skipped_on_nightly(monkeypatch):
    # 夜综述形态（day=明天）不扫生日——礼物提醒放早上（is_eve 早退分支）
    fake = _LLMCapture(_brain_reply("nightly"))
    monkeypatch.setattr(llm, "call_llm", fake)
    tomorrow = date.today() + timedelta(days=1)
    birthday = _task(id_="b1", title="爸爸生日", prio=2,
                     start=datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9))
    _patch(monkeypatch, [birthday])
    with TestingSessionLocal() as db:
        out = graph_mod.run_preflight(db, tomorrow)
    assert "礼物" not in out["final_reply"]


def test_run_preflight_birthday_skipped_in_low_trust(monkeypatch):
    """低介入档跳过生日前瞻（优化批 5 信任闸）：近 14 天采纳率 <30% 时礼物卡
    不再出——简报本体照常（被动应答保留），主动推送物降频。"""
    with TestingSessionLocal() as db:
        for i in range(10):  # 10 样本仅 1 采纳 = 10% < 30%
            row = ai_data.create_proposal(db, kind="schedule", payload={})
            ai_data.record_proposal_outcome(
                db, row.id, "accepted" if i < 1 else "rejected")
        out = graph_mod.run_preflight(db, date.today())
    assert "生日" not in (out["final_reply"] or "")
