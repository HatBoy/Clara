"""批 3：精力画像 / AI 用量 / 统计报告 PNG 导出。"""

import base64
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from conftest import TestingSessionLocal
from models import AiUsageEvent, BehaviorEvent, Task
from schemas import MemoryRuleCreate
from services import llm, llm_usage
from services.ai_data import create_memory_rule
from services.stats import energy_profile

PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
).decode()


def _event(db, task_id: str, kind: str, at: datetime) -> None:
    db.add(BehaviorEvent(task_id=task_id, kind=kind, prev_start=at,
                         created_at=datetime.now()))


def _make_task(db) -> str:
    t = Task(title="样本")
    db.add(t)
    db.flush()
    return t.id


def test_energy_profile_collecting_when_sparse():
    with TestingSessionLocal() as db:
        tid = _make_task(db)
        _event(db, tid, "done", datetime(2026, 11, 10, 9, 0))
        db.commit()
        out = energy_profile(db, days=30)
    assert out["status"] == "collecting"
    assert out["peak"] is None and out["low"] is None
    assert out["sample_count"] == 1


def test_energy_profile_peak_low_and_rhythm_rules():
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        tid = _make_task(db)
        for i in range(5):
            _event(db, tid, "done", (base - timedelta(days=i)).replace(hour=9))
        for i in range(5):
            _event(db, tid, "reschedule", (base - timedelta(days=i)).replace(hour=20))
        db.commit()
        create_memory_rule(db, MemoryRuleCreate(
            category="作息规律", content="上午效率最高"))
        out = energy_profile(db, days=30)
    assert out["status"] == "ok"
    assert out["peak"]["hour"] == 9 and out["peak"]["score"] == 100
    assert out["low"]["hour"] == 20 and out["low"]["score"] == 0
    assert any("上午效率最高" in r["content"] for r in out["profile"]["rules"])


def test_energy_low_tiebreak_takes_better_sampled_hour():
    # 2026-11-17 二轮批 1：低谷同分 tie-break 取样本多者——旧版取样本最少，
    # 把证据最弱的时刻报给用户；abandon 无 prev_start 不落桶（删除操作时刻
    # 与"该时段爱拖延"无关）
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        tid = _make_task(db)
        for i in range(6):
            _event(db, tid, "reschedule", (base - timedelta(days=i)).replace(hour=14))
        for i in range(3):
            _event(db, tid, "reschedule", (base - timedelta(days=i)).replace(hour=16))
        for i in range(5):
            _event(db, tid, "done", (base - timedelta(days=i)).replace(hour=9))
        # abandon 无 prev_start（删除待办）：不落桶（旧版以 created_at 即当下
        # 时刻落桶，混入拖延口径）
        db.add(BehaviorEvent(task_id=tid, kind="abandon", prev_start=None,
                             created_at=base.replace(hour=10)))
        db.commit()
        out = energy_profile(db, days=30)
    assert out["status"] == "ok"
    assert out["low"]["hour"] == 14  # 14 与 16 同为 0 分，14 样本 6 > 16 样本 3
    assert all(s["hour"] != 10 for s in out["slots"])


def test_energy_peak_low_shapes():
    # 2026-11-17 二轮批 1：排期链注入口的单源峰谷——与 energy_profile 同口径，
    # 样本不足返回 None（注入点零变化）；低谷与高峰同小时时无 low 键
    from services.stats import energy_peak_low

    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with TestingSessionLocal() as db:
        tid = _make_task(db)
        for i in range(5):
            _event(db, tid, "done", (base - timedelta(days=i)).replace(hour=9))
        for i in range(5):
            _event(db, tid, "reschedule", (base - timedelta(days=i)).replace(hour=20))
        db.commit()
        out = energy_peak_low(db, days=30)
    assert out == {"peak": {"hour": 9, "score": 100},
                   "low": {"hour": 20, "score": 0}, "sample": 10}
    with TestingSessionLocal() as db:  # 清空遥测 → collecting → None
        db.query(BehaviorEvent).delete()
        db.commit()
        assert energy_peak_low(db, days=30) is None


