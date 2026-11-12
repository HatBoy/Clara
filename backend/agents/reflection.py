"""习惯学习官（阶段二实现，夜间工作流的记忆画像沉淀入口）。

入口契约（docs/ai-agents.md §六 冻结）：run_reflection(db) -> ReflectionReport
（同步函数；services/workflows.py 夜间工作流经 asyncio.to_thread 调用，异常
由壳统一兜底记日志）。

四步执行链（阅读顺序）：
  ⓪ 规则库维护——纯规则零 LLM 两子项：decay_stale_rules 时间衰减（ai_reflect
                          规则超 90 天未印证降 confidence、归零清退，PRD §4.3）
                          + _learn_source_value 源价值学习（按源采纳率结算表给
                          低价值源立一条"源价值"规则，docs/ai-ingest.md §9.4 模板）；
  ① _reflect_candidates  行为归纳——两类信号 → LLM 归纳候选规则（必带
                          evidence + confidence，无证据条目逐条丢弃）：当日已决
                          建议（adjudication 信号）+ 当日用户原话节选（chat 信号，
                          2026-10-20 批 4 增——用户明说的偏好"周五下午别排事"
                          此前永不入画像）；无已决建议且对话信号 <3 → 跳过全部
                          LLM 零成本返回；
  ② arbitrate_rules      冲突消解——候选 vs 现有规则 → LLM 决策
                          new / replace / merge / keep（2026-11-03 记忆重构批 2
                          起提炼为 agents/arbiter.py 公共仲裁者，三写入链收敛；
                          纠偏账预过滤 + 让位硬闸批 2 收紧为「非 user 信号不得动
                          user_manual/chat 规则」——用户明说的规则只有用户本人的
                          明示信号配动，代码 + 提示词双保险）；
  ③ _apply_actions       落库——全部经 services.ai_data 写函数，总量控制整体
                          委托 upsert_memory_rule（满 120 自动淘汰注入序末位）。

纪律（红线）：宁可不学，不可学错——①或②任一步整体解析失败 / 单条校验不过，
都是零落库或丢条，绝不带病落库；call_llm 裸奔不 try（调用失败是故障上抛，
与五节点口径一致）。数据现状约束：modified 的实际偏离不回流（Apply 走前端
管线不回写 proposal），evidence 只能基于 outcome 分布与 reject_reason——
取舍登记 docs/tech-debt.md。
"""

import json
import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from agents.parsing import loads_obj
logger = logging.getLogger(__name__)

from agents.arbiter import arbitrate_rules, filter_dismissed, slice_existing_rules
from agents.prompts import REFLECT_PROMPT
from services.ai_data import (
    adoption_stats_by_source,
    aggregate_deviations,
    day_user_excerpts,
    decay_stale_rules,
    delete_memory_rule,
    list_memory_rules,
    list_proposals,
    prune_behavior_events,
    prune_stale_dismissals,
    proposal_titles,
    upsert_memory_rule,
)
from services.episodic import distill_fact_candidates, expire_episodic_facts, upsert_fact

# 归纳步的输出 token 上限（候选至多几条，短输出小调用；消解步上限随 arbiter 迁走）
_REFLECT_MAX_TOKENS = 800

# behavior 信号候选的置信域（用户需求验证级 0.6-0.8；批 7）
_BEHAVIOR_CONF_MIN = 0.6
_BEHAVIOR_CONF_MAX = 0.8
# REFLECT 输出 facts 候选的条数上限（临时状态一晚至多记 3 条）
_FACTS_MAX = 3

# chat 信号双闸（2026-10-20 批 4）：对话原话推断的规则天生低置信（原话未必
# 是稳定习惯）——每日至多 2 条候选、confidence 钳到 ≤0.55；不足 3 条对话
# 信号不进归纳（单句表达太偶然）
_CHAT_CANDIDATES_MAX = 2
_CHAT_CANDIDATE_CONFIDENCE_CAP = 0.55
_CHAT_MIN_SIGNALS = 3

