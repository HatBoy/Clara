"""QA 维度六：异常兜底与降级矩阵（七维度端到端测试批）。

断言口径经读码核实：
- weather 永不抛出（forecast_for_city 任何失败返 None + SWR 旧值）；
- call_llm_json 解析失败回喂重问一次仍失败 → None → profiler 降级终态 /
  brain 兜底文案零卡；
- engine AI 微调层异常整层回落规则槽（engine.py:117-122，AI 层永不抬升故障）；
- litellm 异常上抛 → chat SSE error 帧（code=agent_error / disabled / no_model）
  ——降级矩阵的壳层翻译；
- 空正文按更大预算重试一次（llm.py:506-516）；
- 过载熔断（_run_heartbeat 负荷率>0.8 → 提醒广播，排期不拦——数据同步/提醒
  永不阻塞用户）；
- 设置撤销反演（undo_last_change ≤8 字短语零 LLM 反演 journal）。
"""

import asyncio
import json
from datetime import datetime

import pytest
from conftest import TestingSessionLocal
from services import llm
from services.weather import forecast_for_city

from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    KEY_ROUTER,
    KEY_SCENARIO,
    MultiKeyLLMCapture,
    activate_model,
    chat_events,
    events_by_type,
    first_of,
    inject_rule,
    patch_scheduler_inputs,
)


# —— 天气断网 ————————————————————————————————————————————————————————————————


def test_weather_failure_never_raises(monkeypatch):
    """天气 API 断网 → 永不抛出（None 降级），排期链零行为变化。"""
    from services import weather as weather_svc

    monkeypatch.setattr(weather_svc, "_fetch_json",
                        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("断网")))
    monkeypatch.setattr(weather_svc, "_reset_cache_for_tests", lambda: None)
    assert forecast_for_city("上海") is None
    assert forecast_for_city(None) is None  # 城市未配置恒 None


# —— LLM 输出形态防御 ————————————————————————————————————————————————————————


def test_profiler_garbage_retries_then_degrades(monkeypatch):
    """profiler 输出恒垃圾 → call_llm_json 重问一次（共 2 调用）→ 降级终态文案，
    零草案零问询零崩溃。"""
    cap = MultiKeyLLMCapture({}, default="抱歉，这不是 JSON")
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch)
    from agents.scheduler import scheduler_app

    out = scheduler_app.invoke({"user_input": "安排写周报", "memory_rules": []})
    assert cap.count(KEY_PROFILER) == 2  # 解析失败回喂重试一次
    assert "排期推理失败" in out["final_reply"]
    assert out.get("draft_plan") in (None, [])
    assert out.get("question_plan") is None


def test_brain_garbage_fallback_reply(monkeypatch):
    """brain 输出垃圾 → 确定性兜底文案 + 空 action/memory，零卡零编造。"""
    cap = MultiKeyLLMCapture({}, default="模型抽风输出")
    monkeypatch.setattr(llm, "call_llm", cap)
    from agents.strategist import strategist_app
    from qa_helpers import patch_strategist_reads

    patch_strategist_reads(monkeypatch)
    out = strategist_app.invoke({"user_input": "帮我规划发布会", "memory_rules": [],
                                 "pending": None, "trigger": "chat"})
    assert "深度思考" in out["final_reply"] or "换个说法" in out["final_reply"]
    assert not (out.get("draft_plan") or [])
    assert not (out.get("inbox_items") or [])


def test_engine_ai_layer_falls_back_on_exception(monkeypatch):
    """engine AI 微调层异常（NoActiveModelError）→ 整层跳过，规则槽照常返回
    （AI 层永不抬升故障）。"""
    from agents.engine import plan_slots

    def _boom(messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        raise llm.NoActiveModelError("qa 模拟")

    monkeypatch.setattr(llm, "call_llm", _boom)
    slots = plan_slots(
        entries=[{"key": "k", "title": "写周报", "priority": 2, "duration": 60}],
        busy=[], now=datetime(2026, 11, 25, 9, 0), day_start_min=480,
        day_end_min=1320, lunch=(690, 810), use_ai=True)
    assert slots["k"] is not None  # 规则槽存活


def test_empty_completion_retries_with_bigger_budget(monkeypatch):
    """推理模型空正文 → call_llm 按更大预算自动重试一次（llm.py:506-516）。"""
    import services.llm as llm_mod

    calls = []

    def _fake_completion(**kw):
        calls.append(kw.get("max_tokens"))
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "重试后的正文"}}]}

    monkeypatch.setattr(llm_mod.litellm, "completion", _fake_completion)
    activate_model()
    out = llm_mod.call_llm([{"role": "user", "content": "hi"}], max_tokens=100)
    assert out == "重试后的正文"
    assert len(calls) == 2 and calls[1] > calls[0]  # 二次更大预算


