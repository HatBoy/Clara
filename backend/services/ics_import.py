"""导入 .ics 日历文件（2026-11-11 批，数据接入页底部入口 POST /api/ingest/ics）。

语义定位——「默认信任直落」，与四源采集管道刻意分离：外部日历是成品日程事实
（应直接进日历参与冲突计算），不是待提炼素材，故不经数据源建议结算、零 LLM。
同步语义（同 2026-11-05 结构化直映射批拍板）：时间照抄源数据，不排期校验、
不拒撞期、过去事件照建；UTC / TZID / 浮动时间一律归一为本地 naive。

映射表：
- VEVENT（含全天 / 重复 / 过去）→ 日历日程（start_time 非空）；VTODO → 收件箱
  待办（start_time 空，DUE 不参与排期）
- 全天（VALUE=DATE）→ all_day=True，start=当日 0 点、DTEND（RFC 排他日）=
  该日 0 点（恰合 Clara [start,end) 半开区间），恰一天归一 end=None（单日规范形）
- RRULE 仅支持 DAILY / WEEKLY;BYDAY / MONTHLY(+UNTIL) 子集（与前端 rrule.ts、
  schemas._RRULE_PATTERN 一致）：COUNT 换算为「第 N 次出现日 +T235959」的
  UNTIL（_rule_occurs 按日比较且当日仍出现，恰展开 N 次）；INTERVAL>1 /
  BYMONTHDAY / BYSETPOS / EXDATE / RDATE 等无法精确表达 → 降级单次并计数
  （EXDATE 照抄会把源端已取消实例加回来 = 错误数据，宁降级不出错）
- 防重复导入：指纹 sha1(UID + 起始锚) 命中既有 Task.source_fingerprint 即跳过
  （镜像 agents/graph.py 同步查重先例）——同文件重导全跳过、改时间 = 新条目

落库策略：尽力而为（外部文件质量不可控，坏条目跳过 + 汇总上报，不连坐整
文件）；写库通道唯一收敛 services/tasks.create_task，不另开通路。落库成功后
广播 tasks_changed（App SSE 收到即 reloadTasks；不走 ingest 事件避免幽灵未读）。
"""

import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Task
from schemas import TaskCreate, _RRULE_PATTERN
from services import workflows
from services.tasks import _BYDAY_TO_ISO, create_task

logger = logging.getLogger(__name__)

# 文件上限 20MB（日历文件正常几百 KB；防误选巨型文件拖垮解析）
_MAX_FILE_BYTES = 20 * 1024 * 1024
# Clara 子集外的 RRULE 键（出现即降级单次——墓碑行建模无法表达这些语义）
_DEGRADE_KEYS = {"BYMONTHDAY", "BYSETPOS", "BYMONTH", "BYWEEKNO", "BYYEARDAY"}
# COUNT 换算上限（防病态规则；月频超此数也远超 UNTIL 可表达范围）
_MAX_COUNT = 10000
# 周频 COUNT 模拟的步数上限（防 BYDAY 集合异常导致死循环）
_MAX_SIM_DAYS = 100000

_ISO_TO_BYDAY = {v: k for k, v in _BYDAY_TO_ISO.items()}


class _SkipEntry(Exception):
    """单条组件不可导入（计入 skipped_invalid，不连坐整文件）。"""


