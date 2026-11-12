"""ics 导入测试（2026-11-11 批）：映射 / 时区归一 / RRULE 换算与降级 / 指纹查重 /
坏条目跳过 / 端点 422 / tasks_changed 广播。时区断言一律 astimezone() 动态算期望，
测试机时区无关。"""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from conftest import TestingSessionLocal
from models import Task
from services import workflows
from services.tasks import expand_occurrences
from sqlalchemy import select

# —— 公共块 ———————————————————————————————————————————————————

_VTIMEZONE_SH = """BEGIN:VTIMEZONE
TZID:Asia/Shanghai
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0800
TZOFFSETTO:+0800
TZNAME:CST
END:STANDARD
END:VTIMEZONE
"""


def _write_ics(tmp_path, text: str) -> str:
    p = tmp_path / "cal.ics"
    # newline=""：write_text 默认会把 \n 转 os.linesep，Windows 上 \r\n 被写成
    # \r\r\n，属性值尾多出 \r 让 icalendar 解析炸——必须原样落盘
    p.write_text(text, encoding="utf-8", newline="")
    return str(p)


def _wrap(components: str, extra: str = "") -> str:
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{extra}{components}\r\nEND:VCALENDAR\r\n"


def _import(client, path: str):
    return client.post("/api/ingest/ics", json={"path": path})


def _tasks() -> list[Task]:
    with TestingSessionLocal() as db:
        return list(db.scalars(select(Task).where(Task.is_deleted.is_(False))))


def _sh_local(y, mo, d, h, mi) -> datetime:
    """上海时刻 → 测试机本地 naive（动态换算，不硬编码偏移）。"""
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone().replace(tzinfo=None)


def _utc_local(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


# —— 基本映射 ——————————————————————————————————————————————————


def test_basic_timed_event(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e1\r\nDTSTART:20261115T090000\r\n"
        "DTEND:20261115T103000\r\nSUMMARY:周会\r\nEND:VEVENT"))
    resp = _import(client, p)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["created_events"] == 1 and body["created_todos"] == 0
    assert body["skipped_duplicates"] == 0 and body["skipped_invalid"] == 0
    tasks = _tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "周会"
    assert t.start_time == datetime(2026, 11, 15, 9, 0)
    assert t.end_time == datetime(2026, 11, 15, 10, 30)
    assert t.duration_mins == 90  # create_task 强制重算 end-start
    assert t.ever_scheduled is True
    assert t.source_key == "ics:cal"
    assert t.source_fingerprint and len(t.source_fingerprint) == 40


def test_utc_z_suffix(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e2\r\nDTSTART:20261115T010000Z\r\n"
        "DTEND:20261115T023000Z\r\nSUMMARY:UTC 事件\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.start_time == _utc_local(2026, 11, 15, 1, 0)
    assert t.end_time == _utc_local(2026, 11, 15, 2, 30)


def test_tzid_with_vtimezone(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e3\r\nDTSTART;TZID=Asia/Shanghai:20261115T090000\r\n"
        "DTEND;TZID=Asia/Shanghai:20261115T100000\r\nSUMMARY:上海事件\r\nEND:VEVENT",
        extra=_VTIMEZONE_SH))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.start_time == _sh_local(2026, 11, 15, 9, 0)
    assert t.end_time == _sh_local(2026, 11, 15, 10, 0)


def test_floating_time_kept(client, tmp_path):
    # 浮动（无时区）时间按本地原样解释
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e4\r\nDTSTART:20261115T090000\r\n"
        "SUMMARY:浮动事件\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    assert _tasks()[0].start_time == datetime(2026, 11, 15, 9, 0)


