"""记忆重构批 3 测试（2026-11-03）：注入统一双段——format_memory_lines 单源 /
assemble_system_prompt core_rules 双段渲染 / 八入口格式一致 / engine clip 补闸。
"""

import json
from datetime import datetime

from conftest import TestingSessionLocal
from models import MemoryRule
from services import llm
from services.ai_data import format_memory_lines

from agents.engine import _refine_with_llm
from agents.prompts import assemble_system_prompt
from services.chat_turn import build_initial_state


def _rule(content: str, *, source="ai_reflect", confidence=0.5,
          slot=None, anchor=None, category="其他"):
    row = MemoryRule(category=category, content=content, source=source,
                     confidence=confidence, evidence="e", slot=slot,
                     structured_anchor=anchor)
    with TestingSessionLocal() as db:
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# —— format_memory_lines 双段分流与排序 ——————————————————————————


def test_format_memory_lines_two_tier_split():
    """铁律段 = user_manual/chat 全量 + ai_reflect（≥0.8 或带锚）；参考段 = 其余。"""
    _rule("晚十一点睡", source="user_manual", confidence=1.0, category="作息规律")
    _rule("周三下午开放讨论", source="chat", confidence=1.0, category="会议偏好")
    _rule("晨间先攻坚", source="ai_reflect", confidence=0.85, category="工作习惯")
    _rule("带锚规则", source="ai_reflect", confidence=0.6, anchor={"weekday": 5})
    _rule("弱假设", source="ai_reflect", confidence=0.4)
    core, ref = format_memory_lines(_session())
    core_texts = " | ".join(core)
    assert "晚十一点睡" in core_texts and "周三下午开放讨论" in core_texts
    assert "晨间先攻坚" in core_texts and "带锚规则" in core_texts
    assert "弱假设" not in core_texts
    assert len(ref) == 1 and "弱假设" in ref[0]


def _session():
    return TestingSessionLocal()


def test_format_memory_lines_slot_grouping():
    """core 段按 slot 分组序 rhythm→meeting→boundary（组内注入序不重排）。"""
    _rule("红线规则", source="user_manual", confidence=1.0, slot="boundary")
    _rule("会议规则", source="user_manual", confidence=1.0, slot="meeting")
    _rule("作息规则", source="user_manual", confidence=1.0, slot="rhythm")
    core, _ = format_memory_lines(_session())
    idx = {name: next(i for i, t in enumerate(core) if name in t)
           for name in ("作息规则", "会议规则", "红线规则")}
    assert idx["作息规则"] < idx["会议规则"] < idx["红线规则"]


def test_format_memory_lines_personality_first_and_ref_clip():
    """人格前置 core 头部；ref 段走 clip_rules 40/3000 双闸。"""
    for i in range(45):
        _rule(f"弱假设{i}：" + "细" * 20, confidence=0.3)
    core, ref = format_memory_lines(_session(), personality=["[回复风格] 简洁"])
    assert core[0] == "[回复风格] 简洁"
    total = sum(len(r) for r in ref)
    assert len(ref) <= 40 and total <= 3000 + 60


def test_format_memory_lines_core_budget_cap():
    """core 兜底闸 60 条 / 4500 字符（防失控）。"""
    for i in range(70):
        _rule(f"铁律{i}：" + "则" * 40, source="user_manual", confidence=1.0)
    core, _ = format_memory_lines(_session())
    assert len(core) <= 60
    assert sum(len(r) for r in core) <= 4500 + 80


# —— assemble_system_prompt 双段渲染 ————————————————————————————


def test_assemble_prompt_legacy_behavior_unchanged():
    """core_rules=None（缺省）保持旧单段行为逐字不变。"""
    out = assemble_system_prompt(["[作息·用户设定] 早睡"], "BASE")
    assert out.startswith("BASE\n\n以下是用户的长期偏好与习惯")
    assert "- [作息·用户设定] 早睡" in out
    assert assemble_system_prompt([], "BASE") == "BASE"


