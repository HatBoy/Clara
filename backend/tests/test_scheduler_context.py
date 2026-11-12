"""context 节点聚合测试（2026-10-22 批 5 建文件）：恢复窗纯函数 + 节点级
busy 扩展。"""

from datetime import datetime

from agents.scheduler.context import _recovery_windows, context_node


def test_recovery_windows_pure():
    # 标题命中恢复类正则 → 活动结束次晨 0:00-10:00；普通任务/坏行跳过不炸
    meta = [
        {"title": "城市马拉松", "start": datetime(2026, 10, 24, 7),
         "end": datetime(2026, 10, 24, 12)},
        {"title": "公司团建晚宴", "start": datetime(2026, 10, 25, 18),
         "end": datetime(2026, 10, 25, 21)},
        {"title": "写作时间", "start": datetime(2026, 10, 24, 9),
         "end": datetime(2026, 10, 24, 10)},
    ]
    assert _recovery_windows(meta) == [
        (datetime(2026, 10, 25, 0), datetime(2026, 10, 25, 10)),
        (datetime(2026, 10, 26, 0), datetime(2026, 10, 26, 10)),
    ]
    assert _recovery_windows([]) == []
    assert _recovery_windows([{"title": None, "end": None}]) == []


def test_context_node_appends_recovery_busy(monkeypatch):
    # 节点级：busy_meta 命中 → 恢复窗并入 ctx["busy"]（finder/adjudicator 同源生效）
    from conftest import TestingSessionLocal

    monkeypatch.setattr("agents.scheduler.context.SessionLocal", TestingSessionLocal)
    out = context_node({
        "context": {"busy": [], "busy_meta": [
            {"title": "爬山", "start": datetime(2026, 10, 24, 8),
             "end": datetime(2026, 10, 24, 12)}]},
        "agent_entries": [], "conflict_queue": []})
    assert out["context"]["busy"] == [
        (datetime(2026, 10, 25, 0), datetime(2026, 10, 25, 10))]
    # 无命中 → busy 原样（浅拷贝不追加）
    out2 = context_node({
        "context": {"busy": [], "busy_meta": [
            {"title": "写作", "start": datetime(2026, 10, 24, 8),
             "end": datetime(2026, 10, 24, 12)}]},
        "agent_entries": [], "conflict_queue": []})
    assert out2["context"]["busy"] == []