def test_energy_endpoint_validates_days(client: TestClient):
    assert client.get("/api/stats/energy-profile", params={"days": 7}).status_code == 200
    assert client.get("/api/stats/energy-profile", params={"days": 15}).status_code == 422


def test_call_llm_records_usage(monkeypatch):
    class FakeClient:
        model = "deepseek/deepseek-chat"

        def invoke(self, messages):
            return AIMessage(content="ok", usage_metadata={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    monkeypatch.setattr(llm, "init_llm", lambda **kw: FakeClient())
    assert llm.call_llm([{"role": "user", "content": "hi"}]) == "ok"
    with TestingSessionLocal() as db:
        rows = list(db.query(AiUsageEvent))
    assert len(rows) == 1
    row = rows[0]
    assert row.total_tokens == 15 and row.usage_source == "provider"
    assert row.model_name == "deepseek/deepseek-chat"


def test_call_llm_records_unknown_usage(monkeypatch):
    class FakeClient:
        model = "custom/model"

        def invoke(self, messages):
            return AIMessage(content="ok")

    monkeypatch.setattr(llm, "init_llm", lambda **kw: FakeClient())
    llm.call_llm([{"role": "user", "content": "hi"}])
    with TestingSessionLocal() as db:
        row = db.query(AiUsageEvent).first()
    assert row is not None
    assert row.total_tokens is None and row.usage_source == "unknown"
    assert row.cost_known is False


def test_usage_zero_tokens_still_provider_sourced():
    # 2026-11-17 二轮批 1：0 是合法 token 值——旧版 or 链把 prompt_tokens=0
    # 当缺失回落另一键得 None，usage_source 误标 unknown
    from services.llm_usage import record_usage

    record_usage(model_name="m", feature="qa",
                 usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    with TestingSessionLocal() as db:
        row = db.query(AiUsageEvent).first()
    assert row is not None
    assert row.total_tokens == 0 and row.usage_source == "provider"
    assert row.prompt_tokens == 0 and row.completion_tokens == 0


def test_usage_summary_aggregates(client: TestClient):
    now = datetime.now()
    with TestingSessionLocal() as db:
        db.add(AiUsageEvent(model_name="m1", role="main", feature="router",
                            prompt_tokens=10, completion_tokens=5, total_tokens=15,
                            cost_usd=0.001, cost_known=True,
                            usage_source="provider", created_at=now))
        db.add(AiUsageEvent(model_name="m2", role="aux", feature="classify",
                            total_tokens=None, usage_source="unknown",
                            cost_known=False, created_at=now))
        db.commit()
    res = client.get("/api/stats/ai-usage", params={"days": 30})
    assert res.status_code == 200
    data = res.json()
    assert data["calls"] == 2
    assert data["total_tokens"] == 15
    assert data["unknown_usage_calls"] == 1
    assert data["unknown_cost_calls"] == 1
    assert {m["model_name"] for m in data["by_model"]} == {"m1", "m2"}
    assert {f["feature"] for f in data["by_feature"]} == {"router", "classify"}
    assert len(data["by_day"]) == 1


def test_usage_record_failure_is_swallowed(monkeypatch):
    """遥测写库异常只记 warning，不向调用方抛出。"""
    import database

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "SessionLocal", boom)
    llm_usage.record_usage(model_name="x", usage={"total_tokens": 3})  # 不应抛


def test_export_stats_png_writes_file_and_validates(client: TestClient, tmp_path):
    dest = tmp_path / "report.png"
    res = client.post("/api/stats/export-image",
                      json={"path": str(dest), "data_base64": PNG_1PX})
    assert res.status_code == 200, res.text
    assert dest.is_file() and dest.read_bytes().startswith(b"\x89PNG")

    bad_ext = tmp_path / "report.jpg"
    assert client.post("/api/stats/export-image",
                       json={"path": str(bad_ext), "data_base64": PNG_1PX}
                       ).status_code == 422
    assert client.post("/api/stats/export-image",
                       json={"path": str(tmp_path / "x.png"), "data_base64": "not-base64"}
                       ).status_code == 422
