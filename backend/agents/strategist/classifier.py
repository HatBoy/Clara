"""Node1 场景嗅探与边界澄清（批 2 正式化，2026-10-21）。

短路栈（任一步命中即定型，先纯代码后 LLM——高频硬信号零成本先出）：
① cron 直打：trigger ∈ {morning, nightly} → scenario_type=trigger，零 LLM
   （run_preflight 收编入口，报点轮不需要语义判定）；
② memory pending 消费：agree → 节点内 upsert_memory_rule（evidence 空不落
   库）+ resolved accepted + 终态；refuse → resolved ignored + 终态；
   unrelated → 不结算走 ④⑤（pending 生命周期对齐 scheduler：答非所问不算
   回答，问询保留等真回答或被新锚点顶替）；
③ clarify pending 消费：agree → 合并恢复（original_input + 补充，恢复轮
   零分类 LLM）+ resolved accepted（当轮结算防 active_pending 死循环——纯
   文本回复轮无新锚点，不结算会反复进消费）；refuse → 终态；unrelated 同②；
④ 危机正则：_CRISIS_RE 命中 → crisis，零 LLM（对照 center_router 规则
   预路由先例——宁漏判给 ⑤ 的 LLM 机会）；
⑤ LLM 七场景分类 + clarify 追问挂起（question_plan + final_reply=问句
   文案，壳把 final_reply 拷进 payload.ask_text——router 前缀消费）。

节点内写库说明：scheduler 子图节点内读库（intake）已有先例，写库是本子图
   新增形态——九键出口无记忆写库键，走出口键须扩契约，节点内写库反而保
   契约零漂移（resolved_proposal 仍由壳结算）。

LLM 调用数 0-1（分类或问询裁决，互斥单次）。
"""

import logging
import re
from datetime import datetime

from agents.nodes import _status_emitter
from agents.parsing import loads_obj

from .prompts import CLASSIFIER_PROMPT, PENDING_JUDGE_PROMPT, SCENARIOS
from .state import StrategistState

logger = logging.getLogger(__name__)

# 危机信号正则（口语高频形态；枚举边界是已知取舍——未列词走 ⑤ LLM 判定）
_CRISIS_RE = re.compile(r"救命|全乱了|崩了|搞砸|来不及|紧急")
_ASK_MAX_CHARS = 80
# 二次引导豁免（优化批 6）：带明确动作词的输入是新话题/新指令——放行走分类，
# 不拦着二次引导（复用 nodes 预路由同源动词形态，模块内自持防环）
_REASK_EXEMPT_RE = re.compile(
    r"安排|排|预约|订|记一下|提醒我|删|取消|什么|哪|几|吗|呢|看看|查|天气")


def classifier_node(state: StrategistState) -> dict:
    """嗅探节点：见模块 docstring 短路栈。输出 trigger_input/scenario_type/
    now 或早退出口键（final_reply/resolved_proposal/question_plan）。"""
    trigger = state.get("trigger") or "chat"
    user_input = (state.get("user_input") or "").strip()
    pending = state.get("pending")
    now = datetime.now().isoformat(timespec="minutes")

    # ① cron 直打（报点轮零语义判定）
    if trigger in ("morning", "nightly"):
        logger.info("[strategist] classifier：cron 直打 scenario=%s（零 LLM）", trigger)
        return {"trigger_input": trigger, "scenario_type": trigger, "now": now}

    # ②③ 幕僚问询消费（memory / clarify 双形态；unrelated 返回 None 落分类）
    if pending and pending.get("kind") == "memory":
        out = _consume_memory(pending, user_input)
        if out is not None:
            logger.info("[strategist] classifier：memory 问询消费（%s）",
                        out.get("resolved_proposal", {}).get("outcome"))
            return out
    elif pending and pending.get("kind") == "strategist_clarify":
        out = _consume_clarify(pending, user_input, now)
        if out is not None:
            logger.info("[strategist] classifier：clarify 问询消费（%s）",
                        out.get("resolved_proposal", {}).get("outcome"))
            return out
        # 二次引导（2026-11-01 优化批 6）：judge 判 unrelated 但本轮是短对话式
        # 回复（≤12 字且无排期/查询强动词）——大概率是没答到点上而非新话题，
        # 换个角度把原诉求再问一次（挂起保留，真回答随时可续）；带明确动作
        # 的输入照旧放行走分类（新话题语义）
        if (user_input and len(user_input) <= 12
                and not _REASK_EXEMPT_RE.search(user_input)):
            ask = pending.get("ask_text") or "补充信息"
            original = (pending.get("original_input") or "").strip()[:40]
            logger.info("[strategist] classifier：clarify 答非所问，二次引导")
            return {"final_reply":
                    f"可能没说清楚——我想问的是：{ask}"
                    + (f"（你说的是「{original}」这件事）" if original else "")
                    + "。方便补充一下吗？"}

    # ④ 危机正则（零 LLM 预打 tag）
    if user_input and _CRISIS_RE.search(user_input):
        logger.info("[strategist] classifier：危机正则命中 scenario=crisis（零 LLM）")
        return {"trigger_input": user_input, "scenario_type": "crisis", "now": now}

    # ⑤ LLM 七场景分类 + clarify 追问挂起
    _status_emitter()("幕僚长正在梳理场景…")  # 过程反馈（提速批 2；短路①-④零帧）
    scenario, ask = _classify(user_input)
    if ask:
        logger.info("[strategist] classifier：LLM 判定 scenario=%s，信息不足追问挂起", scenario)
        return {
            "question_plan": {
                "kind": "strategist_clarify",
                "entries": [],  # 壳对幕僚 kind 放行空 entries（无排期条目）
                "original_input": user_input,
                "scenario_type": scenario,   # 挂起时必存——恢复轮零分类 LLM
                "ask_text": f"想先补一个关键信息：{ask}",
            },
            "final_reply": f"想先补一个关键信息：{ask}",
            "now": now,
        }
    logger.info("[strategist] classifier：LLM 判定 scenario=%s", scenario)
    return {"trigger_input": user_input, "scenario_type": scenario, "now": now}


