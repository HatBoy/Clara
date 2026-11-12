"""复现：手动采集后数据不出现在收件箱/数据源页（2026-11-05 用户报障）。

内存 SQLite + 快照恢复（拿用户真实源配置：滴答清单 MCP trusted + lark CLI 冷却）
→ POST /run?wait=true 两轮 → 逐步检查 [ingest] 日志与库内 tasks/proposals/
ingestion_items 状态，定位条目去向断点。台账不进快照 = 首采语义。
"""
import config

config.DATABASE_URL = "sqlite://"

import database  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

database.engine = database.create_engine(
    "sqlite://", connect_args={"check_same_thread": False},
    poolclass=StaticPool)
database.SessionLocal = database.sessionmaker(
    bind=database.engine, autoflush=False, expire_on_commit=False)

import main  # noqa: E402

database.Base.metadata.create_all(database.engine)
from sqlalchemy import text  # noqa: E402

with database.engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS episodic_facts_fts"))
database._ensure_episodic_fts(database.engine)

from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from services.config_backup import maybe_restore_and_snapshot  # noqa: E402

with SessionLocal() as db:
    maybe_restore_and_snapshot(db)
    from services.settings import get_settings

    s = get_settings(db)
    s.ingest_enabled = True
    db.commit()

client = TestClient(main.app)


def dump_state(tag):
    from models import IngestionItem, Proposal, Task
    from sqlalchemy import select

    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.is_deleted.is_(False))).all()
        props = db.scalars(select(Proposal).where(
            Proposal.outcome.is_(None))).all()
        items = db.scalars(select(IngestionItem)).all()
        print(f"\n[{tag}] 任务 {len(tasks)} 条 / 未决卡 {len(props)} / 台账 {len(items)} 行")
        for t in tasks[:12]:
            print(f"  task: {t.title!r} start={t.start_time} src={t.source_key}")
        for p in props[:12]:
            pl = p.payload or {}
            n = len(pl.get("draft_plan") or pl.get("items") or [])
            print(f"  card#{p.id[:8]} kind={p.kind} from_ingest={pl.get('from_ingest')} 条目{n}")
        from collections import Counter

        c = Counter((i.domain, i.proposed_at is not None, i.rejected_at is not None)
                    for i in items)
        print(f"  台账(domain, proposed, rejected): {dict(c)}")


for round_no in (1, 2):
    print(f"\n{'=' * 64}\n▶ 第 {round_no} 轮手动采集\n{'=' * 64}", flush=True)
    resp = client.post("/api/ingest/run?wait=true")
    print(f"HTTP {resp.status_code} → {resp.json()}")
    dump_state(f"第{round_no}轮后")