def import_ics_file(db: Session, path: str) -> dict:
    """导入 .ics 文件并直落库（返回 IcsImportOut 的 dict 形态，router 直填）。

    失败前置（路径 / 编码 / 解析 / 零条目）抛 ValueError 由 router 翻 422；
    单条坏组件只跳过计数（见模块头「尽力而为」）。
    """
    file_path = _check_path(path)
    text = _read_text(file_path)
    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # icalendar 异常形态多样（解析器内部 ValueError/KeyError…）
        raise ValueError("不是有效的日历文件（iCalendar 解析失败）") from exc
    events = list(cal.walk("VEVENT"))
    todos = list(cal.walk("VTODO"))
    if not events and not todos:
        logger.info("[ics] 导入中止：文件内零条目（%s）", file_path.name)
        raise ValueError("文件中未发现日程或待办条目")
    cal_name = str(cal.get("X-WR-CALNAME") or "").strip() or file_path.stem
    cal_tz = str(cal.get("X-WR-TIMEZONE") or "").strip() or None
    source_key = ("ics:" + cal_name)[:60]
    logger.info("[ics] 开始导入：%s（日历「%s」，VEVENT %d / VTODO %d）",
                file_path.name, cal_name, len(events), len(todos))

    # —— 条目映射（坏条目逐条跳过；ValidationError 是 ValueError 子类一并归 invalid）
    candidates: list[tuple[TaskCreate, str]] = []
    invalid = 0
    degraded = 0
    for ev in events:
        try:
            body, fp, was_degraded = _map_event(ev, source_key, cal_tz)
        except (_SkipEntry, ValueError) as exc:
            invalid += 1
            logger.info("[ics] 跳过事件（%s）：UID=%s", exc, ev.get("UID") or "无")
            continue
        degraded += int(was_degraded)
        candidates.append((body, fp))
    for td in todos:
        try:
            body, fp = _map_todo(td, source_key)
        except (_SkipEntry, ValueError) as exc:
            invalid += 1
            logger.info("[ics] 跳过待办（%s）：UID=%s", exc, td.get("UID") or "无")
            continue
        candidates.append((body, fp))

    # —— 两层查重：文件内同指纹二次出现 + 库内指纹命中（镜像 graph.py 同步查重）
    known = _known_fingerprints(db, {fp for _, fp in candidates})
    seen: set[str] = set()
    unique: list[tuple[TaskCreate, str]] = []
    duplicates = 0
    for body, fp in candidates:
        if fp in seen or fp in known:
            duplicates += 1
            continue
        seen.add(fp)
        unique.append((body, fp))
    if duplicates:
        logger.info("[ics] 指纹查重：跳过重复 %d 条", duplicates)
    if degraded:
        logger.info("[ics] 重复规则降级单次 %d 条（INTERVAL>1 / BYSETPOS / EXDATE 等）", degraded)

    # —— 落库（尽力而为：create_task 的 ValueError 不连坐）
    created_events = created_todos = 0
    for body, _fp in unique:
        try:
            task = create_task(db, body)
        except ValueError as exc:
            invalid += 1
            logger.info("[ics] 落库失败跳过「%s」：%s", body.title, exc)
            continue
        if task.start_time is not None:
            created_events += 1
        else:
            created_todos += 1

    if created_events or created_todos:
        workflows.broadcast({"type": "tasks_changed", "source": "ics"})
    logger.info("[ics] 导入完成：创建日程 %d、待办 %d、重复 %d、坏条目 %d、降级 %d",
                created_events, created_todos, duplicates, invalid, degraded)
    if not created_events and not created_todos:
        logger.info("[ics] 本轮零创建（全部重复或跳过）")
    return {
        "ok": True,
        "calendar": cal_name[:40],
        "parsed_events": len(events),
        "parsed_todos": len(todos),
        "created_events": created_events,
        "created_todos": created_todos,
        "skipped_duplicates": duplicates,
        "skipped_invalid": invalid,
        "degraded_recurring": degraded,
    }


# —— 前置校验与读盘 ——————————————————————————————————————————————


def _check_path(path: str) -> Path:
    """路径四查（镜像备份恢复的防御序）：绝对 / 后缀 / 存在 / 大小。"""
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("文件路径须为绝对路径")
    if p.suffix.lower() != ".ics":
        raise ValueError("所选文件不是 .ics 日历文件")
    if not p.is_file():
        raise ValueError("文件不存在")
    if p.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("文件过大（上限 20MB）")
    return p


def _read_text(p: Path) -> str:
    """解码链：utf-8-sig（吃 BOM，Outlook 常见）→ gb18030（中文旧导出）。"""
    data = p.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文件编码无法识别（支持 UTF-8 / GBK 系）")


def _naive_local(dt: datetime, cal_tz: str | None) -> datetime:
    """带时区 → 本地 naive（惯例同 schemas._to_naive_local）；浮动（naive）时间
    按 X-WR-TIMEZONE（Google 导出的全局时区）解释，解析不出则按本地保留。"""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    if cal_tz:
        try:
            return dt.replace(tzinfo=ZoneInfo(cal_tz)).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    return dt


# —— 组件映射 ———————————————————————————————————————————————————


