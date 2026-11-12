"""全方位真机模拟测试（2026-11-04 批次收口批，方法同 2026-11-01 三轮模拟）。

环境：内存 SQLite + config_snapshot.json 恢复（拿 dev 库真实模型配置与数据源
接入，不污染 dev 数据——快照恢复是既有自愈机制，五表全空即触发）；TestClient
无 lifespan（worker/agent_loop 不启动，纯 API 面）；**真实 LLM**（用户激活的
glm-5.3-flash）——模拟测试的增量价值在真 LLM 行为（提示词/解析/仲裁质量），
pytest 替身覆盖不到的面。

用法：conda activate clara && cd backend && python sim_test.py [场景名...]
不带参数跑全部。输出逐场景 PASS/FAIL 与问题记录（先记录后统一修复）。
"""
import io
import json
import sys
import uuid
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta

__test__ = False  # pytest 不收集本脚本（*_test.py 会被误匹配；自举含快照恢复副作用）

# —— 环境自举：改 DATABASE_URL 到内存前，先让 config 指向 dev 数据目录（快照可读）
import config

config.DATABASE_URL = "sqlite://"

import database  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

database.engine = database.create_engine(
    "sqlite://", connect_args={"check_same_thread": False},
    poolclass=StaticPool)
database.SessionLocal = database.sessionmaker(
    bind=database.engine, autoflush=False, expire_on_commit=False)
database.Base.metadata.create_all(database.engine)
# FTS 虚表（conftest 同款）
from sqlalchemy import text  # noqa: E402

with database.engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS episodic_facts_fts"))
database._ensure_episodic_fts(database.engine)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from database import SessionLocal  # noqa: E402
from services.config_backup import maybe_restore_and_snapshot  # noqa: E402

# 快照恢复：真实模型配置 + 数据源接入进内存库（五表全空触发——dev 快照存在即恢复）
with SessionLocal() as db:
    maybe_restore_and_snapshot(db)

client = TestClient(main.app)

RESULTS: list[tuple[str, bool, str]] = []


def run(name, fn):
    """执行一个场景，捕获异常记 FAIL（先记录后统一修复，不让单场景中断全场）。"""
    print(f"\n{'=' * 60}\n▶ {name}\n{'=' * 60}", flush=True)
    try:
        notes = fn() or ""
        RESULTS.append((name, True, notes))
        print(f"✅ PASS {notes}")
    except Exception as e:
        import traceback

        traceback.print_exc()
        RESULTS.append((name, False, str(e)[:300]))
        print(f"❌ FAIL {e}")


def seed_task(title, **kw):
    from schemas import TaskCreate
    from services.tasks import create_task

    with SessionLocal() as db:
        return create_task(db, TaskCreate(title=title, **kw))


def chat(text):
    """一轮对话（SSE 事件流收集）。"""
    events = []
    with client.stream("POST", "/api/ai/chat", json={"message": text}) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events


def reply_of(events):
    for ev in reversed(events):
        if ev.get("type") == "reply":
            return ev.get("content") or ev.get("text") or ""
    return ""


# ————————————————————————— 场景矩阵 —————————————————————————


def sc_chat_preference_merge():
    """S1 对话偏好仲裁：先立「下午不排会」，再说「周三下午开放讨论」→ 应 merge
    成一条带例外表述（非两条矛盾共存）。"""
    chat("帮我记住：以后下午都不要排会议")
    ev = chat("以后每周三下午都开放讨论，团队一起过进度")
    reply = reply_of(ev)
    with SessionLocal() as db:
        from services.ai_data import list_memory_rules

        rules = [r for r in list_memory_rules(db)
                 if r.source in ("chat", "user_manual")]
        texts = [r.content for r in rules]
        # 断言：不含两条裸矛盾（下午不排会 与 周三开放 原样并存且无例外表述）
        has_plain_conflict = any("不排" in t and "周三" not in t for t in texts) \
            and any("周三" in t and "不排" not in t for t in texts) \
            and not any(("周三" in t and "不排" in t) or "例外" in t or "除" in t for t in texts)
        assert not has_plain_conflict, f"疑似矛盾共存: {texts}"
        print(f"  规则 {len(texts)} 条: {texts}")
        print(f"  回复: {reply[:120]}")


