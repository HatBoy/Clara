"""QA 性能基准·确定性层（mock 零延迟测管线开销，毫秒级随基线跑）。
真实层延迟汇总（AiUsageEvent.latency_ms）见 test_qa_real_llm.py。

口径：
- 轮次 LLM 调用数（文档口径校准：排期轮 router+profiler+adjudicator）；
- mock 零延迟下 chat 全链墙钟 <2s（纯管线开销）；
- finder 吞吐（200 busy 区间 × 7 天窗 p95）；
- parse_avoid_windows 模块缓存微秒级。
"""

import json
import statistics
import time
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal
from models import Task
from services import llm


@pytest.fixture(autouse=True)
def _isolate_sync_snapshot(monkeypatch, tmp_path):
    """sync 用例快照目录隔离到 tmp——perf 直连真 dev sync_snapshot 曾污染
    episodic_facts.json（QA 批事故，已自愈恢复并登记 checklist）。"""
    import services.sync_engine as _se

    monkeypatch.setattr(_se, "get_data_dir", lambda: tmp_path)
    _se.clear_snapshot()
    yield
    _se.clear_snapshot()

from agents.scheduler import scheduler_app
from qa_helpers import (
    KEY_ADJUDICATOR,
    KEY_PROFILER,
    MultiKeyLLMCapture,
    patch_scheduler_inputs,
)


def _prof(entries):
    return json.dumps({"entries": entries, "reply": "好的。"}, ensure_ascii=False)


def test_schedule_turn_llm_call_budget(monkeypatch):
    """排期轮 LLM 调用数 = router 1 + profiler 1 + adjudicator 1（≤3）——
    文档口径「排期轮恒 2 次」指子图内（profiler+adjudicator），router 是
    主图跳；超出预算即成本回归。"""
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof([{"task_id": None, "origin": "agent",
                              "title": "写周报", "duration": 60}]),
        KEY_ADJUDICATOR: json.dumps({"selections": [], "explanation": ""}),
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch)
    from agents.scheduler.prompts import PROFILER_PROMPT as _PP
    from agents.scheduler.prompts import _ADJ_HEAD as _ADJ

    scheduler_app.invoke({"user_input": "安排写周报", "memory_rules": []})
    assert cap.count(_PP) == 1
    assert cap.count(_ADJ) == 1


