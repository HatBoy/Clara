"""Node2 context_fetcher 测试（Track B 批 3，2026-10-21）：七场景键集参数化
（_SCENARIO_KEYS 单源反向验证代码分支）+ 各场景取数内容断言（晨间昨日结局与
高优收件箱 / 夜间四源硬边界 / 危机余量切分 / 干系人 k=10 / 会前天气与 exclude）。
state.now 固定注入（确定性切分 today_past/today_rest），SessionLocal 替测试库。
"""

from datetime import datetime, timedelta

import agents.strategist.context_fetcher as cf
from agents.strategist.context_fetcher import (
    _SCENARIO_KEYS,
    context_fetcher_node,
)
from conftest import TestingSessionLocal
from models import Task
from services import ai_data

_NOW = "2026-10-21T20:00"  # 固定时刻：today_past < 20:00、today_rest ≥ 20:00


def _patch(monkeypatch):
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)


def _state(scenario, **extra):
    s = {"scenario_type": scenario, "now": _NOW,
         "trigger_input": "帮我拆解发布会物料", "user_input": "帮我拆解发布会物料"}
    s.update(extra)
    return s


def _run(monkeypatch, scenario, **extra):
    _patch(monkeypatch)
    return context_fetcher_node(_state(scenario, **extra))["deep_context"]


def _add_task(db, **kw):
    row = Task(**kw)
    db.add(row)
    db.commit()
    return row


# —— 键集矩阵（七场景 + 未知兜底）——————————————————————————————————————


def test_scenario_keys_matrix(monkeypatch, fresh_db):
    # related_facts（2026-11-03 批 6）为全场景统一键（Tier 2 事实软参考）
    for scenario, keys in _SCENARIO_KEYS.items():
        ctx = _run(monkeypatch, scenario)
        assert set(ctx) == {"now", "related_facts"} | set(keys), scenario
    # 白名单外兜底 execution 形
    ctx = _run(monkeypatch, "bogus")
    assert set(ctx) == {"now", "related_facts"} | set(_SCENARIO_KEYS["execution"])


