"""统一搜索（2026-11-06 搜索批 60→100 分档迭代）。

匹配语义：SQLite LIKE 对 ASCII 字母默认大小写不敏感（PRAGMA case_sensitive_like
默认 off）、中文子串天然命中——无需 LOWER 折叠；用户输入里的 %/_ 通配符按
字面转义（ESCAPE '\\'，不放大命中面）。

100 分档新增：
- 系列实例展开：recurrence 命中行在 [-90d, +60d] 窗口按 expand_occurrences 出
  实例行（单系列限 8 例，同 QA _expand_qa_rows 思路——搜"每周例会"能看到
  未来实例日期并跳转定位）；窗口无实例则保留基准行
- 范围扩展：项目名（LIKE，上限 5）与数据源页未决采集建议卡的条目标题
  （Python 侧遍历 payload，仅 from_ingest 卡——数据源页可见口径，上限 10）
- include_done 开关：默认隐藏已完成（done_at 非空），开启后保留

拼音首字母 / 时间词解析在 120 分档补齐（docs/search.md）。取舍：单轮总上限
50 无分页；系列展开窗口口径 [-90d, +60d] 覆盖"找回过去 + 安排将来"主场景。

120 分档新增：
- 拼音/首字母：q 为纯 ASCII 字母时，任务标题按全拼与首字母串 contains 匹配
  （惰性逐行计算 + lru_cache，百级量级无索引压力；"rh" 命中"乳鸽汤"）
- 自然语言时间词：解析「今天/明天/昨天/本周/上周/下周/周X/上周X/本月/上月/
  下月」→ 时间窗过滤并把该词从关键词剥离（「上周 体检」= 上周内搜体检；时间
  词单独出现 = 该窗口的任务清单）。响应 parsed_range 带回供前端展示过滤标签
"""

import re
from datetime import datetime, timedelta
from functools import lru_cache

from pypinyin import Style, lazy_pinyin
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import Project, Proposal, Task
from schemas import (
    SearchOut,
    SearchProposalItemOut,
    SearchProjectOut,
    SearchRangeOut,
    SearchTaskOut,
)
from services.tasks import _attach_cancelled_dates, expand_occurrences

# 单轮总上限（标题/备注命中行数封顶；组内再分配在前端分组时截）
SEARCH_LIMIT = 50
# 系列实例展开：窗口与单系列上限（实例行按时间升序取前 8）
SERIES_WINDOW_BACK_DAYS = 90
SERIES_WINDOW_FWD_DAYS = 60
SERIES_INSTANCE_LIMIT = 8
PROJECT_LIMIT = 5
PROPOSAL_ITEM_LIMIT = 10