# 源价值学习判定闸（docs/ai-ingest.md §9.4）：样本与采纳率双阈值——不足样本
# 不判定（偶发不是模式），低采纳才立规则；confidence 固定 0.6（统计推断非实证）
_SOURCE_MIN_TOTAL = 5
_SOURCE_LOW_RATE = 0.3
_SOURCE_RULE_CONFIDENCE = 0.6

# 已决口径：习惯学习只认用户亲手翻过牌的建议（pending / expired 无信号）
_DECIDED_OUTCOMES = frozenset({"accepted", "modified", "rejected"})


@dataclass
class ReflectionReport:
    """一次反思的结果摘要（workflows 夜间日志一行消费，字段语义见 run_reflection）。"""

    created: int = 0     # 步骤② new 落库条数
    merged: int = 0      # 步骤② merge 落库条数（旧行已删）
    replaced: int = 0    # 步骤② replace 落库条数（旧行已删）
    kept: int = 0        # 步骤② keep 条数（现有规则已足够，不动库）
    discarded: int = 0   # 校验不过被丢弃的候选 / 动作条数（①的丢条 + ②整批丢弃）
    source_rules: int = 0  # 步骤⓪ 源价值规则新增 / 印证条数
    decayed: int = 0     # 步骤⓪ 时间衰减降权条数（PRD §4.3）
    removed: int = 0     # 步骤⓪ 衰减归零清退条数
    anchored: int = 0    # 高置信打锚/重摘条数（优化批 8——记忆行为化白盒面）
    skipped: bool = False  # 当日无已决建议且对话信号不足，①②整体跳过（零 LLM 零成本）
    # 批 7（2026-11-03 记忆重构）扩展：维护与自进化计数
    events_pruned: int = 0   # ⓪.5 行为遥测清理条数
    facts_expired: int = 0   # ⓪.5 过期事实清理条数
    facts_created: int = 0   # ① REFLECT 输出的 episodic 事实落库条数
    distilled: int = 0       # ② 事实蒸馏候选条数（含未过仲裁的）
    reinforced: int = 0      # ③ keep 印证触发置信升档条数

    def __str__(self) -> str:
        tail = "（当日无裁决且对话信号不足，归纳跳过）" if self.skipped else ""
        return (f"衰减 {self.decayed} 条清退 {self.removed} 条；源价值规则 "
                f"{self.source_rules} 条；归纳 new={self.created} "
                f"merge={self.merged} replace={self.replaced} keep={self.kept} "
                f"discard={self.discarded}；事实 +{self.facts_created} 过期 "
                f"{self.facts_expired} 蒸馏 {self.distilled} 升档 {self.reinforced}{tail}")


# —— 步骤⓪：源价值学习（纯规则零 LLM） ——————————————————————————————————————


def _learn_source_value(db: Session) -> int:
    """近 14 天按源结算表中 total≥5 且 rate<0.3 的源 → upsert 一条"源价值"规则
    （同 category+content 再跑即印证更新，不重复堆行）。规则供采集提示词与
    planner 参考"该源的建议少采纳"。返回新增 / 印证条数。"""
    stats = adoption_stats_by_source(db)
    learned = 0
    for agg in stats["by_source"]:
        if agg["total"] < _SOURCE_MIN_TOTAL or agg["rate"] >= _SOURCE_LOW_RATE:
            continue
        upsert_memory_rule(
            db,
            category="源价值",
            content=f"来自 {agg['source_key']} 的建议很少被采纳，提炼时降低其优先级",
            confidence=_SOURCE_RULE_CONFIDENCE,
            evidence=f"近14天 {agg['total']} 条建议仅 {agg['accepted_modified']} 条采纳",
        )
        learned += 1
    return learned


# —— 步骤①：行为归纳（当日已决建议 → 候选规则） ————————————————————————————

# 归纳候选的 content / evidence 截断（提示词契约：content ≤100 字、evidence ≤80 字）
_CAND_CONTENT_CHARS = 100
_CAND_EVIDENCE_CHARS = 80