def test_recent_chat_scenarios_carry_chat_summary(monkeypatch):
    """滚动摘要随 recent_chat 成对注入（2026-10-26）：会前/执行/干系人三场景带
    chat_summary——教练链此前丢摘要，跨天语境接不住；同批晨/夜/危机也补（晨报
    失忆修复：用户昨晚聊的决心晨报要接得住——morning/nightly/crisis 同款断言）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="早前交代")
        ai_data.save_chat_summary(db, summary="用户提过偏好上午写代码", count=1)
    ctx = _run(monkeypatch, "execution")
    assert ctx["chat_summary"] == "用户提过偏好上午写代码"
    # task_breakdown 补键（2026-11-01 优化批 1）：最常用教练场景此前无对话
    # 语境与天气——「帮我把刚才聊的那个活动组织一下」接不住的遗忘点
    assert _run(monkeypatch, "task_breakdown")["chat_summary"] == "用户提过偏好上午写代码"
    assert _run(monkeypatch, "stakeholder")["chat_summary"] == "用户提过偏好上午写代码"
    assert _run(monkeypatch, "meeting_prep")["chat_summary"] == "用户提过偏好上午写代码"
    assert _run(monkeypatch, "morning")["chat_summary"] == "用户提过偏好上午写代码"
    assert _run(monkeypatch, "nightly")["chat_summary"] == "用户提过偏好上午写代码"
    assert _run(monkeypatch, "crisis")["chat_summary"] == "用户提过偏好上午写代码"


# —— task_breakdown：槽位地形 + 相关检索 —————————————————————————————————


def test_task_breakdown_entries_and_related(monkeypatch, fresh_db):
    tomorrow_10 = datetime(2026, 10, 22, 10, 0)
    with TestingSessionLocal() as db:
        _add_task(db, title="跟设计过稿", start_time=tomorrow_10, duration_mins=60)
        _add_task(db, title="发布会物料设计", duration_mins=45)  # 收件箱（未排期）
    ctx = _run(monkeypatch, "task_breakdown")
    assert ctx["tomorrow"][0]["title"] == "跟设计过稿"
    assert ctx["tomorrow"][0]["end"] == "2026-10-22T11:00"
    assert ctx["inbox"][0]["title"] == "发布会物料设计"
    hit = [r for r in ctx["related"] if r["title"] == "发布会物料设计"]
    assert hit and hit[0]["score"] >= 0.4  # 阈值之上按分入列


# —— morning：昨日结局 + 高优收件箱 ——————————————————————————————————————


def test_morning_yesterday_outcomes_and_inbox_high(monkeypatch, fresh_db):
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="schedule", payload={})
        row.created_at = datetime(2026, 10, 20, 9, 0)  # 挪进昨日窗
        db.commit()
        ai_data.record_proposal_outcome(db, row.id, "accepted")
        # 建序在前的是中优，高优排后面——排序后高优在前（复查批 A 口径承袭）
        _add_task(db, title="高优硬骨头", duration_mins=90, priority=1, sort_order=3)
        _add_task(db, title="另一件高优", duration_mins=60, priority=1, sort_order=1)
        _add_task(db, title="低优杂事", duration_mins=10, priority=3)
    ctx = _run(monkeypatch, "morning")
    assert ctx["yesterday_outcomes"] == [{"kind": "schedule", "outcome": "accepted",
                                           "reject_reason": None}]  # 批 4 补键
    assert [t["title"] for t in ctx["inbox_high"]] == ["另一件高优", "高优硬骨头"]


# —— nightly：四源硬边界 ———————————————————————————————————————————————————


def test_nightly_four_sources(monkeypatch, fresh_db):
    with TestingSessionLocal() as db:
        _add_task(db, title="上午过稿会", start_time=datetime(2026, 10, 21, 9, 0),
                  duration_mins=60)
        _add_task(db, title="晚上赶方案", start_time=datetime(2026, 10, 21, 22, 0),
                  duration_mins=60)
        _add_task(db, title="反复挪动的评审", start_time=datetime(2026, 10, 21, 15, 0),
                  duration_mins=30, reschedule_count=2)
        prop = ai_data.create_proposal(db, kind="schedule", payload={})
        prop.created_at = datetime(2026, 10, 21, 12, 0)  # 挪进当日窗（跨天必挪，morning 同款）
        msg = ai_data.append_message(db, role="user", content="今天上午把发布会物料改完了")
        msg.created_at = datetime(2026, 10, 21, 12, 30)
        db.commit()
        ai_data.record_proposal_outcome(db, prop.id, "accepted")
    ctx = _run(monkeypatch, "nightly")
    assert [e["title"] for e in ctx["today_past"]] == ["上午过稿会", "反复挪动的评审"]
    assert [e["title"] for e in ctx["tomorrow"]] == []
    assert ctx["today_outcomes"] == [{"kind": "schedule", "outcome": "accepted",
                                      "reject_reason": None}]  # 批 4 补键
    assert ctx["rescheduled"][0]["reschedule_count"] == 2
    assert any("发布会物料" in x for x in ctx["day_excerpts"])


# —— crisis：今日余量切分 ———————————————————————————————————————————————————


def test_crisis_today_rest_splits_at_now(monkeypatch, fresh_db):
    with TestingSessionLocal() as db:
        _add_task(db, title="上午过稿会", start_time=datetime(2026, 10, 21, 9, 0),
                  duration_mins=60)
        _add_task(db, title="晚间救火", start_time=datetime(2026, 10, 21, 21, 0),
                  duration_mins=45)
    ctx = _run(monkeypatch, "crisis")
    assert [e["title"] for e in ctx["today_rest"]] == ["晚间救火"]


# —— stakeholder：对话窗 k=10 —————————————————————————————————————————————


def test_stakeholder_chat_window_k10(monkeypatch, fresh_db):
    with TestingSessionLocal() as db:
        for i in range(12):
            ai_data.append_message(db, role="user" if i % 2 == 0 else "assistant",
                                   content=f"第{i}轮沟通内容")
    ctx = _run(monkeypatch, "stakeholder")
    assert len(ctx["recent_chat"]) == 10
    assert ctx["recent_chat"][0]["content"] == "第2轮沟通内容"  # 最近 10 条窗口左沿


# —— meeting_prep：天气透传 + 本轮原话剔除 ————————————————————————————————


def test_meeting_prep_weather_and_exclude(monkeypatch, fresh_db):
    monkeypatch.setattr("services.weather.forecast_for_city",
                        lambda city: [{"date": "2026-10-21", "text": "小雨",
                                       "precip": 80}])
    with TestingSessionLocal() as db:
        for i in range(8):
            ai_data.append_message(db, role="assistant", content=f"历史{i}")
        ai_data.append_message(db, role="user", content="帮我准备下周的会")
    ctx = _run(monkeypatch, "meeting_prep", user_input="帮我准备下周的会",
               trigger_input="帮我准备下周的会")
    assert ctx["weather"] == [{"date": "2026-10-21", "text": "小雨", "precip": 80}]
    assert all(m["content"] != "帮我准备下周的会" for m in ctx["recent_chat"])


# —— done 标（2026-10-22 批 1）：entries 完成标记 ————————————————————————


def test_entries_carry_done_flag(monkeypatch, fresh_db):
    # done=用户勾选的完成标记（夜间复盘「计划 vs 实际」完成信号）；未勾选 false
    # 只代表"没勾"，不能断言未完成——提示词铁律已明令
    with TestingSessionLocal() as db:
        _add_task(db, title="已勾选完成", start_time=datetime(2026, 10, 21, 9, 0),
                  duration_mins=60, done_at=datetime(2026, 10, 21, 10, 0))
        _add_task(db, title="未勾选", start_time=datetime(2026, 10, 21, 15, 0),
                  duration_mins=60)
    ctx = _run(monkeypatch, "nightly")
    flags = {e["title"]: e["done"] for e in ctx["today_past"]}
    assert flags == {"已勾选完成": True, "未勾选": False}


# —— 教练溯源 source_context（2026-10-23 批 3）——————————————————————————————


def test_source_context_static_evidence_injection(monkeypatch, fresh_db):
    """相关任务命中带 source_evidence → 静态摘录注入（每条截 300）；无溯源的
    相关任务不入列。探测替身恒 None（本用例只验静态段）。"""
    monkeypatch.setattr("services.data_ingest.probe_source",
                        lambda db, key, q: None)
    with TestingSessionLocal() as db:
        _add_task(db, title="发布会物料设计", duration_mins=45,
                  source_key="cli:fake", source_fingerprint="fp1",
                  source_evidence="素" * 400)  # 400 字 → 注入截 300
        _add_task(db, title="发布会物料印刷", duration_mins=30,
                  source_key="cli:fake", source_evidence="印刷厂报价与交期")
        _add_task(db, title="跟设计过稿", duration_mins=30)  # 相关但无溯源
    ctx = _run(monkeypatch, "task_breakdown")
    assert [it["task"] for it in ctx["source_context"]] == \
        ["发布会物料设计", "发布会物料印刷"]
    assert len(ctx["source_context"][0]["evidence"]) == 300  # 落库 500 注入收紧
    assert ctx["source_context"][1]["evidence"] == "印刷厂报价与交期"


def test_source_context_probe_once_and_scenario_gate(monkeypatch, fresh_db):
    """task_breakdown 探测一次（首个带 source_key 的命中任务、标题作查询）；
    stakeholder 只静态注入不探测（场景闸）。"""
    calls = []
    monkeypatch.setattr(
        "services.data_ingest.probe_source",
        lambda db, key, q: calls.append((key, q)) or "回源搜索片段")
    with TestingSessionLocal() as db:
        _add_task(db, title="发布会物料设计", duration_mins=45,
                  source_key="cli:fake", source_evidence="静态摘录")
    ctx = _run(monkeypatch, "task_breakdown")
    assert calls == [("cli:fake", "发布会物料设计")]  # 每轮至多 1 次
    assert ctx["source_context"][-1] == {"task": "发布会物料设计",
                                         "source_key": "cli:fake",
                                         "probe": "回源搜索片段"}
    ctx2 = _run(monkeypatch, "stakeholder", trigger_input="跟老板谈发布会物料",
                user_input="跟老板谈发布会物料")
    assert len(calls) == 1  # 干系人场景探测闸关，零外部调用
    assert ctx2["source_context"] and "probe" not in ctx2["source_context"][0]


# —— 优化批 7（2026-11-01）：主动关怀键（滞留点名/前夜压力/周回顾） ——————————


def test_morning_stalled_items_names_series(monkeypatch, fresh_db):
    """滞留点名（批 7）：连续 3 天有实例且未勾完成的系列上榜（最小启动建议
    由场景段给）；正常完成/未排期不进清单。"""
    from datetime import date as _date

    base = datetime(2026, 10, 19, 9, 0)  # 周一起连续三天（_NOW=10-21 周三）
    with TestingSessionLocal() as db:
        _add_task(db, title="每日复盘", recurrence_rule="FREQ=DAILY",
                  start_time=base, duration_mins=30)
        _add_task(db, title="已完成的日常", recurrence_rule="FREQ=DAILY",
                  start_time=base, duration_mins=30,
                  done_at=datetime(2026, 10, 20, 10, 0))
    ctx = _run(monkeypatch, "morning")
    stalled = ctx["stalled_items"]
    assert any(s["title"] == "每日复盘" and "连续 3 天" in s["note"] for s in stalled)
    assert all("已完成" not in s["title"] for s in stalled)


def test_nightly_pressure_and_week_review_keys(monkeypatch, fresh_db):
    """前夜压力 + 周回顾（批 7）：明日 ≥3 场高优会 → tomorrow_pressure 非空；
    week_review 周日为 true（_NOW 固定周三 → false），week_rhythm 恒 7 天口径。"""
    day1 = datetime(2026, 10, 22, 9, 0)  # 明日（_NOW=10-21）
    with TestingSessionLocal() as db:
        for i in range(3):
            _add_task(db, title=f"高优评审{i}", priority=1,
                      start_time=day1 + timedelta(hours=i),
                      end_time=day1 + timedelta(hours=i, minutes=90),
                      duration_mins=90)
    ctx = _run(monkeypatch, "nightly")
    assert len(ctx["tomorrow_pressure"]) == 3
    assert ctx["week_review"] is False  # 2026-10-21 是周三
    assert ctx["week_rhythm"]["window_days"] == 7
    # 明日不足三场：压力键空表（阈值保守防打扰）
    with TestingSessionLocal() as db:
        for t in db.query(Task).filter(Task.title.like("高优评审%")).all():
            db.delete(t)
        db.commit()
    assert _run(monkeypatch, "nightly")["tomorrow_pressure"] == []


def test_probe_deep_second_target_when_enabled(monkeypatch, fresh_db):
    """教练溯源深探（优化批 9）：默认只探首个带源任务；coach_probe_deep 开时
    首目标空手换第二个相关任务再探（有界两跳；每源每日帽由 probe_source 把守）。"""
    import services.data_ingest as di

    probed: list[str] = []

    def fake_probe(db, source_key, title):
        probed.append(title)
        # 命中放在次序第二的目标（「第一任务」相关分 0.75 < 「第二任务」1.0）
        # ——首跳（第二任务）空手，验证深探换下一目标再探
        return "素材" if title == "第一任务" else ""

    monkeypatch.setattr(di, "probe_source", fake_probe)
    with TestingSessionLocal() as db:
        from services.settings import get_settings

        s = get_settings(db)
        s.coach_probe_deep = True
        db.commit()
        # 未排期任务（_related_pool = 收件箱 ∪ 冻结时刻起 8 天窗——真实"明天"
        # 落在冻结窗外，用未排期形态确保入池）
        for title in ("第一任务", "第二任务"):
            db.add(Task(title=title, duration_mins=30,
                        source_key="cli:lark", source_evidence="静态素材"))
        db.commit()
    ctx = _run(monkeypatch, "task_breakdown", trigger_input="第二任务",
               user_input="第二任务")
    probes = [e for e in ctx["source_context"] if "probe" in e]
    assert len(probes) == 1 and probes[0]["task"] == "第一任务"
    assert probed == ["第二任务", "第一任务"]  # 两跳：首目标空手换下一个命中即止
