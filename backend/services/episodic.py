"""情节事实域服务（2026-11-03 记忆重构批 6，Tier 2）：带 TTL 的碎片事实。

与 memory_rules（Tier 1 永久规则）分流：「这周感冒」「下周出差」类临时状态
不挤占永久画像。检索走 FTS5 trigram 虚表（database._ensure_episodic_fts 建，
external-content + 触发器同步）+ BM25 + 时间加权；FTS 不可用（老 SQLite）或
短词（<3 字符无 3-gram）回落 LIKE——量级 <千行毫秒级，功能不残废。

注入边界（设计定稿）：只进自然语言节点（qa / smalltalk / strategist）与
统计域（洞察/报告/问答的复盘上下文，2026-11-08 起）作软参考，不进排期硬闸
（Tier 2 是情境参考不是铁律）。
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import _FTS_TRIGRAM_OK
from models import EpisodicFact

logger = logging.getLogger(__name__)

# 检索默认条数（注入「近期相关事实」段的上限）
SEARCH_K = 3
# 时间加权系数：bm25() + _RECENCY_WEIGHT * (now - created 天数)——近一周事实略优先
_RECENCY_WEIGHT = 0.05
# trigram 最小匹配长度（3 字符滑窗，短词回落 LIKE）
_MIN_FTS_CHARS = 3
# 2-gram 评分的功能字黑名单：含这些字的 gram（的了怎/怎么/什么类）泛化过强，
# 剔除后剩余 gram 命中 ≥1 即召回——「感冒了怎么办」→ 有效 gram {感冒}
_FUNCTION_CHARS = set("的了呢吧是在我有和与就也都还很吗怎什么办嗯哦啊这那要能可")
# 写入侧的 expires_days 钳制域（LLM 标注 1-90 天外收敛）
_MAX_EXPIRES_DAYS = 90


def upsert_fact(db: Session, *, topic: str, fact: str, source: str = "chat",
                confidence: float = 0.6, expires_days: int | None = None) -> EpisodicFact | None:
    """写入一条事实（同 topic+fact 完全一致跳过不堆行）。topic/fact 非空校验、
    expires_days 钳 [1, 90]（LLM 标注越界收敛）、content 截 200。返回行或 None
    （非法输入丢弃打日志——宁可不记不可记脏）。"""
    topic = (topic or "").strip()[:60]
    fact = (fact or "").strip()[:200]
    if not topic or not fact:
        return None
    expires_at = None
    if expires_days is not None:
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            days = None
        if days is not None:
            expires_at = datetime.now() + timedelta(
                days=max(1, min(days, _MAX_EXPIRES_DAYS)))
    existing = db.scalar(select(EpisodicFact).where(
        EpisodicFact.topic == topic, EpisodicFact.fact == fact))
    if existing is not None:  # 印证：刷新过期钟（再次提到 = 仍然有效）
        existing.expires_at = expires_at
        db.commit()
        return existing
    row = EpisodicFact(topic=topic, fact=fact, source=source,
                       confidence=confidence, expires_at=expires_at)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("[episodic] 落事实 [%s] %s（%s）", topic, row.id,
                f"{expires_at:%m-%d} 过期" if expires_at else "永久")
    return row


def search_facts(db: Session, query: str, *, k: int = SEARCH_K,
                 now: datetime | None = None) -> list[EpisodicFact]:
    """三层检索 + 过期过滤（确定性，零向量）：
    ① FTS5 trigram MATCH——查询恰为文档子串时 BM25 + 时间加权最优（引号包裹
       防语法注入）；
    ② 2-gram 滑窗 Python 评分——中文自由问句与事实原文词边界不重叠（「感冒了
       怎么办」vs「这周感冒需要多休息」，trigram 短语匹配零命中是常态而非例外），
       滑窗二元组不重叠计数 ≥2 视为相关，量级 <千行毫秒级；
    ③ LIKE 兜底（查询子串包含，含 %/_ 转义）。"""
    now = now or datetime.now()
    q = (query or "").strip()
    if not q:
        return []
    if _FTS_TRIGRAM_OK and len(q.replace(" ", "")) >= _MIN_FTS_CHARS:
        from sqlalchemy import text

        try:
            rows = db.execute(text(
                "SELECT f.id FROM episodic_facts f "
                "JOIN episodic_facts_fts m ON m.rowid = f.rowid "
                "WHERE episodic_facts_fts MATCH :q "
                "AND (f.expires_at IS NULL OR f.expires_at > :now) "
                "ORDER BY bm25(episodic_facts_fts) + :w * "
                "(julianday(:now) - julianday(f.created_at)) LIMIT :k"),
                {"q": f'"{q.replace(chr(34), "")}"', "now": now.isoformat(sep=" "),
                 "w": _RECENCY_WEIGHT, "k": k}).scalars().all()
            if rows:
                return [db.get(EpisodicFact, rid) for rid in rows]
        except Exception as exc:  # FTS 环境异常回落（功能不残废）
            logger.warning("[episodic] FTS 检索异常回落评分层：%s", exc)
    # ② 2-gram 评分 + ③ LIKE 兜底：候选池 = 未过期全量（limit 防御）
    candidates = db.scalars(select(EpisodicFact).where(
        EpisodicFact.expires_at.is_(None) | (EpisodicFact.expires_at > now),
    ).order_by(EpisodicFact.created_at.desc()).limit(500)).all()
    raw_grams = {q[i:i + 2] for i in range(len(q) - 1)} - {"", "  "}
    grams = {g for g in raw_grams if not (set(g) & _FUNCTION_CHARS)} or raw_grams
    scored: list[tuple[float, EpisodicFact]] = []
    for row in candidates:
        text_hit = q in row.fact or q in row.topic  # LIKE 兜底（整串包含）
        hay = f"{row.topic}{row.fact}"
        overlap = len({g for g in grams if g in hay})
        score = overlap + (5 if text_hit else 0)
        if overlap >= 1 or text_hit:
            scored.append((score, row))
    scored.sort(key=lambda p: -p[0])
    return [r for _, r in scored[:k]]


def fact_lines(rows: list[EpisodicFact]) -> list[str]:
    """事实行 → 注入文本行（「[主题] 正文（月底过期）」——过期提示让模型自然
    带时效判断）。"""
    out = []
    for r in rows:
        suffix = ""
        if r.expires_at is not None:
            suffix = f"（{r.expires_at:%m-%d} 前有效）"
        out.append(f"[{r.topic}] {r.fact}{suffix}")
    return out


def list_episodic_facts(db: Session, *, limit: int = 100) -> list[EpisodicFact]:
    """设置页事实清单（倒序）。"""
    return list(db.scalars(select(EpisodicFact).order_by(
        EpisodicFact.created_at.desc()).limit(limit)).all())


def delete_fact(db: Session, fact_id: str) -> bool:
    """手动删除单条事实（白盒裁决权；触发器同步清 FTS 索引）。"""
    row = db.get(EpisodicFact, fact_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("[episodic] 手动删除事实 %s", fact_id)
    return True


def expire_episodic_facts(db: Session, *, now: datetime | None = None) -> int:
    """过期事实物理清理（夜间反思⓪.5 维护步）：已过期行删除（触发器同步）。
    返回清理行数。"""
    now = now or datetime.now()
    stale = db.scalars(select(EpisodicFact).where(
        EpisodicFact.expires_at.is_not(None),
        EpisodicFact.expires_at <= now)).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
        logger.info("[episodic] 清理 %d 条过期事实", len(stale))
    return len(stale)


# 蒸馏阈值：同主题 ≥3 条未过期事实 → 蒸馏为 Tier 1 候选（反复出现的临时状态
# 是稳定习惯的强信号，批 7）
_DISTILL_MIN = 3


def distill_fact_candidates(db: Session, *, now: datetime | None = None) -> list[dict]:
    """事实蒸馏（夜间反思②步前置，批 7 纯规则组装 + LLM 仲裁裁决）：同 topic
    ≥3 条未过期事实 → Tier 1 候选（signal=behavior、confidence=0.6 验证级起点、
    content 拼接 top 事实摘要——规则文本的精炼由仲裁消解步顺带完成）。原事实行
    保留（有过期钟自然退化，与规则并存无害）。"""
    now = now or datetime.now()
    rows = db.scalars(select(EpisodicFact).where(
        EpisodicFact.expires_at.is_(None) | (EpisodicFact.expires_at > now),
    ).order_by(EpisodicFact.created_at.desc())).all()
    by_topic: dict[str, list[EpisodicFact]] = {}
    for r in rows:
        by_topic.setdefault(r.topic, []).append(r)
    candidates = []
    for topic, facts in by_topic.items():
        if len(facts) < _DISTILL_MIN:
            continue
        joined = "；".join(f.fact[:30] for f in facts[:3])
        candidates.append({
            "category": "其他",
            "content": f"{topic}相关事项反复出现：{joined}"[:100],
            "evidence": f"同主题事实 {len(facts)} 条（近段对话与复盘）"[:80],
            "confidence": 0.6,
            "signal": "behavior",
        })
    return candidates
