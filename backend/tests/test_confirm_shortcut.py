"""草案/删除确认短路（confirm_shortcut 图第一跳，2026-10-23 聊天通讯渠道配套）。

远程渠道无卡片按钮——草案/删除卡的确认走对话式消费，桌面同享（与点卡片
采纳等价）。覆盖：active_draft / active_delete 消息锚点判定（活跃 / 顶替
失活 / 已决 / 非目标 kind）；短路三分支（确认走 applied 出口落库 / 拒绝
declined / 不命中放行）；删除确认节点内执行与拒绝；调整语不误吞；真图端
到端（短路轮零 LLM 秒回直达 END）。"""

from datetime import datetime, timedelta

from agents.graph import build_graph
from agents.nodes import confirm_shortcut
from conftest import TestingSessionLocal
from schemas import TaskCreate
from services import ai_data
from services.chat_turn import finalize_turn
from services.tasks import create_task, list_tasks


def _tomorrow(hour: int = 10) -> str:
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _make_draft(entries: list[dict] | None = None) -> str:
    """预置一张活跃草案卡（schedule 行 + assistant 消息锚点），返回行 id。"""
    entries = entries if entries is not None else [
        {"title": "体检", "start": _tomorrow(10), "duration": 30},
        {"title": "取快递", "start": _tomorrow(16), "duration": 15},
    ]
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="schedule",
                                      payload={"draft_plan": entries})
        ai_data.append_message(db, role="user", content="把待办排一下")
        ai_data.append_message(db, role="assistant", content="已出草案",
                               proposal_id=row.id)
        return row.id


def _make_delete(task_ids: list[tuple[str, str]]) -> str:
    """预置一张活跃删除确认卡（delete 行 + 锚点），入参 (task_id, title) 对。"""
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="delete", payload={
            "tasks": [{"task_id": tid, "title": title}
                      for tid, title in task_ids]})
        ai_data.append_message(db, role="user", content="删掉体检")
        ai_data.append_message(db, role="assistant", content="请确认删除",
                               proposal_id=row.id)
        return row.id


def _outcome(pid: str) -> str | None:
    with TestingSessionLocal() as db:
        row = ai_data.get_proposal(db, pid)
        return row.outcome if row else "行缺失"


# —— 锚点判定 ——————————————————————————————————————————————————————————————


def test_active_draft_anchor_lifecycle():
    pid = _make_draft()
    with TestingSessionLocal() as db:
        snap = ai_data.active_draft(db)
    assert snap["proposal_id"] == pid
    assert len(snap["draft_plan"]) == 2

    # 穿插闲聊轮（无锚点消息）不影响锚定
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="在吗")
        ai_data.append_message(db, role="assistant", content="在的")
        assert ai_data.active_draft(db) is not None

    # 下一张卡顶替锚点 → 旧草案失活
    with TestingSessionLocal() as db:
        new = ai_data.create_proposal(db, kind="question", payload={
            "kind": "no_time", "entries": [{"title": "体检"}], "ask_text": "几点？"})
        ai_data.append_message(db, role="assistant", content="几点？",
                               proposal_id=new.id)
        assert ai_data.active_draft(db) is None  # 锚点已是 question 行
        assert ai_data.active_pending(db) is not None

    # 已决（accepted）不活跃
    pid2 = _make_draft()
    with TestingSessionLocal() as db:
        ai_data.record_proposal_outcome(db, pid2, "accepted")
        assert ai_data.active_draft(db) is None


def test_active_delete_anchor():
    pid = _make_delete([("t-x", "旧任务")])
    with TestingSessionLocal() as db:
        snap = ai_data.active_delete(db)
    assert snap["proposal_id"] == pid
    assert snap["tasks"][0]["task_id"] == "t-x"
    # delete 活跃时 draft 判定互斥（锚点唯一）
    with TestingSessionLocal() as db:
        assert ai_data.active_draft(db) is None


# —— 短路三分支 ————————————————————————————————————————————————————————————


