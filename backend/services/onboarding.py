"""首启入职访谈落库（2026-11-17 批 1）。

三步访谈的答案在这里一次写入：设置子集走既有 update_settings 校验链；规则走
create_memory_rule（source=user_manual、confidence=1.0）。完成时删除启动期
`seed_memory_rules_if_empty` 生成的种子规则（evidence 以「从应用设置的」开头），
避免种子与访谈规则重复；跳过则保留种子，只写 onboarded_at。
"""

import logging
from datetime import datetime

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.orm import Session

from models import MemoryRule
from schemas import MemoryRuleCreate, SettingsUpdate
from services.ai_data import _SEED_EVIDENCE_PREFIX, create_memory_rule
from services.settings import get_settings, update_settings

logger = logging.getLogger(__name__)


def complete_onboarding(db: Session, *, settings: SettingsUpdate | None,
                        rules: list[MemoryRuleCreate], skipped: bool) -> dict:
    """写设置 + 记忆规则 + onboarded_at。返回 {"row", "rules_created"}。"""
    if settings is not None and settings.model_fields_set:
        update_settings(db, settings)
    rules_created = 0
    if not skipped:
        db.execute(sa_delete(MemoryRule).where(
            MemoryRule.evidence.like(f"{_SEED_EVIDENCE_PREFIX}%")))
        db.commit()
        for body in rules[:5]:
            dup = db.scalar(select(MemoryRule.id).where(
                MemoryRule.category == body.category,
                MemoryRule.content == body.content))
            if dup is not None:
                continue
            create_memory_rule(db, body)
            rules_created += 1
    row = get_settings(db)
    row.onboarded_at = datetime.now()
    db.commit()
    db.refresh(row)
    logger.info("首启访谈完成：%s，写入规则 %d 条",
                "已跳过" if skipped else "已完成", rules_created)
    return {"row": row, "rules_created": rules_created}
