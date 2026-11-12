"""AI 数据域业务层（2026-09-20 增，阶段二）：记忆画像 / 建议日志 / 会话消息三表 CRUD
+ 信任渐进统计 + 冷启动种子。

三表同属一个记忆闭环（proposals 的 outcome 是习惯学习官的输入 → 产出 memory_rules
→ 注入对话；messages 挂 proposals 展示卡片态），reflection 与 ai_coach 壳的调用横跨
多表，合并单文件（同 reminders.py 一域一文件惯例）。

注入序约定（接缝契约）：list_memory_rules 的排序 = user_manual 优先 → confidence
降序 → updated_at 降序。手写区 prompts.py 做预算截断时直接对返回列表头部切片即可，
排序由本层保证。
"""

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import ChatState, MemoryDismissal, MemoryRule, Message, Proposal
from schemas import MemoryRuleCreate, MemoryRuleUpdate
from services.settings import get_settings
from services.tasks import list_tasks

logger = logging.getLogger(__name__)

# 记忆规则总量硬上限（PRD §4.3：满预算时新增一条须淘汰一条低置信旧规则）
MEMORY_RULES_MAX = 120

# —— 对话滚动上下文参数（2026-10-20 增；三层 = 近期原文 + 滚动摘要 + 长期画像）———
# 近期原文条数：注入节点的"活记忆"窗口（超出部分由滚动摘要覆盖）
CHAT_RECENT_K = 10
# 每条历史消息注入截断字符数（防单条超长消息撑爆 token）
CHAT_MSG_CHARS = 200
# 压缩缓冲：未摘要数超过 K + 缓冲才触发压缩（避免每轮都调 LLM 压缩）
CHAT_COMPRESS_BUFFER = 6
# 硬上限：消息总数超过此值时更早的消息不参与任何压缩（count 游标只前进，见 tech-debt）
CHAT_MAX_WINDOW = 500
# 字符量触发阈值（2026-10-26 增）：未摘要积压字符超此值也触发压缩——条数阈值
# 管短消息高频对话，字符阈值管长消息低频对话（摘要新鲜度跟信息量走，非条数）
CHAT_COMPRESS_CHARS = 6000
# 压缩切块参数（2026-10-26 增，防单次压缩调用塞爆小档位模型）：单条消息超长
# 截断 + 单块字符上限贪心组块，块间逐次 LLM 滚动合并（详见 graph.compact_chat_history）
CHAT_COMPACT_MSG_CHARS = 4000
CHAT_COMPACT_CHUNK_CHARS = 12000


# —— 记忆规则 ——————————————————————————————————————————————

# Tier 1 固定槽位（2026-11-03 记忆重构）：双段注入分组序 = 此元组序
SLOT_VALUES = ("rhythm", "meeting", "boundary", "other")

# boundary 槽的 content 关键词（红线边界语义修正：category 看不出的从正文识别）
_BOUNDARY_HINTS = ("免打扰", "红线", "绝不", "别排", "不排会", "不开会",
                   "不安排", "晚上不", "晚间不", "周末不", "不要排", "不排任何")

# category → slot 基础映射（REFLECT_PROMPT 六分类为底）
_SLOT_BY_CATEGORY = {
    "作息规律": "rhythm",
    "会议偏好": "meeting",
    "工作习惯": "meeting",
    "其他": "other",
}


def slot_of(category: str, content: str) -> str:
    """规则 → 固定槽位的纯规则映射（LLM 不输出 slot，后端单源计算）。

    category 基础映射（作息规律→rhythm；会议偏好/工作习惯→meeting；优先级
    倾向/排期偏好等其余→other）+ content 关键词二次修正（免打扰/红线/绝不/
    别排类 → boundary——「周五下午别排事」category 是排期偏好但语义是红线）。
    宁可归 other 不误归 boundary（boundary 进 CORE 段分组展示，误报更显眼）。
    """
    slot = _SLOT_BY_CATEGORY.get(category or "", "other")
    text = content or ""
    if any(hint in text for hint in _BOUNDARY_HINTS):
        slot = "boundary"
    return slot


def rule_label(row: MemoryRule) -> str:
    """规则行 → 注入单行文本（分级标签单源，2026-11-03 记忆重构收敛）：
    [分类·用户设定]（user_manual/chat 源，铁律级）/ [分类·AI学习·置信N]（ai_reflect
    按实际置信）。chat_turn / workflows 两处内联格式化收敛到此；替身缺列容错
    （getattr）——加列批必查测试替身（tech-debt 教训）。"""
    source = getattr(row, "source", "ai_reflect")
    category = getattr(row, "category", "")
    content = getattr(row, "content", "")
    if source in ("user_manual", "chat"):
        return f"[{category}·用户设定] {content}"
    confidence = getattr(row, "confidence", None) or 0.0
    return f"[{category}·AI学习·置信{round(confidence, 2)}] {content}"


def format_memory_lines(db: Session, *, personality: list[str] | None = None
                        ) -> tuple[list[str], list[str]]:
    """双段注入单源（2026-11-03 记忆重构批 3）：(core_lines, reference_lines)。

    core 铁律段 = 人格规则前置 + user_manual/chat 源全量 + ai_reflect 中
    （confidence ≥0.8 或 structured_anchor 非空）的行，按 slot 分组序
    （rhythm→meeting→boundary→other，组内注入序不重排——稳定排序保证）；
    兜底双闸 60 条 / 4500 字符（正常 ≈10 条达不到，纯防失控）。
    reference 参考段 = 其余 ai_reflect，clip_rules 40 条 / 3000 字符头部切片
    （延迟 import agents.prompts——prompts 纯常量模块无环，延迟保守同 chat_turn
    先例）。
    行格式 rule_label 单源（分级标签）；替身缺列容错 getattr——加列批必查测试
    替身（tech-debt 教训）。
    消费形态：state["memory_rules"] = core + ref（单列表契约零变化），
    自然语言节点另拿 core 传 assemble_system_prompt(core_rules=...) 双段渲染。"""
    core_rows: list = []
    ref_rows: list = []
    for r in list_memory_rules(db):
        anchor = getattr(r, "structured_anchor", None)
        source = getattr(r, "source", "ai_reflect")
        confidence = getattr(r, "confidence", 0.0) or 0.0
        if (source in ("user_manual", "chat")
                or (source == "ai_reflect"
                    and (confidence >= 0.8 or anchor is not None))):
            core_rows.append(r)
        else:
            ref_rows.append(r)

    def _slot_rank(row) -> int:
        slot = getattr(row, "slot", None) or "other"
        try:
            return SLOT_VALUES.index(slot)
        except ValueError:
            return len(SLOT_VALUES) - 1  # 未知槽位归末组（other）

    core_rows.sort(key=_slot_rank)  # 稳定排序：组内注入序不重排
    core = list(personality or []) + [rule_label(r) for r in core_rows]
    # core 兜底双闸（与 clip_rules 同逻辑、独立预算：铁律段正常远达不到上限）
    capped: list[str] = []
    used = 0
    for line in core:
        if len(capped) >= 60 or used + len(line) > 4500:
            break
        capped.append(line)
        used += len(line)
    from agents.prompts import clip_rules  # 延迟 import（prompts 无 services 依赖，防环保守）

    ref = clip_rules([rule_label(r) for r in ref_rows])
    return capped, ref