def _escape_like(q: str) -> str:
    """LIKE 通配符按字面转义（%/_/\\ 各自转义，配合 ESCAPE '\\' 子句）"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match(text: str | None, q: str) -> bool:
    """Python 侧大小写不敏感子串（proposal 条目标题用；SQL LIKE 之外的补充）"""
    return text is not None and q.lower() in text.lower()


# —— 120 分档：拼音/首字母匹配 ——


@lru_cache(maxsize=2048)
def _pinyin_keys(title: str) -> tuple[str, str]:
    """标题 → (全拼串, 首字母串)，lru_cache 惰性记忆（百级标题重复搜索零重算）"""
    return (
        "".join(lazy_pinyin(title)).lower(),
        "".join(lazy_pinyin(title, style=Style.FIRST_LETTER)).lower(),
    )


def _match_pinyin(title: str, ql: str) -> bool:
    """纯字母 q 对标题的拼音匹配（含英文标题自身的 contains）"""
    if ql in title.lower():
        return True
    full, initials = _pinyin_keys(title)
    return ql in full or ql in initials


# —— 120 分档：自然语言时间词解析 ——

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_AR_NUM = {str(i): i for i in range(1, 8)}
# 周X / 周三：先剥离「上周/下周/本周」前缀再取星期；带星期的组合词必须排在
# 纯周词之前（regex alternance 有序——「上周」在前会把「上周三」截成「上周」）
_TIME_WORD_RE = re.compile(
    r"(今天|明天|昨天|后天|前天"
    r"|(?:上|下)?周([一二三四五六日天1-7])"
    r"|本周|这周|上周|下周|本月|这月|上月|下月)"
)
_DAY_DELTA = {"今天": 0, "明天": 1, "昨天": -1, "后天": 2, "前天": -2}


def _week_anchor(day0: datetime, week_shift: int) -> datetime:
    """本周/上周/下周的周一 0 点（week_shift 0/-1/+1；ISO 周一起）"""
    monday = day0 - timedelta(days=day0.weekday())
    return monday + timedelta(weeks=week_shift)


def _parse_time_range(q: str) -> tuple[datetime, datetime, str] | None:
    """解析 q 里的首个时间词 → (窗起, 窗止, 命中词)；无时间词返回 None。

    词集：单日（今天/明天/昨天/后天/前天）、周窗（本周/这周/上周/下周）、
    月窗（本月/这月/上月/下月）、周X 单日（本周周X；上周X = 上周该日）。
    窗止为开区间（[lo, hi)），与任务窗口查询口径一致。
    """
    m = _TIME_WORD_RE.search(q)
    if not m:
        return None
    word = m.group(0)
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if word in _DAY_DELTA:
        lo = day0 + timedelta(days=_DAY_DELTA[word])
        return lo, lo + timedelta(days=1), word
    if word in ("本周", "这周"):
        lo = _week_anchor(day0, 0)
        return lo, lo + timedelta(days=7), word
    if word == "上周":
        lo = _week_anchor(day0, -1)
        return lo, lo + timedelta(days=7), word
    if word == "下周":
        lo = _week_anchor(day0, 1)
        return lo, lo + timedelta(days=7), word
    if word in ("本月", "这月", "上月", "下月"):
        first = day0.replace(day=1)
        if word == "上月":
            lo = (first - timedelta(days=1)).replace(day=1)
        elif word == "下月":
            lo = (first + timedelta(days=32)).replace(day=1)
        else:
            lo = first
        hi = (lo + timedelta(days=32)).replace(day=1)
        return lo, hi, word
    # 周X / 上周X / 下周X：正则已保证星期位合法
    weekday = _CN_NUM.get(m.group(2)) or _AR_NUM.get(m.group(2))  # ISO: 周一=1..周日=7
    iso_wd = 7 if weekday == 7 else weekday
    shift = 0
    if word.startswith("上"):
        shift = -1
    elif word.startswith("下"):
        shift = 1
    lo = _week_anchor(day0, shift) + timedelta(days=iso_wd - 1)
    return lo, lo + timedelta(days=1), word


def _expand_series_rows(rows: list[SearchTaskOut]) -> list[SearchTaskOut]:
    """系列命中行展开为窗口内实例行（start/end 替换为实例时刻）。

    有实例则整行替换（每实例一行、可独立跳转）；窗口无实例保留基准行。
    end = 实例 + duration（无时长按默认一小时卡口径 60min）。
    """
    lo = datetime.now() - timedelta(days=SERIES_WINDOW_BACK_DAYS)
    hi = datetime.now() + timedelta(days=SERIES_WINDOW_FWD_DAYS)
    out: list[SearchTaskOut] = []
    # 展开需要 ORM 行上的 recurrence_rule/start_time——SearchTaskOut 已带；
    # expand_occurrences 按 getattr 读字段，pydantic 模型同样可用
    for row in rows:
        if not row.recurrence_rule or not row.start_time:
            out.append(row)
            continue
        occs = expand_occurrences(row, lo, hi)[:SERIES_INSTANCE_LIMIT]
        if not occs:
            out.append(row)
            continue
        dur = timedelta(minutes=row.duration_mins or 60)
        for occ in occs:
            out.append(row.model_copy(update={"start_time": occ, "end_time": occ + dur}))
    return out


def search_all(db: Session, q: str, *, include_done: bool = False) -> SearchOut:
    """聚合搜索：任务（含系列实例展开/拼音/时间窗）+ 项目 + 数据源建议条目。"""
    raw = (q or "").strip()
    if not raw:
        return SearchOut()

    # 时间词解析与剥离（120 分档）：「上周 体检」→ 窗口 + 关键词"体检"；
    # 词后关键词为空 = 纯时间浏览（该窗口任务清单）
    parsed = _parse_time_range(raw)
    if parsed:
        lo, hi, word = parsed
        kw = raw.replace(word, " ", 1)
        kw = re.sub(r"\s+", " ", kw).strip()
        range_out = SearchRangeOut(label=word, start=lo, end=hi)
    else:
        lo = hi = None
        kw = raw
        range_out = None

    # —— 任务匹配（软删恒滤 / 完成态按开关）——
    base_conds = [Task.is_deleted.is_(False)]
    if not include_done:
        base_conds.append(Task.done_at.is_(None))
    if not kw:
        # 纯时间词：窗口内全量（无关键词面，拉全活跃行再按窗筛）
        tasks = list(db.scalars(select(Task).where(*base_conds).order_by(Task.sort_order)))
    elif re.fullmatch(r"[a-z]+", kw.lower()):
        # 纯字母：拼音/首字母分支（SQL 摸不到拼音，全量拉取 Python 侧匹配）
        ql = kw.lower()
        cand = list(db.scalars(select(Task).where(*base_conds).order_by(Task.sort_order)))
        tasks = [t for t in cand if _match_pinyin(t.title, ql) or _match(t.notes, ql)][:SEARCH_LIMIT]
    else:
        pat = f"%{_escape_like(kw)}%"
        tasks = list(
            db.scalars(
                select(Task)
                .where(
                    *base_conds,
                    or_(Task.title.like(pat, escape="\\"), Task.notes.like(pat, escape="\\")),
                )
                .order_by(Task.sort_order)
                .limit(SEARCH_LIMIT)
            )
        )

    task_rows: list[SearchTaskOut] = []
    if tasks:
        _attach_cancelled_dates(db, tasks)
        ids = {t.project_id for t in tasks if t.project_id}
        names = (
            {p.id: p.name for p in db.scalars(select(Project).where(Project.id.in_(ids)))}
            if ids
            else {}
        )
        for t in tasks:
            row = SearchTaskOut.model_validate(t)
            row.project_name = names.get(t.project_id)
            task_rows.append(row)
        task_rows = _expand_series_rows(task_rows)
        # 时间窗过滤在展开后做：系列实例逐例落窗（基准行也按其 start_time 判）
        if lo is not None:
            task_rows = [r for r in task_rows if r.start_time and lo <= r.start_time < hi]

    # —— 项目 / 建议条目：不受时间窗影响；纯时间词时也照常按关键词匹配 ——
    pat = f"%{_escape_like(kw)}%" if kw else ""
    projects = (
        [
            SearchProjectOut(id=p.id, name=p.name)
            for p in db.scalars(
                select(Project).where(Project.name.like(pat, escape="\\")).limit(PROJECT_LIMIT)
            )
        ]
        if kw
        else []
    )
    ql = (kw or "").lower()
    proposal_items: list[SearchProposalItemOut] = []
    if kw:
        for p in db.scalars(select(Proposal).where(Proposal.outcome.is_(None))):
            payload = p.payload or {}
            if not payload.get("from_ingest"):
                continue
            for key, kind in (("items", "inbox"), ("draft_plan", "schedule")):
                for idx, entry in enumerate(payload.get(key) or []):
                    if len(proposal_items) >= PROPOSAL_ITEM_LIMIT:
                        break
                    if _match(entry.get("title"), ql):
                        proposal_items.append(
                            SearchProposalItemOut(
                                proposal_id=p.id, idx=idx, kind=kind, title=entry["title"]
                            )
                        )
    return SearchOut(
        tasks=task_rows[:SEARCH_LIMIT],
        projects=projects,
        proposal_items=proposal_items,
        parsed_range=range_out,
    )
