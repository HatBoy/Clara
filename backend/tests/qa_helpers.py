"""QA 套件共享替身与 harness（2026-11-23 七维度端到端测试批）。

供 test_qa_*.py 复用；不改既有 64 个测试文件各自的替身。锚点纪律：
- LLM 替身只打 services.llm.call_llm（call_llm_aux 未配 aux 槽时字面委托，
  打 call_llm 同时拦 aux——services/llm.py:460-465）；
- 分发键引用各 prompts 常量本体（import 单一来源，提示词改名即测试报错
  而非静默走 default）；
- 冻结时钟经 datetime 子类 patch 各入口节点模块属性（intake/classifier/
  nodes/engine 四个 datetime.now() 消费面）。
"""

import json
from datetime import date, datetime, timedelta

import agents.scheduler.context as scheduler_context_mod
import agents.scheduler.intake as intake_mod
import agents.strategist.classifier as strategist_classifier_mod
from agents.prompts import (
    REFLECT_PROMPT,
    RESOLVE_PROMPT,
    ROUTER_PROMPT,
    SETTINGS_PROMPT,
)
from agents.scheduler.prompts import _ADJ_HEAD, PROFILER_PROMPT
from agents.strategist.prompts import (
    CLASSIFIER_PROMPT,
    PENDING_JUDGE_PROMPT,
    SCENARIO_SECTIONS,
)
from conftest import TestingSessionLocal
from services import llm as llm_svc

# —— 分发键单源（test_qa_* 统一从这里 import，防提示词改名静默穿透）——————
KEY_PROFILER = PROFILER_PROMPT
KEY_ADJUDICATOR = _ADJ_HEAD  # 「时间裁决官」system 头（build_adjudicator_prompt 组装）
KEY_ROUTER = ROUTER_PROMPT
KEY_SETTINGS = SETTINGS_PROMPT
KEY_CLASSIFIER = CLASSIFIER_PROMPT
# PENDING_JUDGE_PROMPT 是 .format 模板（{ask}/{reply} 运行期替换）——全模板作
# 分发键会失配，用占位符之前的稳定人格前缀
KEY_PENDING_JUDGE = "幕僚长·问询裁决官"
KEY_REFLECT = REFLECT_PROMPT
KEY_RESOLVE = RESOLVE_PROMPT
KEY_SCENARIO = {name: f"【场景：{name}" for name in SCENARIO_SECTIONS}


class MultiKeyLLMCapture:
    """call_llm 增强替身：多键分发 + 全量记录 + 调用计数 + 可编程失败。

    - replies: {分发键: 应答str | [按序消费的应答list] | callable(user)->str}
    - default / default_fn：未命中分发键时的兜底（垃圾注入/恒故障注入用）
    - fail_times: {分发键或 "*": N}——命中键的前 N 次调用抛 NoActiveModelError
      （模拟 LLM 故障；"*" 对全部调用生效）
    - calls: [(system, user, json_mode, max_tokens)]；count(key) 供调用数断言
    """

    def __init__(self, replies=None, default="", default_fn=None, fail_times=None):
        self.replies = dict(replies or {})
        self.default = default
        self.default_fn = default_fn
        self.fail_times = dict(fail_times or {})
        self.calls: list[tuple[str, str, bool, int | None]] = []
        self._fail_seen: dict[str, int] = {}

    # ——替身协议（对齐 services.llm.call_llm 签名：json_mode/max_tokens/on_chunk）——
    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        self.calls.append((system, user, json_mode, max_tokens))
        key = self.match(system)
        gate = "*" if "*" in self.fail_times else key
        if gate is not None and self._fail_seen.get(gate, 0) < self.fail_times.get(gate, 0):
            self._fail_seen[gate] = self._fail_seen.get(gate, 0) + 1
            raise llm_svc.NoActiveModelError("qa 模拟 LLM 故障")
        reply = self._reply_for(key, user)
        if on_chunk is not None and reply:  # 双协议：流式分支透传（打字机替身）
            for i in range(0, len(reply), 8):
                on_chunk(reply[i:i + 8])
        return reply

    def match(self, system: str) -> str | None:
        for key in self.replies:
            if key in system:
                return key
        return None

    def _reply_for(self, key, user):
        if key is not None:
            preset = self.replies[key]
            if callable(preset):
                return preset(user)
            if isinstance(preset, list):
                if len(preset) > 1:
                    return preset.pop(0)
                return preset[0]
            return preset
        if self.default_fn is not None:
            return self.default_fn(user)
        return self.default

    # ——观测面——
    def count(self, key: str) -> int:
        return sum(1 for system, _, _, _ in self.calls if key in system)

    def users(self, key: str) -> list[str]:
        return [user for system, user, _, _ in self.calls if key in system]

    def systems(self, key: str) -> list[str]:
        """system 段观测（裁决器哲学/精力/天气段在 system，user 载荷看不到）。"""
        return [system for system, _, _, _ in self.calls if key in system]