# —— chat 壳层降级矩阵（SSE error 帧契约） —————————————————————————————————————


def test_chat_llm_exception_translates_to_error_frame(monkeypatch, client):
    """排期轮 LLM 抛异常（litellm 路径）→ SSE error 帧 code=agent_error + done
    收尾——前端降级提示，绝不 500 或挂死。真读库设置（闸门需 ai_enabled），
    子图会话 patch 到测试库（intake.SessionLocal 模块级绑定）。"""
    activate_model()
    import agents.scheduler.context as _ctx
    import agents.scheduler.intake as _intake

    monkeypatch.setattr(_intake, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(_ctx, "SessionLocal", TestingSessionLocal)
    from agents.scheduler.prompts import PROFILER_PROMPT as _PP

    from agents.prompts import ROUTER_PROMPT as _RP

    def _boom(messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        if _PP in messages[0]["content"]:
            raise RuntimeError("qa 模拟网络故障")
        if _RP in messages[0]["content"]:
            return '{"intent": "schedule", "reason": "qa"}'
        return ""

    monkeypatch.setattr(llm, "call_llm", _boom)
    events = chat_events(client, "明天写周报")
    err = first_of(events, "error")
    assert err and err["code"] == "agent_error"
    assert events[-1]["type"] == "done"


def test_chat_disabled_gate(client):
    """AI 主开关关闭 → error 帧 code=disabled（check_ai 闸在轮次前毫秒级拦截）。
    真读库设置（不 patch settings——SimpleNamespace 缺 ai_enabled 会炸闸门）。"""
    from services.settings import get_settings as _gs

    with TestingSessionLocal() as db:
        s = _gs(db)
        s.ai_enabled = False
        db.commit()
    events = chat_events(client, "安排写周报")
    err = first_of(events, "error")
    assert err and err["code"] == "disabled"
    assert events[-1]["type"] == "done"


def test_chat_no_model_gate(monkeypatch, client):
    """零激活模型 → error 帧 code=no_model（前端引导卡直达模型接入）。"""
    events = chat_events(client, "安排写周报")
    err = first_of(events, "error")
    assert err and err["code"] == "no_model"


# —— 过载熔断与撤销反演 ——————————————————————————————————————————————————————


def test_overload_heartbeat_warns_but_never_blocks(monkeypatch):
    """负荷率 >0.8 → overload 提醒广播；排期链不受影响（提醒不拦截——数据
    同步/提醒永不阻塞用户操作）。"""
    from datetime import timedelta

    from models import Task

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        for h in range(10):  # 10h/8h 预算 > 0.8
            db.add(Task(title=f"满载{h}", start_time=today + timedelta(hours=h),
                        end_time=today + timedelta(hours=h + 1), duration_mins=60))
        db.commit()

    got = []

    def _fake_broadcast(event):  # 真函数是同步 put_nowait——替身必须同步形态
        got.append(event)

    async def _no_advice(ratio):
        return None  # 真函数内部自吞异常回 None；替身模拟返回形态而非抛异常

    monkeypatch.setattr("services.workflows.broadcast", _fake_broadcast)
    monkeypatch.setattr("services.workflows._fire_push", lambda *a, **kw: None)
    monkeypatch.setattr("services.workflows._overload_advice", _no_advice)
    from services import workflows

    # workflows.SessionLocal 是模块级绑定（conftest 只换 database.SessionLocal）
    from conftest import TestingSessionLocal as _TSL

    monkeypatch.setattr(workflows, "SessionLocal", _TSL, raising=False)
    asyncio.run(workflows._run_heartbeat())
    assert got and got[0]["type"] == "overload"
    assert "80%" in got[0]["headline"]


def test_undo_preference_inversion():
    """设置撤销反演：prefer 写入 → undo_last_change 反演 journal → 规则删除 +
    journal 回滚。"""
    from services import chat_config
    from services.ai_data import list_memory_rules

    with TestingSessionLocal() as db:
        out = chat_config.apply_preference(db, raw="以后周五下午别排事",
                                           category="排期偏好",
                                           content="周五下午别排事")
        assert out["ok"]
        assert list_memory_rules(db) and list_memory_rules(db)[0].source == "chat"
        # settings_handler 落 kind=settings_change 行（journal 进 payload.actions，
        # undo_last_change 按行反演）
        from models import Proposal

        db.add(Proposal(kind="settings_change",
                        payload={"actions": [out["journal"]]}))
        db.commit()
        undone = chat_config.undo_last_change(db)
        assert undone["ok"]
        assert list_memory_rules(db) == []