def _map_event(ev, source_key: str, cal_tz: str | None) -> tuple[TaskCreate, str, bool]:
    """VEVENT → (TaskCreate, 指纹, 是否降级)。不可导入抛 _SkipEntry。"""
    if ev.get("RECURRENCE-ID") is not None:
        raise _SkipEntry("系列例外行不单独导入")
    if str(ev.get("STATUS") or "").upper() == "CANCELLED":
        raise _SkipEntry("事件已取消")
    summary = str(ev.get("SUMMARY") or "").strip()
    if not summary:
        raise _SkipEntry("缺少标题")
    dtstart = ev.get("DTSTART")
    if dtstart is None or dtstart.dt is None:
        raise _SkipEntry("缺少开始时间")
    raw_start = dtstart.dt

    end: datetime | None = None
    if isinstance(raw_start, datetime):
        # 带时刻：DTEND / DURATION 推 end，皆无则 60 分钟缺省（结构化直映射先例）
        all_day = False
        start = _naive_local(raw_start, cal_tz)
        dtend = ev.get("DTEND")
        if dtend is not None and dtend.dt is not None:
            v = dtend.dt
            end = _naive_local(v, cal_tz) if isinstance(v, datetime) else datetime(v.year, v.month, v.day)
        else:
            dur = ev.get("DURATION")
            if dur is not None and dur.dt is not None:
                end = start + dur.dt  # timedelta；负值由 create_task 的 end>start 校验拦
        duration = None if end is not None else 60
    else:
        # 全天（VALUE=DATE）：DTEND 为 RFC 排他日界，恰合 Clara [start,end) 半开区间
        all_day = True
        start = datetime(raw_start.year, raw_start.month, raw_start.day)
        dtend = ev.get("DTEND")
        if dtend is not None and dtend.dt is not None:
            v = dtend.dt
            d = v.date() if isinstance(v, datetime) else v
            end = datetime(d.year, d.month, d.day)
            if end == start + timedelta(days=1):
                end = None  # 恰一天 → Clara 单日规范形（end 空，渲染按 start+24h）
        duration = None

    recurrence, was_degraded = _map_rrule(ev, start)
    uid = str(ev.get("UID") or "").strip()
    fp = _event_fingerprint(uid, summary, start, end, all_day)
    body = TaskCreate(
        title=summary[:30],
        duration_mins=duration,
        start_time=start,
        end_time=end,
        all_day=all_day,
        recurrence_rule=recurrence,
        notes=_compose_notes(ev),
        source_key=source_key,
        source_fingerprint=fp,
    )
    return body, fp, was_degraded


def _map_todo(td, source_key: str) -> tuple[TaskCreate, str]:
    """VTODO → 收件箱待办（start_time 恒空；DUE 不参与排期）。"""
    if str(td.get("STATUS") or "").upper() == "CANCELLED":
        raise _SkipEntry("待办已取消")
    summary = str(td.get("SUMMARY") or "").strip()
    if not summary:
        raise _SkipEntry("缺少标题")
    duration = None
    dur = td.get("DURATION")
    if dur is not None and dur.dt is not None:
        mins = round(dur.dt.total_seconds() / 60)
        if 1 <= mins <= 1440:
            duration = mins
    uid = str(td.get("UID") or "").strip()
    raw = f"ics:{uid or summary}|todo"
    fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    body = TaskCreate(
        title=summary[:30],
        duration_mins=duration,
        notes=_compose_notes(td),
        priority=_map_priority(td.get("PRIORITY")),
        source_key=source_key,
        source_fingerprint=fp,
    )
    return body, fp


def _compose_notes(comp) -> str | None:
    """DESCRIPTION（icalendar 已还原 \\n \\, \\; 转义）+ LOCATION 拼合，截 500。"""
    desc = str(comp.get("DESCRIPTION") or "").strip()
    loc = str(comp.get("LOCATION") or "").strip()
    parts = [p for p in (desc, f"地点：{loc}" if loc else None) if p]
    return "\n".join(parts)[:500] if parts else None


def _map_priority(raw) -> int | None:
    """ics PRIORITY（RFC：0=无、1 最高、9 最低）→ Clara 1高/2中/3低；0/缺 → None。"""
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= p <= 3:
        return 1
    if 4 <= p <= 5:
        return 2
    if 6 <= p <= 9:
        return 3
    return None


def _event_fingerprint(uid: str, summary: str, start: datetime,
                       end: datetime | None, all_day: bool) -> str:
    """VEVENT 指纹：UID + 起始锚（同文件重导全跳过、改时间 = 新条目）；UID 缺失
    用标题+起止复合兜底（仍可判同文件重导）。"""
    anchor = start.date().isoformat() if all_day else start.strftime("%Y%m%dT%H%M%S")
    raw = f"ics:{uid}|{anchor}" if uid else f"ics:{summary}|{anchor}|{end or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# —— RRULE 子集换算 —————————————————————————————————————————————