def find_rule_conflicts(db: Session, *, category: str, content: str,
                        exclude_id: str | None = None) -> list[dict]:
    """手动新增/编辑保存前的冲突预检（纯规则零 LLM 秒回，2026-11-03 用户拍板
    「冲突提示用户确认」形态）：normalize_title 归一化 + difflib 相似度 ≥0.6
    （复用排期链 similar_title 查重口径），排除编辑场景自身。返回冲突行 +
    similarity，供前端弹「合并覆盖 / 两者并存」。"""
    import difflib

    from services.tasks import normalize_title

    target = normalize_title(content)
    if not target:
        return []
    hits: list[dict] = []
    for row in list_memory_rules(db):
        if exclude_id and row.id == exclude_id:
            continue
        ratio = difflib.SequenceMatcher(
            None, target, normalize_title(row.content)).ratio()
        if ratio >= 0.6:
            hits.append({"id": row.id, "category": row.category,
                         "content": row.content, "source": row.source,
                         "similarity": round(ratio, 2)})
    return hits


def prune_stale_dismissals(db: Session, *, days: int = 90) -> int:
    """纠偏账过期清理（夜间反思⓪.5 维护步调用）：超 N 天的删除记录不再拦截
    同义候选（口味会变，同 ai_reflect 衰减周期）。返回清理行数。"""
    cutoff = datetime.now() - timedelta(days=days)
    stale = db.scalars(select(MemoryDismissal).where(
        MemoryDismissal.dismissed_at < cutoff)).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
        logger.info("纠偏账清理 %d 条过期记录", len(stale))
    return len(stale)


def prune_behavior_events(db: Session, *, days: int = 90) -> int:
    """行为遥测过期清理（夜间反思⓪.5 维护步调用，批 5）：偏差聚类窗 14 天，
    90 天留存已覆盖回溯需要；返回清理行数。"""
    from models import BehaviorEvent

    cutoff = datetime.now() - timedelta(days=days)
    stale = db.scalars(select(BehaviorEvent).where(
        BehaviorEvent.created_at < cutoff)).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
        logger.info("行为遥测清理 %d 条过期事件", len(stale))
    return len(stale)


# 偏差聚类的最小同簇样本数（用户需求：同类情境重复验证 ≥3 次 → 验证级候选）
_DEVIATION_MIN_COUNT = 3


def aggregate_deviations(db: Session, *, days: int = 14) -> list[dict]:
    """行为偏差聚类（夜间反思①步取数，批 7 纯规则零 LLM）：behavior_events 按
    (kind, weekday, period) 分桶，同簇 count ≥3 出 {kind, weekday, period,
    count, avg_delta, samples}——喂给 REFLECT 作证据素材（规则文本由 LLM 归纳，
    聚类只管计数）。窗口 14 天与 execution_rhythm 同口径。"""
    from models import BehaviorEvent

    cutoff = datetime.now() - timedelta(days=days)
    rows = db.scalars(select(BehaviorEvent).where(
        BehaviorEvent.created_at >= cutoff)).all()
    buckets: dict[tuple, list[BehaviorEvent]] = {}
    for ev in rows:
        ctx = ev.context or {}
        key = (ev.kind, ctx.get("weekday"), ctx.get("period"))
        buckets.setdefault(key, []).append(ev)
    out: list[dict] = []
    for (kind, weekday, period), evs in sorted(buckets.items(),
                                               key=lambda p: -len(p[1])):
        if len(evs) < _DEVIATION_MIN_COUNT:
            continue
        deltas = [e.delta_min for e in evs if e.delta_min is not None]
        out.append({
            "kind": kind, "weekday": weekday, "period": period,
            "count": len(evs),
            "avg_delta": round(sum(deltas) / len(deltas)) if deltas else None,
            "samples": [
                {"prev_start": e.prev_start.isoformat() if e.prev_start else None,
                 "new_start": e.new_start.isoformat() if e.new_start else None,
                 "delta_min": e.delta_min,
                 "before_meeting": (e.context or {}).get("before_meeting"),
                 "after_meeting": (e.context or {}).get("after_meeting")}
                for e in evs[:3]],
        })
    return out


def list_memory_rules(db: Session) -> list[MemoryRule]:
    """全量规则，按注入序：user_manual 优先 → chat 次之 → confidence 降序 →
    updated_at 降序。chat（2026-10-22 批 3 对话写入的"用户明说"偏好）与
    user_manual 同是最高信任级，但手写仍最先；ai_reflect 殿后靠 confidence。"""
    return list(
        db.scalars(
            select(MemoryRule).order_by(
                MemoryRule.source != "user_manual",  # False(0) 排前 → user_manual 优先
                MemoryRule.source != "chat",  # 同上 → chat 次之
                MemoryRule.confidence.desc(),
                MemoryRule.updated_at.desc(),
            )
        )
    )


def get_memory_rule(db: Session, rule_id: str) -> MemoryRule | None:
    return db.get(MemoryRule, rule_id)