def sc_episodic_fact_same_turn():
    """S2 事实同跳：「我这周感冒了」→ settings 同跳落 episodic_facts（带过期钟）。"""
    before = _fact_count()
    chat("我这周感冒了，可能会好得慢一点")
    after = _fact_count()
    assert after > before, f"事实未落库 before={before} after={after}"
    with SessionLocal() as db:
        from services.episodic import list_episodic_facts

        facts = list_episodic_facts(db)
        print(f"  事实: {[(f.topic, f.fact, f.expires_at and f.expires_at.strftime('%m-%d')) for f in facts]}")


def _fact_count():
    with SessionLocal() as db:
        from services.episodic import list_episodic_facts

        return len(list_episodic_facts(db))


def sc_overload_persistent_dedup():
    """S3 过载持久日去重：负荷超 80% 两次 _run_heartbeat → 仅一条消息一行认领。"""
    noon = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=8)
    seed_task("满负荷模拟", start_time=noon, end_time=noon + timedelta(hours=8))
    import asyncio

    from services import workflows as wf

    asyncio.run(wf._run_heartbeat())
    asyncio.run(wf._run_heartbeat())  # 第二次（模拟重启后——认领行在库）
    with SessionLocal() as db:
        from models import Message, WorkflowRun
        from sqlalchemy import select

        msgs = db.scalars(select(Message).where(
            Message.content.like("%熔断线%"))).all()
        claims = db.scalars(select(WorkflowRun).where(
            WorkflowRun.name == "overload_notify")).all()
        assert len(msgs) == 1, f"过载消息 {len(msgs)} 条（应 1）"
        assert len(claims) == 1, f"认领行 {len(claims)}（应 1）"
        print(f"  消息 1 条 / 认领 1 行 ✓: {msgs[0].content[:60]}")


def sc_heartbeat_boot_grace():
    """S4 首跑宽限：_booted_at 拨到现在 → 宽限内不认领。"""
    from datetime import datetime as dt

    from services import workflows as wf

    wf._booted_at = dt.now()
    wf._last_heartbeat = None
    assert wf._claim_heartbeat(dt.now()) is False
    assert wf._claim_heartbeat(dt.now() + timedelta(minutes=3)) is True
    print("  宽限内拒 / 宽限过认领 ✓")


def sc_manual_ingest_roundtrip():
    """S5 手动采集全链（真数据源真 LLM）：等待端点返回摘要 + 直通任务建库。"""
    with SessionLocal() as db:
        from services.settings import get_settings

        s = get_settings(db)
        s.ingest_enabled = True
        db.commit()
    import time

    t0 = time.time()
    resp = client.post("/api/ingest/run?wait=true")
    body = resp.json()
    print(f"  耗时 {time.time() - t0:.1f}s 摘要: {body}")
    assert resp.status_code in (200, 202), resp.text
    assert not body.get("skipped"), f"被跳过: {body['skipped']}"
    if body.get("silent"):
        print("  静默轮（各源无新数据）——直通计数必然 0，链路本身 PASS")
        return
    total = body.get("drafts", 0) + body.get("inbox", 0) \
        + body.get("direct_calendar", 0) + body.get("direct_inbox", 0)
    print(f"  本轮产出 {total} 条（建卡 {body['drafts'] + body['inbox']} / "
          f"直通 {body['direct_calendar'] + body['direct_inbox']}）")


def sc_connections_empty_ok():
    """S6 未配 Composio Key（内存库无 key）→ GET /connections 200 []。"""
    resp = client.get("/api/integrations/connections")
    assert resp.status_code == 200 and resp.json() == [], resp.text
    print("  200 [] ✓")


