"""共享测试基建：内存 SQLite + StaticPool + get_db 覆盖 + 每用例重建表。

app 是单例、dependency_overrides 是全局的——多个测试文件必须共用这一份基建，
各自建内存引擎会互相覆盖 override。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from services import workflows
from services.seed import seed_if_empty

# autouse 时钟隔离（见 _punctual_by_default）替换前留存的真函数，
# 专门用例从 conftest 导入还原后直测边界
_REAL_CATCHUP_SILENT = workflows._catchup_silent

# 内存库 + StaticPool：所有连接共享同一个内存实例，建表/读写才互通
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """每个测试用干净的表；顺带清 mcp_session 会话池模块态（假 client 由各用例
    monkeypatch factory 注入，池若跨用例残留会串台）。2026-10-23 批 3 起：全局把
    database.SessionLocal 替换为内存库会话（chat 轮次任务自开 SessionLocal 落库，
    不再经 Depends(get_db)——不替换会写进真实 dev 库）+ 复位轮次锁（TestClient
    每测试新事件循环，旧锁跨循环绑定会炸）。"""
    from services import chat_channels, chat_turn, llm as llm_svc, mcp_session

    import database

    mcp_session._reset_for_tests()
    chat_turn.reset_turn_lock()
    chat_channels._reset_for_tests()  # epoch/去重/关闭位/主循环引用复位
    llm_svc._init_cache = None  # init_llm 微缓存（2026-10-26 提速批 3）防跨用例吃到上例激活模型
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    # episodic FTS 虚表（2026-11-03 记忆重构）：create_all 不认识虚表、drop_all
    # 也不删它——主表重建后虚表残留上例 rowid 索引会串台，须先 DROP 再随 create_all
    # 重建主表后补建（init_db 同款出口）
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS episodic_facts_fts"))
    Base.metadata.create_all(engine)
    database._ensure_episodic_fts(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _punctual_by_default(monkeypatch):
    """runner 用例默认按"准点触发"跑（2026-10-25 补账静默配套）：_catchup_silent
    恒 False 隔离真实时钟——距报点 >10min 的真实时刻跑测试不会随机静默吞掉
    广播/push 断言（test_workflows/test_jobs/test_run_ingest 三文件 runner 用例
    连坐）；补账静默路径由专门用例显式覆盖为 True 或还原真函数验证。"""
    monkeypatch.setattr(
        workflows, "_catchup_silent", lambda report_min, now=None: False
    )


@pytest.fixture
def client() -> TestClient:
    """无 lifespan 的 TestClient（不进 context manager，不触发建表/seed，不碰真实 dev 库）。"""
    return TestClient(app)


@pytest.fixture
def seeded_db(fresh_db):
    """需要 seed 数据的用例显式请求：灌入 117 任务 + 3 项目。"""
    with TestingSessionLocal() as db:
        seed_if_empty(db)
