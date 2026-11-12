"""Node3 brain 测试（Track B 批 4，2026-10-21）：钳制矩阵直调（strategy 硬截 /
checklist 钳制与兜底合成 / duration 钳 [15,240] 归整 15 倍 / task_id 白名单 /
action 越枚举丢条目 / memory evidence 空丢整条）+ 垃圾输出兜底 + build_prompt
三段组装与场景路由 + 子图 e2e（危机正则零分类直穿大脑，替身按「【场景：X】」
分发）。
"""

import agents.strategist.context_fetcher as cf
from agents.strategist.brain import _clamp_out, brain_node
from agents.strategist.prompts import (
    OUTPUT_SCHEMA,
    SCENARIO_SECTIONS,
    STRATEGIST_SKELETON,
    build_strategist_prompt,
)
from agents.strategist import strategist_app
from conftest import TestingSessionLocal


class _Capture:
    """call_llm 替身：按 system 子串分发（场景标记键），全量记录调用。"""

    def __init__(self, replies=None, default="{}"):
        self.replies = replies or {}
        self.default = default
        self.systems: list[str] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        self.systems.append(system)
        for key, reply in self.replies.items():
            if key in system:
                return reply
        return self.default


# —— 钳制矩阵（_clamp_out 直调）——————————————————————————————————————————


def test_clamp_strategy_and_checklist():
    out = _clamp_out({
        "strategy": "## 结论先行\n" + "长" * 2000,
        "checklist": ["  打电话确认场地  ", "", "x" * 60, None, 3, "c", "d", "e"],
    }, set())
    assert len(out["strategy_output"]) == 1600      # 硬截
    # 垃圾条目不占名额（先滤后截）：5 条有效全收
    assert out["checklist"] == ["打电话确认场地", "x" * 40, "c", "d", "e"]


def test_clamp_checklist_empty_stays_empty():
    # 不由 strategy 合成——空 checklist 是指导形态零卡信号（dispatcher 消费）
    out = _clamp_out({"strategy": "## 首个结论\n正文", "checklist": []}, set())
    assert out["checklist"] == []


def test_clamp_action_items_matrix():
    valid = {"t1"}
    out = _clamp_out({"action_items": [
        {"title": "拆场地", "duration": 300, "action": "schedule"},      # 300→240
        {"title": "短联系", "duration": 7, "action": "schedule"},        # 7→15
        {"title": "非整刻", "duration": 23, "action": "schedule"},       # 23→30 归整
        {"title": "坏估时", "duration": "一小时", "action": "schedule"}, # 非法→无键
        {"title": "幻觉id", "task_id": "ghost", "action": "reschedule"}, # 越白名单→无键
        {"title": "真id", "task_id": "t1", "action": "reschedule"},      # 保留
    ] + [{"title": f"超量{i}", "action": "inbox"} for i in range(8)],  # 够 6 即停
    }, valid)["action_items"]
    assert out[0] == {"title": "拆场地", "action": "schedule", "duration": 240}
    assert out[1] == {"title": "短联系", "action": "schedule", "duration": 15}
    assert out[2]["duration"] == 30
    assert "duration" not in out[3]
    assert "task_id" not in out[4]
    assert out[5] == {"title": "真id", "action": "reschedule", "task_id": "t1"}
    assert len(out) == 6                              # 上限 6


def test_clamp_action_enum_and_default():
    out = _clamp_out({"action_items": [
        {"title": "越枚举", "action": "delete"},        # 越枚举丢整条
        {"title": None, "action": "schedule"},          # 无标题丢弃
        {"title": "缺action有估时", "duration": 60},    # 推导 schedule
        {"title": "缺action无估时"},                    # 推导 inbox
    ]}, set())["action_items"]
    assert [a["title"] for a in out] == ["缺action有估时", "缺action无估时"]
    assert out[0]["action"] == "schedule" and out[1]["action"] == "inbox"


def test_clamp_memory_updates():
    out = _clamp_out({"memory_updates": [
        {"content": "下午3点不宜深度创作", "evidence": "近两周三次推迟"},   # 全字段保留
        {"content": "无依据规则", "evidence": ""},                        # evidence 空丢整条
        {"category": "x" * 60, "content": "c2", "evidence": "e2"},      # category 截 30
        {"content": "c3", "evidence": "e3"},                            # 缺省归类
    ] + [{"content": "超量", "evidence": "e"} for _ in range(3)],      # ≤2 截断
    }, set())["memory_updates"]
    assert out[0] == {"category": "作息规律", "content": "下午3点不宜深度创作",
                      "evidence": "近两周三次推迟"}
    assert len(out) == 2
    assert out[1]["category"] == "x" * 30


def test_brain_garbage_output_falls_back(monkeypatch):
    cap = _Capture(default="not-json")
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = brain_node({"scenario_type": "execution", "trigger_input": "写不动了",
                      "deep_context": {}, "memory_rules": []})
    assert out["strategy_output"].startswith("刚才的深度思考没能成形")
    assert out["action_items"] == [] and out["memory_updates"] == []


# —— build_prompt 组装 ———————————————————————————————————————————————————


def test_build_prompt_three_sections_and_routing():
    system, user = build_strategist_prompt(
        "crisis", "救命全乱了", {"now": "2026-10-21T20:00"}, ["[作息] 早睡"])
    assert STRATEGIST_SKELETON in system
    assert SCENARIO_SECTIONS["crisis"] in system
    assert OUTPUT_SCHEMA in system
    assert "救命全乱了" in user and "早睡" in user
    # 未知场景回落 execution 段
    system2, _ = build_strategist_prompt("bogus", "", {}, [])
    assert SCENARIO_SECTIONS["execution"] in system2


# —— 子图 e2e：危机正则零分类直穿大脑 ——————————————————————————————————


def test_e2e_crisis_flow_to_brain(monkeypatch, fresh_db):
    import agents.strategist.dispatcher as disp

    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    cap = _Capture(replies={"【场景：crisis": (
        '{"strategy": "先止血。\\n- 四象限分堆", "checklist": ["砍掉日报"], '
        '"action_items": [{"title": "重排评审", "duration": 30, '
        '"action": "reschedule"}], "memory_updates": []}')})
    monkeypatch.setattr("services.llm.call_llm", cap)
    out = strategist_app.invoke({"user_input": "救命，下午全乱了", "trigger": "chat"})
    assert len(cap.systems) == 1                       # 危机正则跳过分类，唯一 LLM=大脑
    assert "【场景：crisis" in cap.systems[0]
    assert out["scenario_type"] == "crisis"
    assert out["strategy_output"].startswith("先止血")
    assert out["checklist"] == ["砍掉日报"]
    assert out["action_items"][0]["action"] == "reschedule"
    assert "deep_context" in out                       # Node2 快照随行
    # dispatcher 出口（批 5 起）：无 task_id 的 reschedule 降级 inbox 附注；
    # chat 轮 checklist 非空 → preflight 卡（headline 首行合成）；reply=策略+附注
    assert "未能自动改期" in out["inbox_items"][0]["notes"]
    assert out["preflight"]["headline"] == "先止血。"
    assert "已放收件箱" in out["final_reply"]
    assert not (out.keys() & {"draft_plan", "resolved_proposal", "question_plan"})