def test_all_day_single_day(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e5\r\nDTSTART;VALUE=DATE:20261120\r\nSUMMARY:全天单日\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.all_day is True
    assert t.start_time == datetime(2026, 11, 20, 0, 0)
    assert t.end_time is None  # 单日规范形
    assert t.duration_mins is None


def test_all_day_span_and_one_day_normalize(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e6\r\nDTSTART;VALUE=DATE:20261120\r\n"
        "DTEND;VALUE=DATE:20261123\r\nSUMMARY:出差三天\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:e7\r\nDTSTART;VALUE=DATE:20261201\r\n"
        "DTEND;VALUE=DATE:20261202\r\nSUMMARY:恰一天\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    tasks = {t.title: t for t in _tasks()}
    assert tasks["出差三天"].end_time == datetime(2026, 11, 23, 0, 0)  # RFC 排他日 = 末日次日 0 点
    assert tasks["恰一天"].end_time is None  # DTEND=+1d 归一单日


def test_timed_no_dtend_defaults_60(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e8\r\nDTSTART:20261115T090000\r\nSUMMARY:无结束事件\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.end_time is None and t.duration_mins == 60  # 结构化直映射「duration 恒 60」先例


def test_duration_property(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:e9\r\nDTSTART:20261115T090000\r\n"
        "DURATION:PT1H30M\r\nSUMMARY:时长属性\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.end_time == datetime(2026, 11, 15, 10, 30) and t.duration_mins == 90


def test_title_notes_truncate_and_unescape(client, tmp_path):
    long_title = "这是一个特别特别特别特别特别长的标题超过三十个字符上限会被截断掉的标题"
    desc = "第一行\\n第二行\\,带逗号\\;带分号" + "补" * 400
    p = _write_ics(tmp_path, _wrap(
        f"BEGIN:VEVENT\r\nUID:e10\r\nDTSTART:20261115T090000\r\nSUMMARY:{long_title}\r\n"
        f"DESCRIPTION:{desc}\r\nLOCATION:3F 会议室\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert len(t.title) == 30 and t.title == long_title[:30]
    assert len(t.notes) <= 500
    assert t.notes.startswith("第一行\n第二行,带逗号;带分号")
    assert "地点：3F 会议室" in t.notes


# —— RRULE 换算 ——————————————————————————————————————————————————


def test_rrule_daily_count_to_until(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:r1\r\nDTSTART:20261115T090000\r\n"
        "RRULE:FREQ=DAILY;COUNT=5\r\nSUMMARY:每日站会\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.recurrence_rule == "FREQ=DAILY;UNTIL=20261119T235959"
    occs = expand_occurrences(t, datetime(2026, 11, 14), datetime(2026, 12, 15))
    assert len(occs) == 5  # 恰好 COUNT 次
    assert occs[0] == datetime(2026, 11, 15, 9, 0)


def test_rrule_weekly_until_date_form(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:r2\r\nDTSTART:20261116T090000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231\r\nSUMMARY:周一三五\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    t = _tasks()[0]
    assert t.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T235959"


def test_rrule_weekly_no_byday_and_monthly_count(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:r3\r\nDTSTART:20261116T090000\r\n"
        "RRULE:FREQ=WEEKLY\r\nSUMMARY:每周一补码\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:r4\r\nDTSTART:20261115T090000\r\n"
        "RRULE:FREQ=MONTHLY;COUNT=3\r\nSUMMARY:月度复盘\r\nEND:VEVENT"))
    assert _import(client, p).status_code == 200
    tasks = {t.title: t for t in _tasks()}
    assert tasks["每周一补码"].recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"  # 补 DTSTART 星期
    assert tasks["月度复盘"].recurrence_rule == "FREQ=MONTHLY;UNTIL=20270115T235959"


def test_rrule_degrade_cases(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:d1\r\nDTSTART:20261115T090000\r\n"
        "RRULE:FREQ=DAILY;INTERVAL=2\r\nSUMMARY:隔日降级\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:d2\r\nDTSTART:20261115T090000\r\n"
        "RRULE:FREQ=MONTHLY;BYDAY=1MO\r\nSUMMARY:序数降级\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:d3\r\nDTSTART:20261115T090000\r\n"
        "RRULE:FREQ=DAILY\r\nEXDATE:20261116T090000\r\nSUMMARY:例外降级\r\nEND:VEVENT"))
    body = _import(client, p).json()
    assert body["degraded_recurring"] == 3 and body["created_events"] == 3
    tasks = {t.title: t for t in _tasks()}
    assert all(t.recurrence_rule is None for t in tasks.values())  # 降级单次仍创建


# —— VTODO ———————————————————————————————————————————————————————


def test_vtodo_mapping(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VTODO\r\nUID:t1\r\nSUMMARY:整理会议材料\r\nDURATION:PT2H\r\n"
        "PRIORITY:2\r\nDESCRIPTION:会前完成\r\nEND:VTODO"))
    body = _import(client, p).json()
    assert body["created_todos"] == 1 and body["created_events"] == 0
    t = _tasks()[0]
    assert t.start_time is None and t.ever_scheduled is False  # 收件箱待办
    assert t.duration_mins == 120 and t.priority == 1  # ics 1-3 → 高
    assert t.notes == "会前完成"


def test_vtodo_priority_and_duration_clamp(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VTODO\r\nUID:t2\r\nSUMMARY:无优先级待办\r\nDURATION:PT25H\r\nEND:VTODO\r\n"
        "BEGIN:VTODO\r\nUID:t3\r\nSUMMARY:低优先级\r\nPRIORITY:9\r\nEND:VTODO"))
    assert _import(client, p).status_code == 200
    tasks = {t.title: t for t in _tasks()}
    assert tasks["无优先级待办"].priority == 2  # 0/缺 → None → 库默认 2
    assert tasks["无优先级待办"].duration_mins is None  # 超 1440 丢弃
    assert tasks["低优先级"].priority == 3


# —— 指纹查重 ————————————————————————————————————————————————————


def test_reimport_all_skipped_no_broadcast(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:p1\r\nDTSTART:20261115T090000\r\nSUMMARY:重导事件\r\nEND:VEVENT"))
    q: asyncio.Queue = workflows.subscribe()
    try:
        assert _import(client, p).json()["created_events"] == 1
        assert q.get_nowait() == {"type": "tasks_changed", "source": "ics"}
        body = _import(client, p).json()  # 同文件二导
        assert body["created_events"] == 0
        assert body["skipped_duplicates"] == 1
        assert q.empty()  # 零创建不广播
    finally:
        workflows.unsubscribe(q)
    assert len(_tasks()) == 1


def test_same_uid_new_dtstart_creates(client, tmp_path):
    p1 = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:u1\r\nDTSTART:20261115T090000\r\nSUMMARY:改期事件\r\nEND:VEVENT"))
    p2 = tmp_path / "cal2.ics"
    p2.write_text(_wrap(
        "BEGIN:VEVENT\r\nUID:u1\r\nDTSTART:20261116T090000\r\nSUMMARY:改期事件\r\nEND:VEVENT"),
        encoding="utf-8", newline="")
    assert _import(client, p1).json()["created_events"] == 1
    body = _import(client, str(p2)).json()
    assert body["created_events"] == 1 and body["skipped_duplicates"] == 0  # 改时间 = 新条目
    assert len(_tasks()) == 2


# —— 坏条目与端点错误 ————————————————————————————————————————————


def test_mixed_bad_entries_skip(client, tmp_path):
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:b1\r\nSUMMARY:缺开始时间\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b2\r\nDTSTART:20261115T090000\r\nEND:VEVENT\r\n"  # 无 SUMMARY
        "BEGIN:VEVENT\r\nUID:b3\r\nDTSTART:20261115T090000\r\nSUMMARY:已取消\r\n"
        "STATUS:CANCELLED\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b4\r\nRECURRENCE-ID:20261116T090000\r\n"
        "DTSTART:20261116T090000\r\nSUMMARY:系列例外行\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b5\r\nDTSTART:20261115T100000\r\nDTEND:20261115T090000\r\n"
        "SUMMARY:结束早于开始\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b6\r\nDTSTART:20261115T090000\r\nSUMMARY:好条目\r\nEND:VEVENT"))
    body = _import(client, p).json()
    assert body["created_events"] == 1 and body["skipped_invalid"] == 5  # 好条目不连坐
    assert [t.title for t in _tasks()] == ["好条目"]


def test_endpoint_errors(client, tmp_path):
    # 相对路径
    assert _import(client, "cal.ics").status_code == 422
    # 不存在
    assert _import(client, str(tmp_path / "nope.ics")).status_code == 422
    # 非 .ics 后缀
    other = tmp_path / "note.txt"
    other.write_text("x", encoding="utf-8")
    assert _import(client, str(other)).status_code == 422
    # 非 ics 内容
    bad = tmp_path / "bad.ics"
    bad.write_text("这不是日历文件", encoding="utf-8")
    assert _import(client, str(bad)).status_code == 422
    # 零条目
    empty = tmp_path / "empty.ics"
    empty.write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", encoding="utf-8")
    resp = _import(client, str(empty))
    assert resp.status_code == 422 and "未发现" in resp.json()["detail"]
    # body 缺 path（pydantic 422）
    assert client.post("/api/ingest/ics", json={}).status_code == 422


def test_past_event_imported(client, tmp_path):
    # 同步语义：过去事件也照建（时间照抄源数据）
    p = _write_ics(tmp_path, _wrap(
        "BEGIN:VEVENT\r\nUID:old1\r\nDTSTART:20200101T090000\r\n"
        "DTEND:20200101T100000\r\nSUMMARY:过去事件\r\nEND:VEVENT"))
    body = _import(client, p).json()
    assert body["created_events"] == 1
    assert _tasks()[0].start_time == datetime(2020, 1, 1, 9, 0)
