"""seed 数据灌入用例（seed_if_empty 标记与打包 no-op 闸）。

背景：2026-11-15 起 lifespan 不再调用 seed（任何模式启动零灌入，用户拍
板——启动重灌测试数据干扰程序测试；测试数据改由 backend/gen_test_data.py
手动生成）。本文件覆盖 seed_if_empty 自身语义（供 pytest seeded_db fixture
使用）；dev 再生闸门 refill_seed_proposals 已随调用点退役删除。
"""

from sqlalchemy import func, select

from conftest import TestingSessionLocal
from models import Project, Proposal
from services import seed
from services.seed import seed_if_empty
from services.seed_data import SEED_PROPOSALS


def _proposal_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Proposal))


def test_seed_proposals_carry_seed_marker(seeded_db):
    """seed_if_empty 灌的卡带 from_ingest + seed 双标记（前端按 seed 标记识别可再生的测试数据）。"""
    with TestingSessionLocal() as db:
        rows = db.scalars(select(Proposal)).all()
        assert len(rows) == len(SEED_PROPOSALS)
        assert all(
            r.payload.get("seed") is True and r.payload.get("from_ingest") is True
            for r in rows
        )


def test_seed_if_empty_noop_when_packaged(fresh_db, monkeypatch):
    """打包形态恒 no-op：测试数据不得灌给正式用户。"""
    monkeypatch.setattr(seed, "IS_PACKAGED", True)
    with TestingSessionLocal() as db:
        assert seed_if_empty(db) is False
        assert db.scalar(select(func.count()).select_from(Project)) == 0
        assert _proposal_count(db) == 0