def test_chat_pipeline_wall_clock_under_2s(monkeypatch, client):
    """mock 零延迟下 chat 排期轮 SSE 全程 <2s（纯管线/DB 开销——真实延迟大头
    在 LLM 侧）。"""
    from qa_helpers import activate_model, chat_events

    activate_model()
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof([{"task_id": None, "origin": "agent",
                              "title": "写周报", "duration": 60}]),
        KEY_ADJUDICATOR: json.dumps({"selections": [], "explanation": ""}),
        "意图路由器": '{"intent": "schedule", "reason": "qa"}',
    })
    monkeypatch.setattr(llm, "call_llm", cap)
    patch_scheduler_inputs(monkeypatch, patch_settings=False)  # 闸门真读库
    import agents.scheduler.context as _ctx
    import agents.scheduler.intake as _intake

    monkeypatch.setattr(_intake, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(_ctx, "SessionLocal", TestingSessionLocal)
    t0 = time.perf_counter()
    events = chat_events(client, "安排写周报")
    wall = time.perf_counter() - t0
    assert events[-1]["type"] == "done"
    assert wall < 2.0, f"管线墙钟超预算：{wall:.2f}s"


def test_finder_throughput_200_busy(monkeypatch):
    """finder 吞吐：200 区间 busy × 7 天窗 × 5 条目——candidate_slots p95<50ms
    （SQLite 内存库纯 CPU 扫描，回归即性能 Bug）。"""
    from services.tasks import candidate_slots

    now = datetime(2026, 11, 25, 9, 0)
    busy = []
    base = now.replace(hour=0)
    for i in range(200):  # 7 天 × ~28 区间/天（15 分钟粒度占满网格）
        d = base + timedelta(days=i % 8, minutes=(i * 23) % 600)
        busy.append((d, d + timedelta(minutes=10)))
    durations = [15, 30, 60, 120, 30]
    samples = []
    for dur in durations:
        t0 = time.perf_counter()
        candidate_slots(now, dur, busy, day_start_min=480, day_end_min=1320,
                        lunch=(690, 810), k=5)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
    assert p95 < 50, f"candidate_slots p95={p95:.1f}ms 超 50ms 预算"


def test_parse_avoid_windows_cache_fast():
    """parse_avoid_windows 模块缓存：同规则集二次解析 <1ms/千次。"""
    from services.preference_slots import parse_avoid_windows

    rules = ("周五下午别排事", "工作日上午不排会", "每月末留白")
    parse_avoid_windows(list(rules))  # 首次建缓存
    t0 = time.perf_counter()
    for _ in range(1000):
        parse_avoid_windows(list(rules))
    per_call_ms = (time.perf_counter() - t0) * 1000 / 1000
    assert per_call_ms < 1.0, f"缓存命中仍超 1ms：{per_call_ms:.3f}"


def test_sync_incremental_request_budget(monkeypatch):
    """增量请求预算：双端无变化的同步轮 ≤ 3 请求（manifest GET + 探测基线）
    ——高频轮不该全表推送。"""
    import services.sync_engine as se

    class _CountingDav(se.WebDavClient.__mro__ and object):
        def __init__(self):
            self.manifest = None
            self.tables = {}
            self.get_count = 0
            self.push_count = 0

        def ensure_dirs(self): pass
        def fetch_manifest(self):
            self.get_count += 1
            return self.manifest
        def fetch_table(self, name):
            self.get_count += 1
            return self.tables.get(name)
        def push_table(self, name, payload):
            self.push_count += 1
            self.tables[name] = payload
        def push_manifest(self, manifest, *, if_none_match=False):
            self.manifest = manifest
        def put_readme(self, content): pass
        def close(self): pass
        def manifest_hashes(self):
            return {t: v.get("sha1") for t, v in (self.manifest or {}).get("tables", {}).items()}

    CFG = type("Cfg", (), {"webdav_url": "https://dav.x", "webdav_username": "u",
                           "webdav_password": "p", "device_id": "dev-1"})()
    dav = _CountingDav()
    monkeypatch.setattr(se, "WebDavClient", lambda *a, **kw: dav)
    with TestingSessionLocal() as db:
        se.run_sync(db, dav, CFG)  # 首推（建基线快照）
    assert dav.manifest is not None
    dav.get_count = 0
    dav.push_count = 0
    with TestingSessionLocal() as db:
        out = se.run_sync(db, dav, CFG)  # 零变化轮
    assert out["tables_changed"] == 0
    assert dav.push_count == 0, "零变化轮出现全表推送"


def test_reflection_pipeline_fast(monkeypatch):
    """run_reflection 管线（替身 LLM 零延迟）<500ms——夜间批不拖慢事件循环。"""
    from services import llm as llm_svc
    from services.ai_data import create_proposal, record_proposal_outcome

    from agents import reflection
    from agents.prompts import REFLECT_PROMPT

    fake = MultiKeyLLMCapture({REFLECT_PROMPT: '{"candidates": [], "facts": []}'})
    monkeypatch.setattr(llm_svc, "call_llm", fake)
    with TestingSessionLocal() as db:
        p = create_proposal(db, kind="inbox", payload={"items": [
            {"title": "x", "duration": 30}]})
        record_proposal_outcome(db, p.id, "rejected", reason="r")
        t0 = time.perf_counter()
        reflection.run_reflection(db)
        wall = time.perf_counter() - t0
    assert wall < 0.5, f"reflection 管线 {wall:.2f}s 超 500ms"


@pytest.mark.parametrize('days', [180, 365])
def test_long_history_retention_and_short_query(days, tmp_path):
    """文件库测保留窗口与短词召回；记录耗时和空间，不把 CI 机器绝对性能写成 SLA。"""
    from sqlalchemy import create_engine, insert, select, func
    from sqlalchemy.orm import Session
    from database import Base, _ensure_episodic_fts
    from models import BehaviorEvent, EpisodicFact
    from services import episodic, ai_data
    engine = create_engine(f'sqlite:///{tmp_path / "年度测试.db"}')
    Base.metadata.create_all(engine)
    _ensure_episodic_fts(engine)
    now = datetime.now()
    try:
        with Session(engine) as db:
            db.connection().exec_driver_sql('PRAGMA journal_mode=WAL')
            db.execute(insert(BehaviorEvent), [{'kind':'resize','created_at':now-timedelta(days=n)} for n in range(days)])
            db.execute(insert(EpisodicFact), [{'topic':'行程','fact':'常规事项','source':'chat','created_at':now} for _ in range(550)])
            db.add(EpisodicFact(topic='出差', fact='永久样本', source='chat', created_at=now-timedelta(days=days)))
            db.add(EpisodicFact(topic='出差', fact='过期样本', source='chat', expires_at=now-timedelta(days=1)))
            db.commit()
            assert ai_data.prune_behavior_events(db) == days - 90
            assert episodic.expire_episodic_facts(db) == 1
            elapsed = []
            for _ in range(20):
                start = time.perf_counter()
                assert episodic.search_facts(db, '出差')[0].fact == '永久样本'
                elapsed.append((time.perf_counter()-start)*1000)
            assert db.scalar(select(func.count()).select_from(BehaviorEvent)) == 90
            print(f'{days} 天：短词 P95={sorted(elapsed)[18]:.1f}ms，库和 WAL 共 {sum(f.stat().st_size for f in tmp_path.glob("年度测试.db*"))} 字节')
    finally:
        engine.dispose()


@pytest.mark.parametrize('batch', [1, 3])
def test_schedule_stream_stage_percentiles(monkeypatch, batch):
    """从请求入口到各 SSE 帧产出时计时；不将 ASGI 测试客户端的整包缓冲算作首帧。"""
    import asyncio
    from math import ceil
    from routers.ai_coach import chat
    from schemas import ChatIn
    from qa_helpers import activate_model
    from services import chat_turn

    activate_model()
    cap = MultiKeyLLMCapture({
        KEY_PROFILER: _prof([{'task_id': None, 'origin': 'agent', 'title': f'验收事项{i}', 'duration': 30} for i in range(batch)]),
        KEY_ADJUDICATOR: '{"selections":[],"explanation":""}',
        '意图路由器': '{"intent":"schedule","reason":"测试"}',
    })
    monkeypatch.setattr(llm, 'call_llm', cap)
    monkeypatch.setattr(chat_turn, 'schedule_compaction', lambda: None)
    patch_scheduler_inputs(monkeypatch, patch_settings=False)
    samples = {key: [] for key in ('status', 'draft', 'done')}

    async def collect():
        for iteration in range(101):
            first = {}
            with TestingSessionLocal() as db:
                t0 = time.perf_counter()
                response = await chat(ChatIn(message='安排写周报'), db=db)
                async for frame in response.body_iterator:
                    if not frame.startswith('data: '):
                        continue
                    ev = json.loads(frame[6:])
                    if ev['type'] in samples:
                        if ev['type'] == 'draft':
                            assert len(ev['draft_plan']) == batch
                            assert all(entry['start'] for entry in ev['draft_plan'])
                        first.setdefault(ev['type'], (time.perf_counter() - t0) * 1000)
                assert first.keys() == samples.keys(), first
                assert first['status'] <= first['draft'] <= first['done']
                if iteration:  # 第一轮预热不计时
                    for key in samples:
                        samples[key].append(first[key])
    asyncio.run(collect())
    assert cap.count(KEY_PROFILER) == cap.count(KEY_ADJUDICATOR) == 101
    for key, values in samples.items():
        values.sort()
        p95 = values[ceil(.95 * len(values)) - 1]
        print(f'排期 {batch} 项 {key}：P50 {statistics.median(values):.1f} ms，P95 {p95:.1f} ms，n={len(values)}')
        assert p95 < 2000, (key, values)


def test_memory_workload_5000_events_500_facts(tmp_path):
    """文件 WAL；校验 FTS 实际命中而非回退后碰巧找对，再测热查询。"""
    from database import Base, _ensure_episodic_fts
    from models import BehaviorEvent, EpisodicFact, MemoryRule
    from services import ai_data, episodic
    from sqlalchemy import create_engine, event, insert
    from sqlalchemy.orm import Session

    path = tmp_path / '容量.db'
    engine = create_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        _ensure_episodic_fts(engine)
        with Session(engine) as db:
            assert db.connection().exec_driver_sql('PRAGMA journal_mode=WAL').scalar() == 'wal'
            db.execute(insert(BehaviorEvent), [{'kind': 'resize'} for _ in range(5000)])
            db.execute(insert(EpisodicFact), [dict(topic='项目', source='chat', fact=
                '目标项目验收会议' if i == 0 else f'无关事实{i}') for i in range(500)])
            db.add(MemoryRule(source='user_manual', category='边界', content='周三下午不开会', slot='boundary'))
            db.commit()
            sql = []
            def capture(conn, cursor, statement, params, context, many):
                sql.append(statement)
            event.listen(engine, 'before_cursor_execute', capture)
            try:
                assert episodic.search_facts(db, '目标项目')[0].fact == '目标项目验收会议'
                assert any(' MATCH ' in q for q in sql)
                assert not any('ORDER BY episodic_facts.created_at DESC' in q for q in sql)
            finally:
                event.remove(engine, 'before_cursor_execute', capture)
            for name, read in [('FTS', lambda: episodic.search_facts(db, '目标项目')[0].fact),
                               ('核心槽位', lambda: ''.join(ai_data.injected_rules(db).core))]:
                samples = []
                for i in range(101):
                    start = time.perf_counter()
                    result = read()
                    elapsed = (time.perf_counter() - start) * 1000
                    assert ('目标项目' if name == 'FTS' else '周三下午不开会') in result
                    if i:
                        samples.append(elapsed)
                p95 = sorted(samples)[94]
                print(f'{name}: n=100 P50={statistics.median(samples):.3f}ms P95={p95:.3f}ms')
                assert p95 < 100
            print(f'容量：库={path.stat().st_size}B WAL={path.with_suffix(".db-wal").stat().st_size}B')
    finally:
        engine.dispose()