def _judge(ask: str, reply: str) -> str:
    """问询裁决小提示词（两类 pending 共用）。返回 agree/refuse/unrelated；
    垃圾输出按 unrelated（保守：不写库不结算，回分类兜底）。"""
    from services.llm import call_llm_aux

    out = call_llm_aux(
        [{"role": "system",
          "content": PENDING_JUDGE_PROMPT.format(ask=ask, reply=reply)},
         {"role": "user", "content": reply}],
        json_mode=True, max_tokens=60)
    data = loads_obj(out)
    if isinstance(data, dict) and data.get("action") in ("agree", "refuse", "unrelated"):
        return data["action"]
    logger.warning("[strategist] 问询裁决输出不可解析，兜底 unrelated（残文头 80 字：%s）",
                   (out or "")[:80])
    return "unrelated"


def _consume_memory(pending: dict, user_input: str) -> dict | None:
    """memory 问询消费：agree 写库+终态 / refuse 终态 / unrelated → None 走分类。

    2026-11-03 记忆重构批 2：agree 落库前查纠偏账——用户删除过的同义规则
    （difflib ≥0.6）不再写入（问询确认制已是 human-in-the-loop，不走仲裁，
    纠偏账是唯一前置闸），命中时改回执告知。"""
    action = _judge(pending.get("ask_text") or "", user_input)
    pid = pending["proposal_id"]
    if action == "agree":
        rule = pending.get("rule") if isinstance(pending.get("rule"), dict) else {}
        content = (rule.get("content") or "").strip()[:200]
        evidence = (rule.get("evidence") or "").strip()[:200]
        category = (rule.get("category") or "作息规律").strip()[:30]
        if content and evidence:  # "无出处的画像不落库"（upsert 同款纪律）
            from agents.arbiter import filter_dismissed
            from database import SessionLocal
            from services.ai_data import upsert_memory_rule
            with SessionLocal() as db:
                kept, _ = filter_dismissed(db, [{
                    "category": category, "content": content, "evidence": evidence,
                    "confidence": 0.5, "signal": "adjudication"}])
                if kept:
                    upsert_memory_rule(db, category=category, content=content,
                                       confidence=0.5, evidence=evidence,
                                       source="ai_reflect")
                    reply = f"已写入长期记忆：{content}。之后排期与简报都会参考它。"
                else:
                    reply = ("这条和之前删除过的偏好很接近，先不写入了——"
                             "如果确实想要，可直接在设置 → 记忆画像手动添加。")
        else:  # 提案字段残缺：不写库但照常结算（用户已同意，落不了是提案问题）
            reply = "这条提案信息不全（缺少依据），先不写入——下次复盘再提。"
        return {"resolved_proposal": {"proposal_id": pid, "outcome": "accepted"},
                "final_reply": reply}
    if action == "refuse":
        return {"resolved_proposal": {"proposal_id": pid, "outcome": "ignored"},
                "final_reply": "好，这条不写入记忆。"}
    return None


def _consume_clarify(pending: dict, user_input: str, now: str) -> dict | None:
    """clarify 问询消费：agree 合并恢复续管道 / refuse 终态 / unrelated → None。"""
    # 挂起原诉求并入裁决输入（2026-11-01 优化批 1）：用户补充是否真在回应当时
    # 的诉求，裁决器需看见原诉求而不只是追问句——答非所问判定更准（tech-debt
    # 已登记「clarify 恢复轮信任挂起快照」的轻量改善，提示词层不动状态键）
    ask = pending.get("ask_text") or ""
    original = (pending.get("original_input") or "").strip()
    if original:
        ask = f"{ask}（当时诉求：{original[:80]}）"
    action = _judge(ask, user_input)
    pid = pending["proposal_id"]
    if action == "agree":
        merged = ((pending.get("original_input") or "")
                  + f"\n（补充）{user_input}")
        return {"resolved_proposal": {"proposal_id": pid, "outcome": "accepted"},
                "trigger_input": merged,
                "scenario_type": pending.get("scenario_type") or "execution",
                "now": now}
    if action == "refuse":
        return {"resolved_proposal": {"proposal_id": pid, "outcome": "ignored"},
                "final_reply": "好，这次先不展开。需要时随时说。"}
    return None


def _classify(user_input: str) -> tuple[str, str | None]:
    """七场景分类小提示词。返回 (scenario, ask|None)；垃圾输出兜底
    execution（教练默认场景——旧 coach 对教练意图内输入的全兜底先例）。"""
    from services.llm import call_llm_aux

    out = call_llm_aux(
        [{"role": "system", "content": CLASSIFIER_PROMPT},
         {"role": "user", "content": user_input}],
        json_mode=True, max_tokens=120)
    data = loads_obj(out)
    if isinstance(data, dict) and data.get("scenario") in SCENARIOS:
        ask = data.get("ask")
        # 「无追问」的字面量形态滤除（2026-11-01 真机批）：LLM 常把 JSON null
        # 写成字符串 "null"/"none" 表达没有追问——真机曾字面输出
        # 「想先补一个关键信息：null」挂起假问询
        ask = (ask.strip()[:_ASK_MAX_CHARS]
               if isinstance(ask, str) and ask.strip()
               and ask.strip().lower() not in ("null", "none", "无") else None)
        return data["scenario"], ask
    logger.warning("[strategist] 场景分类输出不可解析，兜底 execution（残文头 80 字：%s）",
                   (out or "")[:80])
    return "execution", None