def test_assemble_prompt_two_tier_rendering():
    """双段：铁律段标题 + 参考段标题；memory_rules 中 core 行剔除不重复。"""
    core = ["[回复风格] 简洁", "[作息·用户设定] 早睡"]
    merged = core + ["[其他·AI学习·置信0.4] 弱假设"]
    out = assemble_system_prompt(merged, "BASE", core_rules=core)
    assert "以下是用户的核心规则（铁律，必须遵守）：" in out
    assert "以下是用户的长期偏好与习惯（按重要度排序），供参考：" in out
    core_pos = out.index("核心规则")
    ref_pos = out.index("供参考")
    assert core_pos < ref_pos
    assert out.count("[作息·用户设定] 早睡") == 1  # core 行从参考段剔除
    assert "[其他·AI学习·置信0.4] 弱假设" in out


def test_assemble_prompt_core_empty_falls_to_reference():
    """core 为空列表（非 None）时只渲染参考段（不产出空铁律段标题）。"""
    out = assemble_system_prompt(["[其他·AI学习·置信0.4] 弱假设"], "BASE", core_rules=[])
    assert "核心规则" not in out
    assert "弱假设" in out


# —— chat 壳 initial_state ——————————————————————————————————————


def test_build_initial_state_carries_core_key():
    """壳注入 memory_rules=core+ref（合并单列表契约不变）+ core_memory 新键。"""
    _rule("晚十一点睡", source="user_manual", confidence=1.0, category="作息规律")
    _rule("弱假设", source="ai_reflect", confidence=0.4)
    with TestingSessionLocal() as db:
        state = build_initial_state(db, "明天有什么安排")
    assert state["memory_rules"] == state["core_memory"] + [
        r for r in state["memory_rules"] if r not in set(state["core_memory"])]
    assert any("晚十一点睡" in r for r in state["core_memory"])
    assert state["pending"] is None


# —— engine._refine_with_llm 补 clip ————————————————————————————


def test_refine_with_llm_clips_rules_payload():
    """SLOT_REFINE 载荷 memory_rules 过 clip_rules（唯一曾不 clip 的 JSON 入口）。"""
    captured = {}

    def fake_call(messages, *, json_mode=False, max_tokens=None, **kw):
        captured["payload"] = json.loads(messages[1]["content"])
        return json.dumps({"slots": []})

    import unittest.mock as mock

    long_rules = [f"[其他·AI学习·置信0.4] 规则{i}：" + "细" * 40 for i in range(80)]
    with mock.patch.object(llm, "call_llm", fake_call):
        _refine_with_llm(candidates={"k1": {"key": "k1", "title": "t", "duration": 30}},
                         rule_slots={"k1": datetime(2026, 11, 4, 9, 0)},
                         now=datetime(2026, 11, 4, 8, 0),
                         load_summary=None, weather=None, rules=long_rules)
    assert len(captured["payload"]["memory_rules"]) <= 40


# —— run_preflight 分级格式统一（跨入口一致）——————————————————————


def test_run_preflight_uses_graded_format(monkeypatch):
    """晨间简报入口换双段单源：分级标签进子图（旧版此处恒 [分类] 无分级）。"""
    from datetime import date, timedelta

    from agents import graph
    from models import Task

    captured = {}

    class _FakeBrain:
        def invoke(self, state):
            captured["memory_rules"] = state.get("memory_rules")
            return {"preflight": {"headline": "早", "checklist": []},
                    "final_reply": "简报"}

    # graph 内 from agents.strategist import strategist_app（函数内 import）——
    # patch agents.strategist 模块属性即命中
    import agents.strategist as strat

    monkeypatch.setattr(strat, "strategist_app", _FakeBrain())
    _rule("晚十一点睡", source="user_manual", confidence=1.0, category="作息规律")
    with TestingSessionLocal() as db:
        # 今天中午的日程（钉死当日时刻——原 now+2h 在 22 点后跑会跨午夜落到
        # 明天，run_preflight(今天) 静好早退、FakeBrain 不被调，时段性必红）
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        db.add(Task(title="已有日程", start_time=noon,
                    end_time=noon + timedelta(hours=1)))
        db.commit()
        graph.run_preflight(db, date.today())
    assert any("·用户设定" in r for r in captured["memory_rules"])