def frozen_dt_cls(anchor: datetime):
    """冻结 now 的 datetime 子类工厂（test_scheduler_graph._FrozenDT 泛化）。
    锚日选周三/周六等固定锚，规避 SLOT_MAX_DAYS=7 与「下周五」类相对日漂移。"""

    class FrozenDT(datetime):
        _anchor = anchor

        @classmethod
        def now(cls, tz=None):
            a = cls._anchor
            return cls(a.year, a.month, a.day, a.hour, a.minute, a.second)

    return FrozenDT


def freeze_pipeline_clock(monkeypatch, anchor: datetime):
    """全图 chat 链时钟冻结（四个 datetime.now() 消费面 + engine）：
    nodes(center_router/confirm_shortcut) / scheduler intake（子图 now 单源，
    profiler/finder/adjudicator 经 state 传递）/ strategist classifier（子图 now
    单源，context_fetcher._state_now 读 state["now"]）/ engine.auto_schedule_run。"""
    frozen = frozen_dt_cls(anchor)
    import agents.nodes as nodes_mod
    import agents.engine as engine_mod

    monkeypatch.setattr(nodes_mod, "datetime", frozen)
    monkeypatch.setattr(intake_mod, "datetime", frozen)
    monkeypatch.setattr(strategist_classifier_mod, "datetime", frozen)
    monkeypatch.setattr(engine_mod, "datetime", frozen)
    return frozen


