"""seed 建议卡 seed 标记与 dev 再生闸门（refill_seed_proposals）用例。

背景（2026-10-26）：建议卡是可消耗的测试数据，seed_if_empty 的 projects 空闸门
只认首启——消耗完后库仍非空，扩充数据永远灌不进。refill 独立闸门"无未决 seed
卡即补"，打包形态 no-op。见 services/seed.py 各 docstring。
"""

from sqlalchemy import func, select

from conftest import TestingSessionLocal
from models import Project, Proposal
from services import seed
from services.ai_data import create_proposal
from services.seed import refill_seed_proposals, seed_if_empty
from services.seed_data import SEED_PROPOSALS


def _proposal_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Proposal))


def test_seed_proposals_carry_seed_marker(seeded_db):
    """seed_if_empty 灌的卡带 from_ingest + seed 双标记（refill 判定的前提）。"""
    with TestingSessionLocal() as db:
        rows = db.scalars(select(Proposal)).all()
        assert len(rows) == len(SEED_PROPOSALS)
        assert all(
            r.payload.get("seed") is True and r.payload.get("from_ingest") is True
            for r in rows
        )


def test_refill_replenishes_after_consumed(fresh_db):
    """无未决 seed 卡（空库 / 全部消耗完）→ refill 补一批；再消耗完再补。"""
    with TestingSessionLocal() as db:
        assert refill_seed_proposals(db) is True
        assert _proposal_count(db) == len(SEED_PROPOSALS)
        # 全部结清 = 用户测试消耗完 → 重启 refill 再补一批（旧行保留已决态）
        for row in db.scalars(select(Proposal)).all():
            row.outcome = "accepted"
        db.commit()
        assert refill_seed_proposals(db) is True
        assert _proposal_count(db) == len(SEED_PROPOSALS) * 2


def test_refill_skips_when_pending_seed_left(fresh_db):
    """剩有未决 seed 卡（部分消耗）不重灌——防重复堆积。"""
    with TestingSessionLocal() as db:
        refill_seed_proposals(db)
        rows = db.scalars(select(Proposal)).all()
        for row in rows[:-1]:
            row.outcome = "rejected"
        db.commit()  # rows[-1] 留未决
        assert refill_seed_proposals(db) is False
        assert _proposal_count(db) == len(SEED_PROPOSALS)


def test_refill_ignores_non_seed_pending(fresh_db):
    """真实采集建议（无 seed 标记）不拦 refill——闸门只认 seed 卡。"""
    with TestingSessionLocal() as db:
        create_proposal(db, kind="inbox", payload={"from_ingest": True, "items": [{"title": "真实采集"}]})
        assert refill_seed_proposals(db) is True


def test_refill_noop_when_packaged(fresh_db, monkeypatch):
    """打包形态恒 no-op：测试数据不得反复灌给正式用户。"""
    monkeypatch.setattr(seed, "IS_PACKAGED", True)
    with TestingSessionLocal() as db:
        assert refill_seed_proposals(db) is False
        assert _proposal_count(db) == 0


def test_seed_if_empty_noop_when_packaged(fresh_db, monkeypatch):
    """首启 seed 同口径（2026-11-06 真机批）：打包版全新空库必须恒空白——
    此前缺闸曾把测试任务/建议卡灌给正式用户，夜间整理流再对测试待办出草案。"""
    monkeypatch.setattr(seed, "IS_PACKAGED", True)
    with TestingSessionLocal() as db:
        assert seed_if_empty(db) is False
        assert db.scalar(select(func.count()).select_from(Project)) == 0
        assert _proposal_count(db) == 0