def _valid_confidence(v) -> bool:
    """confidence 合法性：数值且 (0, 1]——0.0 置信的"规则"没有学习价值。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and 0 < float(v) <= 1)


def _reflect_candidates(decisions: list[dict], existing_rules: list[str],
                        chat_excerpts: list[str], day: date,
                        deviations: list[dict] | None = None
                        ) -> tuple[list[dict], list[dict], int]:
    """LLM 归纳 → (规则候选, episodic 事实候选, 丢弃数)。整体解析失败 → ([], [], 0)
    整批视为垃圾。

    day 形参（2026-10-21 复查批 B 增）：取数日期由 run_reflection 入口取一次
    贯穿全程——旧版入口与这里各取一次 date.today()，跨 0 点长跑时提示词日期
    与取数日期漂移。

    deviations（批 7）：behavior_events 偏差聚类（纯规则计数）作第三类证据进
    提示词；signal=behavior 的候选 confidence 钳 [0.6, 0.8]（验证级区间——
    统计验证的起点高于 chat 推断、上限不越用户显式）。

    逐条校验（提示词先讲对，这里再兜底）：content str 非空≤100 字、evidence str
    非空≤80 字、confidence ∈ (0,1]——无出处的画像不落库；category 缺失降"其他"。
    signal 双保险（2026-10-20 批 4）：模型标 "chat"（依据用户原话归纳）才认
    chat；批 7 扩 "behavior"（偏差聚类归纳）；其余一律 adjudication。chat 候选
    每日至多 2 条（超出丢弃计入 dropped）、confidence 钳到 ≤0.55——原话未必是
    稳定习惯，学错代价大于漏学。"""
    from services.llm import call_llm_aux

    out = call_llm_aux(
        [{"role": "system", "content": REFLECT_PROMPT},
         {"role": "user", "content": json.dumps(
             {"today": day.isoformat(),
              "decisions": decisions,
              "chat_excerpts": chat_excerpts,
              "existing_rules": existing_rules,
              "deviations": deviations or []}, ensure_ascii=False)}],
        json_mode=True, max_tokens=_REFLECT_MAX_TOKENS)
    data = loads_obj(out)
    if data is None:
        return [], [], 0
    candidates: list[dict] = []
    facts: list[dict] = []
    dropped = 0
    chat_count = 0
    for c in data.get("candidates") or []:
        content = c.get("content") if isinstance(c, dict) else None
        evidence = c.get("evidence") if isinstance(c, dict) else None
        confidence = c.get("confidence") if isinstance(c, dict) else None
        category = c.get("category") if isinstance(c, dict) else None
        if (not isinstance(content, str) or not content.strip()
                or not isinstance(evidence, str) or not evidence.strip()
                or not _valid_confidence(confidence)):
            dropped += 1
            continue
        raw_signal = c.get("signal")
        signal = raw_signal if raw_signal in ("chat", "behavior") else "adjudication"
        value = float(confidence)
        if signal == "chat":
            if chat_count >= _CHAT_CANDIDATES_MAX:
                dropped += 1
                continue
            chat_count += 1
            value = min(value, _CHAT_CANDIDATE_CONFIDENCE_CAP)
        elif signal == "behavior":
            value = max(_BEHAVIOR_CONF_MIN, min(value, _BEHAVIOR_CONF_MAX))
        candidates.append({
            "category": category.strip() if isinstance(category, str) and category.strip() else "其他",
            "content": content.strip()[:_CAND_CONTENT_CHARS],
            "evidence": evidence.strip()[:_CAND_EVIDENCE_CHARS],
            "confidence": value,
            "signal": signal,
        })
    # episodic 事实候选（批 7 兜底通道：chat excerpts 里当轮没走 settings 链的
    # 临时状态，最迟次晨入库；非法条目丢弃）
    for f in (data.get("facts") or [])[:_FACTS_MAX]:
        if not isinstance(f, dict):
            continue
        topic = f.get("topic") if isinstance(f.get("topic"), str) else ""
        fact = f.get("fact") if isinstance(f.get("fact"), str) else ""
        if topic.strip() and fact.strip():
            facts.append({"topic": topic.strip()[:60], "fact": fact.strip()[:200],
                          "expires_days": f.get("expires_days")})
    return candidates, facts, dropped


# —— 步骤②：冲突消解（agents/arbiter.py 公共仲裁者，2026-11-03 批 2 迁出） ————


# —— 步骤③：落库 ——————————————————————————————————————————————————————————————


def _apply_actions(db: Session, actions: list[dict], report: ReflectionReport) -> None:
    """keep 印证演化（批 7）；new 直接 upsert；replace/merge 先删旧行再 upsert
    合成终态——先删再写腾出名额，防 upsert 的满 120 自动淘汰误伤无辜行。

    keep 演化（2026-11-03 记忆重构）：目标行 observation_count += 1、
    last_confirmed_at 刷新；ai_reflect 行 count ≥3 且 confidence <0.8 自动升档
    min(0.8, +0.15)——0.8 上限（1.0 只属用户显式），升到 0.8 次晚打锚步自动
    生效为硬约束（观察→验证→法则的中间档自动爬升，法则级仍由用户拍板）。"""
    from models import MemoryRule
    from datetime import datetime

    for a in actions:
        if a["action"] == "keep":
            report.kept += 1
            row = db.get(MemoryRule, a.get("rule_id")) if a.get("rule_id") else None
            if row is not None:
                row.observation_count = (getattr(row, "observation_count", 0) or 0) + 1
                row.last_confirmed_at = datetime.now()
                if (row.source == "ai_reflect"
                        and row.observation_count >= 3
                        and (row.confidence or 0) < 0.8):
                    row.confidence = round(min(0.8, (row.confidence or 0) + 0.15), 2)
                    report.reinforced += 1
            continue
        if a["action"] in ("replace", "merge"):
            delete_memory_rule(db, a["rule_id"])
        upsert_memory_rule(db, category=a["category"], content=a["content"],
                           confidence=a["confidence"], evidence=a["evidence"])
        if a["action"] == "new":
            report.created += 1
        elif a["action"] == "replace":
            report.replaced += 1
        else:
            report.merged += 1


# —— 入口（签名冻结） ————————————————————————————————————————————————————————

# 已决建议进提示词的行数上限（防上下文膨胀；当日已决建议通常个位数）
_MAX_DECISION_ROWS = 50


def run_reflection(db: Session) -> ReflectionReport:
    """习惯学习主入口：⓪衰减清算 + 源价值 → ①归纳 → ②消解 → ③落库 → 结果摘要。

    陈年清算（衰减/源价值）先于早退——规则库维护不依赖当日信号；无已决建议
    且对话信号 <3 条 → skipped=True 零 LLM 返回（夜间常态：多数天既没有裁决
    也没有足够原话）；①输出垃圾 → 零落库；②输出垃圾 → 候选整批丢弃零落库
    （宁可不学不可学错）；call_llm 异常原样上抛（故障就是故障，壳兜底）。
    取数日期入口取一次贯穿全程（2026-10-21 复查批 B）——跨 0 点长跑不再让
    提示词日期与取数日期漂移。"""
    report = ReflectionReport()
    report.decayed, report.removed = decay_stale_rules(db)
    # 高置信打锚先于"无信号"早退（同衰减先例）：安静夜晚锚也要刷新（置信
    # 跌破阈值/文本改写后的重摘都在这一晚发生）；批 7 起顺带回填旧行 slot
    report.anchored = _anchor_high_confidence_rules(db)
    report.source_rules = _learn_source_value(db)
    # ⓪.5 维护步（批 7，纯规则零 LLM）：遥测/事实/纠偏账三清——过期即物理清理
    report.events_pruned = prune_behavior_events(db)
    report.facts_expired = expire_episodic_facts(db)
    prune_stale_dismissals(db)

    today = date.today()
    decisions = [p for p in list_proposals(db, day=today)
                 if p.outcome in _DECIDED_OUTCOMES]
    excerpts = day_user_excerpts(db, day=today)
    deviations = aggregate_deviations(db)
    if not decisions and len(excerpts) < _CHAT_MIN_SIGNALS and not deviations:
        report.skipped = True
        return report

    # 现有规则切片单源（arbiter.slice_existing_rules：40 条/3000 字符双闸，ORM
    # 行形态）——REFLECT 输入的字符串形态与 arbitrate 的 existing 共用同一份切片
    existing_capped = slice_existing_rules(db)
    rows = [{
        "kind": p.kind,
        "titles": proposal_titles(p.kind, p.payload or {}),
        "outcome": p.outcome,
        "reject_reason": (p.payload or {}).get("reject_reason"),
        # modified 偏离（优化批 4 回流）：用户把建议改成了什么的最直接证据——
        # 改时间=时段偏好、改时长=估时修正；item_diffs 是条目级结算的暂存形态
        "modified_diff": (p.payload or {}).get("modified_diff")
                        or (p.payload or {}).get("item_diffs"),
    } for p in decisions[:_MAX_DECISION_ROWS]]
    candidates, facts, dropped = _reflect_candidates(
        rows, [f"[{r.category}] {r.content}" for r in existing_capped],
        excerpts, today, deviations=deviations)
    report.discarded += dropped
    # episodic 事实落库（批 7 兜底通道：upsert 内部去重与钳制）
    for f in facts:
        if upsert_fact(db, topic=f["topic"], fact=f["fact"],
                       source="reflect", expires_days=f.get("expires_days")) is not None:
            report.facts_created += 1
    # 事实蒸馏候选（批 7：同 topic ≥3 条 → Tier 1 候选并入仲裁队列）
    distilled = distill_fact_candidates(db)
    report.distilled = len(distilled)
    candidates = candidates + distilled
    if not candidates:
        return report

    # 纠偏账丢弃计数（2026-11-03 批 2：arbitrate 内部亦兜底过滤，此处取计数进报告）
    _, dismissed_n = filter_dismissed(db, candidates)
    report.discarded += dismissed_n
    actions = arbitrate_rules(db, candidates=candidates)
    if actions is None:  # 消解整批垃圾 → 候选全部作废
        report.discarded += len(candidates)
        return report
    _apply_actions(db, actions, report)
    return report


def _anchor_high_confidence_rules(db) -> int:
    """高置信规则结构化打锚（2026-11-01 优化批 8 自进化）：置信 ≥0.8 且文本可
    被 preference_slots 解析成避让窗的规则，预解析存 structured_anchor——设置页
    记忆画像行据此显「已生效为硬约束」徽标（记忆行为化的白盒可观察面）；删行
    即摘锚（行没了锚自然失效），置信跌破阈值或文本改写后下一晚自动重打。
    一致性守卫：锚存在但运行时解析不出窗（提示词/解析器代际漂移）→ warning
    暴露。返回打锚行数。全链闸消费锚形态（state 契约演进）登记后续项。"""
    from services.preference_slots import parse_avoid_windows

    n = 0
    for rule in list_memory_rules(db):
        # 批 7：旧行 slot 回填（2026-11-03 前的行 slot 为 NULL，打锚步每晚顺带
        # 补齐——不写一次性迁移，三晚内自然收敛）
        if getattr(rule, "slot", None) is None:
            from services.ai_data import slot_of

            rule.slot = slot_of(rule.category, rule.content)
            n += 1
        text = f"[{rule.category}] {rule.content}"
        windows = parse_avoid_windows([text]) or parse_avoid_windows([rule.content])
        anchor = windows[0] if (rule.confidence or 0) >= 0.8 and windows else None
        if getattr(rule, "structured_anchor", None) != anchor:
            rule.structured_anchor = anchor
            n += 1
        if rule.structured_anchor and not windows:
            logger.warning("规则锚与运行时解析漂移：%r", rule.content)
    if n:
        db.commit()
    return n
