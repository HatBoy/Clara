"""services/workflows.py 测试（基建见 conftest.py）。

谓词纯函数直测（不跑 asyncio 循环，同 test_reminders 先例）；runner 用
asyncio.run 同步驱动（项目未装 pytest-asyncio）。模块态计时器（_last_activity /
_booted_at / _last_heartbeat）经 monkeypatch 重置；SessionLocal 换 TestingSessionLocal。
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from conftest import _REAL_CATCHUP_SILENT, TestingSessionLocal
from models import Task, WorkflowRun
from services import ai_data, workflows
from services.settings import get_settings


def _use_test_db(monkeypatch) -> None:
    monkeypatch.setattr(workflows, "SessionLocal", TestingSessionLocal)


# 时钟隔离 autouse fixture（_punctual_by_default）与真函数留存
# （_REAL_CATCHUP_SILENT）已上提到 conftest.py——test_jobs / test_run_ingest
# 的 runner 用例同受保护


def _reset_idle_state(monkeypatch, *, last_activity=None, booted_at=None) -> None:
    monkeypatch.setattr(workflows, "_last_activity", last_activity)
    monkeypatch.setattr(workflows, "_booted_at", booted_at or datetime.now())


def _reset_heartbeat_state(monkeypatch, *, booted_at=None) -> None:
    """heartbeat 模块态重置（2026-11-04 真机批配套）：首跑宽限（_booted_at 锚，
    默认拨回 10 分钟前=宽限已过）+ tick 计时。日去重已改持久认领
    （workflow_runs overload_notify 行，测试库随 fresh_db 每用例重建，无需重置）。"""
    monkeypatch.setattr(workflows, "_booted_at", booted_at or datetime.now() - timedelta(minutes=10))
    monkeypatch.setattr(workflows, "_last_heartbeat", None)


def _add_task(title="任务", **kw) -> str:
    with TestingSessionLocal() as db:
        row = Task(title=title, **kw)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _next_monday() -> date:
    d = date.today()
    return d + timedelta(days=(1 - d.isoweekday()) % 7 or 7)


# —— 空闲判定 ——


def test_is_idle_requires_quiet_activity(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now()
    # 最近活动在阈值内 → 忙
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=1))
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, now) is False
    # 活动超阈值且无进行中日程 → 空闲
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=30))
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, now) is True


def test_is_idle_with_ongoing_task(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now()
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=30))
    _add_task("进行中", start_time=now - timedelta(minutes=30))  # 无 end → 按 1h，进行中
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, now) is False
    # 已结束的单次任务不算
    _add_task("已结束", start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1))
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, now) is False  # 上一条仍在进行
    # 无进行中（只留已结束）
    with TestingSessionLocal() as db:
        db.query(Task).filter(Task.title == "进行中").delete()
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, now) is True


def test_is_idle_no_activity_ever(monkeypatch):
    # 启动后从未有请求（_last_activity=None）：只看日程
    _use_test_db(monkeypatch)
    _reset_idle_state(monkeypatch, last_activity=None)
    with TestingSessionLocal() as db:
        assert workflows._is_idle(db, datetime.now()) is True


# —— 负荷计算（规则层） ——


def test_day_load_minutes_single_tasks():
    day = date.today()
    noon = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
    _add_task("一小时", start_time=noon, end_time=noon + timedelta(hours=1))
    _add_task("无结束按一小时", start_time=noon + timedelta(hours=2))  # 无 end → 1h
    with TestingSessionLocal() as db:
        assert workflows.day_load_minutes(db, day) == 120


def test_day_load_minutes_weekly_series_and_cancelled():
    monday = _next_monday()
    base = datetime.combine(monday, datetime.min.time()) + timedelta(hours=9)
    tid = _add_task("周一站会", start_time=base, end_time=base + timedelta(minutes=30),
                    recurrence_rule="FREQ=WEEKLY;BYDAY=MO")
    # 墓碑：取消本周一实例
    with TestingSessionLocal() as db:
        db.add(Task(title="周一站会", parent_id=tid, original_time=base, is_deleted=True))
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows.day_load_minutes(db, monday) == 0  # 当日唯一实例被取消
    # 非周一不算
    with TestingSessionLocal() as db:
        assert workflows.day_load_minutes(db, monday + timedelta(days=1)) == 0


def test_load_ratio_uses_work_minutes_setting():
    day = date.today()
    noon = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
    _add_task("六小时", start_time=noon, end_time=noon + timedelta(hours=6))
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.work_minutes_per_day = 480  # 8h
        db.commit()
        assert workflows.load_ratio(db, day) == 360 / 480  # 0.75
        settings.work_minutes_per_day = 0
        db.commit()
        assert workflows.load_ratio(db, day) == 0.0  # 分母 0 防御


# —— 触发谓词：晨间 ——


def test_claim_morning_before_time(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=7, minute=0)
    with TestingSessionLocal() as db:
        assert workflows._claim_morning(db, now) is False


def test_claim_morning_claims_once(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=10, minute=0)  # 报点后且在补账窗内（默认 08:30+3h）
    with TestingSessionLocal() as db:
        assert workflows._claim_morning(db, now) is True
        assert workflows._claim_morning(db, now) is False  # 当日认领幂等


def test_claim_morning_catchup_window_expires(monkeypatch):
    # 补账截止窗（2026-10-25 启动收敛）：默认报点 08:30，窗外（≥11:30）当日整跳
    # ——不认领也不落 workflow_runs 行（下午开机的「晨间」简报只剩打扰），次日正常
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        assert workflows._claim_morning(db, datetime.now().replace(hour=12, minute=0)) is False
        assert db.scalar(select(WorkflowRun).where(WorkflowRun.name == "morning")) is None
        assert workflows._claim_morning(db, datetime.now().replace(hour=10, minute=0)) is True


def test_catchup_silent_threshold(monkeypatch):
    # 纯函数边界：距报点 ≤10 分钟 = 准点（False），>10 分钟 = 补账（True）。
    # 还原真函数（autouse 恒 False 不适用于直测）
    monkeypatch.setattr(workflows, "_catchup_silent", _REAL_CATCHUP_SILENT)
    base = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    assert workflows._catchup_silent(8 * 60 + 30, base.replace(hour=8, minute=39)) is False
    assert workflows._catchup_silent(8 * 60 + 30, base.replace(hour=8, minute=41)) is True
    # 报点未到（负差）按准点——调用方（认领谓词）保证已过报点，防御口径
    assert workflows._catchup_silent(20 * 60, base.replace(hour=8, minute=0)) is False


def test_claim_morning_respects_setting(monkeypatch):
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.morning_brief_min = 600  # 10:00
        db.commit()
        now = datetime.now().replace(hour=9, minute=0)
        assert workflows._claim_morning(db, now) is False
        late = datetime.now().replace(hour=10, minute=30)
        assert workflows._claim_morning(db, late) is True


# —— 触发谓词：数据采集 ——


def test_claim_ingest_disabled_by_default(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=23, minute=0)  # 恒晚于默认 09:00
    with TestingSessionLocal() as db:
        assert workflows._claim_ingest(db, now) is False  # 默认关：永不认领（不落 workflow_runs 行）


def test_claim_ingest_claims_once_when_enabled(monkeypatch):
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.ingest_enabled = True
        settings.ingest_runs_per_day = 1  # 单轮档：当日认领一次即幂等（多槽见下例）
        db.commit()
    now = datetime.now().replace(hour=23, minute=0)
    with TestingSessionLocal() as db:
        assert workflows._claim_ingest(db, now) is True
        assert workflows._claim_ingest(db, now) is False  # 当日认领幂等


def test_claim_ingest_multi_slot_per_frequency(monkeypatch):
    # 频率档位（2026-10-23）：默认 2 次——首轮 09:00 认领后 +5h（14:00）第二槽独立
    # 认领（name 后缀 ingest@2），当日各槽幂等
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.ingest_enabled = True
        settings.ingest_brief_min = 540  # 09:00
        settings.ingest_runs_per_day = 2
        db.commit()
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        assert workflows._claim_ingest(db, base.replace(hour=8, minute=59)) is False
        assert workflows._claim_ingest(db, base.replace(hour=9)) is True  # 首槽
        assert workflows._claim_ingest(db, base.replace(hour=13, minute=59)) is False
        assert workflows._claim_ingest(db, base.replace(hour=14)) is True  # 第二槽 +5h
        assert workflows._claim_ingest(db, base.replace(hour=23)) is False  # 两槽当日幂等


def test_claim_ingest_four_slots_catch_up_one_per_tick(monkeypatch):
    # 4 档错过补账：晚 23 点首次 tick 起逐槽补领——每 tick 一槽（错过不丢，
    # 在途闸串行靠后续 tick 逐个消化）
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.ingest_enabled = True
        settings.ingest_brief_min = 540  # 09:00 → 槽 09/12/15/18
        settings.ingest_runs_per_day = 4
        db.commit()
    late = datetime.now().replace(hour=23, minute=0)
    with TestingSessionLocal() as db:
        claims = sum(workflows._claim_ingest(db, late) for _ in range(4))
        assert claims == 4  # 四槽逐 tick 补齐
        assert workflows._claim_ingest(db, late) is False  # 补齐后幂等


def test_claim_ingest_respects_setting_time(monkeypatch):
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.ingest_enabled = True
        settings.ingest_brief_min = 600  # 10:00
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows._claim_ingest(db, datetime.now().replace(hour=9, minute=0)) is False
        assert workflows._claim_ingest(db, datetime.now().replace(hour=10, minute=30)) is True


# —— 触发谓词：夜间 ——


def test_claim_nightly_before_20(monkeypatch):
    _use_test_db(monkeypatch)
    _reset_idle_state(monkeypatch, last_activity=None)
    now = datetime.now().replace(hour=19, minute=0)
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, now) is False


def test_claim_nightly_idle_triggers_once(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=21, minute=0)
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=30),
                      booted_at=now - timedelta(hours=10))
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, now) is True
        assert workflows._claim_nightly(db, now) is False


def test_claim_nightly_busy_by_activity(monkeypatch):
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=21, minute=0)
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=2),
                      booted_at=now - timedelta(hours=10))
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, now) is False


def test_claim_nightly_boot_grace_exempts_idle(monkeypatch):
    # 启动宽限内（补账语义）：哪怕刚有活动也触发
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=21, minute=0)
    _reset_idle_state(monkeypatch, last_activity=now, booted_at=now - timedelta(seconds=30))
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, now) is True


def test_claim_nightly_respects_setting_time(monkeypatch):
    # 2026-10-20 设置化：报点时刻随 settings.nightly_start_min（原硬编码 20:00 退役）
    _use_test_db(monkeypatch)
    _reset_idle_state(monkeypatch, last_activity=None)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.nightly_start_min = 1080  # 18:00
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, datetime.now().replace(hour=17, minute=59)) is False
        assert workflows._claim_nightly(db, datetime.now().replace(hour=18, minute=0)) is True
        assert workflows._claim_nightly(db, datetime.now().replace(hour=23, minute=0)) is False  # 当日幂等


# —— 触发谓词：会前准备（prep，2026-10-20 增） ————————————————————————————————


def test_claim_prep_shared_threshold_no_idle_gate(monkeypatch):
    # 与 nightly 共用报点门槛但无空闲条件：忙（刚有活动）也照跑——静默建卡不打扰
    _use_test_db(monkeypatch)
    now = datetime.now().replace(hour=21, minute=0)
    _reset_idle_state(monkeypatch, last_activity=now - timedelta(minutes=2),
                      booted_at=now - timedelta(hours=10))  # 忙碌态：nightly 会被拦
    with TestingSessionLocal() as db:
        assert workflows._claim_nightly(db, now) is False
        assert workflows._claim_prep(db, now) is True  # prep 不看空闲


def test_claim_prep_threshold_and_daily_idempotent(monkeypatch):
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.nightly_start_min = 1080  # 18:00
        db.commit()
    with TestingSessionLocal() as db:
        assert workflows._claim_prep(db, datetime.now().replace(hour=17, minute=59)) is False
        assert workflows._claim_prep(db, datetime.now().replace(hour=18, minute=0)) is True
        assert workflows._claim_prep(db, datetime.now().replace(hour=23, minute=0)) is False  # 当日幂等


# —— 四流独立开关（2026-10-21 复查批 D1）—————————————————————————————————————


@pytest.mark.parametrize(("claim", "key"), [
    (workflows._claim_morning, "morning_enabled"),
    (workflows._claim_nightly, "nightly_enabled"),
    (workflows._claim_prep, "prep_enabled"),
    (workflows._claim_hardtask, "hardtask_enabled"),
])
def test_claim_disabled_by_flow_switch(monkeypatch, claim, key):
    # 开关关 = 永不认领不落 workflow_runs 行（同 ingest_enabled 语义），时刻/空闲
    # 条件全部短路；四流各关各的互不影响
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        setattr(get_settings(db), key, False)
        db.commit()
    late = datetime.now().replace(hour=23, minute=59)  # 恒过各流时刻门槛
    with TestingSessionLocal() as db:
        assert claim(db, late) is False
        assert list(db.scalars(select(WorkflowRun))) == []


# —— 触发谓词：心跳 ——


def test_claim_heartbeat_interval(monkeypatch):
    now = datetime.now()
    _reset_heartbeat_state(monkeypatch)
    assert workflows._claim_heartbeat(now) is True
    assert workflows._claim_heartbeat(now + timedelta(hours=1)) is False
    assert workflows._claim_heartbeat(now + timedelta(hours=3, minutes=1)) is True


def test_claim_heartbeat_boot_grace(monkeypatch):
    """首跑吃启动宽限（2026-11-04 真机批）：BOOT_GRACE_S 内不认领（应用刚启动
    页面未稳不弹过载提醒），宽限过后首轮 tick 认领。"""
    boot = datetime.now() - timedelta(seconds=30)
    _reset_heartbeat_state(monkeypatch, booted_at=boot)
    assert workflows._claim_heartbeat(boot + timedelta(seconds=60)) is False  # 宽限内
    assert workflows._claim_heartbeat(boot + timedelta(seconds=125)) is True  # 宽限过


# —— 夜间重要整理（优先级预排；测试库无激活模型 → 纯规则层零 LLM） ——————————


def test_collect_priority_plan_needs_two():
    _add_task("唯一待办", priority=1, duration_mins=30, sort_order=0)
    now = datetime.now()
    with TestingSessionLocal() as db:
        assert workflows._collect_priority_plan(db, now) is None  # 只有 1 条不出卡


def test_collect_priority_plan_pick_and_defaults():
    # 取材：优先级 1→3（同优先级内 sort_order）、已完成任务不入选（旧碎片块
    # 漏 done_at 过滤曾把已完成任务捞出排期）、未估时保守默认 30 分钟
    _add_task("低优已估", priority=3, duration_mins=45, sort_order=0)
    _add_task("高优A", priority=1, duration_mins=30, sort_order=3)
    _add_task("中优无耗时", priority=2, duration_mins=None, sort_order=5)
    _add_task("已完成高优", priority=1, duration_mins=30, sort_order=1,
              done_at=datetime.now() - timedelta(days=1))
    now = datetime.now()
    with TestingSessionLocal() as db:
        payload = workflows._collect_priority_plan(db, now)
        items = payload["draft_plan"]
        assert [i["title"] for i in items] == ["高优A", "中优无耗时", "低优已估"]
        assert all(i["task_id"] for i in items)
        assert items[0]["priority"] == 1 and items[2]["priority"] == 3  # 真实优先级带出
        assert items[1]["duration"] == 30  # 未估时默认
        assert items[2]["duration"] == 45  # 已估时照抄
        # start 由引擎产生：未来时刻且同批互斥不叠位（now 按分钟截断对齐引擎
        # 契约——引擎内部 second=0 取整，带秒比较在网格恰逢整分时偶发假失败）
        assert all(datetime.fromisoformat(i["start"])
                   >= now.replace(second=0, microsecond=0)
                   for i in items)
        assert len({i["start"] for i in items}) == 3


# —— runner（asyncio.run 驱动；SessionLocal 已指测试库） ——


def test_run_heartbeat_broadcasts_on_overload(monkeypatch):
    _use_test_db(monkeypatch)
    _reset_heartbeat_state(monkeypatch)
    day = date.today()
    noon = datetime.combine(day, datetime.min.time()) + timedelta(hours=8)
    _add_task("满负荷", start_time=noon, end_time=noon + timedelta(hours=8))  # 8h/8h = 100%
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_heartbeat())
        event = q.get_nowait()
        assert event["type"] == "overload" and event["ratio"] == 1.0
        # AI 建议补落对话框（2026-11-04 真机批）：assistant 消息可回看（不建卡）
        from models import Message
        from sqlalchemy import select as _sel

        with TestingSessionLocal() as db:
            msgs = db.scalars(_sel(Message).where(Message.role == "assistant")).all()
            assert len(msgs) == 1 and "熔断线" in msgs[0].content
            assert msgs[0].proposal_id is None
    finally:
        workflows.unsubscribe(q)


def test_run_heartbeat_daily_dedup(monkeypatch):
    """过载提醒日去重（2026-11-04 真机批；同日二次修订改持久认领）：当日已广播
    后再次 tick 负荷仍过载 → 零广播（长开机器不再每 3h 一弹、**重启也不再弹**
    ——认领行落 workflow_runs，跨启动免疫）；删认领行（模拟次日）再跑恢复广播。"""
    _use_test_db(monkeypatch)
    _reset_heartbeat_state(monkeypatch)
    noon = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=8)
    _add_task("满负荷", start_time=noon, end_time=noon + timedelta(hours=8))
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_heartbeat())
        assert q.get_nowait()["type"] == "overload"  # 首次广播（落认领行）
        asyncio.run(workflows._run_heartbeat())
        assert q.empty()  # 持久日去重：第二次零输出
        with TestingSessionLocal() as db:  # 模拟次日：删当日认领行
            from models import WorkflowRun
            from sqlalchemy import delete as _del

            db.execute(_del(WorkflowRun).where(WorkflowRun.name == "overload_notify"))
            db.commit()
        asyncio.run(workflows._run_heartbeat())
        assert q.get_nowait()["type"] == "overload"
    finally:
        workflows.unsubscribe(q)


def test_run_heartbeat_silent_when_healthy(monkeypatch):
    _use_test_db(monkeypatch)
    _reset_heartbeat_state(monkeypatch)
    _add_task("轻负荷", start_time=datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=9),
              end_time=None)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_heartbeat())
        assert q.empty()  # 未越阈零输出（零 LLM 零打扰）
    finally:
        workflows.unsubscribe(q)


def test_run_heartbeat_ai_advice_attached(monkeypatch):
    """过载 AI 推迟建议（2026-11-01 优化批 1）：后台单次 LLM 出 advice 随广播
    ——headline 事实仍确定性拼装（谎报纪律）；心跳提示词单源可断言。"""
    import json as _json

    from agents.prompts import HEARTBEAT_ADVICE_PROMPT
    from services import llm as llm_svc

    _use_test_db(monkeypatch)
    _reset_heartbeat_state(monkeypatch)
    noon = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=8)
    _add_task("满负荷评审", start_time=noon, end_time=noon + timedelta(hours=8))
    calls: list[str] = []

    def fake_llm(messages, *, json_mode=False, max_tokens=None):
        calls.append(messages[0]["content"])
        return _json.dumps({"advice": "评审材料可挪明早，今天先保既定会议"},
                           ensure_ascii=False)

    monkeypatch.setattr(llm_svc, "call_llm", fake_llm)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_heartbeat())
        event = q.get_nowait()
        assert event["type"] == "overload"
        assert event["advice"] == "评审材料可挪明早，今天先保既定会议"
        assert "80% 熔断线" in event["headline"]  # 事实确定性拼装不回退
        assert calls == [HEARTBEAT_ADVICE_PROMPT]
    finally:
        workflows.unsubscribe(q)


def test_run_heartbeat_ai_failure_falls_back_to_template(monkeypatch):
    # LLM 故障回落纯模板：广播照发零 advice——AI 层失效不连坐监控本身
    from services import llm as llm_svc

    _use_test_db(monkeypatch)
    _reset_heartbeat_state(monkeypatch)
    noon = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=8)
    _add_task("满负荷", start_time=noon, end_time=noon + timedelta(hours=8))

    def boom(*_a, **_k):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(llm_svc, "call_llm", boom)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_heartbeat())
        event = q.get_nowait()
        assert event["type"] == "overload" and "advice" not in event
        assert "80% 熔断线" in event["headline"]
    finally:
        workflows.unsubscribe(q)


def test_run_morning_skips_without_model(monkeypatch):
    # 零激活（不造模型行）：静默跳过，不落任何消息
    _use_test_db(monkeypatch)
    asyncio.run(workflows._run_morning())
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []


def test_run_morning_builds_preflight_proposal(monkeypatch):
    # 替身 run_preflight 返回简报（批 D 起真实现落地，桩跳过语义不再适用）：
    # assistant 消息落库 + kind=preflight 建议卡 + morning 广播
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_preflight(db, day):
        return {"preflight": {"headline": "上午两场连轴", "checklist": ["提前打印材料"]},
                "final_reply": "今天是高压日，注意留白。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda *a: _fake_preflight)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_morning())
        event = q.get_nowait()
        assert event["type"] == "morning" and event["headline"] == "上午两场连轴"
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and messages[0].content == "今天是高压日，注意留白。"
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "preflight"
        assert proposals[0].payload["checklist"] == ["提前打印材料"]


def test_run_morning_catchup_round_silent(monkeypatch):
    # 补账轮静默（2026-10-25 启动收敛）：距报点 >10min 的认领轮不广播不推渠道
    # ——卡与面板消息照落（打开面板即见），toast/提示音随 broadcast 一并消失
    _use_test_db(monkeypatch)
    _activate_model()
    monkeypatch.setattr(workflows, "_catchup_silent", lambda report_min, now=None: True)

    def _fake_preflight(db, day):
        return {"preflight": {"headline": "上午两场连轴", "checklist": ["提前打印材料"]},
                "final_reply": "今天是高压日，注意留白。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda *a: _fake_preflight)
    pushes: list[str] = []
    monkeypatch.setattr(workflows, "_fire_push", lambda text: pushes.append(text))
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_morning())
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()
    finally:
        workflows.unsubscribe(q)
    assert pushes == []
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and messages[0].content == "今天是高压日，注意留白。"
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "preflight"


def test_run_morning_real_preflight_smoke(monkeypatch):
    # 晨间真链路冒烟（2026-10-21 复查批 C 增，Track B 批 6 迁移）：不替身
    # _load_agent——跑真 run_preflight（MemorySaver 零 config 崩溃曾被替身掩盖的
    # 同款教训），仅替身 services.llm.call_llm 截获提示词；strategist 子图读库
    # 走 cf/disp 模块级 SessionLocal，须一并替到内存库（防读真 dev 库）。
    # 断言：morning 场景 deep_context 键真实进载荷、cron 单次 LLM、卡照建
    _use_test_db(monkeypatch)
    import agents.strategist.context_fetcher as cf
    import agents.strategist.dispatcher as disp
    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    _activate_model()
    _add_task("季度汇报", priority=1,
              start_time=datetime.now().replace(hour=10, minute=0, second=0, microsecond=0))
    _add_task("预约体检", priority=1)  # 未排期高优 → inbox_high 键
    captured: list[list[dict]] = []

    def _fake_call_llm(messages, **kw):
        captured.append(messages)
        return json.dumps({"strategy": "## 上午重点：季度汇报\n上午先攻季度汇报。",
                           "checklist": ["过一遍材料"], "action_items": [],
                           "memory_updates": []}, ensure_ascii=False)

    monkeypatch.setattr("services.llm.call_llm", _fake_call_llm)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_morning())
        event = q.get_nowait()
        day = date.today()
        assert event["type"] == "morning"
        assert event["headline"] == f"{day.month}月{day.day}日 晨间启航"
    finally:
        workflows.unsubscribe(q)
    assert len(captured) == 1  # cron 轮单次 LLM（classifier 直打零调用）
    user_content = captured[0][1]["content"]
    for key in ("today", "inbox_high", "load_summary", "weather", "now"):
        assert f'"{key}"' in user_content
    assert "季度汇报" in user_content and "预约体检" in user_content
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "preflight"
        assert proposals[0].payload["checklist"] == ["过一遍材料"]


# —— runner：会前准备（prep，2026-10-20 增） ——————————————————————————————————


def test_run_prep_builds_proposal_and_broadcast(monkeypatch):
    # 明日有重要会议 → 替身 run_meeting_prep 返回清单：kind=prep 卡 + 消息 + 广播
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_prep(db, day):
        assert day == date.today() + timedelta(days=1)  # 目标日=明日
        return {"meetings": [{"task_id": "t1", "title": "评审会",
                              "start": "2026-10-21T14:00:00",
                              "headline": "确认评审范围", "checklist": ["过一遍需求文档"]}],
                "final_reply": "明日评审会已备好准备清单。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda *a: _fake_prep)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_prep())
        event = q.get_nowait()
        assert event["type"] == "prep" and "1 场" in event["headline"]
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and messages[0].content == "明日评审会已备好准备清单。"
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "prep"
        assert proposals[0].payload["meetings"][0]["checklist"] == ["过一遍需求文档"]


def test_run_prep_silent_when_no_meetings(monkeypatch):
    # 明日无重要会议（替身返回空 meetings）→ 零卡零消息零广播
    _use_test_db(monkeypatch)
    _activate_model()
    monkeypatch.setattr(workflows, "_load_agent",
                        lambda *a: lambda db, day: {"meetings": [], "final_reply": ""})
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_prep())
        assert q.qsize() == 0
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []
        assert ai_data.list_proposals(db) == []


def test_run_prep_skips_without_active_model(monkeypatch):
    # 零激活模型：静默跳过（降级矩阵），不调 agent
    _use_test_db(monkeypatch)
    called = []
    monkeypatch.setattr(workflows, "_load_agent", lambda *a: lambda db, day: called.append(1))
    asyncio.run(workflows._run_prep())
    assert called == []
    with TestingSessionLocal() as db:
        assert ai_data.list_proposals(db) == []


# —— 周报自动生成（2026-11-07 统计 120 分档）—————————————————————————————


def test_claim_weekly_report_weekday_and_time_gate(monkeypatch):
    """周级谓词：仅周日晚过报点认领；周一/周日报点前/当日二次都不认领。"""
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        get_settings(db).nightly_start_min = 1080  # 18:00
        db.commit()
    sunday = datetime(2026, 11, 8, 21, 0)  # 周日（weekday=6）
    monday = datetime(2026, 11, 9, 21, 0)  # 周一（weekday=0）
    assert monday.weekday() == 0 and sunday.weekday() == 6  # 语义锚定
    with TestingSessionLocal() as db:
        assert workflows._claim_weekly_report(db, monday) is False
        assert workflows._claim_weekly_report(db, sunday.replace(hour=17, minute=59)) is False
        assert workflows._claim_weekly_report(db, sunday) is True
        assert workflows._claim_weekly_report(db, sunday.replace(hour=23, minute=0)) is False


def test_claim_weekly_report_disabled_no_row(monkeypatch):
    """开关关 = 永不认领不落 workflow_runs 行（同四流开关语义）。"""
    _use_test_db(monkeypatch)
    with TestingSessionLocal() as db:
        get_settings(db).weekly_report_enabled = False
        db.commit()
    sunday = datetime(2026, 11, 8, 21, 0)
    with TestingSessionLocal() as db:
        assert workflows._claim_weekly_report(db, sunday) is False
        assert list(db.scalars(select(WorkflowRun))) == []


def test_run_weekly_report_builds_message_and_broadcast(monkeypatch):
    """runner：激活 + 替身 generate_report → 全文消息 + weekly_report 广播
    （headline 取【总评】行）+ 渠道推送全文。"""
    _use_test_db(monkeypatch)
    _activate_model()
    fake = SimpleNamespace(id="r1", content="【总评】本周节奏平稳。\n\n## 概述\n\n正文。")
    monkeypatch.setattr(workflows.stats_service, "generate_report",
                        lambda db, period: fake)
    pushes: list[str] = []
    monkeypatch.setattr(workflows, "_fire_push", lambda text: pushes.append(text))
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_weekly_report())
        event = q.get_nowait()
        assert event["type"] == "weekly_report"
        assert event["headline"] == "本周节奏平稳。"
        assert event["report_id"] == "r1"
    finally:
        workflows.unsubscribe(q)
    assert pushes == [fake.content]
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and messages[0].content == fake.content


def test_run_weekly_report_catchup_silent(monkeypatch):
    """补账轮静默：留档消息照落，广播与渠道推送全跳（准点通知补账留痕口径）。"""
    _use_test_db(monkeypatch)
    _activate_model()
    fake = SimpleNamespace(id="r2", content="【总评】补账轮。\n\n## 概述\n\nx")
    monkeypatch.setattr(workflows.stats_service, "generate_report",
                        lambda db, period: fake)
    monkeypatch.setattr(workflows, "_catchup_silent", lambda report_min, now=None: True)
    pushes: list[str] = []
    monkeypatch.setattr(workflows, "_fire_push", lambda text: pushes.append(text))
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_weekly_report())
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()
    finally:
        workflows.unsubscribe(q)
    assert pushes == []
    with TestingSessionLocal() as db:
        assert len(ai_data.list_messages(db)) == 1


def test_run_weekly_report_skips_without_model(monkeypatch):
    """零激活：静默跳过（降级矩阵）——generate_report 不被调、零消息零留档。"""
    _use_test_db(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(workflows.stats_service, "generate_report",
                        lambda db, period: calls.append(period))
    asyncio.run(workflows._run_weekly_report())
    assert calls == []
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []


def test_run_nightly_collects_and_expires(monkeypatch):
    _use_test_db(monkeypatch)
    _add_task("碎片A", priority=3, duration_mins=10, sort_order=0)
    _add_task("碎片B", priority=3, duration_mins=10, sort_order=1)
    # 造一条过期未决建议（2 天前创建）
    with TestingSessionLocal() as db:
        stale = ai_data.create_proposal(db, kind="schedule", payload={})
        from models import Proposal

        db.get(Proposal, stale.id).created_at = datetime.now() - timedelta(days=2)
        db.commit()
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_nightly())
        event = q.get_nowait()
        assert event["type"] == "nightly" and event["proposal_id"]
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and "预排" in messages[0].content
        from models import Proposal

        assert db.get(Proposal, stale.id).outcome == "expired"  # 旧建议已过期；
        # 夜间草案本身保持待处理（待晨间确认），是唯一未决行
        assert len(ai_data.list_proposals(db, unresolved_only=True)) == 1


def test_run_nightly_catchup_round_silent(monkeypatch):
    # 补账轮静默（夜间版）：碎片草案卡与消息照落，nightly 广播与渠道推送全跳
    # ——20 点后开机的三连 toast 收敛（prep/hardtask 同判同款，不另立用例）
    _use_test_db(monkeypatch)
    monkeypatch.setattr(workflows, "_catchup_silent", lambda report_min, now=None: True)
    _add_task("碎片A", priority=3, duration_mins=10, sort_order=0)
    _add_task("碎片B", priority=3, duration_mins=10, sort_order=1)
    pushes: list[str] = []
    monkeypatch.setattr(workflows, "_fire_push", lambda text: pushes.append(text))
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_nightly())
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()
    finally:
        workflows.unsubscribe(q)
    assert pushes == []
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1 and "预排" in messages[0].content
        assert len(ai_data.list_proposals(db, unresolved_only=True)) == 1


def test_run_nightly_eve_brief_builds_preflight(monkeypatch):
    # 激活 + 替身 run_preflight（明日）→ 🌙 前夜综述消息 + preflight 卡
    # （不足 2 条待办 → 零预排卡；学习段替身返回字符串，logger 原样消费）
    _use_test_db(monkeypatch)
    _activate_model()

    def fake_load(module, name):
        if name == "run_preflight":
            return lambda db, day: {"preflight": {"headline": "明早两场连轴",
                                                  "checklist": ["今晚打印材料"]},
                                    "final_reply": "为明天的高压上午做准备。"}
        return lambda db: "学习替身"

    monkeypatch.setattr(workflows, "_load_agent", fake_load)
    asyncio.run(workflows._run_nightly())
    with TestingSessionLocal() as db:
        messages = ai_data.list_messages(db)
        assert len(messages) == 1
        assert messages[0].content == "🌙 前夜综述：为明天的高压上午做准备。"
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "preflight"
        assert proposals[0].payload["headline"] == "明早两场连轴"


# —— 数据采集 runner（替身 _load_agent 驱动，不真 import 手写区） ——


def _activate_model() -> None:
    import uuid as _uuid

    from models import ModelProvider

    with TestingSessionLocal() as db:
        db.add(ModelProvider(name=f"m-{_uuid.uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def test_run_ingest_skips_without_model(monkeypatch):
    # 零激活（不造模型行）：静默跳过，不落任何消息
    _use_test_db(monkeypatch)
    asyncio.run(workflows._run_ingest())
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []


def test_run_ingest_skips_when_overloaded(monkeypatch):
    # 过载日采集不拦（2026-11-04 用户拍板口径修订：采集是数据同步不是排期——
    # 熔断闸移除，过载引导归 heartbeat 提醒）：负荷率 0.9 照常跑采集
    _use_test_db(monkeypatch)
    _activate_model()
    monkeypatch.setattr(workflows, "load_ratio", lambda db, day: 0.9)
    called = []

    def _fake_ingest(db, day):
        called.append(day)
        return {}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    asyncio.run(workflows._run_ingest())
    assert called != []  # 采集照常执行（旧口径断言「未被调」已废）
    assert workflows.ingest_in_flight() is False  # 在途闸已清
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []  # 空产物零消息（旧口径行为兼容）


def test_run_ingest_runs_at_exact_threshold(monkeypatch):
    # 边界：恰好 80% 不拦（>0.8 才熔断）——采集照跑
    _use_test_db(monkeypatch)
    _activate_model()
    monkeypatch.setattr(workflows, "load_ratio", lambda db, day: 0.8)

    def _fake_ingest(db, day):
        return {"draft_plan": [], "final_reply": ""}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    asyncio.run(workflows._run_ingest())
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []  # 综述不落消息（headline 随广播）


def test_run_ingest_builds_schedule_proposal(monkeypatch):
    # 替身 run_ingest 返回非空草案：schedule 建议卡 + ingest 广播（综述不落聊天
    # 消息——2026-10-25 功能边界：数据源页是采集建议唯一视图）
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {"draft_plan": [{"task_id": None, "title": "写周报",
                                "start": "2026-10-15T09:00:00", "duration": 30}],
                "final_reply": "从接入源提炼出 1 项待办。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_ingest())
        event = q.get_nowait()
        assert event["type"] == "ingest" and event["proposal_id"]
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        # 综述不再落聊天消息（对话框零采集痕迹）；headline 前 60 字随广播可见
        assert ai_data.list_messages(db) == []
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "schedule"
        assert proposals[0].payload["draft_plan"][0]["title"] == "写周报"
        # from_ingest 标记（2026-10-23 数据源页）：采集卡 7 天审阅窗 + 页面过滤依据
        assert proposals[0].payload["from_ingest"] is True


def test_run_ingest_empty_draft_no_proposal(monkeypatch):
    # 空草案不建建议卡；reply 空走兜底文案
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {"draft_plan": [], "final_reply": ""}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_ingest())
        event = q.get_nowait()
        assert event["type"] == "ingest" and event["proposal_id"] is None
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        # 空草案轮：兜底文案只随广播 headline（对话框与消息表零痕迹）
        assert ai_data.list_messages(db) == []
        assert ai_data.list_proposals(db, unresolved_only=True) == []


def test_run_ingest_silent_round_no_message_no_broadcast(monkeypatch):
    # 静默轮（2026-10-23 频率档位配套）：run_ingest 返回 silent 键 → 不落消息
    # 不广播（提频后无新数据的轮次不打扰）；认领行已落由谓词测试覆盖
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {"silent": True, "draft_plan": [], "inbox_items": [],
                "evidence": [], "final_reply": ""}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_ingest())
        assert q.empty()  # 无广播
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []
    assert workflows.ingest_in_flight() is False  # 在途闸照清


def test_run_ingest_manual_bypasses_catchup_silence(monkeypatch):
    """手动轮豁免补账静默（2026-11-04 真机批）：距报点再远（补账静默必中）也
    照常广播——真机 15:30 手动刷新被判静默轮跳过广播，直落收件箱 4 条任务
    前端收不到 ingest 事件不可见。"""
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {"draft_plan": [], "inbox_items": [], "evidence": [],
                "final_reply": "高可信源直通完成。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    # 补账静默恒 True（模拟距报点 >10min）——手动轮必须绕过它
    monkeypatch.setattr(workflows, "_catchup_silent", lambda report_min, now=None: True)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_ingest(manual=True))
        event = q.get_nowait()
        assert event["type"] == "ingest"  # 广播照发
    finally:
        workflows.unsubscribe(q)
    assert workflows.ingest_in_flight() is False


# —— 采集提炼配套：inbox 卡 / evidence / 自动收录 / 在途闸 / 夜间挂接 ——————————


def test_run_ingest_builds_inbox_card(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {"inbox_items": [
            {"title": "买牛奶", "duration": 15, "notes": "来自购物清单邮件"},
            {"title": "回信给张三"},
            {"title": ""},  # 无标题条目被丢
        ], "final_reply": "提炼出收件箱清单。"}

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_ingest())
        event = q.get_nowait()
        assert event["type"] == "ingest" and event["proposal_id"]
    finally:
        workflows.unsubscribe(q)
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert len(proposals) == 1 and proposals[0].kind == "inbox"
        assert [it["title"] for it in proposals[0].payload["items"]] == ["买牛奶", "回信给张三"]
        # 综述不落聊天消息（2026-10-25 功能边界）：数据源页是采集建议唯一视图
        assert ai_data.list_messages(db) == []


def test_run_ingest_evidence_into_schedule_payload(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()

    def _fake_ingest(db, day):
        return {
            "draft_plan": [{"task_id": None, "title": "周会", "start": "2026-10-15T10:00:00",
                            "duration": 60}],
            "evidence": [{"idx": 0, "source": "Gmail", "source_key": "ca-1",
                          "item_id": "m1", "title": "周会改期", "time": None,
                          "fingerprint": "f" * 40}],
            "final_reply": "1 项排期。",
        }

    monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
    asyncio.run(workflows._run_ingest())
    with TestingSessionLocal() as db:
        proposals = ai_data.list_proposals(db, unresolved_only=True)
        assert proposals[0].kind == "schedule"
        assert proposals[0].payload["evidence"][0]["source_key"] == "ca-1"


def test_ingest_gate_blocks_reentry(monkeypatch):
    _use_test_db(monkeypatch)
    _activate_model()

    assert workflows.try_begin_ingest() is True
    try:
        assert workflows.ingest_in_flight() is True
        assert workflows.try_begin_ingest() is False  # 占用中不可再占

        def _fake_ingest(db, day):
            return {"draft_plan": [], "final_reply": "x"}

        monkeypatch.setattr(workflows, "_load_agent", lambda module, attr: _fake_ingest)
        asyncio.run(workflows._run_ingest())  # 闸占用中：静默让位，不落消息
        with TestingSessionLocal() as db:
            assert ai_data.list_messages(db) == []
    finally:
        workflows._end_ingest()
    assert workflows.ingest_in_flight() is False


def test_run_ingest_gate_cleared_on_skip(monkeypatch):
    # ai_enabled 关 / 零激活等早退路径也必须清闸（finally 兜底），否则一次跳过永久 409
    _use_test_db(monkeypatch)
    asyncio.run(workflows._run_ingest())  # 零激活 → 静默跳过
    assert workflows.ingest_in_flight() is False
    assert workflows.try_begin_ingest() is True
    workflows._end_ingest()


def test_run_nightly_purges_and_applies_trust(monkeypatch):
    _use_test_db(monkeypatch)
    _reset_idle_state(monkeypatch, last_activity=None)
    calls: list[str] = []
    monkeypatch.setattr("services.data_ingest.purge_stale_items",
                        lambda db, **kw: calls.append("purge") or 0)
    monkeypatch.setattr("services.data_ingest.apply_trust_policy",
                        lambda db: calls.append("trust") or {"paused": [], "by_source": []})
    asyncio.run(workflows._run_nightly())
    assert calls == ["purge", "trust"]


def test_run_nightly_reflection_failure_does_not_break_rest(monkeypatch):
    # 反思段 LLM 故障不再连坐同夜后续（2026-10-21 复查批 C 修）：前夜综述照落、
    # purge/trust 照跑、nightly 广播照出（旧版异常穿透跳过全部收尾）
    _use_test_db(monkeypatch)
    _activate_model()

    def fake_load(module, name):
        if name == "run_reflection":
            def _boom(db):
                raise RuntimeError("LLM 网络故障模拟")

            return _boom
        if name == "run_preflight":
            return lambda db, day: {"preflight": {"headline": "明早平稳", "checklist": []},
                                    "final_reply": "明早节奏平稳。"}
        raise AssertionError(name)

    monkeypatch.setattr(workflows, "_load_agent", fake_load)
    calls: list[str] = []
    monkeypatch.setattr("services.data_ingest.purge_stale_items",
                        lambda db, **kw: calls.append("purge") or 0)
    monkeypatch.setattr("services.data_ingest.apply_trust_policy",
                        lambda db: calls.append("trust") or {"paused": [], "by_source": []})
    q = workflows.subscribe()
    try:
        asyncio.run(workflows._run_nightly())
        event = q.get_nowait()
        assert event["type"] == "nightly"
    finally:
        workflows.unsubscribe(q)
    assert calls == ["purge", "trust"]
    with TestingSessionLocal() as db:
        assert any("前夜综述" in m.content for m in ai_data.list_messages(db))


def test_run_prep_low_trust_tier_skips(monkeypatch):
    """低介入档跳过会前主动清单（优化批 5 信任闸）：近 14 天采纳率 <30% 时
    prep 卡不再生成——明日会议提醒照常走 Reminder 通道（低介入≠静默）。"""
    import services.ai_data as ai_data_mod

    _use_test_db(monkeypatch)
    _activate_model()
    with TestingSessionLocal() as db:
        for i in range(10):  # 10 样本仅 2 采纳 = 20% < 30%
            row = ai_data_mod.create_proposal(db, kind="schedule", payload={})
            ai_data_mod.record_proposal_outcome(
                db, row.id, "accepted" if i < 2 else "rejected")
    called: list = []

    def fake_prep(db, day):
        called.append(day)
        return {"meetings": [], "final_reply": "x"}

    monkeypatch.setattr(workflows, "_load_agent", lambda m, a: fake_prep)
    asyncio.run(workflows._run_prep())
    assert called == []  # 低介入档：主动清单未生成


def test_collect_priority_plan_uses_history_duration():
    """夜间预排个性化估时（优化批 5）：未估时条目先查同类历史均值——旧版
    一刀切 30 分钟，预排时长从此贴实际（同批槽位因此更准）。"""
    from models import Task

    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        db.add(Task(title="季度汇报", duration_mins=90,
                    done_at=datetime.now() - timedelta(days=2)))
        db.commit()
    _add_task("季度汇报冲刺", priority=1, duration_mins=None, sort_order=0)
    _add_task("另一件高优", priority=1, duration_mins=30, sort_order=1)
    with TestingSessionLocal() as db:
        payload = workflows._collect_priority_plan(db, datetime.now())
    by_title = {i["title"]: i["duration"] for i in payload["draft_plan"]}
    # 「季度汇报冲刺」与已完成的「季度汇报」difflib ≥0.6 → 历史均值 90 覆盖默认 30
    assert by_title["季度汇报冲刺"] == 90
    assert by_title["另一件高优"] == 30  # 无同类历史照旧
