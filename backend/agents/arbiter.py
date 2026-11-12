"""记忆仲裁者（2026-11-03 记忆重构批 2）：候选规则 vs 现有规则的冲突消解单点。

从反思链 _resolve_actions 提炼为公共服务（决策/执行分离：本模块只出动作 JSON，
落库仍由调用方执行）——三条写入链收敛于此：
- 反思链（adjudication/chat/behavior 信号）：动作照旧落 ai_reflect；
- chat_config 对话设置（user 信号）：动作落 source=chat，keep=印证升信任级；
- strategist 问询确认制不走仲裁（human-in-the-loop 已足，仅纠偏账预过滤）。

四维防线（自反思链原样迁入 + 批 2 收紧）：
① 纠偏账预过滤——用户删除过的规则（memory_dismissals 指纹 difflib ≥0.6）候选
   整条丢弃（用户删了就别再推）；
② existing 双闸切片——40 条 / 3000 字符头部切片（防画像库涨满后小档位模型
   上下文溢出致学习链静默停摆）；
③ 逐条校验——action 四值 / candidate_idx 回抄命中 / rule_id 真实存在 /
   confidence 强制继承候选已钳值（不信模型回抄绕过钳制）；
④ 让位硬闸（批 2 收紧）——signal != "user" 的候选不得 replace/merge
   source ∈ {user_manual, chat} 的规则：用户明说的规则（手写/对话设置/亲笔
   编辑升级）只有用户本人的明示信号配动；此前仅拦 chat 信号，adjudication
   行为归纳可把 user_manual 行替换成 ai_reflect 版——违反「最高裁决权」需求，
   收紧为非 user 全拦（代码 + RESOLVE_PROMPT 让位规则双保险）。

返回 None = LLM 失败/整批不可解析（调用方决定降级：反思链整批丢弃、chat_config
降级直写——两条调用方语义不同故不在此兜底）。

事务边界：本函数只读不写；调用方 replace/merge 的先删后写跨两次 commit 保持
既有惯例（夜间单线程 + chat 链 TURN_LOCK 串行无并发撕裂面）。
"""

import difflib
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.parsing import loads_obj
from agents.prompts import RESOLVE_PROMPT
from services.ai_data import list_memory_rules

logger = logging.getLogger(__name__)

# 消解步输出 token 上限（动作行短输出小调用，自反思链迁入同值）
_ARBITER_MAX_TOKENS = 1000

# existing 进提示词的双闸（assemble_system_prompt / clip_rules 同款口径）
_EXISTING_MAX_COUNT = 40
_EXISTING_CHAR_BUDGET = 3000

# 纠偏账指纹命中阈值（与 chat_config.remove_preference / 排期链查重同 0.6 口径）
_DISMISSAL_SIM = 0.6

_CAND_EVIDENCE_CHARS = 80


def slice_existing_rules(db: Session) -> list:
    """现有规则 ORM 行的头部切片（注入序 = 优先级序；条数 ≤40 + 累计 ≤3000 字符
    双闸）。单源供 arbitrate_rules 与反思链 REFLECT 输入共用（反思链还需 ORM 行
    读 id/source）。"""
    existing_capped: list = []
    chars = 0
    for r in list_memory_rules(db):
        line = f"[{r.category}] {r.content}"
        if len(existing_capped) >= _EXISTING_MAX_COUNT or chars + len(line) > _EXISTING_CHAR_BUDGET:
            break
        existing_capped.append(r)
        chars += len(line)
    return existing_capped


def filter_dismissed(db: Session, candidates: list[dict]) -> tuple[list[dict], int]:
    """纠偏账预过滤：候选 content 归一化指纹与 memory_dismissals.fingerprint
    相等或 difflib ≥0.6 → 命中丢弃（用户删了就别再推）。signal="user" 的候选
    豁免——用户明确重说即改主意，自己的旧删除不拦自己的新明示。返回
    (过滤后候选, 丢弃数)。"""
    from models import MemoryDismissal
    from services.tasks import normalize_title

    ledger = [d.fingerprint for d in db.scalars(select(MemoryDismissal)).all()]
    kept: list[dict] = []
    dropped = 0
    for c in candidates:
        if c.get("signal") == "user":
            kept.append(c)
            continue
        target = normalize_title(c.get("content") or "")
        hit = any(
            (target == fp) or (target and fp and difflib.SequenceMatcher(
                None, target, fp).ratio() >= _DISMISSAL_SIM)
            for fp in ledger)
        if hit:
            dropped += 1
            logger.info("[arbiter] 候选命中纠偏账丢弃：%r", c.get("content"))
        else:
            kept.append(c)
    return kept, dropped


def arbitrate_rules(db: Session, *, candidates: list[dict]) -> list[dict] | None:
    """候选 vs 现有规则 → 动作列表（四防线见模块 docstring）。

    candidates 形态：[{category, content, evidence, confidence, signal}]，signal ∈
    adjudication（裁决归纳）/ chat（对话推断，低置信）/ behavior（行为偏差验证，
    0.6-0.8）/ user（用户明示，可动任何源）。整体解析失败 → None。
    """
    from services.llm import call_llm_aux

    candidates, _dismissed = filter_dismissed(db, candidates)
    if not candidates:
        return []
    existing = slice_existing_rules(db)
    out = call_llm_aux(
        [{"role": "system", "content": RESOLVE_PROMPT},
         {"role": "user", "content": json.dumps(
             {"candidates": [{"idx": i, **c} for i, c in enumerate(candidates)],
              "existing": [{"id": r.id, "category": r.category, "content": r.content,
                            "source": r.source} for r in existing]}, ensure_ascii=False)}],
        json_mode=True, max_tokens=_ARBITER_MAX_TOKENS)
    data = loads_obj(out)
    if data is None:
        return None
    valid_ids = {r.id for r in existing}
    source_by_id = {r.id: r.source for r in existing}
    actions: list[dict] = []
    for a in data.get("actions") or []:
        if not isinstance(a, dict) or a.get("action") not in ("new", "replace", "merge", "keep"):
            continue
        action = a["action"]
        rule_id = a.get("rule_id")
        idx = a.get("candidate_idx")
        cand = (candidates[idx]
                if isinstance(idx, int) and not isinstance(idx, bool)
                and 0 <= idx < len(candidates) else None)
        if cand is None:
            continue  # 候选对应断裂（idx 缺失/越界/幻觉）
        if action in ("replace", "merge"):
            if not (isinstance(rule_id, str) and rule_id in valid_ids):
                continue
            # 让位硬闸（批 2 收紧）：非 user 信号不得动用户明说规则
            if (cand.get("signal") != "user"
                    and source_by_id.get(rule_id) in ("user_manual", "chat")):
                continue
        content = a.get("content")
        if action != "keep" and not (isinstance(content, str) and content.strip()):
            continue
        evidence = a.get("evidence")
        category = a.get("category")
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        actions.append({
            "action": action,
            "rule_id": rule_id if isinstance(rule_id, str) else None,
            "category": category.strip() if isinstance(category, str) and category.strip() else "其他",
            "content": content.strip()[:120] if action != "keep" else None,
            "evidence": evidence.strip()[:_CAND_EVIDENCE_CHARS],
            "confidence": cand["confidence"],  # 继承候选已钳值，不信回抄
            "signal": cand.get("signal"),
        })
    return actions
