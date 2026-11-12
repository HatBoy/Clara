"""Node3 麦肯锡大脑（批 4 正式化，2026-10-21）。

build_strategist_prompt 场景路由单次 LLM（json_mode=True，max_tokens=4000）
→ 防御性钳制（对照 nodes._parse_coach_out 先例：字段形态错配丢弃/截断，
不连坐整批）→ strategy_output / checklist / action_items / memory_updates。
垃圾输出（非 JSON/非 dict）→ 确定性兜底文案 + 空 action/memory（scheduler
profiler 同款先例：下游见空即跳过，绝不因模型抽风产出半成品建议）。

LLM 调用数 1（解析失败窄域修复重试 +1，仅失败轮）。

2026-11-11 真机批两处修复（deepseek-flash 下拆解场景偶发兜底文案根因）：
①max_tokens 2000→4000——strategy 900 字 + 6 条 action_items 在中文
tokenizer 下超 2000 token 上限，输出中途截断成半截 JSON（真机 dump 实证
pos 1163 戛然而止）；②改走 call_llm_json 站进解析失败回喂重试伞（该伞
2026-11-01 优化批 4 建立时只覆盖 schedule 链，brain 直调 call_llm 漏在
伞外——残余的非法字符类失败靠重试救回）。
"""

import logging

from agents.nodes import _status_emitter
from agents.parsing import call_llm_json

from .prompts import build_strategist_prompt
from .state import StrategistState

logger = logging.getLogger(__name__)

_FALLBACK_REPLY = ("刚才的深度思考没能成形，这条先不给半成品建议。"
                   "换个说法再问我一次；如果只是想把事情排进日历，"
                   "直接说「帮我排一下」就好。")
_ACTIONS = ("schedule", "inbox", "reschedule")
_STRATEGY_MAX_CHARS = 1600   # 800 软限（提示词）+ 一倍硬截（钳制层）
_BRAIN_MAX_TOKENS = 4000     # 2026-11-11 真机批：2000 会截断长策略输出（见模块头）


def brain_node(state: StrategistState) -> dict:
    """大脑节点：单次 LLM 产策略四件套（钳制后）。"""
    scenario = state.get("scenario_type") or "execution"
    deep_context = state.get("deep_context") or {}
    system, user = build_strategist_prompt(
        scenario, state.get("trigger_input") or "",
        deep_context, state.get("memory_rules") or [])
    _status_emitter()("正在组织思路…")  # 单次 LLM 大头过程反馈
    data = call_llm_json(system, user, max_tokens=_BRAIN_MAX_TOKENS)
    if not isinstance(data, dict):
        # 兜底轮 checklist 恒空：dispatcher 据此零卡纯文字（cron 轮由 headline
        # 确定性合成，卡上只有空清单——异常路径少 UI 产物原则）
        logger.warning("[strategist] brain：输出无法解析（含重试），走兜底零卡")
        return {"strategy_output": _FALLBACK_REPLY, "checklist": [],
                "action_items": [], "memory_updates": []}
    result = _clamp_out(data, _known_ids(deep_context))
    logger.info("[strategist] brain：策略 %d 字 / checklist %d / action %d / "
                "memory %d", len(result.get("strategy_output") or ""),
                len(result.get("checklist") or []),
                len(result.get("action_items") or []),
                len(result.get("memory_updates") or []))
    return result


def _clamp_out(data: dict, valid_ids: set[str]) -> dict:
    """钳制层（提示词契约的代码侧落实）：strategy 硬截；checklist 前 6×40
    字、空则由 strategy 首行合成；action_items ≤6、duration 钳 [15,240] 归整
    15 倍数、task_id 白名单越界置 None、action 越枚举丢弃（缺省按有无
    duration 补 schedule/inbox）；memory_updates ≤2、evidence 空整条丢弃。"""
    strategy = data.get("strategy")
    strategy = strategy.strip()[:_STRATEGY_MAX_CHARS] if isinstance(strategy, str) else ""
    checklist = []
    for c in data.get("checklist") or []:
        if len(checklist) >= 6:  # 先滤后截：垃圾条目不占名额（够数即停）
            break
        if isinstance(c, str) and c.strip():
            checklist.append(c.strip()[:40])
    # 不做「空则由 strategy 合成」——空 checklist 是 dispatcher 出卡条件之一
    # （指导形态 LLM 给空数组 → 零卡纯文字，语义保真优先于卡面饱满）
    actions = []
    for it in data.get("action_items") or []:
        if len(actions) >= 6:
            break
        if not isinstance(it, dict):
            continue
        title = it.get("title")
        title = title.strip()[:30] if isinstance(title, str) and title.strip() else None
        if not title:
            continue
        action = it.get("action")
        duration = _clamp_duration(it.get("duration"))
        if action is not None and action not in _ACTIONS:
            continue  # 越枚举丢整条（计划钳制表；宁少勿错）
        if action is None:
            # 缺省推导：有估时 → 排期意图；无估时 → 收件箱（Node4 同款分流）
            action = "schedule" if duration else "inbox"
        task_id = it.get("task_id")
        task_id = task_id if isinstance(task_id, str) and task_id in valid_ids else None
        entry = {"title": title, "action": action}
        if duration:
            entry["duration"] = duration
        if task_id:
            entry["task_id"] = task_id
        actions.append(entry)
    memories = []
    for m in data.get("memory_updates") or []:
        if len(memories) >= 2:
            break
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        evidence = m.get("evidence")
        category = m.get("category")
        content = content.strip()[:200] if isinstance(content, str) else ""
        evidence = evidence.strip()[:200] if isinstance(evidence, str) else ""
        if not content or not evidence:  # 无出处的画像不落库（classifier 消费同款纪律）
            continue
        category = (category.strip()[:30] if isinstance(category, str) and category.strip()
                    else "作息规律")
        memories.append({"category": category, "content": content, "evidence": evidence})
    return {"strategy_output": strategy, "checklist": checklist,
            "action_items": actions, "memory_updates": memories}


def _clamp_duration(v) -> int | None:
    """估时钳制：正值 → [15, 240] 归整 15 倍数；非法/缺失 → None（条目保留，
    Node4 按 inbox 流向兜住）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    d = int(v)
    if d <= 0:
        return None
    return max(15, min(240, round(d / 15) * 15))


def _known_ids(deep_context: dict) -> set[str]:
    """deep_context 已知任务 id 集合（task_id 白名单原料）——遍历各键清单里
    的 dict 条目取 id（today/tomorrow/today_rest/inbox/related 等场景键通用，
    无需按场景枚举键名）。"""
    ids: set[str] = set()
    for v in deep_context.values():
        if isinstance(v, list):
            for e in v:
                if isinstance(e, dict) and isinstance(e.get("id"), str):
                    ids.add(e["id"])
    return ids
