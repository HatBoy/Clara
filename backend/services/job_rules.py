"""定时任务识别规则层（2026-10-20 增，「设置-定时任务」配套）：重要会议与
高难任务的纯规则判定——零 LLM、透明可解释（口径在定时任务页 desc 同源展示）。

设计定调（docs/scheduled-jobs.md）：
- 识别层纯规则零 token：priority / 时长 / 标题关键词三分支或；LLM 在生成层
  自带"不重要可 skip"兜底（先例：MERGE 信号不足不归纳、PLANNER unrelated），
  识别不花成本。用户打的 priority 就是最可靠信号。
- 时长口径与既有"高强度"派生口径对齐（nodes.task_intensity /
  calendar_load_summary 均为 ≥90min 或 priority≤1），关键词是第三条独立分支。
- 函数吃鸭子类型对象（不 import models）：SimpleNamespace 任务替身直测；
  pick_* 只做查询 + 委托，真表测（conftest.TestingSessionLocal）。
"""

import re
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from services.tasks import _task_dur, expand_occurrences, list_tasks

# "长会/大任务"阈值（分钟）：90 = 既有高强度派生口径，不另造第二套数
LONG_MEETING_MINS = 90

# 重要会议标题关键词（第三条或分支；review / 1:1 覆盖英文与半角写法）。
# 2026-10-22 起去下划线导出：reminders 分级提前量与排期子图缓冲泛化共用单源
MEETING_KEYWORDS_RE = re.compile(r"会议|评审|汇报|路演|面试|答辩|复盘|review|1:1", re.IGNORECASE)
# 高难任务标题关键词（与会议词表分离：写周报不是难事，评审会也未必是会议）
_HARD_KEYWORDS = re.compile(r"方案|设计|报告|论文|筹备|规划|调研", re.IGNORECASE)


def is_important_meeting(task) -> bool:
    """重要会议三分支或：priority≤1（用户标记的高优）∨ 时长 ≥90 分钟（长会）
    ∨ 标题命中会议关键词。只看任务属性不看时刻——目标日窗口过滤是
    pick_important_meetings 的职责，本函数保持纯判定。"""
    if (getattr(task, "priority", None) or 3) <= 1:
        return True
    if _task_dur(task) >= LONG_MEETING_MINS:
        return True
    return bool(MEETING_KEYWORDS_RE.search(task.title or ""))


def hard_task_score(task, now: datetime) -> float | None:
    """高难任务判定 + 滞留权重：未排期（start_time 空）才有资格；不命中
    （时长 ≥90 ∨ priority≤1 ∨ 关键词）返回 None。权重 = 滞留天数 +
    reschedule_count（改期一次≈多拖一天，量纲折算）+ ever_scheduled 计 0.5
    （排过又拖回的沉没成本）——每晚取最高 1 条，权重即"最该被协助"的排序。"""
    if task.start_time is not None:
        return None
    hit = (
        (task.duration_mins or 0) >= LONG_MEETING_MINS
        or (task.priority or 3) <= 1
        or _HARD_KEYWORDS.search(task.title or "")
    )
    if not hit:
        return None
    score = (now - task.created_at).days + (task.reschedule_count or 0)
    if getattr(task, "ever_scheduled", False):
        score += 0.5
    return score


def pick_important_meetings(db: Session, day: date) -> list[tuple[object, datetime]]:
    """目标日重要会议（实例级，按开始时刻升序），单次与重复系列两路合流：
    ①单次任务走窗口查询（list_tasks(start, end) 按交集过滤）；
    ②重复系列主行（recurrence_rule 非空且非例外行）恒不在窗口查询的
    start_time 语义里可靠命中——「每周一站会」主行基准在上周，窗口查询会漏，
    必须全量取系列行经 expand_occurrences 展开实例判定（墓碑/活体例外日已
    挖空）。返回 (task, 实例开始时刻) 列表；无命中空列表。"""
    lo = datetime.combine(day, datetime.min.time())
    hi = lo + timedelta(days=1)
    picked: list[tuple[object, datetime]] = []
    for t in list_tasks(db, start=lo, end=hi):
        if t.recurrence_rule is not None:
            continue  # 系列走展开路（窗口查询按 OR 谓词会带回系列行，防双计）
        if t.start_time is not None and is_important_meeting(t):
            picked.append((t, t.start_time))
    for t in list_tasks(db):
        if t.recurrence_rule is None or t.parent_id is not None or t.start_time is None:
            continue  # 只看系列主行（例外/墓碑行是子行，日期已聚合进主行）
        for occ in expand_occurrences(t, lo, hi):
            if is_important_meeting(t):
                picked.append((t, occ))
    picked.sort(key=lambda pair: pair[1])
    return picked


def pick_hard_task(db: Session, now: datetime):
    """未排期高难候选里 hard_task_score 最高 1 条（同分取创建早——越早创建
    越先滞留）；无候选返回 None。每晚至多协助 1 条（LLM 预算 + 少打扰）。"""
    best = None
    best_key: tuple[float, datetime] | None = None
    for t in list_tasks(db, unscheduled=True):
        score = hard_task_score(t, now)
        if score is None:
            continue
        key = (-score, t.created_at)  # 分高优先；同分创建早优先
        if best_key is None or key < best_key:
            best, best_key = t, key
    return best


def pick_stalled_tasks(db: Session, *, days: int = 3, limit: int = 3) -> list[dict]:
    """晨间滞留点名原料（2026-11-01 优化批 7）：「排了却不做」的清单——
    ①连续多日有实例且全程未勾完成（每日系列的典型形态）；②单次任务排期
    已过 ≥days 天仍未完成未删除（过期未做）。返回 [{title, note}] 截 limit，
    按滞留信号（连续天数/过期天数）降序。与 pick_hard_task 互补：那边挑
    未排期难任务，这边点名已排却不动的。纯规则零 LLM。"""
    from services.tasks import expand_occurrences, list_tasks

    today = date.today()
    lo = datetime.combine(today - timedelta(days=days), datetime.min.time())
    hi = datetime.combine(today, datetime.min.time())
    stalled: list[tuple[int, str, str]] = []
    for t in list_tasks(db, start=lo, end=hi):
        if t.done_at is not None:
            continue
        if (t.recurrence_rule is not None and t.parent_id is None
                and t.start_time is not None):
            occ_days = {occ.date() for occ in expand_occurrences(t, lo, hi)}
            past = sum(1 for d in occ_days if d < today)
            if past >= days:
                stalled.append((past, t.title, f"连续 {past} 天排而未完成"))
        elif (t.recurrence_rule is None and t.start_time is not None
              and t.start_time < hi - timedelta(days=days - 1)):
            overdue = (today - t.start_time.date()).days
            stalled.append((overdue, t.title, f"排期已过 {overdue} 天未完成"))
    stalled.sort(key=lambda p: -p[0])
    return [{"title": title, "note": note} for _s, title, note in stalled[:limit]]
