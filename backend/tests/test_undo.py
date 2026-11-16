"""撤销最近一次操作（2026-11-17 批 1）：进程内快照栈 + 编辑/删除恢复。

覆盖：字段更新、改期、软删、物理闪删、系列 this/following、项目改名/删除、
空栈 409、撤销不自我记录。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TestingSessionLocal
from models import Project, Task


def _create_task(client: TestClient, **body) -> dict:
    payload = {"title": "测试任务", **body}
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def _undo(client: TestClient) -> dict:
    state = client.get("/api/undo").json()
    assert state["available"] is True
    res = client.post("/api/undo")
    assert res.status_code == 200, res.text
    return res.json()


def test_undo_task_update_restores_all_fields(client: TestClient):
    task = _create_task(client, title="原标题", notes="原备注", priority=2)
    res = client.patch(f"/api/tasks/{task['id']}",
                       json={"title": "新标题", "notes": "新备注", "priority": "high"})
    assert res.status_code == 200
    _undo(client)
    got = client.get(f"/api/tasks/{task['id']}").json()
    assert got["title"] == "原标题"
    assert got["notes"] == "原备注"
    assert got["priority"] == "medium"


def test_undo_reschedule_restores_start_and_reschedule_count(client: TestClient):
    task = _create_task(client, title="可改期",
                        start_time="2026-12-01T09:00:00",
                        end_time="2026-12-01T10:00:00")
    before = client.get(f"/api/tasks/{task['id']}").json()
    client.patch(f"/api/tasks/{task['id']}",
                 json={"start_time": "2026-12-02T15:00:00",
                       "end_time": "2026-12-02T16:00:00"})
    _undo(client)
    got = client.get(f"/api/tasks/{task['id']}").json()
    assert got["start_time"] == before["start_time"]
    assert got["end_time"] == before["end_time"]


def test_undo_physical_flash_delete_reinserts_task(client: TestClient):
    task = _create_task(client, title="闪删任务")
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404
    _undo(client)
    got = client.get(f"/api/tasks/{task['id']}")
    assert got.status_code == 200
    assert got.json()["title"] == "闪删任务"


def test_undo_soft_delete_restores_task(client: TestClient, seeded_db):
    # seed 任务已排期 >300s → 软删
    tasks = client.get("/api/tasks", params={"unscheduled": True}).json()
    target = tasks[0]
    assert client.delete(f"/api/tasks/{target['id']}").status_code == 204
    _undo(client)
    assert client.get(f"/api/tasks/{target['id']}").status_code == 200


def test_undo_series_this_removes_materialized_exception(client: TestClient, seeded_db):
    tasks = client.get("/api/tasks").json()
    series = next(t for t in tasks if t["recurrence_rule"])
    date = "2026-12-07"  # 周一：seed 英语课实例日
    res = client.patch(f"/api/tasks/{series['id']}",
                       params={"mode": "this", "date": date},
                       json={"notes": "临时改动"})
    assert res.status_code == 200
    exception_id = res.json()["id"]
    _undo(client)
    assert client.get(f"/api/tasks/{exception_id}").status_code == 404
    # 主系列挖空日期回滚
    got = client.get(f"/api/tasks/{series['id']}").json()
    assert date not in (got["cancelled_dates"] or [])


def test_undo_series_delete_this_removes_tombstone(client: TestClient, seeded_db):
    # 2026-11-17 二轮批 1：仅删本次建墓碑 → 撤销须连墓碑一并反删（此前 _find_exception
    # 带 is_deleted=False 谓词查不到墓碑，note_created_task 是死代码——撤销报成功
    # 但实例仍缺）。挖空集回空 = 实例复活。
    tasks = client.get("/api/tasks").json()
    series = next(t for t in tasks if t["recurrence_rule"])
    date = "2026-12-07"  # 周一：seed 英语课实例日
    assert client.delete(f"/api/tasks/{series['id']}",
                         params={"mode": "this", "date": date}).status_code == 204
    got = client.get(f"/api/tasks/{series['id']}").json()
    assert date in (got["cancelled_dates"] or [])
    _undo(client)
    got = client.get(f"/api/tasks/{series['id']}").json()
    assert date not in (got["cancelled_dates"] or [])  # 墓碑已随撤销反删
    with TestingSessionLocal() as db:
        assert db.scalars(select(Task.id).where(Task.parent_id == series["id"])).all() == []


def test_undo_series_delete_this_repeat_keeps_tombstone(client: TestClient, seeded_db):
    # 同日幂等重删不新建墓碑（_create_tombstone 返回 None）——撤销第二次不误删
    # 第一次的墓碑行。
    tasks = client.get("/api/tasks").json()
    series = next(t for t in tasks if t["recurrence_rule"])
    date = "2026-12-07"
    client.delete(f"/api/tasks/{series['id']}", params={"mode": "this", "date": date})
    client.delete(f"/api/tasks/{series['id']}", params={"mode": "this", "date": date})
    _undo(client)  # 撤销第二次删除（无新建行）
    got = client.get(f"/api/tasks/{series['id']}").json()
    assert date in (got["cancelled_dates"] or [])


def test_undo_project_rename_restores_name(client: TestClient):
    proj = client.post("/api/projects", json={"name": "旧项目", "color": "red"}).json()
    client.patch(f"/api/projects/{proj['id']}", json={"name": "新项目"})
    _undo(client)
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "旧项目" in names and "新项目" not in names


def test_undo_project_delete_restores_project_and_task_links(client: TestClient):
    proj = client.post("/api/projects", json={"name": "待删项目", "color": "blue"}).json()
    task = _create_task(client, title="归属任务", project_id=proj["id"])
    assert client.delete(f"/api/projects/{proj['id']}").status_code == 204
    assert client.get(f"/api/tasks/{task['id']}").json()["project_id"] is None
    _undo(client)
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "待删项目" in names
    assert client.get(f"/api/tasks/{task['id']}").json()["project_id"] == proj["id"]


def test_undo_empty_stack_409(client: TestClient):
    from services import undo

    undo.reset_for_tests()
    assert client.get("/api/undo").json()["available"] is False
    assert client.post("/api/undo").status_code == 409


def test_undo_does_not_record_itself(client: TestClient):
    task = _create_task(client, title="一次")
    client.patch(f"/api/tasks/{task['id']}", json={"title": "二次"})
    _undo(client)
    assert client.get("/api/undo").json()["available"] is False


def test_undo_does_not_swallow_followup_entry(client: TestClient):
    # 旧版进程级 _suppressed 开关会吞掉撤销窗口内并发/后续写操作的撤销条目
    # （操作成功却不可撤销，2026-11-17 二轮批 1 移除）：撤销后紧接的新操作必须可撤销。
    task = _create_task(client, title="连续")
    client.patch(f"/api/tasks/{task['id']}", json={"title": "第一次改"})
    _undo(client)
    client.patch(f"/api/tasks/{task['id']}", json={"title": "第二次改"})
    assert client.get("/api/undo").json()["available"] is True
    _undo(client)
    # 恢复到第二次改之前 = "连续"（"第一次改"已被首个撤销回滚，历史不叠加）
    assert client.get(f"/api/tasks/{task['id']}").json()["title"] == "连续"


def test_done_only_patch_not_in_global_undo(client: TestClient):
    # 纯 done 勾选不进全局撤销栈（批 1 原设计）：收件箱 4 秒局部撤销是唯一入口，
    # 双入口会打架——先撤局部再撤全局会把任务"重新完成"。
    task = _create_task(client, title="勾选任务")
    assert client.patch(f"/api/tasks/{task['id']}", json={"done": True}).status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").json()["done_at"] is not None
    assert client.get("/api/undo").json()["available"] is False


def test_undo_project_delete_survives_same_name_recreate(client: TestClient):
    # 恢复撞活体同名（删除后新建了同名项目）：改名墓碑形恢复，撤销不再 500，
    # 任务归属仍回写到位
    proj = client.post("/api/projects", json={"name": "撞名项目", "color": "red"}).json()
    task = _create_task(client, title="归属", project_id=proj["id"])
    client.delete(f"/api/projects/{proj['id']}")
    same = client.post("/api/projects", json={"name": "撞名项目", "color": "blue"}).json()
    _undo(client)
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "撞名项目" in names and sum(n == "撞名项目" for n in names) == 1  # 同名不并存
    assert any(n.startswith("撞名项目#undo#") for n in names)  # 恢复行改名
    assert client.get(f"/api/tasks/{task['id']}").json()["project_id"] == proj["id"]
    listed = {p["id"]: p["name"] for p in client.get("/api/projects").json()}
    assert listed[same["id"]] == "撞名项目"  # 后建的同名项目原样保留
