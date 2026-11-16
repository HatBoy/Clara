"""日程模板（2026-11-17 批 2）：CRUD / 重名 409 / 应用生成任务。"""

from fastapi.testclient import TestClient


def _create(client: TestClient, **body) -> dict:
    payload = {"name": "周会", "title": "周会", **body}
    res = client.post("/api/templates", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def test_template_crud_and_duplicate_name(client: TestClient):
    tpl = _create(client, name="周会", title="周会", duration_mins=60,
                  start_min=600, priority=1, notes="同步进展")
    assert tpl["start_min"] == 600
    assert len(client.get("/api/templates").json()) == 1
    # 重名 409
    dup = client.post("/api/templates", json={"name": "周会", "title": "另一个"})
    assert dup.status_code == 409
    # 改名 + 删除
    res = client.patch(f"/api/templates/{tpl['id']}", json={"name": "周一例会"})
    assert res.status_code == 200 and res.json()["name"] == "周一例会"
    assert client.delete(f"/api/templates/{tpl['id']}").status_code == 204
    assert client.get("/api/templates").json() == []


def test_apply_template_with_time_creates_scheduled_task(client: TestClient):
    tpl = _create(client, name="周会", title="周会", duration_mins=90,
                  start_min=570, priority=1, notes="同步进展",
                  recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
                  location="会议室 A", travel_mins=30,
                  subtasks=["准备材料", "确认参会人"])
    res = client.post(f"/api/templates/{tpl['id']}/apply",
                      json={"date": "2026-12-07"})
    assert res.status_code == 201, res.text
    task = res.json()
    assert task["title"] == "周会"
    assert task["start_time"].startswith("2026-12-07T09:30")
    assert task["duration_mins"] == 90
    assert task["priority"] == "high"
    assert task["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert task["location"] == "会议室 A"
    assert task["travel_mins"] == 30
    assert [s["title"] for s in task["subtasks"]] == ["准备材料", "确认参会人"]


def test_apply_template_without_time_creates_unscheduled(client: TestClient):
    tpl = _create(client, name="待办模板", title="整理资料", duration_mins=30)
    res = client.post(f"/api/templates/{tpl['id']}/apply", json={})
    assert res.status_code == 201
    task = res.json()
    assert task["start_time"] is None and task["end_time"] is None


def test_template_apply_missing_404(client: TestClient):
    assert client.post("/api/templates/no-such/apply", json={}).status_code == 404


def test_schedule_templates_in_sync_tables():
    from services.sync_engine import NATURAL_KEYS, SYNC_TABLES

    assert "schedule_templates" in SYNC_TABLES
    assert NATURAL_KEYS["schedule_templates"] == ("name",)
