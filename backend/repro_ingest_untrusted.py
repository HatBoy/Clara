"""复现二：非 trusted 结构化源 —— 直映射产物应建卡进「数据源」页人工确认，
不直落（用户产品规则：trusted 开关是直落/建卡的分界）。内存库 + 快照恢复，
把滴答源 trusted 置 False、清台账、窗口放宽（模拟新条目重采）。
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
    from models import IngestSource
    from services.settings import get_settings

    s = get_settings(db)
    s.ingest_enabled = True
    db.commit()
    # 滴答源关 trusted：结构化直映射产物应建卡而非直落
    row = db.query(IngestSource).filter(
        IngestSource.source_key == "滴答清单 (国内版)").one()
    row.trusted = False
    row.window_hours = 168  # 宽窗：拉到全量条目（游标默认窗可能已滤）
    db.commit()

client = TestClient(main.app)
resp = client.post("/api/ingest/run?wait=true")
print(f"HTTP {resp.status_code} → {resp.json()}")

from models import IngestionItem, Proposal, Task
from sqlalchemy import select

with SessionLocal() as db:
    tasks = db.scalars(select(Task).where(Task.is_deleted.is_(False))).all()
    cards = db.scalars(select(Proposal).where(Proposal.outcome.is_(None))).all()
    items = db.scalars(select(IngestionItem)).all()
    print(f"\n任务 {len(tasks)} / 未决卡 {len(cards)} / 台账 {len(items)}")
    for t in tasks:
        print(f"  task: {t.title!r} start={t.start_time}")
    for p in cards:
        pl = p.payload or {}
        print(f"  card kind={p.kind} from_ingest={pl.get('from_ingest')}")
        for it in (pl.get("items") or pl.get("draft_plan") or []):
            print(f"    - {it.get('title')!r} src={it.get('source_ref')}")
