"""任务数据导出（2026-11-17 批 4）：CSV 表格与 .ics 日历两件套。

路径纪律同 export_report_md：绝对路径 + 后缀 + 父目录存在；违规 ValueError
（router 翻 422）。只导出活跃任务（软删不进导出）；未排期待办进 CSV、不进 ICS
（没有时间的事件无法落到日历上）。
"""

import csv
import io
import logging
from datetime import datetime, timedelta
from pathlib import Path

from icalendar import Calendar
from icalendar import Event as IcsEvent
from icalendar import vRecur
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Project
from services.tasks import list_tasks

logger = logging.getLogger(__name__)

CSV_HEADER = [
    "id", "标题", "项目", "优先级", "开始时间", "结束时间", "预计时长(分)", "全天",
    "重复规则", "完成时间", "备注", "子任务", "创建时间",
]
_PRIORITY_LABEL = {1: "高", 2: "中", 3: "低"}
_ICS_PRIORITY = {1: 1, 2: 5, 3: 9}


def _check_path(path: str, suffix: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("导出路径须为绝对路径")
    if p.suffix.lower() != suffix:
        raise ValueError(f"导出文件须以 {suffix} 结尾")
    if not p.parent.is_dir():
        raise ValueError(f"目标目录不存在：{p.parent}")
    return p


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def export_tasks_csv(db: Session, path: str) -> dict:
    """全量活跃任务 → CSV（utf-8-sig：Excel 直开中文不乱码）。

    含完成与未排期（软删不导出）；子任务压成一列「标题(已完成)」用 " | " 连接，
    保一任务一行便于表格透视。"""
    out = _check_path(path, ".csv")
    tasks = list_tasks(db)
    projects = {p.id: p.name for p in db.scalars(select(Project))}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for t in tasks:
        subtasks = " | ".join(
            f"{s.title}(已完成)" if getattr(s, "done", False) else s.title
            for s in (getattr(t, "subtasks", None) or [])
        )
        writer.writerow([
            t.id,
            t.title,
            projects.get(t.project_id, "") if t.project_id else "",
            _PRIORITY_LABEL.get(t.priority, "中"),
            _fmt_dt(t.start_time),
            _fmt_dt(t.end_time),
            t.duration_mins or "",
            "是" if t.all_day else "否",
            t.recurrence_rule or "",
            _fmt_dt(t.done_at),
            t.notes or "",
            subtasks,
            _fmt_dt(t.created_at),
        ])
    data = "﻿" + buf.getvalue()
    # newline=""：csv 自带 \r\n，避免 Windows 文本层再翻译成 \r\r\n
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    logger.info("任务导出 CSV → %s（%d 条）", out, len(tasks))
    return {"ok": True, "path": str(out), "count": len(tasks),
            "size_bytes": len(data.encode("utf-8"))}


def export_tasks_ics(db: Session, path: str) -> dict:
    """活跃且已排期的任务 → .ics（RFC 5545）。

    重复系列带 RRULE + EXDATE（墓碑/活体例外行的 original_time 挖空对应日期）；
    全天走 VALUE=DATE 半开区间（Clara 的 [start,end) 与 RFC 排他 DTEND 同语义）；
    定时用浮动本地时间（不带 TZID，与库内 naive 本地时间一致）。"""
    out = _check_path(path, ".ics")
    tasks = list_tasks(db)
    cal = Calendar()
    cal.add("prodid", "-//Clara//Clara Task Export//ZH")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Clara 任务导出")
    now = datetime.now()
    exported = 0
    for t in tasks:
        if t.start_time is None:
            continue
        ev = IcsEvent()
        ev.add("uid", f"{t.id}@clara.local")
        ev.add("summary", t.title)
        ev.add("dtstamp", now)
        if t.notes:
            ev.add("description", t.notes)
        if t.done_at is not None:
            ev.add("status", "COMPLETED")  # 外部日历降噪：已完成历史日程可按状态过滤
        ev.add("priority", _ICS_PRIORITY.get(t.priority, 5))
        if t.all_day:
            d0 = t.start_time.date()
            d1 = t.end_time.date() if t.end_time else d0 + timedelta(days=1)
            ev.add("dtstart", d0)
            ev.add("dtend", d1)
        else:
            ev.add("dtstart", t.start_time)
            if t.end_time:
                ev.add("dtend", t.end_time)
            else:
                ev.add("duration", timedelta(
                    minutes=t.duration_mins or 60))  # 前端默认一小时卡口径
        if t.recurrence_rule:
            try:
                ev.add("rrule", vRecur.from_ical(t.recurrence_rule))
            except Exception:  # noqa: BLE001——规则损坏按单次导出，不连坐整文件
                logger.warning("任务 %s 的重复规则无法解析，按单次导出：%s",
                               t.id, t.recurrence_rule)
            else:
                exdates = _cancelled_instances(db, t)
                if exdates:
                    ev.add("exdate", exdates)
        cal.add_component(ev)
        exported += 1
    payload = cal.to_ical()
    out.write_bytes(payload)
    logger.info("任务导出 ICS → %s（%d 条）", out, exported)
    return {"ok": True, "path": str(out), "count": exported,
            "size_bytes": len(payload)}


def _cancelled_instances(db: Session, task) -> list:
    """系列被挖空的实例时刻（墓碑 + 活体例外两类，口径同 tasks._attach_cancelled_dates
    ——活体例外改期跨日后其当前开始日同样挖空）。全天系列给 date（VALUE=DATE 的
    EXDATE），定时系列给 datetime（含时区为浮动本地；例外的开始时刻只在跨日时
    并入，同日改钟点不产生噪声 EXDATE）。"""
    from models import Task

    originals = [
        dt for dt in db.scalars(
            select(Task.original_time).where(Task.parent_id == task.id))
        if dt is not None
    ]
    moved_days = {  # 活体例外改期跨日：新日开始日的实例由例外行自身代表，主规则挖空
        start.date() for start, original, deleted in db.execute(
            select(Task.start_time, Task.original_time, Task.is_deleted).where(
                Task.parent_id == task.id))
        if not deleted and start is not None
        and (original is None or start.date() != original.date())
    }
    if task.all_day:
        return sorted({dt.date() for dt in originals} | moved_days)
    # 定时系列的实例时刻 = 日 + 主任务开始钟点（口径同 tasks._instance_time）——
    # EXDATE 必须对准实例自身时刻才生效（例外的开始钟点可能已改）。
    clock = task.start_time.time() if task.start_time is not None else None
    out = set(originals)
    if clock is not None:
        out |= {datetime(d.year, d.month, d.day, clock.hour, clock.minute)
                for d in moved_days}
    return sorted(out)
