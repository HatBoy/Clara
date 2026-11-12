"""记忆重构批 4 测试（2026-11-03）：排期硬闸四处补齐——裁决器微调槽复查偏好窗 /
apply_slot_decisions 落库重验 / _postvalidate_drafts 采集后验 / auto_schedule_run
单卡记忆接入。全部走 preference_slots 单源（含模块缓存）。
"""

from datetime import date, datetime, timedelta

from conftest import TestingSessionLocal
from services.ai_data import upsert_memory_rule
from services.settings import get_settings

# 偏好规则：周五下午不排事（boundary 槽、可解析为避让窗）
FRIDAY_RULE = dict(category="排期偏好", content="周五下午别排事",
                   confidence=1.0, source="user_manual")


def _next_friday_after(dt: datetime) -> datetime:
    """dt 之后最近的周五（含当日午后的语义由调用方定）。"""
    d = dt
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def test_auto_schedule_run_respects_avoid_window():
    """单卡智能安排（批 4 前零记忆消费）：设「周五下午别排事」后，槽不落周五下午。"""
    from agents.engine import NoSlotError, auto_schedule_run
    from services.tasks import create_task
    from schemas import TaskCreate

    with TestingSessionLocal() as db:
        upsert_memory_rule(db, evidence="亲笔", **FRIDAY_RULE)
        t = create_task(db, TaskCreate(title="整理文档"))
        decision = auto_schedule_run(db, t.id)
        slot = decision["slot"]
        assert slot is not None
        # 槽不得落在周五 12:00-18:00 窗内（preference_slots 下午=720-1080）
        if slot.weekday() == 4:
            assert not (720 <= slot.hour * 60 + slot.minute < 1080)


def test_apply_slot_decisions_rejects_window_slot():
    """落库重验带避让窗：决策给周五下午槽 → failed（批 4 前重验不查偏好窗）。"""
    from services.tasks import apply_slot_decisions, create_task
    from schemas import TaskCreate

    with TestingSessionLocal() as db:
        upsert_memory_rule(db, evidence="亲笔", **FRIDAY_RULE)
        t = create_task(db, TaskCreate(title="整理文档"))
        friday = _next_friday_after(datetime.now() + timedelta(days=1))
        friday = friday.replace(hour=14, minute=0, second=0, microsecond=0)
        if friday <= datetime.now():
            friday += timedelta(days=7)
        ok, failed = apply_slot_decisions(db, [{
            "task_id": t.id, "title": t.title, "slot": friday}])
        assert ok == [] and len(failed) == 1
        assert failed[0]["reason"] == "未来 7 天没有可用空档"


def test_postvalidate_drafts_drops_window_entries():
    """采集草案后验带偏好窗（批 4 前不查）：窗内草案被丢弃并附注。"""
    from agents.graph import _postvalidate_drafts
    from services.preference_slots import parse_avoid_windows

    with TestingSessionLocal() as db:
        upsert_memory_rule(db, evidence="亲笔", **FRIDAY_RULE)
        rules = [f"[{FRIDAY_RULE['category']}·用户设定] {FRIDAY_RULE['content']}"]
        avoid = parse_avoid_windows(rules)
        assert avoid and avoid[0]["weekday"] == 4
    friday = _next_friday_after(datetime.now())
    in_window = friday.replace(hour=15, minute=0)
    out_window = friday.replace(hour=10, minute=0)  # 周五上午不受影响
    result = {
        "draft_plan": [
            {"title": "窗内草案", "start": in_window.isoformat(), "duration": 60},
            {"title": "窗外草案", "start": out_window.isoformat(), "duration": 60},
        ],
        "evidence": [{"idx": 0, "source": "mcp"}, {"idx": 1, "source": "mcp"}],
        "final_reply": "综述",
    }
    _postvalidate_drafts(result, busy=[], day_start_min=480, day_end_min=1320,
                         lunch=None, avoid_windows=avoid)
    titles = [d["title"] for d in result["draft_plan"]]
    assert titles == ["窗外草案"]
    assert [e["idx"] for e in result["evidence"]] == [0]  # idx 重排
    assert "窗内草案" in result["final_reply"]


def test_postvalidate_drafts_none_windows_unchanged():
    """avoid_windows=None（缺省）行为不变：不查偏好窗（直调用例兼容）。"""
    from agents.graph import _postvalidate_drafts

    friday = _next_friday_after(datetime.now()).replace(hour=15)
    result = {"draft_plan": [{"title": "窗内草案", "start": friday.isoformat(),
                              "duration": 60}],
              "evidence": [], "final_reply": "综述"}
    _postvalidate_drafts(result, busy=[], day_start_min=480, day_end_min=1320,
                         lunch=None)
    assert len(result["draft_plan"]) == 1


def test_adjudicator_adjust_respects_avoid_window(monkeypatch):
    """裁决器微调槽后验补查（批 4 前 ±90min 微调可跳出偏好窗）：
    主荐周五 11:00（窗外），LLM 微调到 12:30（+90 恰在合法域、且落进下午窗
    720-1080）→ valid 拒 → 回落候选原值。"""
    from agents.scheduler.adjudicator import _adjudicate
    from services import llm as llm_svc

    base = _next_friday_after(datetime.now() + timedelta(days=1)).replace(hour=11, minute=0)
    adj_target = base.replace(hour=12, minute=30)

    import json as _json

    monkeypatch.setattr(llm_svc, "call_llm", lambda *a, **kw: _json.dumps({
        "selections": [{"key": "a0", "adjust_start": adj_target.isoformat()}],
        "explanation": ""}))
    rules = [f"[{FRIDAY_RULE['category']}·用户设定] {FRIDAY_RULE['content']}"]
    state = {
        "context": {"memory_rules": rules, "day_start_min": 480,
                    "day_end_min": 1320, "lunch": None, "day_budget": None,
                    "long_chain_ends": [], "busy_meta": {}},
        "now": datetime.now().replace(second=0, microsecond=0),
        "philosophy_ids": [],
        "busy": [],
    }
    clean = [{"title": "写文档", "duration": 60}]
    slots = [{"key": "a0", "slot": base.isoformat(), "why": "test"}]
    _adjudicate(state, clean, [], slots, None, {})
    # 微调槽落进偏好窗被拒 → 回落候选原值（周五上午 11:00）
    assert clean[0]["start"] == base.isoformat()


def test_engine_plan_slots_rules_path_covers_single_card():
    """auto_schedule_run 传 rules 后 plan_slots 双层消费（规则层硬筛）——
    与对话/批量入口偏好口径归一（同库两条路径一致性冒烟）。"""
    from agents.engine import auto_schedule_run
    from schemas import TaskCreate
    from services.tasks import create_task, get_task

    with TestingSessionLocal() as db:
        upsert_memory_rule(db, evidence="亲笔", **FRIDAY_RULE)
        t = create_task(db, TaskCreate(title="另一件文档"))
        decision = auto_schedule_run(db, t.id)
        slot = decision["slot"]
        if slot.weekday() == 4:
            assert not (720 <= slot.hour * 60 + slot.minute < 1080)
        # 落库后任务确实排上（未抛 NoSlotError）
        assert get_task(db, t.id) is not None
