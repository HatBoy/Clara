"""QA 维度二扩展：管理哲学叠加矩阵与记忆仲裁例外合并指令（发版前验收批）。

断言口径经读码核实：
- 哲学段注入单点 build_adjudicator_prompt（agents/scheduler/prompts.py:145-151）：
  启用项文本（two-minute 与选槽无关不注）拼【主人的管理哲学】段——mit-3 是
  纯 prompt 注入无代码执行点（prompts.py:398-400 无 conflicts），文本进 system
  即其全部实现面，缺达即软约束整层失效；
- 哲学叠加无打架：mit-3 + work-rest-balance + 自定义周五红线三源并存时
  （philosophy_texts 顺序照选择去重 prompts.py:419-431；personality_rules
  风格→预设→自定义三段拼接 prompts.py:434-448），裁决器 system 预设段齐、
  profiler 载荷带 [自定义哲学] 原文，且硬闸避让窗仍由 preference_slots 从
  [自定义哲学] 行独立生效（d2 已证单源，此处证叠加不互相覆盖）；
- Bug#4 修复回归（2026-11-24 发版验收批）：RESOLVE_PROMPT 规则 4「例外开放
  必须合并」——2026-11-23 真实层两轮实测真仲裁判 keep 丢用户开放语义
  （qa-real-20261123-231707/231931.json soft WARN），修复在规则 6 保守默认
  之上加例外开放确定性指令；文本缺失即修复回退（真实层行为验收见
  test_qa_real_llm 冲突自愈用例硬断言）。
"""

import json
from datetime import datetime, timedelta

from agents.prompts import RESOLVE_PROMPT, personality_rules
from services import llm

from agents.scheduler import scheduler_app
from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    MultiKeyLLMCapture,
    patch_scheduler_inputs,
)

ANCHOR = datetime(2026, 11, 25, 9, 0)  # 周三
ADJ_KEEP = json.dumps({"selections": [], "explanation": ""}, ensure_ascii=False)

STACK_IDS = "mit-3,work-rest-balance"
CUSTOM_REDLINE = "周五16:00后绝不安排任何工作。"


def _prof(entries, reply="好的。"):
    return json.dumps({"entries": entries, "reply": reply}, ensure_ascii=False)


def _entry(title, duration, **profile):
    return {"task_id": None, "origin": "agent", "title": title,
            "duration": duration, "profile": profile or {}}


def _invoke(monkeypatch, user_input, entries, *, philosophy=None,
            memory_rules=None):
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof(entries),
        KEY_ADJUDICATOR: ADJ_KEEP,
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch, philosophy=philosophy)
    state = {"user_input": user_input, "memory_rules": memory_rules or []}
    return scheduler_app.invoke(state), cap


# —— mit-3 每日三要事：纯 prompt 注入面的存在性 ————————————————————————————


def test_mit3_reaches_adjudicator_prompt(monkeypatch):
    """启用 mit-3 → 裁决器 system 含每日三要事文本（无代码执行点的哲学，
    注入即全部约束面）。"""
    out, cap = _invoke(monkeypatch, "安排个产品评审", [
        _entry("产品评审", 60)], philosophy="mit-3")
    assert cap.count(KEY_ADJUDICATOR) == 1
    assert "每日三要事" in cap.systems(KEY_ADJUDICATOR)[0]


def test_mit3_disabled_no_injection(monkeypatch):
    """未启用 → 同输入零注入（设置开关真实介入，非恒在文案）。"""
    out, cap = _invoke(monkeypatch, "安排个产品评审", [
        _entry("产品评审", 60)], philosophy=None)
    assert cap.count(KEY_ADJUDICATOR) == 1
    assert "每日三要事" not in cap.systems(KEY_ADJUDICATOR)[0]


# —— 三哲学叠加：三源并存无互相覆盖、硬闸仍独立生效 ————————————————————————


def test_three_philosophies_stack_all_reach_adjudicator(monkeypatch):
    """mit-3 + 张弛有度 + 自定义周五红线叠加：裁决器 system 两预设文本齐、
    profiler 载荷带 [自定义哲学] 原文——三源并存零覆盖。"""
    rules = personality_rules(None, STACK_IDS, CUSTOM_REDLINE)
    out, cap = _invoke(monkeypatch, "安排个产品评审", [
        _entry("产品评审", 60)], philosophy=STACK_IDS, memory_rules=rules)
    system = cap.systems(KEY_ADJUDICATOR)[0]
    assert "每日三要事" in system and "张弛有度" in system
    profiler_users = cap.users(KEY_PROFILER)
    assert profiler_users and "[自定义哲学]" in profiler_users[0] \
        and "绝不安排任何工作" in profiler_users[0]


def test_stack_custom_redline_still_hard(monkeypatch):
    """叠加态下显式排周五 16:30 → 硬闸照常拦截问询（哲学叠加不削弱红线，
    恰好结束允许/跨线拦截语义与 d2 单源一致）。"""
    rules = personality_rules(None, STACK_IDS, CUSTOM_REDLINE)
    fri = ANCHOR + timedelta(days=2)  # 2026-11-27 周五
    start = fri.replace(hour=16, minute=30)
    out, cap = _invoke(
        monkeypatch, "周五16:30安排产品评审",
        [{"task_id": None, "origin": "user", "title": "产品评审",
          "duration": 60, "start": start.isoformat(), "explicit_time": "16:30",
          "profile": {}}],
        philosophy=STACK_IDS, memory_rules=rules)
    assert not out.get("applied_plan") and not out.get("draft_plan")
    assert out.get("question_plan") and "16:00" in out["final_reply"]
    assert cap.count(KEY_ADJUDICATOR) == 0  # 硬闸先于裁决器拦截


# —— Bug#4 修复回归：仲裁提示词例外合并指令 —————————————————————————————————


def test_resolve_prompt_exception_merge_directive():
    """RESOLVE_PROMPT 必须含「例外开放必须合并」确定性指令：user 信号候选对
    现有禁排规则的局部开放一律 merge，不得 keep（丢开放语义）不得 new
    （裸矛盾并列）——真仲裁保守默认的例外豁免（修复 2026-11-23 Bug#4）。"""
    assert "例外开放必须合并" in RESOLVE_PROMPT
    assert "一律 merge" in RESOLVE_PROMPT
    assert "不得 keep" in RESOLVE_PROMPT and "不得 new" in RESOLVE_PROMPT
    assert "例外开放技术对齐" in RESOLVE_PROMPT  # 合成终态示例随指令在文
    # 保守默认保留，但显式让位于例外规则
    assert "拿不准时选 keep" in RESOLVE_PROMPT
    assert "不算" in RESOLVE_PROMPT and "必须 merge" in RESOLVE_PROMPT