def test_shortcut_confirm_applies_and_finalizes():
    pid = _make_draft()
    ret = confirm_shortcut({"user_input": "可以"})
    assert ret["resolved_proposal"] == {"proposal_id": pid, "outcome": "accepted"}
    assert len(ret["applied_plan"]) == 2
    assert "已安排" in ret["final_reply"] and "体检" in ret["final_reply"]

    # 走壳出口：任务真落库 + 行 accepted + assistant 消息锚定
    with TestingSessionLocal() as db:
        events = finalize_turn(db, ret)
    assert any(e["type"] == "applied" for e in events)
    with TestingSessionLocal() as db:
        titles = {t.title for t in list_tasks(db, start=datetime.now(),
                                              end=datetime.now() + timedelta(days=2))}
    assert {"体检", "取快递"} <= titles
    assert _outcome(pid) == "accepted"


def test_shortcut_decline():
    pid = _make_draft()
    ret = confirm_shortcut({"user_input": "先不用了"})
    assert "applied_plan" not in ret  # 拒绝不落库
    assert ret["resolved_proposal"]["outcome"] == "declined"
    with TestingSessionLocal() as db:
        finalize_turn(db, ret)
    assert _outcome(pid) == "declined"
    with TestingSessionLocal() as db:
        assert list_tasks(db, start=datetime.now(),
                          end=datetime.now() + timedelta(days=2)) == []


def test_shortcut_delete_confirmed():
    with TestingSessionLocal() as db:
        t1 = create_task(db, TaskCreate(title="旧复盘"))
        t2 = create_task(db, TaskCreate(title="过期提醒"))
    pid = _make_delete([(t1.id, "旧复盘"), (t2.id, "过期提醒")])

    ret = confirm_shortcut({"user_input": "确认删除"})
    assert "已删除：旧复盘、过期提醒" in ret["final_reply"]
    with TestingSessionLocal() as db:
        finalize_turn(db, ret)
        ids = {t.id for t in list_tasks(db, unscheduled=True)}
    assert t1.id not in ids and t2.id not in ids  # 节点内已真删
    assert _outcome(pid) == "accepted"


def test_shortcut_delete_declined_keeps_tasks():
    with TestingSessionLocal() as db:
        t1 = create_task(db, TaskCreate(title="保留任务"))
    pid = _make_delete([(t1.id, "保留任务")])

    ret = confirm_shortcut({"user_input": "先不删了"})
    assert ret["resolved_proposal"]["outcome"] == "declined"
    with TestingSessionLocal() as db:
        finalize_turn(db, ret)
        assert any(t.id == t1.id for t in list_tasks(db, unscheduled=True))
    assert _outcome(pid) == "declined"


def test_shortcut_no_match_passthrough():
    pid = _make_draft()
    # 调整语带时间词——不误吞，留给排期管道出新草案
    assert confirm_shortcut({"user_input": "可以，改成明天下午3点"}) == {}
    # 无关话题不短路
    assert confirm_shortcut({"user_input": "今天天气怎么样"}) == {}
    # 无活跃卡时确认词不短路
    with TestingSessionLocal() as db:
        ai_data.record_proposal_outcome(db, pid, "ignored")
        assert ai_data.active_draft(db) is None
    assert confirm_shortcut({"user_input": "可以"}) == {}


# —— 真图端到端（短路轮零 LLM，不需要替身）——————————————————————————————


def test_graph_shortcut_end_to_end():
    pid = _make_draft(entries=[{"title": "爬山", "start": _tomorrow(9),
                                "duration": 180}])
    final = build_graph().invoke({"user_input": "确认", "memory_rules": []})
    assert final["final_reply"].startswith("已安排")
    assert final["applied_plan"][0]["title"] == "爬山"
    assert final["resolved_proposal"]["proposal_id"] == pid
    # 图只产出口键，落库在壳——补 finalize 后任务真写入（完整链路语义）
    with TestingSessionLocal() as db:
        finalize_turn(db, final)
        assert any(t.title == "爬山" for t in list_tasks(
            db, start=datetime.now(), end=datetime.now() + timedelta(days=2)))