def patch_scheduler_inputs(monkeypatch, *, philosophy=None, weather=None,
                           anchor: datetime | None = None,
                           settings_overrides=None, patch_settings=True):
    """排期子图一站式替身：SessionLocal patch 点（intake/context 两处直连库
    节点）+ 时钟冻结 + 设置 + 天气。读库不替身——任务行由用例在真测试库
    （TestingSessionLocal）自建，intake 的 list_tasks/能量/节奏走真查询，
    端到端口径更实。anchor 缺省 2026-11-25 09:00（周三，规避 7 天窗与
    「下周五」类相对日漂移）。"""
    from types import SimpleNamespace

    from services import settings as settings_svc
    from services import weather as weather_svc

    monkeypatch.setattr(intake_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(scheduler_context_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(intake_mod, "datetime", frozen_dt_cls(
        anchor or datetime(2026, 11, 25, 9, 0)))
    if patch_settings:
        base = SimpleNamespace(
            work_minutes_per_day=480, calendar_start_min=480, calendar_end_min=1320,
            lunch_enabled=True, lunch_start_min=690, lunch_end_min=810,
            reminder_lead_min=15, weather_city=None,
            ai_philosophy=philosophy, ai_philosophy_custom=None)
        for k, v in (settings_overrides or {}).items():
            setattr(base, k, v)
        monkeypatch.setattr(settings_svc, "get_settings", lambda db: base)
    monkeypatch.setattr(weather_svc, "forecast_for_city", lambda city: weather)


def patch_nodes_session(monkeypatch):
    """主图 nodes 的 settings_handler/confirm_shortcut 等节点用模块级绑定的
    SessionLocal 开会话（agents/nodes.py:44 from database import）——conftest
    只换 database.SessionLocal 不跟随；且 pytest 收集期任何文件导入 nodes 后
    绑定即固定为 dev 库（QA 批实测污染 2 行记忆规则已清理）。settings/chat
    全链用例必须打源模块属性（test_chat_settings.py:32 先例）。"""
    import agents.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)


def patch_strategist_reads(monkeypatch):
    """幕僚子图读库替身：dispatcher/context_fetcher 模块级 SessionLocal 绑定
    patch 到测试库（classifier 函数内延迟导入，conftest 的 database.SessionLocal
    替换已覆盖）。真空读——任务行由用例在测试库自建。"""
    import agents.strategist.context_fetcher as cf_mod
    import agents.strategist.dispatcher as dispatcher_mod

    monkeypatch.setattr(cf_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(dispatcher_mod, "SessionLocal", TestingSessionLocal)


def activate_model(client=None):
    """插激活模型行放行 check_ai 闸门（test_ai_coach._activate_model 口径）。"""
    import uuid

    from models import ModelProvider

    with TestingSessionLocal() as db:
        db.add(ModelProvider(name=f"qa-mock-{uuid.uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def chat_events(client, message: str) -> list[dict]:
    """POST /api/ai/chat SSE 全量收帧（sim_test.py 口径）：返回 data 帧列表。"""
    resp = client.post("/api/ai/chat", json={"message": message})
    assert resp.status_code == 200, resp.text[:300]
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def events_by_type(events, ev_type: str) -> list[dict]:
    return [e for e in events if e.get("type") == ev_type]


def first_of(events, ev_type: str) -> dict | None:
    hits = events_by_type(events, ev_type)
    return hits[0] if hits else None


def settle_draft(client, proposal_id: str, entries: list[dict], *,
                 outcome: str = "accepted",
                 modified_entries: list[dict] | None = None) -> list[dict]:
    """与桌面同路：按原卡稳定 ID 提交决定，然后按回执核对真实任务。"""
    url = f'/api/ai/proposals/{proposal_id}'
    stable = client.get(url).json()['item_ids']
    assert len(stable) == len(entries)
    resp = client.post(url + '/decide', json={'items': [
        {'item_id': identity, 'action':'accept'} if outcome == 'accepted' else
        {'item_id': identity, 'action':'modify', 'entry':(modified_entries or entries)[i]}
        for i, identity in enumerate(stable)]})
    assert resp.status_code == 200, resp.text[:300]
    return [client.get(f"/api/tasks/{r['task_id']}").json() for r in resp.json()['receipts']]


def inject_rule(db, content: str, *, source: str = "user_manual",
                confidence: float = 1.0, category: str = "排期偏好",
                evidence: str | None = None, slot: str | None = None):
    """直插 MemoryRule（含「·AI学习·置信X」标签行时 confidence 只装饰文本，
    硬闸解析读的是标签——inject_ai_rule 语义由调用方拼 content）。"""
    from models import MemoryRule

    row = MemoryRule(category=category, content=content, source=source,
                     confidence=confidence, evidence=evidence, slot=slot)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_busy(day: date, *ranges: tuple[int, int]) -> list[tuple[datetime, datetime]]:
    """某天的 (起始分钟, 结束分钟) 区间 → busy [(start, end)]。"""
    return [(datetime(day.year, day.month, day.day) + timedelta(minutes=a),
             datetime(day.year, day.month, day.day) + timedelta(minutes=b))
            for a, b in ranges]


def next_weekday(base: date, weekday: int) -> date:
    """base 之后（不含当天）第一个指定 weekday（0=周一）。"""
    delta = (weekday - base.weekday()) % 7
    if delta == 0:
        delta = 7
    return base + timedelta(days=delta)


def qa_report_path(name: str):
    """真实层 JSON 报告落点：backend/data/qa-reports/（测试基建产物，
    dev 数据目录随 gitignore 不入库）。"""
    from pathlib import Path

    import os
    out_dir = Path(os.environ.get("CLARA_QA_REPORT_DIR") or (Path(__file__).resolve().parents[2] / ".pybuild" / "qa-reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / name
