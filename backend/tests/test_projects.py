"""阶段一项目读/写接口测试（基建见 conftest.py）。"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TestingSessionLocal
from models import Project, Task


def _pid_by_name(name: str) -> str:
    """seed 项目的 id（写路径以 id 寻址，测试先从 ORM 反查）。"""
    with TestingSessionLocal() as db:
        return db.scalar(select(Project.id).where(Project.name == name))


def test_projects_list_shape_and_order(client: TestClient, seeded_db):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    projects = resp.json()
    # 顺序 = seed 数组序（sort_order）；color 存 CSS 变量串原样透出
    assert [p["name"] for p in projects] == ["Alpha Launch", "日常运营", "学习提升"]
    assert projects[0]["color"] == "var(--accent)"
    assert projects[1]["color"] == "var(--green)"
    assert projects[2]["color"] == "var(--orange)"
    assert all(p["starred"] is False for p in projects)


def test_task_project_id_backref(client: TestClient, seeded_db):
    # 任务的 project_id 正确回指项目；无项目任务为 null
    tasks = client.get("/api/tasks").json()
    projects = client.get("/api/projects").json()
    okr = next(t for t in tasks if t["title"] == "OKR 对齐会")
    alpha = next(p for p in projects if p["name"] == "Alpha Launch")
    assert okr["project_id"] == alpha["id"]
    assert next(t for t in tasks if t["title"] == "夜跑")["project_id"] is None


def test_soft_deleted_project_filtered(client: TestClient, seeded_db):
    with TestingSessionLocal() as db:
        project = db.query(Project).filter_by(name="学习提升").first()
        project.is_deleted = True
        db.commit()
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert names == ["Alpha Launch", "日常运营"]


# —— 写路径：新建 ——


def test_create_project_201_shape_and_sort_order(client: TestClient, seeded_db):
    resp = client.post("/api/projects", json={"name": "新项目", "color": "#6366f1"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "新项目" and body["color"] == "#6366f1"
    assert body["starred"] is False
    assert len(body["id"]) == 36  # UUID 形
    # sort_order 排 seed 末位（3 个 seed 项目 0..2）
    with TestingSessionLocal() as db:
        assert db.query(Project).filter_by(name="新项目").one().sort_order == 3


def test_create_project_duplicate_name_409(client: TestClient, seeded_db):
    resp = client.post("/api/projects", json={"name": "Alpha Launch", "color": "#6366f1"})
    assert resp.status_code == 409


def test_create_project_after_delete_same_name_201(client: TestClient, seeded_db):
    # 软删行仍占 name 唯一约束——删除时墓碑重写 name，之后允许新建同名项目
    assert client.delete(f"/api/projects/{_pid_by_name('学习提升')}").status_code == 204
    resp = client.post("/api/projects", json={"name": "学习提升", "color": "#22c55e"})
    assert resp.status_code == 201


def test_create_project_name_length_422(client: TestClient, seeded_db):
    assert client.post("/api/projects", json={"name": "x" * 21, "color": "#6366f1"}).status_code == 422
    assert client.post("/api/projects", json={"name": "   ", "color": "#6366f1"}).status_code == 422  # strip 后空


# —— 写路径：更新 ——


def test_update_project_rename_keeps_task_fk(client: TestClient, seeded_db):
    # id 寻址的核心断言：改名只动 name，任务外键原值不动（前端以 name 引用靠联动刷新）
    pid = _pid_by_name("日常运营")
    with TestingSessionLocal() as db:
        before = {row[0] for row in db.query(Task.id).filter_by(project_id=pid).all()}
    assert before  # seed 该项目下有任务
    resp = client.patch(f"/api/projects/{pid}", json={"name": "运营日常"})
    assert resp.status_code == 200 and resp.json()["name"] == "运营日常"
    with TestingSessionLocal() as db:
        after = {row[0] for row in db.query(Task.id).filter_by(project_id=pid).all()}
    assert after == before


def test_update_project_rename_conflict_409(client: TestClient, seeded_db):
    resp = client.patch(f"/api/projects/{_pid_by_name('日常运营')}", json={"name": "Alpha Launch"})
    assert resp.status_code == 409


def test_update_project_star_persists(client: TestClient, seeded_db):
    pid = _pid_by_name("Alpha Launch")
    resp = client.patch(f"/api/projects/{pid}", json={"starred": True})
    assert resp.status_code == 200 and resp.json()["starred"] is True
    # 键省略的字段不动（exclude_unset 语义锚）
    assert resp.json()["name"] == "Alpha Launch" and resp.json()["color"] == "var(--accent)"
    with TestingSessionLocal() as db:
        assert db.get(Project, pid).starred is True


def test_update_project_empty_body_untouched(client: TestClient, seeded_db):
    # 空 patch 短路：不发 UPDATE，实体原样返回
    resp = client.patch(f"/api/projects/{_pid_by_name('Alpha Launch')}", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alpha Launch" and resp.json()["starred"] is False


def test_update_project_name_null_422(client: TestClient, seeded_db):
    # Optional 类型放行显式 null，但 name 是 NOT NULL 列——service 拦为 422（否则 IntegrityError 500）
    resp = client.patch(f"/api/projects/{_pid_by_name('Alpha Launch')}", json={"name": None})
    assert resp.status_code == 422


def test_update_project_404_and_soft_deleted_404(client: TestClient, seeded_db):
    assert client.patch(f"/api/projects/{uuid.uuid4()}", json={"starred": True}).status_code == 404
    pid = _pid_by_name("学习提升")
    with TestingSessionLocal() as db:
        db.get(Project, pid).is_deleted = True
        db.commit()
    assert client.patch(f"/api/projects/{pid}", json={"starred": True}).status_code == 404


# —— 写路径：删除 ——


def test_delete_project_detaches_tasks(client: TestClient, seeded_db):
    pid = _pid_by_name("日常运营")
    with TestingSessionLocal() as db:
        task_ids = [row[0] for row in db.query(Task.id).filter_by(project_id=pid).all()]
    assert task_ids  # seed 该项目下有任务
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    with TestingSessionLocal() as db:
        # 归属任务全部置空（回默认收件箱）
        assert db.query(Task).filter(Task.id.in_(task_ids), Task.project_id.is_not(None)).count() == 0
        tomb = db.get(Project, pid)
        assert tomb.is_deleted is True
        assert tomb.name != "日常运营" and len(tomb.name) <= 20  # 墓碑重写且不超列宽
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert names == ["Alpha Launch", "学习提升"]


def test_delete_project_404_and_soft_deleted_404(client: TestClient, seeded_db):
    assert client.delete(f"/api/projects/{uuid.uuid4()}").status_code == 404
    pid = _pid_by_name("学习提升")
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.delete(f"/api/projects/{pid}").status_code == 404  # 软删后再删 404