def _map_rrule(ev, base: datetime) -> tuple[str | None, bool]:
    """组件 RRULE → (Clara 子集串 | None, 是否降级)。无 RRULE → (None, False)。

    可精确表达：DAILY / WEEKLY(±BYDAY，缺省补 DTSTART 星期) / MONTHLY(每月同
    日号)，均可带 UNTIL；COUNT 换算为「第 N 次出现日 +T235959」的 UNTIL。其余
    （YEARLY / INTERVAL>1 / 子集外键 / EXDATE / RDATE / 病态参数）降级单次。
    """
    prop = ev.get("RRULE")
    if prop is None:
        return None, False
    if ev.get("EXDATE") is not None or ev.get("RDATE") is not None:
        return None, True  # 例外/追加实例走墓碑行建模，导入期无法表达，宁降级
    freqs = [str(x).upper() for x in (prop.get("FREQ") or [])]
    freq = freqs[0] if len(freqs) == 1 else ""
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return None, True
    if _DEGRADE_KEYS & set(prop.keys()):
        return None, True
    try:
        if int((prop.get("INTERVAL") or [1])[0]) != 1:
            return None, True
    except (TypeError, ValueError):
        return None, True

    byday: list[str] = []
    if freq == "WEEKLY":
        raw_byday = [str(x).upper() for x in (prop.get("BYDAY") or [])]
        byday = [b for b in raw_byday if b in _BYDAY_TO_ISO]
        if len(byday) != len(raw_byday):
            return None, True  # 含序数前缀（如 2MO）等子集外码
        if not byday:
            byday = [_ISO_TO_BYDAY[base.isoweekday()]]  # 无 BYDAY → DTSTART 星期
        rule = f"FREQ=WEEKLY;BYDAY={','.join(byday)}"
    else:
        # MONTHLY 仅支持「每月同日号」（Clara 语义）；带 BYDAY（每月第 N 个周 X，
        # icalendar 保留序数码如 1MO）静默忽略会产出错误数据，降级
        if prop.get("BYDAY"):
            return None, True
        rule = f"FREQ={freq}"

    untils = list(prop.get("UNTIL") or [])
    counts = list(prop.get("COUNT") or [])
    if untils:
        u = untils[0]
        if isinstance(u, datetime):
            u = _naive_local(u, None).date()
        if u < base.date():
            return None, True  # 系列已死（UNTIL 早于首次出现）
        rule += f";UNTIL={u.strftime('%Y%m%d')}T235959"  # 「当日仍出现」口径
    elif counts:
        try:
            n = int(counts[0])
        except (TypeError, ValueError):
            return None, True
        if not 1 <= n <= _MAX_COUNT:
            return None, True
        last = _nth_occurrence_day(freq, byday, base, n)
        if last is None:
            return None, True
        rule += f";UNTIL={last.strftime('%Y%m%d')}T235959"
    if not _RRULE_PATTERN.fullmatch(rule):
        return None, True  # 兜底（正常路径不可达）
    return rule, False


def _nth_occurrence_day(freq: str, byday: list[str], base: datetime, n: int) -> date | None:
    """模拟规则第 n 次出现的日期（INTERVAL=1；与 _rule_occurs 同口径按日判定）。
    返回 None = 超出步数上限（病态规则，调用方降级）。"""
    if freq == "DAILY":
        return base.date() + timedelta(days=n - 1)
    if freq == "WEEKLY":
        weekdays = {_BYDAY_TO_ISO[b] for b in byday}
        day, steps = base.date(), 0
        while steps < _MAX_SIM_DAYS:
            if day.isoweekday() in weekdays:
                n -= 1
                if n == 0:
                    return day
            day += timedelta(days=1)
            steps += 1
        return None
    return (base + relativedelta(months=n - 1)).date()  # MONTHLY：同日号跨月（月末钳制随 relativedelta）


def _known_fingerprints(db: Session, fps: set[str]) -> set[str]:
    """库内既有指纹集（镜像 agents/graph.py 同步查重先例；500 一片防超大 IN）。"""
    known: set[str] = set()
    items = sorted(f for f in fps if f)
    for i in range(0, len(items), 500):
        known.update(db.scalars(
            select(Task.source_fingerprint).where(
                Task.source_fingerprint.in_(items[i:i + 500]),
                Task.is_deleted.is_(False))))
    return known