def sc_schedule_preference_hardgate():
    """S7 偏好硬闸：立「周五下午别排事」后对话排周五下午任务 → 槽位避让。"""
    chat("帮我记住：周五下午不要排任何事")
    events = chat("帮我安排一个两小时的文档整理时间，就这周五吧")
    reply = reply_of(events)
    print(f"  回复: {reply[:150]}")
    starts = [d.get("start", "") for ev in events if ev.get("type") == "draft"
              for d in ev.get("items", [])]
    fri = _next_friday()
    for s in starts:
        d = datetime.fromisoformat(s)
        if d.weekday() == 4:
            assert not (720 <= d.hour * 60 + d.minute < 1080), \
                f"草案落在周五下午禁区: {s}"
    print(f"  草案槽位 {starts or '（问询/其他出口）'} ✓")


def _next_friday():
    d = datetime.now() + timedelta(days=1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def sc_qa_memory_answer():
    """S8 QA 记忆召回：问「我这周身体状况如何」→ 回复应提到感冒（episodic 注入）。"""
    # S2 已落感冒事实
    events = chat("我这周身体状态怎么样，适合安排高强度工作吗")
    reply = reply_of(events)
    print(f"  回复: {reply[:200]}")
    assert "感冒" in reply or "身体" in reply or "休息" in reply, \
        f"回复未见事实呼应: {reply[:100]}"


def sc_run_reflection_real():
    """S9 夜间反思真链路（真 LLM）：造裁决+行为偏差 → run_reflection → 候选/演化。"""
    from services import ai_data
    from agents.reflection import run_reflection
    from models import BehaviorEvent

    with SessionLocal() as db:
        p = ai_data.create_proposal(db, kind="schedule", payload={})
        ai_data.record_proposal_outcome(db, p.id, "rejected", reason="周五下午有别的安排")
        ai_data.append_message(db, role="user", content="周五下午别给我排东西，我要留白")
        for _ in range(3):
            db.add(BehaviorEvent(task_id="sim", kind="reschedule", delta_min=90,
                                 context={"weekday": 4, "period": "下午"}))
        db.commit()
    from services import workflows as wf
    from unittest.mock import patch

    with patch.object(wf, "_catchup_silent", lambda m, now=None: False):
        # 反思直调（不经壳）
        with SessionLocal() as db:
            report = run_reflection(db)
            print(f"  报告: {report}")
            assert not report.skipped, "反思早退（应有信号）"


def sc_messages_after_filter():
    """S10 未读位点恢复：GET /messages?after= 只返回此后消息。"""
    with SessionLocal() as db:
        from services.ai_data import append_message

        a = append_message(db, role="assistant", content="旧消息")
        append_message(db, role="assistant", content="新消息")
        cutoff = a.created_at
    resp = client.get("/api/ai/messages", params={"after": cutoff.isoformat(sep=" ")})
    rows = resp.json()
    assert [r["content"] for r in rows] == ["新消息"], rows
    print(f"  位点过滤 ✓（{len(rows)} 条）")


ALL = [
    ("S1 对话偏好仲裁合并", sc_chat_preference_merge),
    ("S2 事实同跳落库", sc_episodic_fact_same_turn),
    ("S3 过载持久日去重", sc_overload_persistent_dedup),
    ("S4 heartbeat 首跑宽限", sc_heartbeat_boot_grace),
    ("S5 手动采集全链(真源真LLM)", sc_manual_ingest_roundtrip),
    ("S6 未配Key连接列表空200", sc_connections_empty_ok),
    ("S7 偏好硬闸避让周五下午", sc_schedule_preference_hardgate),
    ("S8 QA 事实召回", sc_qa_memory_answer),
    ("S9 夜间反思真链路", sc_run_reflection_real),
    ("S10 消息位点过滤", sc_messages_after_filter),
]

if __name__ == "__main__":
    wanted = sys.argv[1:]
    # 依赖顺序：S2 先于 S8（感冒事实）；其余独立
    for name, fn in ALL:
        if wanted and not any(w in name for w in wanted):
            continue
        run(name, fn)
    print(f"\n{'=' * 60}\n汇总: {sum(1 for _, ok, _ in RESULTS if ok)}/{len(RESULTS)} PASS")
    for name, ok, note in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {note[:80]}" if not ok else ""))
