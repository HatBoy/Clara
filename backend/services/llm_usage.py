"""AI 用量采集（2026-11-17 批 3）：单点写入 llm_usage_events。

防御纪律：任何遥测失败只 warning，绝不影响 LLM 调用主链；用独立 SessionLocal
（不占调用方请求会话）。成本按 LiteLLM 本地价格表估算（LITELLM_LOCAL_MODEL_COST_MAP
已开），未知模型 cost 置空、cost_known=False。
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session

from models import AiUsageEvent

logger = logging.getLogger(__name__)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _estimate_cost(model_name: str, prompt: int | None,
                   completion: int | None) -> tuple[float | None, bool]:
    if prompt is None or completion is None or not model_name:
        return None, False
    try:
        import litellm

        pc, cc = litellm.cost_per_token(
            model=model_name, prompt_tokens=prompt, completion_tokens=completion)
        return float(pc) + float(cc), True
    except Exception:  # noqa: BLE001——价格表没覆盖的模型/自定义中转
        return None, False


def record_usage(*, model_name: str, role: str = "main", feature: str | None = None,
                 usage: dict | None, latency_ms: int | None = None) -> None:
    """记录一次 LLM 调用（usage 为 provider 原始 dict 或 None）。"""
    try:
        from database import SessionLocal  # 调用时解析：测试替换 database.SessionLocal 即生效
        usage = usage or {}

        def _first(*keys: str):
            # 逐键显式判 None（二轮批 1）：or 链会把合法的 0 值 token 当缺失，
            # 回落到另一键得 None、usage_source 误标 unknown
            for k in keys:
                v = usage.get(k)
                if v is not None:
                    return v
            return None

        prompt = _to_int(_first("prompt_tokens", "input_tokens"))
        completion = _to_int(_first("completion_tokens", "output_tokens"))
        total = _to_int(_first("total_tokens"))
        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        known = total is not None
        cost, cost_known = _estimate_cost(model_name, prompt, completion)
        with SessionLocal() as db:
            db.add(AiUsageEvent(
                model_name=(model_name or "")[:100],
                role=role if role in ("main", "aux") else "main",
                feature=(feature or "")[:60] or None,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cost_usd=cost,
                cost_known=cost_known,
                usage_source="provider" if known else "unknown",
                latency_ms=latency_ms,
            ))
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("AI 用量记录失败（不影响调用）", exc_info=True)


def usage_summary(db, *, days: int = 30) -> dict:
    """按日/模型/功能聚合用量（2026-11-17 批 3；只读）。"""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    since = datetime.now() - timedelta(days=days)
    rows = list(db.scalars(
        select(AiUsageEvent).where(AiUsageEvent.created_at >= since)
        .order_by(AiUsageEvent.created_at.desc())))

    totals = {"calls": len(rows), "prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "cost_usd": 0.0,
              "unknown_usage_calls": 0, "unknown_cost_calls": 0}
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_feature: dict[str, dict] = {}

    def _bucket(mapping: dict, key: str) -> dict:
        return mapping.setdefault(key, {"calls": 0, "tokens": 0, "cost_usd": 0.0})

    for r in rows:
        tokens = r.total_tokens or 0
        totals["prompt_tokens"] += r.prompt_tokens or 0
        totals["completion_tokens"] += r.completion_tokens or 0
        totals["total_tokens"] += tokens
        if r.cost_usd is not None:
            totals["cost_usd"] += r.cost_usd
        if r.usage_source != "provider":
            totals["unknown_usage_calls"] += 1
        if not r.cost_known:
            totals["unknown_cost_calls"] += 1
        for mapping, key in (
            (by_day, r.created_at.date().isoformat()),
            (by_model, r.model_name),
            (by_feature, r.feature or "（未标注）"),
        ):
            b = _bucket(mapping, key)
            b["calls"] += 1
            b["tokens"] += tokens
            if r.cost_usd is not None:
                b["cost_usd"] += r.cost_usd

    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return {
        "days": days,
        **{k: (round(v, 6) if k == "cost_usd" else v) for k, v in totals.items()},
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_model": sorted(({"model_name": k, **v} for k, v in by_model.items()),
                           key=lambda x: -x["calls"]),
        "by_feature": sorted(({"feature": k, **v} for k, v in by_feature.items()),
                             key=lambda x: -x["calls"]),
        "recent": [{
            "created_at": r.created_at.isoformat(timespec="seconds"),
            "model_name": r.model_name, "feature": r.feature, "role": r.role,
            "total_tokens": r.total_tokens, "cost_usd": r.cost_usd,
            "cost_known": r.cost_known,
        } for r in rows[:10]],
    }


def prune_usage_events(db: Session, *, days: int = 90) -> int:
    """留存清理（夜间维护挂接，2026-11-17 二轮批 1）：面板最大窗口即 90 天，
    更早行物理删除——高频对话用户年数万行，usage_summary 全量拉行聚合会随之
    变慢。返回删除数。"""
    cutoff = datetime.now() - timedelta(days=days)
    n = db.execute(sa_delete(AiUsageEvent).where(
        AiUsageEvent.created_at < cutoff)).rowcount
    db.commit()
    return n or 0
