"""批 4：任务 CSV / ICS 导出。"""

from datetime import date, datetime

from fastapi.testclient import TestClient
from icalendar import Calendar

from conftest import TestingSessionLocal
from models import Project, Subtask, Task
from schemas import TaskCreate
from services.export import export_tasks_csv, export_tasks_ics
from services.tasks import create_task


def _seed_mixed(db) -> None:
    """一个带项目/子任务/备注的定时任务 + 一个全天任务 + 一个未排期待办。"""
    proj = Project(name="工作", color="#6366f1")
    db.add(proj)
    db.flush()
    task = create_task(db, TaskCreate(
        title="周会", start_time=datetime(2026, 11, 18, 9, 0),
        end_time=datetime(2026, 11, 18, 10, 0), project_id=proj.id,
        priority=1, notes="同步进度",
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU",
    ))
    db.add(Subtask(task_id=task.id, title="准备材料", done_at=datetime.now(), sort_order=0))
    create_task(db, TaskCreate(
        title="出差", start_time=datetime(2026, 11, 20), all_day=True))
    create_task(db, TaskCreate(title="买牛奶"))
    db.commit()


def test_csv_export_writes_bom_and_rows(tmp_path):
    with TestingSessionLocal() as db:
        _seed_mixed(db)
        out = tmp_path / "tasks.csv"
        res = export_tasks_csv(db, str(out))
    assert res["count"] == 3
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig：Excel 直开中文不乱码
    text = raw.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert len(lines) == 4  # 表头 + 3 行
    assert "标题" in lines[0] and "子任务" in lines[0]
    meeting = next(l for l in lines if "周会" in l)
    assert "工作" in meeting and "高" in meeting and "同步进度" in meeting
    assert "准备材料(已完成)" in meeting and "FREQ=WEEKLY" in meeting
    assert "出差" in text and "买牛奶" in text


def test_csv_export_rejects_bad_path(tmp_path):
    with TestingSessionLocal() as db:
        for bad in (str(tmp_path / "x.txt"), "relative.csv"):
            try:
                export_tasks_csv(db, bad)
            except ValueError:
                continue
            raise AssertionError(f"应拒绝路径：{bad}")


def test_ics_export_events_rrule_exdate_and_all_day(tmp_path):
    with TestingSessionLocal() as db:
        _seed_mixed(db)
        series = db.query(Task).filter(Task.title == "周会").one()
        # 墓碑：第二周周会实例被删（EXDATE 应挖空 11-25）
        db.add(Task(title="周会", parent_id=series.id, is_deleted=True,
                    original_time=datetime(2026, 11, 25, 9, 0), sort_order=99))
        # 活体例外改期跨日（2026-11-17 二轮批 1 口径）：12-02 周二实例挪到 12-04
        # 周五 11:00——原日挖空 + 新日按系列钟点 09:00 挖空（例外的开始钟点不算），
        # 例外行自身作为独立 VEVENT 出现在新时刻
        db.add(Task(title="周会", parent_id=series.id,
                    original_time=datetime(2026, 12, 2, 9, 0),
                    start_time=datetime(2026, 12, 4, 11, 0),
                    end_time=datetime(2026, 12, 4, 12, 0), sort_order=99))
        db.commit()
        out = tmp_path / "tasks.ics"
        res = export_tasks_ics(db, str(out))
    assert res["count"] == 3  # 未排期待办不进日历；例外行独立成事件
    cal = Calendar.from_ical(out.read_bytes())
    events = cal.walk("VEVENT")
    assert len(events) == 3
    by_summary = {str(e.get("summary")): e for e in events}
    meetings = [e for e in events if str(e.get("summary")) == "周会"]
    assert len(meetings) == 2  # 主系列（带 RRULE）+ 跨日例外（独立单次事件）
    meeting = next(e for e in meetings if e.get("rrule") is not None)
    exception = next(e for e in meetings if e.get("rrule") is None)

    assert str(meeting.get("uid")) == f"{series.id}@clara.local"
    assert meeting.get("dtstart").dt == datetime(2026, 11, 18, 9, 0)  # 浮动本地时间
    assert meeting.get("rrule")["FREQ"] == ["WEEKLY"]
    exdates = meeting.get("exdate")
    assert exdates is not None
    assert [d.dt for d in exdates.dts] == [
        datetime(2026, 11, 25, 9, 0),
        datetime(2026, 12, 2, 9, 0),
        datetime(2026, 12, 4, 9, 0),  # 新日按系列钟点对齐实例时刻
    ]
    assert int(meeting.get("priority")) == 1
    assert exception.get("dtstart").dt == datetime(2026, 12, 4, 11, 0)

    # 已完成任务带 STATUS:COMPLETED（二轮批 1：外部日历可按状态过滤历史噪音）
    done = create_task(db=TestingSessionLocal(), body=TaskCreate(
        title="已完成会", start_time=datetime(2026, 11, 19, 9, 0),
        end_time=datetime(2026, 11, 19, 10, 0)))
    with TestingSessionLocal() as db2:
        row = db2.get(Task, done.id)
        row.done_at = datetime(2026, 11, 19, 12, 0)
        db2.commit()
        out2 = tmp_path / "done.ics"
        export_tasks_ics(db2, str(out2))
    cal2 = Calendar.from_ical(out2.read_bytes())
    evs = {str(e.get("summary")): e for e in cal2.walk("VEVENT")}
    assert str(evs["已完成会"].get("status")).upper() == "COMPLETED"

    allday = by_summary["出差"]
    assert allday.get("dtstart").dt == date(2026, 11, 20)
    assert allday.get("dtend").dt == date(2026, 11, 21)  # 半开区间：次日 0 点
    assert allday.get("rrule") is None


def test_export_endpoints_validate(client: TestClient, tmp_path):
    bad = client.post("/api/export/tasks-csv", json={"path": str(tmp_path / "a.txt")})
    assert bad.status_code == 422
    ok = client.post("/api/export/tasks-ics", json={"path": str(tmp_path / "a.ics")})
    assert ok.status_code == 200 and ok.json()["path"].endswith(".ics")
