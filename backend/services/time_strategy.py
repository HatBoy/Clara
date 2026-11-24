"""在不写日历的前提下对照候选排期，以及用户实际调整过的计划时长。"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AppSettings, BehaviorEvent, EstimatePreference, MemoryRule, Task
from services.job_rules import MEETING_KEYWORDS_RE
from services.settings import DEFAULTS
from services.tasks import expand_occurrences, list_tasks, normalize_title

FOCUS_MIN = 60


def _longest(lo, hi, busy):
    cursor, longest = lo, 0
    for start, end in sorted(busy):
        if start >= hi:
            break
        if end <= cursor:
            continue
        longest = max(longest, (start - cursor).total_seconds() / 60)
        cursor = max(cursor, end)
    return int(max(longest, (hi - cursor).total_seconds() / 60))


def focus_preview(db: Session, task_id: str, original: date, day: date, start_min: int, end_min: int, *, earlier=()):
    task = db.get(Task, task_id)
    if not task or task.is_deleted or task.all_day or end_min <= start_min:
        raise ValueError('无效的候选区间或任务')
    midnight = datetime.combine(day, time.min)
    settings = db.get(AppSettings, 1)
    def setting(key):
        return getattr(settings, key) if settings else DEFAULTS[key]
    def at(minute):
        return midnight + timedelta(minutes=minute)
    def buffered(begin, end, title):
        if setting('meeting_buffer_enabled') and MEETING_KEYWORDS_RE.search(title):
            from agents.engine import MEETING_HEAD_MIN, MEETING_TAIL_MIN
            return begin - timedelta(minutes=MEETING_HEAD_MIN), end + timedelta(minutes=MEETING_TAIL_MIN)
        return begin, end
    lo, hi = at(setting('calendar_start_min')), at(setting('calendar_end_min'))
    others = [(at(setting('lunch_start_min')), at(setting('lunch_end_min')))] if setting('lunch_enabled') else []
    removed = []
    for row in list_tasks(db, start=midnight, end=at(1440)):
        if not row.start_time:
            continue
        span = (row.end_time - row.start_time if row.end_time else
                timedelta(minutes=1440 if row.all_day else row.duration_mins or 60))
        starts = (expand_occurrences(row, midnight - span, at(1440)) if row.recurrence_rule else [row.start_time])
        for begin in starts:
            dest = removed if row.id == task_id and begin.date() == original else others
            dest.append(buffered(begin, begin + span, row.title))
    others.extend(buffered(begin, end, title) for begin, end, title in earlier)
    new = buffered(at(start_min), at(end_min), task.title)
    before = _longest(lo, hi, others + removed)
    after = _longest(lo, hi, others + [new])
    lost = day == date.today() + timedelta(days=1) and before >= FOCUS_MIN > after
    alternative = None
    if lost:
        for m in sorted(range(setting('calendar_start_min'), setting('calendar_end_min'), 15), key=lambda x: abs(x-start_min)):
            candidate = buffered(at(m), at(m+end_min-start_min), task.title)
            if (candidate[0] < lo or candidate[1] > hi or
                    any(a < candidate[1] and candidate[0] < b for a,b in others)):
                continue
            if _longest(lo, hi, others + [candidate]) >= FOCUS_MIN:
                alternative = at(m).strftime('%H:%M')
                break
    return {'before':before, 'after':after, 'last_focus':lost, 'alternative':alternative}


def nightly_focus_note(db, draft, tomorrow):
    """同日夜间认领只写一张卡；只对确实会占掉明日末段专注时间的预排出声。"""
    earlier = []
    for item in draft:
        if not item.get('task_id') or not item.get('start'):
            continue
        start = datetime.fromisoformat(item['start'])
        if start.date() != tomorrow:
            continue
        minute = start.hour * 60 + start.minute
        end = minute + int(item.get('duration') or 60)
        if end > 1440:
            continue
        proposed = (start, start + timedelta(minutes=end-minute), item.get('title') or '')
        if not MEETING_KEYWORDS_RE.search(proposed[2]):
            earlier.append(proposed)
            continue
        try:
            delta = focus_preview(db, item['task_id'], tomorrow, tomorrow, minute, end, earlier=earlier)
        except ValueError:
            continue
        earlier.append(proposed)
        if delta['last_focus']:
            alt = f"；可考虑 {delta['alternative']}" if delta['alternative'] else '；当日未找到保留完整专注时间的替代槽'
            return f"若采纳「{item['title']}」，明天最长连续空档由 {delta['before']} 缩至 {delta['after']} 分钟{alt}。日历尚未修改。"
    return None


def weekly_calibrations(db: Session, *, now=None):
    now = now or datetime.now()
    rows = db.execute(select(BehaviorEvent, Task.title).join(Task, Task.id == BehaviorEvent.task_id).where(
        BehaviorEvent.created_at >= now - timedelta(days=7), BehaviorEvent.created_at <= now,
        BehaviorEvent.kind == 'resize', BehaviorEvent.planned_min.is_not(None)))
    groups = defaultdict(lambda: defaultdict(list))
    names = {}
    for event, title in rows:
        key = normalize_title(title)
        if (event.context or {}).get("temporary_exception"):
            continue
        if not key or not event.planned_min or not event.delta_min or event.created_at.weekday() >= 5:
            continue
        names[key] = title
        groups[key][event.created_at.date()].append((event.planned_min,event.planned_min+event.delta_min))
    result = []
    for key, days in groups.items():
        if len(days) < 3:
            continue
        old, new = (round(median([median(v[i] for v in events) for events in days.values()])) for i in (0,1))
        if 0 < new <= 1440 and abs(new-old) >= 5 and calibrated_estimate(db,names[key]) != new:
            result.append({'title':names[key], 'previous':old, 'minutes':new, 'days':len(days)})
    return sorted(result, key=lambda item:(-item['days'],item['title']))


def calibrated_estimate(db: Session, title: str):
    row = db.get(EstimatePreference, normalize_title(title))
    return row.minutes if row else None


def accept_calibration(db: Session, title: str, minutes: int):
    if not any(item['title'] == title and item['minutes'] == minutes for item in weekly_calibrations(db)):
        if calibrated_estimate(db, title) == minutes:
            return
        raise ValueError('估时证据已变化，请刷新')
    db.merge(EstimatePreference(title_key=normalize_title(title), minutes=minutes))
    db.commit()


def pause_this_week(db: Session, rule_id: str, pause: bool):
    row = db.get(MemoryRule, rule_id)
    if not row:
        raise ValueError('规则已不存在')
    row.pause_until = date.today() + timedelta(days=6-date.today().weekday()) if pause else None
    db.commit()
    return row