def create_memory_rule(db: Session, body: MemoryRuleCreate) -> MemoryRule:
    """用户手写入口（设置 → 记忆画像）：source 恒 user_manual、confidence 恒 1.0。
    满 120 条抛 ValueError（router 翻 422）——手写规则不挤占习惯学习官存量，让用户先清理。"""
    if len(list_memory_rules(db)) >= MEMORY_RULES_MAX:
        raise ValueError(f"记忆规则已达上限 {MEMORY_RULES_MAX} 条，请先删除部分规则")
    row = MemoryRule(
        category=body.category,
        content=body.content,
        slot=slot_of(body.category, body.content),
        source="user_manual",
        confidence=1.0,
        evidence=body.evidence,
        last_confirmed_at=datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("新增手写记忆规则 [%s] %s", row.category, row.id)
    return row


def update_memory_rule(db: Session, rule_id: str, patch: MemoryRuleUpdate) -> MemoryRule | None:
    """部分更新（exclude_unset：键不出现 = 不动）；返回 None = 规则不存在。

    2026-11-03 记忆重构升级语义：用户手动编辑 AI 推导行（source != user_manual）
    时自动升 source=user_manual / confidence=1.0 / last_confirmed_at=now——亲笔
    改动即最高裁决（用户需求：AI 无权再覆盖，衰减亦豁免）；category/content
    变化时 slot 重算。"""
    row = get_memory_rule(db, rule_id)
    if row is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return row
    for key, value in data.items():
        setattr(row, key, value)
    promoted = row.source != "user_manual"
    if promoted:  # AI 推导行被亲笔编辑 → 升最高裁决级（用户需求：AI 无权再覆盖）
        row.source = "user_manual"
        row.confidence = 1.0
        row.last_confirmed_at = datetime.now()
    row.slot = slot_of(row.category, row.content)
    db.commit()
    db.refresh(row)
    logger.info("更新记忆规则 %s：字段 %s%s", rule_id, sorted(data),
                "（AI 推导行手动编辑已升 user_manual）" if promoted else "")
    return row


def delete_memory_rule(db: Session, rule_id: str, *, dismiss: bool = False) -> bool:
    """硬删（非业务数据无软删价值）；返回 False = 规则不存在。

    dismiss=True（2026-11-03 记忆重构）= 用户主动删除：落 memory_dismissals 纠偏
    账（原文 + 归一化指纹），此后反思链/仲裁者的同义候选被预过滤——「用户删了
    就别再推」。反思链 replace/merge 的先删不传 True（那是合并非纠偏）。"""
    row = get_memory_rule(db, rule_id)
    if row is None:
        return False
    if dismiss:
        from services.tasks import normalize_title

        db.add(MemoryDismissal(
            fingerprint=normalize_title(row.content)[:200],
            content=row.content,
            category=row.category,
            source=row.source,
        ))
    db.delete(row)
    db.commit()
    logger.info("删除记忆规则 %s%s", rule_id, "（已记纠偏账）" if dismiss else "")
    return True


def upsert_memory_rule(
    db: Session,
    *,
    category: str,
    content: str,
    confidence: float,
    evidence: str | None = None,
    source: str = "ai_reflect",
    last_confirmed_at: datetime | None = None,
) -> MemoryRule:
    """习惯学习官的落库入口（手写区调用）：同 category + content 完全一致视为同一
    规则（更新 confidence/evidence/last_confirmed_at，即"印证"），否则新建；新建前
    若已满上限，先淘汰 confidence 最低（并列取 last_confirmed_at 最旧、再取
    updated_at 最旧）一行——新增一条换一条（PRD §4.3 总量控制）。

    注意：ai_reflect 来源的规则必须带 evidence（"无出处的画像不落库"），本函数
    不强拦（信任手写区在归纳链内已丢弃无证据候选），落库前由调用方保证。
    """
    existing = db.scalar(
        select(MemoryRule).where(
            MemoryRule.category == category, MemoryRule.content == content
        )
    )
    if existing is not None:
        existing.confidence = confidence
        existing.evidence = evidence
        existing.last_confirmed_at = last_confirmed_at or datetime.now()
        existing.slot = existing.slot or slot_of(category, content)  # 旧行 NULL 顺带回填
        db.commit()
        db.refresh(existing)
        logger.info("记忆规则印证更新 [%s] confidence=%.2f %s", category, confidence, existing.id)
        return existing

    count = len(list_memory_rules(db))
    if count >= MEMORY_RULES_MAX:
        victim = list_memory_rules(db)[-1]  # 注入序末位 = 最低置信且最旧
        logger.info("记忆规则满 %d 条，淘汰 [%s] %s", MEMORY_RULES_MAX, victim.category, victim.id)
        db.delete(victim)
    row = MemoryRule(
        category=category,
        content=content,
        slot=slot_of(category, content),
        source=source,
        confidence=confidence,
        evidence=evidence,
        last_confirmed_at=last_confirmed_at or datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("归纳落库记忆规则 [%s] confidence=%.2f %s", category, confidence, row.id)
    return row


# 时间衰减参数（PRD §4.3：超 N 天默认 90 未印证 → 学习时降 confidence，归零删除）
_DECAY_STALE_DAYS = 90
_DECAY_STEP = 0.1
# 降至该值以下视为归零（浮点尾差容差），删除陈年规则
_DECAY_FLOOR = 0.05


def decay_stale_rules(db: Session, *, stale_days: int = _DECAY_STALE_DAYS,
                      step: float = _DECAY_STEP) -> tuple[int, int]:
    """时间衰减（PRD §4.3，夜间反思链每晚清算）：ai_reflect 规则 last_confirmed_at
    早于 stale_days 天前 → confidence 降 step；降至 ≤_DECAY_FLOOR 视为归零删除
    （白盒界面可见 = confidence 变低 / 行消失）。user_manual（用户手写恒 1.0，
    机器不降权）与 last_confirmed_at 为空的行不衰减。返回 (降权条数, 删除条数)。

    衰减后再被归纳印证（upsert 同 category+content）会重设 confidence 并刷新
    last_confirmed_at——衰减周期重新起算，语义正确。"""
    cutoff = datetime.now() - timedelta(days=stale_days)
    decayed = removed = 0
    for row in list_memory_rules(db):
        if (row.source != "ai_reflect" or row.last_confirmed_at is None
                or row.last_confirmed_at >= cutoff):
            continue
        new_conf = round(row.confidence - step, 2)
        if new_conf <= _DECAY_FLOOR:
            db.delete(row)
            removed += 1
            logger.info("记忆规则归零清退 [%s] %s", row.category, row.id)
        else:
            row.confidence = new_conf
            decayed += 1
    db.commit()
    return decayed, removed


# —— 建议日志 ——————————————————————————————————————————————


def create_proposal(db: Session, *, kind: str, payload: dict) -> Proposal:
    """落一条待处理建议（outcome 恒空起步）。payload 由壳组装（draft_plan / checklist）。"""
    row = Proposal(kind=kind, payload=payload)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_proposals(
    db: Session,
    *,
    day: date | None = None,
    unresolved_only: bool = False,
    limit: int = 50,
) -> list[Proposal]:
    """建议查询：day 给定 = 只取该日创建（习惯学习官取当日行为信号的口径）；创建序倒序
    （最新建议在前，前端面板/晨间确认消费）。"""
    stmt = select(Proposal).order_by(Proposal.created_at.desc()).limit(limit)
    if day is not None:
        lo = datetime.combine(day, datetime.min.time())
        stmt = stmt.where(Proposal.created_at >= lo, Proposal.created_at < lo + timedelta(days=1))
    if unresolved_only:
        stmt = stmt.where(Proposal.outcome.is_(None))
    return list(db.scalars(stmt))


def pending_prep_by_task(db: Session) -> dict[str, str]:
    """未决会前准备卡展开 {task_id: proposal_id}（claim_due 会前重推标记消费）。
    新卡优先（创建序倒序前 10 条）；同任务多卡取最新一条——旧卡已决/过期后
    新卡接管重推标记。卡被采纳/拒绝/过期（outcome 非空）即整体退出映射，
    提醒回归普通文案。"""
    rows = list(db.scalars(
        select(Proposal)
        .where(Proposal.kind == "prep", Proposal.outcome.is_(None))
        .order_by(Proposal.created_at.desc())
        .limit(10)
    ))
    mapping: dict[str, str] = {}
    for p in rows:
        for m in (p.payload or {}).get("meetings") or []:
            tid = m.get("task_id")
            if isinstance(tid, str) and tid not in mapping:
                mapping[tid] = p.id
    return mapping


# modified 偏离比对的字段域（优化批 4）：schedule 条目与 inbox 条目的公共可编辑键
_DIFF_FIELDS = ("title", "start", "start_time", "duration", "duration_mins",
                "priority", "notes")


def _modified_diff(applied: list[dict] | None, originals: list[dict],
                   indexes: list[int] | None = None) -> list[dict]:
    """编辑后条目 vs payload 原快照的字段级偏离（优化批 4 modified 回流）。
    indexes 给定时按序号取条目（条目级结算），否则整表对位（整卡结算）。
    返回 [{idx, title, changes: {字段: [原值, 新值]}}]——只记有变化的条目；
    applied 缺失/长度不符/无变化返回空表（不落空键）。"""
    if not applied:
        return []
    out: list[dict] = []
    slots = indexes if indexes is not None else list(range(len(originals)))
    for pos, entry in zip(slots, applied):
        if not isinstance(entry, dict):
            continue
        if not isinstance(pos, int) or not (0 <= pos < len(originals)):
            continue
        old = originals[pos] if isinstance(originals[pos], dict) else {}
        changes = {k: [old.get(k), entry[k]] for k in _DIFF_FIELDS
                   if k in entry and entry[k] != old.get(k)}
        if changes:
            out.append({"idx": pos, "title": entry.get("title") or old.get("title"),
                        "changes": changes})
    return out


def record_proposal_outcome(
    db: Session, proposal_id: str, outcome: str, reason: str | None = None,
    applied_entries: list[dict] | None = None,
) -> Proposal | None:
    """回写处理结果（采纳/修改后采纳/拒绝/忽略）：outcome + resolved_at；reason
    （拒绝理由，习惯学习信号）并入 payload.reject_reason——JSON 列整体替换 setattr，
    禁止就地改（同 McpServer.env 惯例）。幂等：重复回写直接覆盖，旧拒绝理由随
    覆盖清理（不带理由的 outcome 不残留脏数据）。

    modified 回流（2026-11-01 优化批 4）：outcome=modified 且带 applied_entries
    时，与 payload 原快照（draft_plan / items）比对生成 modified_diff **并列新键**
    （原快照不回写——既有拍板），反思链从「只见 outcome 分布」升级为「看得见
    用户实际改成了什么」（改时间=时段偏好、改时长=估时修正，最强学习信号）。"""
    row = db.get(Proposal, proposal_id)
    if row is None:
        return None
    row.outcome = outcome
    row.resolved_at = datetime.now()
    payload = dict(row.payload or {})
    if reason is not None:
        payload["reject_reason"] = reason
    else:
        payload.pop("reject_reason", None)
    if outcome == "modified" and applied_entries:
        originals = (payload.get("draft_plan") if row.kind == "schedule"
                     else payload.get("items")) or []
        diff = _modified_diff(applied_entries, originals)
        if diff:
            payload["modified_diff"] = diff
    row.payload = payload
    db.commit()
    db.refresh(row)
    return row


def settle_proposal_items(
    db: Session, proposal_id: str, indexes: list[int], outcome: str,
    applied_entries: list[dict] | None = None,
) -> tuple[Proposal, dict] | None:
    """条目级部分结算（2026-10-24 数据源页单条操作批）：从待处理卡中移除指定
    条目（payload 收缩），剩余条目继续待审；全部清空才回写 outcome 结束整卡。
    返回 (行, 被结算条目的 mini payload)——路由层用它调 on_proposal_settled，
    使拒绝只静默被拒条目的指纹（同卡其余条目不受牵连；整卡拒绝牵连全部是旧口径）。
    错误用 ValueError 携带码：行不存在 → None；已决或非卡形态 → "settled"；
    序号越界/空 → "index"。evidence.idx 与 draft_plan 序对齐是溯源契约，收缩后
    同步重排；mini 的 evidence 保留原 idx（钩子只读指纹不看 idx）。

    modified 回流（2026-11-01 优化批 4）：条目级 modified 且带 applied_entries
    时，偏离记入 payload.item_diffs（卡未清空的暂存形态——整卡结算后反思链
    经 payload 一并读到；清空单条卡则同时落 modified_diff 常规键）。

    kind 扩展（2026-11-11 确认卡三件套统一批）：minutes 卡条目键同 inbox
    （items，条目 dict）；hardtask 卡条目键 steps（纯标题字符串数组——
    _modified_diff 对非 dict 条目自然跳过、evidence 重排仅 schedule、台账
    钩子只认 schedule/inbox，均不受字符串条目影响）。"""
    row = db.get(Proposal, proposal_id)
    if row is None:
        return None
    if row.outcome is not None or row.kind not in (
            "inbox", "schedule", "minutes", "hardtask"):
        raise ValueError("settled")
    items_key = ("draft_plan" if row.kind == "schedule"
                 else "steps" if row.kind == "hardtask" else "items")
    payload = dict(row.payload or {})
    items = list(payload.get(items_key) or [])
    drop = set(indexes)
    if not drop or any(i < 0 or i >= len(items) for i in drop):
        raise ValueError("index")
    settled_items = [items[i] for i in sorted(drop)]
    payload[items_key] = [it for i, it in enumerate(items) if i not in drop]
    mini: dict = {items_key: settled_items}
    if row.kind == "schedule":
        kept, mini_evs = [], []
        for ev in payload.get("evidence") or []:
            (mini_evs if ev.get("idx") in drop else kept).append(ev)
        for new_idx, ev in enumerate(kept):
            ev["idx"] = new_idx
        payload["evidence"] = kept
        mini["evidence"] = mini_evs
    if outcome == "modified" and applied_entries:
        diff = _modified_diff(applied_entries, items, indexes)
        if diff:
            payload.setdefault("item_diffs", []).extend(diff)
            if not payload[items_key]:
                payload["modified_diff"] = payload.pop("item_diffs")
    row.payload = payload  # JSON 列整体替换 setattr（record_proposal_outcome 惯例）
    if not payload[items_key]:
        row.outcome = outcome
        row.resolved_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row, mini


def get_proposal(db: Session, proposal_id: str) -> Proposal | None:
    """单条建议（active_pending 的锚点行读取）。"""
    return db.get(Proposal, proposal_id)


def expire_stale_proposals(db: Session, *, before: datetime,
                           ingest: bool | None = None) -> int:
    """超时未决建议批量置 expired（夜间整理收尾调用）：僵尸待处理行会稀释采纳率
    统计的分母。返回过期行数。

    ingest 口径（2026-10-23 数据源页增）：None = 全量（原行为）；True = 仅采集
    来源卡（payload.from_ingest——数据源页审阅队列留 7 天慢慢整理）；False = 排除
    采集卡。payload JSON 判定在 Python 侧过滤（未决行量级几十，不值得 SQLite
    JSON 谓词）。"""
    rows = list(
        db.scalars(
            select(Proposal).where(
                Proposal.outcome.is_(None), Proposal.created_at < before
            )
        )
    )
    now = datetime.now()
    n = 0
    for row in rows:
        if ingest is not None and bool((row.payload or {}).get("from_ingest")) != ingest:
            continue
        row.outcome = "expired"
        row.resolved_at = now
        n += 1
    db.commit()
    if n:
        logger.info("过期未决建议 %d 条已置 expired（ingest=%s）", n, ingest)
    return n


# —— 会话消息 ——————————————————————————————————————————————


def append_message(db: Session, *, role: str, content: str, proposal_id: str | None = None,
                   source: str | None = None) -> Message:
    """追加一条会话消息（user / assistant）。source：消息来源标记（None=桌面端 /
    "feishu"|"dingtalk"=远程渠道，2026-10-23 聊天通讯增）——仅作展示层徽标，
    不参与上下文组装（统一上下文决策）。"""
    row = Message(role=role, content=content, proposal_id=proposal_id, source=source)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_messages(db: Session, *, limit: int = 200,
                  after: datetime | None = None) -> list[Message]:
    """最近 limit 条消息，时间正序返回（面板启动加载口径：倒序取够再反转）。
    after（2026-11-04 通知分轨配套）：只返回 created_at 晚于位点的消息——前端
    未读位点恢复用（「上次见面后到达的 assistant 消息」计数）。"""
    stmt = select(Message)
    if after is not None:
        stmt = stmt.where(Message.created_at > after)
    rows = list(
        db.scalars(stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))
    )
    rows.reverse()
    return rows


# —— 对话信号节选（反思链①步对话学习，2026-10-20 批 4）———————————————————

# 纯应答/确认语（零画像价值——问询确认走 proposals 裁决链，不在这里重复学）；
# 2026-10-21 复查批 B 扩词表：嗯+/哦+/哈+ 叠字（"嗯嗯嗯嗯"恰过长度闸）、
# 语气尾（"好的呀""行吧""ok了""收到了"）——残余漏网属可接受噪声，REFLECT
# 步还会过滤
_CHAT_ACK_RE = re.compile(
    r"^(好的?[呀啊吧哈]?|嗯+|哦+|哈+|ok(了)?|行(吧|嘞)?|收到[了啦]?|谢谢[啦呀]?|不了|可以[呀啊吧]?)[!！。.\s]*$",
    re.IGNORECASE)
_CHAT_EXCERPT_MIN_CHARS = 4   # 太短无信号
_CHAT_EXCERPT_MAX_CHARS = 60  # 每条裁断（提示词契约 evidence ≤80 留余量）
_CHAT_EXCERPT_LIMIT = 20
_CHAT_DEDUP_PREFIX = 12       # 前 12 字相同视为同话题近似重复


def day_user_excerpts(db: Session, *, day: date,
                      limit: int = _CHAT_EXCERPT_LIMIT) -> list[str]:
    """当日 user 角色消息节选（反思链对话学习的输入信号）。

    四道闸：len≥4（"好""嗯"级太短无信号）/ 纯应答正则剔除 / 近似去重
    （前 12 字相同只留首条——同话题追问与重复发送）/ 上限 limit 条、每条
    裁 60 字。时间正序返回（信号按时间线读更准）。"""
    day_start = datetime(day.year, day.month, day.day)
    rows = db.scalars(select(Message).where(
        Message.role == "user",
        Message.created_at >= day_start,
        Message.created_at < day_start + timedelta(days=1),
    ).order_by(Message.created_at.asc(), Message.id.asc()))
    excerpts: list[str] = []
    seen_prefixes: set[str] = set()
    for row in rows:
        text = (row.content or "").strip()
        if len(text) < _CHAT_EXCERPT_MIN_CHARS or _CHAT_ACK_RE.match(text):
            continue
        key = text[:_CHAT_DEDUP_PREFIX]
        if key in seen_prefixes:
            continue
        seen_prefixes.add(key)
        excerpts.append(text[:_CHAT_EXCERPT_MAX_CHARS])
        if len(excerpts) >= limit:
            break
    return excerpts


# —— 对话滚动上下文（2026-10-20 增）————————————————————————————————


def get_chat_state(db: Session) -> ChatState:
    """对话状态单例行（id 恒 1）：不存在则建（get_or_create 语义）。"""
    row = db.get(ChatState, 1)
    if row is None:
        row = ChatState(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def chat_context(db: Session, *, recent_k: int = CHAT_RECENT_K,
                 exclude: str | None = None) -> tuple[str | None, list[Message]]:
    """近期上下文二元组（summary | None, 最近 recent_k 条正序）——qa/smalltalk
    等自然语言节点注入对话历史用。

    exclude = 本轮用户原话：壳 chat 端点在图运行前已 append_message 落库，
    节点注入时若不剔除会与 user 载荷重复注入——从尾部剔除一条 content 相等
    的 user 消息（只剔一条，防御历史里恰好出现同文句）。"""
    summary = get_chat_state(db).summary or None
    rows = list_messages(db, limit=recent_k + 1)
    if exclude is not None:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].role == "user" and rows[i].content == exclude:
                del rows[i]
                break
    return summary, rows[-recent_k:]


def chat_recent_k(db: Session) -> int:
    """近期原文窗口 K 的档位自适应（2026-10-26）：读激活模型 context_length 映射
    ——小窗口收窄让位摘要（省预算），大窗口让"活记忆"更长（原文细节比摘要更能
    支撑多轮引用）。无激活模型回落 CHAT_RECENT_K。只影响 qa/smalltalk 注入窗；
    压缩保留窗仍恒 CHAT_RECENT_K（游标与切片口径单源，两窗少量重叠无害）。"""
    from services.model_providers import get_active_model  # 函数内 import（services 惯例）

    model = get_active_model(db)
    if model is None:
        return CHAT_RECENT_K
    ctx_len = model.context_length
    if ctx_len <= 16384:
        return 6
    if ctx_len <= 65536:
        return 10
    if ctx_len <= 262144:
        return 12
    return 16


class CompactPartialError(Exception):
    """压缩块级中断（2026-10-26 增）：done = 已成功并入摘要的消息条数（游标已
    推进，部分成果保留）。定义在 services 层——routers 与 agents 都能无环导入。
    自动路径的壳照旧全吞（下轮从游标续压）；手动 /compact 端点翻译为 partial。"""

    def __init__(self, done: int):
        super().__init__(f"compact partial: {done} merged")
        self.done = done


def pending_compaction_slice(db: Session) -> list[Message]:
    """未摘要切片 [summarized_count : n-K]（保留窗 K 条继续作原文）——compaction_due
    阈值判定与 /compact 强制压缩共用同一切片口径；无待摘要消息返回空表。窗口硬
    上限 CHAT_MAX_WINDOW 之外的消息不参与（起点不早于 n - 上限）。"""
    rows = list_messages(db, limit=CHAT_MAX_WINDOW)
    n = len(rows)
    lo = max(get_chat_state(db).summarized_count, n - CHAT_MAX_WINDOW)
    return rows[lo:max(n - CHAT_RECENT_K, 0)]


def compaction_due(db: Session) -> list[Message] | None:
    """判定是否需要压缩（None = 不需要，零 LLM）。双阈值任一命中即触发
    （2026-10-26 增字符阈值）：

    - 未摘要切片条数 > CHAT_COMPRESS_BUFFER（原条数口径的等价变形：未摘要数
      = 切片 + K，阈值 K + 缓冲 ⟺ 切片 > 缓冲）——管短消息高频对话的节奏；
    - 未摘要积压字符 > CHAT_COMPRESS_CHARS——管长消息低频对话（几条万字长文
      该压了，不能等凑满条数）。

    待压缩片段经 pending_compaction_slice 取。count 游标前进式：压缩过的消息
    不重复压缩。

    count-first 快路径（2026-11-01 优化批 3）：绝大多数轮次远未达阈值——
    两条标量聚合（COUNT + 窗口内 LENGTH 求和，list_messages 同序 (created_at,
    id) 行号）先判，未达阈值零整窗加载（旧版每轮轮末 list_messages(500)
    全量拉回只为算切片）；达阈值才走 pending_compaction_slice 权威切片。"""
    n = db.scalar(select(func.count()).select_from(Message)) or 0
    summarized = get_chat_state(db).summarized_count
    hi = max(n - CHAT_RECENT_K, 0)
    lo = max(summarized, n - CHAT_MAX_WINDOW)
    if hi <= lo:
        return None
    rn = func.row_number().over(
        order_by=[Message.created_at, Message.id]).label("rn")
    sub = select(Message.content, rn).subquery()
    chars = db.scalar(
        select(func.coalesce(func.sum(func.length(sub.c.content)), 0))
        .where(sub.c.rn > lo, sub.c.rn <= hi)) or 0
    if hi - lo <= CHAT_COMPRESS_BUFFER and chars <= CHAT_COMPRESS_CHARS:
        return None
    return pending_compaction_slice(db)


def save_chat_summary(db: Session, *, summary: str, count: int) -> None:
    """写入滚动摘要并推进压缩游标（compact_chat_history 专用）。"""
    row = get_chat_state(db)
    row.summary = summary
    row.summarized_count = count
    db.commit()


def clear_chat_session(db: Session) -> None:
    """清空对话会话（/clear 命令，2026-10-21）：删全部消息 + 重置滚动摘要与压缩
    游标（ChatState docstring 预留的语义）——上下文注入（近期原文 + 摘要）从零
    起步。建议行（proposals）不清：它们是待办建议不是对话内容。"""
    db.query(Message).delete()
    state = db.get(ChatState, 1)
    if state is not None:
        state.summary = None
        state.summarized_count = 0
    db.commit()


# 问询归属子图判定域（Track B 批 2）：payload.kind → origin 派生用，
# 与 agents/nodes.QUESTION_KINDS 全集对齐——排期六类归 scheduler、幕僚两类
# 归 strategist，未知 kind 派生 None（消费侧回落 LLM 路由兜底）
_SCHEDULER_PENDING_KINDS = frozenset(
    {"conflict", "similar", "no_time", "past", "frequency", "no_slot"})
_STRATEGIST_PENDING_KINDS = frozenset({"strategist_clarify", "memory"})


def _anchor_proposal(db: Session):
    """最后一条带建议行锚点的 assistant 消息对应的行对象（2026-10-23 增，
    active_pending / active_draft / active_delete 三兄弟共用的锚点判定）：
    倒序扫近 50 条消息，无锚点的轮次（闲聊/QA）跳过不影响锚定；行对象物理
    缺失继续往前找；找到即返回（是否活跃未决由调用方判 kind / outcome）。"""
    for m in reversed(list_messages(db, limit=50)):
        if m.role != "assistant" or not m.proposal_id:
            continue
        row = get_proposal(db, m.proposal_id)
        if row is None:
            continue  # 防御性行缺失：继续往前找
        return row
    return None


def active_pending(db: Session) -> dict | None:
    """当前活跃的排期问询快照（壳 build_initial_state 每轮注入 state["pending"]，
    2026-10-17 增）。

    判定（消息锚点）：倒序找最后一条带 proposal_id 的 assistant 消息——其行
    未决（outcome 为空）且 kind=question 即活跃；中间穿插的闲聊/QA 轮次（消息
    无锚点）不影响；下一个带建议行的轮次（draft/applied/新问询）把锚点顶替
    过去，问询自然失活；隔夜由 expire_stale_proposals 兜底置 expired。

    返回快照 {proposal_id, kind, entries, suggestion, similar_to, date_hint,
    original_input, ask_text, origin}——payload 原样透出 + origin 派生键
    （Track B 批 2 增：按 payload.kind 判问询归属子图，center_router 消费侧
    确定性分流，旧表行天然全覆盖零迁移），planner 的 resolution 判定与
    ROUTER 问询前缀都读它。"""
    row = _anchor_proposal(db)
    if row is None or row.outcome is not None or row.kind != "question":
        return None  # 最后锚点行不是活跃问询（已被新卡顶替 / 已决）→ 无 pending
    p = dict(row.payload or {})
    kind = p.get("kind")
    origin = ("scheduler" if kind in _SCHEDULER_PENDING_KINDS
              else "strategist" if kind in _STRATEGIST_PENDING_KINDS
              else None)
    return {
        "proposal_id": row.id,
        "kind": kind,
        "entries": [e for e in p.get("entries") or [] if isinstance(e, dict)],
        "suggestion": p.get("suggestion"),
        "similar_to": p.get("similar_to"),
        "date_hint": p.get("date_hint"),
        "original_input": p.get("original_input"),
        "ask_text": p.get("ask_text"),
        "scenario_type": p.get("scenario_type"),
        "rule": p.get("rule") if isinstance(p.get("rule"), dict) else None,
        "origin": origin,
    }


def active_draft(db: Session) -> dict | None:
    """当前活跃的排期草案卡快照（2026-10-23 增，聊天通讯渠道配套）：判定同
    active_pending（消息锚点），kind=schedule 未决行——applied 直落轮的行
    落库即置 accepted，不会误判。远程渠道无卡片按钮，用户回「确认/算了」由
    图头 confirm_shortcut 短路消费（桌面对话确认同享）。返回
    {proposal_id, draft_plan}；草案条目空（防御）返回 None。"""
    row = _anchor_proposal(db)
    if row is None or row.outcome is not None or row.kind != "schedule":
        return None
    entries = [e for e in (row.payload or {}).get("draft_plan") or []
               if isinstance(e, dict) and e.get("title")]
    if not entries:
        return None
    return {"proposal_id": row.id, "draft_plan": entries}


def active_delete(db: Session) -> dict | None:
    """当前活跃的删除确认卡快照（同 active_draft 批次增）：kind=delete 未决
    行。返回 {proposal_id, tasks}——tasks 元素 {task_id, title}（delete_confirmer
    契约，重复系列不进卡）。"""
    row = _anchor_proposal(db)
    if row is None or row.outcome is not None or row.kind != "delete":
        return None
    tasks = [t for t in (row.payload or {}).get("tasks") or []
             if isinstance(t, dict) and t.get("task_id")]
    if not tasks:
        return None
    return {"proposal_id": row.id, "tasks": tasks}


# —— 信任渐进统计（PRD §10：样本 ≥10 且采纳率 ≥60% 解锁主动推送；<30% 回退） ——


def adoption_stats(db: Session, *, window_days: int = 14) -> dict:
    """近 window_days 天已决建议的采纳率。口径：total = outcome ∈ {accepted,
    modified, rejected, ignored}（expired 是用户从未看见的建议，分子分母双侧剔除）；
    rate = (accepted + modified) / total，total=0 时 0.0。"""
    lo = datetime.now() - timedelta(days=window_days)
    rows = list(
        db.scalars(
            select(Proposal.outcome).where(
                Proposal.created_at >= lo, Proposal.outcome.is_not(None)
            )
        )
    )
    outcomes = [o for o in rows if o != "expired"]
    total = len(outcomes)
    hits = sum(1 for o in outcomes if o in ("accepted", "modified"))
    return {"total": total, "accepted_modified": hits, "rate": hits / total if total else 0.0}


def recent_reject_reasons(db: Session, *, days: int = 7, limit: int = 5) -> list[dict]:
    """近 days 天拒绝理由摘录（2026-11-01 优化批 6 抽公共——intake 排期链与
    qa 问答共用）：用户明确拒绝过的同类建议时间/理由，出建议时当场避开。
    [{"titles": [≤2], "reason": ≤60 字}]；无理由的拒绝行跳过。"""
    lo = datetime.now() - timedelta(days=days)
    out: list[dict] = []
    for p in db.scalars(select(Proposal).where(
            Proposal.outcome == "rejected", Proposal.created_at >= lo).limit(limit * 2)):
        reason = (p.payload or {}).get("reject_reason")
        if not isinstance(reason, str) or not reason.strip():
            continue
        out.append({"titles": proposal_titles(p.kind, p.payload or {})[:2],
                    "reason": reason.strip()[:60]})
        if len(out) >= limit:
            break
    return out


def trust_tier(db: Session) -> str:
    """信任渐进档位（PRD §10.3，2026-11-01 优化批 5 起全局消费——此前
    adoption_stats 只统计零消费方）：近 14 天已决建议 ≥10 样本且采纳率 ≥60% →
    "enhanced"（主动建议增强档——现有主动性全开，日志可见）；<30% → "low"
    （低介入档：生日前瞻 / 会前准备清单等主动推送物跳过，被动应答与既有
    简报恒保留——低介入≠静默）；其余与样本不足 → "normal"（不惩罚新用户）。"""
    stats = adoption_stats(db)
    if stats["total"] < 10:
        return "normal"
    if stats["rate"] >= 0.6:
        return "enhanced"
    if stats["rate"] < 0.3:
        return "low"
    return "normal"


def _proposal_source_keys(kind: str, payload: dict) -> set[str]:
    """从建议卡 payload 提取其覆盖的源键集合（纯函数）：schedule 卡读 evidence
    溯源数组、inbox 卡逐条读 source_ref——M1-①/② 冻结的 payload 契约。旧卡无
    这些键返回空集（不参与按源统计）。"""
    keys: set[str] = set()
    if kind == "schedule":
        for e in payload.get("evidence") or []:
            if e.get("source_key"):
                keys.add(e["source_key"])
    elif kind == "inbox":
        for it in payload.get("items") or []:
            ref = it.get("source_ref") or {}
            if ref.get("source_key"):
                keys.add(ref["source_key"])
    return keys


def adoption_stats_by_source(db: Session, *, window_days: int = 14) -> dict:
    """按源结算表（M2-③，采集信任联动的数据源）：窗口内 kind∈{schedule, inbox} 已决
    建议按源聚合——payload 在 Python 侧拆源键（JSON 列内容下 SQL 聚合不可靠，也保
    持与 payload 契约单一实现）。expired 双侧剔除（同 adoption_stats 口径）。

    返回 {total, accepted_modified, rate, eligible, by_source: [{source_key, total,
    accepted_modified, rate}]}：前四项为采集建议全局口径，eligible = 开启自动收录
    资格（total≥10 且 rate≥0.6，update_settings 校验与前端摘要共用同一数据源）。
    """
    lo = datetime.now() - timedelta(days=window_days)
    rows = list(db.execute(
        select(Proposal.kind, Proposal.outcome, Proposal.payload).where(
            Proposal.created_at >= lo, Proposal.outcome.is_not(None),
            Proposal.kind.in_(["schedule", "inbox"]),
        )
    ))
    by: dict[str, dict] = {}
    total = hits = 0
    for kind, outcome, payload in rows:
        if outcome == "expired":
            continue
        total += 1
        hit = outcome in ("accepted", "modified")
        if hit:
            hits += 1
        # 无源键（旧卡/占位）只进全局口径，不进按源表
        for key in _proposal_source_keys(kind, payload or {}):
            agg = by.setdefault(key, {"source_key": key, "total": 0, "accepted_modified": 0})
            agg["total"] += 1
            if hit:
                agg["accepted_modified"] += 1
    for agg in by.values():
        agg["rate"] = agg["accepted_modified"] / agg["total"] if agg["total"] else 0.0
    rate = hits / total if total else 0.0
    return {
        "total": total,
        "accepted_modified": hits,
        "rate": rate,
        "eligible": total >= 10 and rate >= 0.6,
        "by_source": sorted(by.values(), key=lambda a: -a["total"]),
    }


# —— 跨天重复防线（2026-10-15 增）——————————————————————————————————


def dedup_context(db: Session, *, settled_days: int = 14, ahead_days: int = 14) -> list[str]:
    """Reduce 语义去重对照集（docs/ai-ingest.md §五 模板 ② 的扩充版）：未排期待办 ∪
    未来 ahead_days 天已排期任务标题 ∪ 近 settled_days 天已决建议（accepted/modified/
    rejected）标题。

    指纹台账挡内容级重复（同一条目不重复进 LLM），本集挡同实异名——LLM 提炼出的
    标题与库内既有任务/昨日已决建议措辞不同但实指同一事（如"周五交报告" vs
    "提交周报"）时，Reduce 对照本集语义去重不再重复建议。"""
    titles = [t.title for t in list_tasks(db, unscheduled=True)]
    now = datetime.now()
    titles += [t.title for t in list_tasks(
        db, start=now, end=now + timedelta(days=ahead_days))]
    since = now - timedelta(days=settled_days)
    rows = db.scalars(select(Proposal).where(
        Proposal.kind.in_(["schedule", "inbox"]),
        Proposal.outcome.in_(["accepted", "modified", "rejected"]),
        Proposal.created_at >= since,
    ))
    for r in rows:
        titles += proposal_titles(r.kind, r.payload or {})
    return [t for t in titles if t]


def proposal_titles(kind: str, payload: dict) -> list[str]:
    """已决建议快照的标题集（schedule 卡读 draft_plan、inbox 卡读 items）。
    公开给 agents.reflection 复用（decisions 行的 titles 提取同一实现不漂移）。"""
    if kind == "schedule":
        items = payload.get("draft_plan") or []
    elif kind == "inbox":
        items = payload.get("items") or []
    else:
        return []
    return [d.get("title") for d in items if isinstance(d, dict) and d.get("title")]


# —— 冷启动种子（PRD §10 D0：表空时从 app_settings 自动推导） ——————————————


def _fmt_hm(minutes: int) -> str:
    """分钟数 → "HH:MM"（种子文案用）。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def seed_memory_rules_if_empty(db: Session) -> bool:
    """memory_rules 表空时从应用设置推导 3 条种子规则（source=user_manual、
    confidence=1.0）：午休时段（开启时）/ 每日工时 / 可视工作区间。幂等：非空
    即跳过。返回是否实际灌入。首次启用 AI 的引导填写（3-5 条）以此为底，剩余
    由用户在设置 → 记忆画像补充。"""
    if db.scalar(select(MemoryRule.id).limit(1)) is not None:
        return False
    settings = get_settings(db)
    rows = []
    if settings.lunch_enabled:
        rows.append(
            MemoryRule(
                category="作息规律",
                content=f"{_fmt_hm(settings.lunch_start_min)}-{_fmt_hm(settings.lunch_end_min)} 是午休时段，尽量不安排会议或高强度任务",
                source="user_manual",
                confidence=1.0,
                evidence="从应用设置的午休时段自动推导",
                last_confirmed_at=datetime.now(),
            )
        )
    rows.append(
        MemoryRule(
            category="作息规律",
            content=f"每日有效工作时长约 {settings.work_minutes_per_day // 60} 小时，排期总量以此为上限评估负荷",
            source="user_manual",
            confidence=1.0,
            evidence="从应用设置的每日工作时长自动推导",
            last_confirmed_at=datetime.now(),
        )
    )
    rows.append(
        MemoryRule(
            category="作息规律",
            content=f"日常工作时间段约 {_fmt_hm(settings.calendar_start_min)}-{_fmt_hm(settings.calendar_end_min)}，排期尽量落在该区间内",
            source="user_manual",
            confidence=1.0,
            evidence="从应用设置的日历可视区间自动推导",
            last_confirmed_at=datetime.now(),
        )
    )
    db.add_all(rows)
    db.commit()
    logger.info("记忆画像冷启动：从应用设置推导种子规则 %d 条", len(rows))
    return True
