"""统计报告服务（2026-11-06 批 D；2026-11-07 口径重定义）：统计页数据聚合 +
AI 周/月报生成。

口径钉死（防双口径漂移）——2026-11-07 起「日程侧」放弃勾选完成信号改日历世界观：

- **完成待办**按 done_at 落窗 ∧ start_time 空（完成时仍未排期的纯待办——
  收件箱有完成闭环，勾选可信）；**排期后完成**（start_time 非空 ∧ done_at）
  是弱信号，不作 KPI，只经 BehaviorEvent done 事件进「实际 vs 计划」洞察；
- **日程发生**（events_held）= 窗口内已开始（occ < now）的排期实例数，重复
  系列经 expand_occurrences 按次展开（墓碑挖空）——「过去即发生」，不依赖
  逐项勾选（用户观察：日程无强制确认，勾选口径恒近零）；
- **时间花费**按已发生日程实例的排期时长（_task_dur：duration_mins 优先、
  回落 end-start——「安排的时长」近似，会被 start/end 不变式重算覆盖；全天
  恒 0 不计）；**排期总时长/负载率**分子 = 窗口内全部实例（含未来今天）时长
  和，同样按次展开；
- **新增待办** = created_at 落窗 ∧ start_time 空且未删（当前形态口径，与
  完成待办对称排除排期项——出身待办不可判：create_task 带时间即置
  ever_scheduled）；**待办消化率** = 完成待办 ÷ 新增待办（>100% 在消化积压）；
- **实际 vs 计划**偏差只用 BehaviorEvent done 事件的 planned_min/delta_min
  （历史真值快照，不受不变式重算影响）——behavior 卡数据源；execution_rhythm
  （勾选口径）保持不动——AI 链多消费方（排期估时对照/幕僚长量化对照）依赖；
- **环比 delta** = 上一同长滚动窗三数差（完成待办/日程发生/排期总分钟）；
  inbox_backlog（时点存量）环比无意义不进 delta；
- **完成滞后**（待办）= created→done 中位天数；**排期提前量**（日程）=
  created→实例开始中位天数（非系列——系列锚点创建日无提前量语义）；
- **AI 协作** = adoption_stats 近 14 天窗 + Proposal kind=schedule 落窗计数；
- 周期起始：周报 = 本周一 0 点，月报 = 本月 1 号 0 点；报告正文第一行
  【总评】约定见 _REPORT_INSTRUCTION。

AI 报告不注入人格（工具属性，照 build_custom_brief 先例）；LLM 异常/未配置
回落纯数据模板文案（报告永远生成得出来，失败只是少了总结）。
"""

import json
import logging
import re
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import BehaviorEvent, EpisodicFact, Project, Proposal, StatsReport, Task
from services.ai_data import (
    adoption_stats,
    aggregate_deviations,
    format_memory_lines,
    list_memory_rules,
)
from services.episodic import fact_lines, list_episodic_facts, search_facts, upsert_fact
from services.llm import call_llm
from services.settings import get_settings
from services.tasks import _task_dur, execution_rhythm, expand_occurrences, list_tasks

logger = logging.getLogger(__name__)


def _day0(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start_of(now: datetime) -> datetime:
    """本周一 0 点（getDay 周日=0 → (day+6)%7 回退天数，口径同前端周视图）。"""
    return _day0(now - timedelta(days=(now.weekday()) % 7))


def month_start_of(now: datetime) -> datetime:
    return _day0(now.replace(day=1))


def period_range(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """周期 [start, end)：week = 本周一起 7 天；month = 本月 1 号起当月天数。"""
    n = now or datetime.now()
    if period == "week":
        start = week_start_of(n)
        return start, start + timedelta(days=7)
    start = month_start_of(n)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def _calculate_warnings(summary: dict, rhythm: dict | None,
                        baseline: dict | None = None) -> list[dict]:
    """规则预警（确定性，不依赖 LLM）：按 KPI 阈值生成问题列表（最多 3 个，
    按优先级排序）。三个用途：AI 洞察的上下文注入、LLM 不可用时的降级文案、
    前端快捷操作按钮行。

    预警规则（文案带具体数字目标）：
    1. 积压 > 20 → 提示清理
    2. 拖延榜首且改期 ≥3 次 → 提示拆解或降级
    3. 负载率 > 100% → 提示过载
    4. 待办消化率 < 60% → 提示执行力下降
    5. 完成滞后 > 7 天 → 提示拖延严重
    6. 个人基线越界（baseline 给定时）——「相对于自己的常态」而非通用阈值：
       负载率超个人 P75（有历史信号时）/ 完成待办低于个人 P25

    action 仅两种真实跳转：open_inbox（收件箱）/ view_week（日程页）。
    """
    warnings = []
    ov = summary["overview"]
    lag = summary["lag"]

    # 1. 收件箱积压预警（高优先级）
    if ov["inbox_backlog"] > 20:
        target = 15
        need_complete = ov["inbox_backlog"] - target
        warnings.append({
            "severity": "high",
            "message": f"收件箱积压 {ov['inbox_backlog']} 项，建议本周完成 {need_complete} 项，降至健康水平（<{target}项）",
            "action": "open_inbox",
        })

    # 2. 拖延榜首预警（高优先级）
    if rhythm and rhythm.get("top_deferred") and len(rhythm["top_deferred"]) > 0:
        top = rhythm["top_deferred"][0]
        if top["count"] >= 3:
            warnings.append({
                "severity": "high",
                "message": f"拖延榜首任务「{top['title']}」已改期 {top['count']} 次，考虑拆解为子任务或降低优先级",
                "action": "open_inbox",
            })

    # 3. 负载率过载预警（中优先级）
    if ov["load_rate"] > 1.0:
        over_hours = round((ov["scheduled_minutes"] - ov["capacity_minutes"]) / 60, 1)
        # range.from/to 是 isoformat 串（summary_base 构造即序列化），先解析再相减
        # ——曾直接 str - str 抛 TypeError（该超载分支长期无数据触发、未被测试覆盖）
        range_days = (
            datetime.fromisoformat(summary["range"]["to"])
            - datetime.fromisoformat(summary["range"]["from"])
        ).days
        warnings.append({
            "severity": "medium",
            "message": f"本期负载率 {round(ov['load_rate'] * 100)}%，超出容量 {over_hours}h，建议减少排期或每日增加 {round(over_hours / range_days, 1)}h 工时",
            "action": "view_week",
        })

    # 4. 待办消化率低预警（中优先级）
    if ov["todo_digest_rate"] is not None and ov["todo_digest_rate"] < 0.6:
        target_rate = 0.75
        current_done = ov["todos_done"]
        target_done = round(ov["todos_created"] * target_rate)
        need_more = max(0, target_done - current_done)
        warnings.append({
            "severity": "medium",
            "message": f"待办消化率仅 {round(ov['todo_digest_rate'] * 100)}%，建议再完成 {need_more} 项待办，提升至 {round(target_rate * 100)}%",
            "action": "open_inbox",
        })

    # 5. 完成滞后严重预警（低优先级）
    if lag["todos_days"] is not None and lag["todos_days"] > 7:
        warnings.append({
            "severity": "low",
            "message": f"待办完成平均滞后 {lag['todos_days']} 天，建议缩短至 5 天以内，提升执行效率",
            "action": "open_inbox",
        })

    # 6. 个人基线越界预警（低优先级，文案带「个人基线」标注）：固定阈值之外
    #    的个性化信号——p50=0 视为无历史信号跳过（冷启动防空报）
    if baseline:
        lq = baseline.get("load_rate")
        if lq and lq["p75"] > 0 and ov["load_rate"] > lq["p75"]:
            warnings.append({
                "severity": "low",
                "message": (f"个人基线：本期负载率 {round(ov['load_rate'] * 100)}% 高于你近 "
                            f"{baseline['periods']} 期常态上限（P75={round(lq['p75'] * 100)}%），注意留缓冲"),
                "action": "view_week",
            })
        tq = baseline.get("todos_done")
        if tq and tq["p25"] > 0 and ov["todos_done"] < tq["p25"]:
            warnings.append({
                "severity": "low",
                "message": (f"个人基线：本期完成待办 {ov['todos_done']} 项低于你近 "
                            f"{baseline['periods']} 期常态下限（P25={tq['p25']} 项），回顾卡点"),
                "action": "open_inbox",
            })

    # 按优先级排序并限制最多3个
    severity_order = {"high": 0, "medium": 1, "low": 2}
    warnings.sort(key=lambda w: severity_order[w["severity"]])
    return warnings[:3]


# 行为推断注入上限（指令层无条数要求，这里防失控兜底）
_PATTERN_MAX = 3


def _calculate_behavior_insights(summary: dict) -> list[str]:
    """行为模式推断（2026-11-09 反思批，确定性零 LLM）：从既有统计数字推断
    隐藏在数据背后的行为模式——比规则预警（问题现象）深一层（行为解释），
    注入洞察/报告/问答供 LLM 解读展开。

    纪律：每条带数据门槛（最小样本量 + 显著差异），不达标静默——宁缺毋滥，
    不牵强附会（用户拍板口径）。规则：
    1. 要事后置：高优完成率显著低于低优——先做轻松的，难事一拖再拖
    2. 要事霸占拖延榜：拖延榜首是高优先级且反复改期——在逃避要事
    3. 估时乐观：完成实际时长持续超计划——排期不留缓冲
    4. 计划执行脱节：排了很多只勾选完成很少——排期与执行两张皮
    """
    out: list[str] = []
    habits = summary.get("habits") or {}
    rhythm = summary.get("rhythm") or {}
    behavior = summary.get("behavior") or {}

    # 1. 要事后置（高优样本 ≥3 且完成率 <50% 且显著低于低优 ≥15 个百分点）
    pri = habits.get("priority_completion") or {}
    high, low = pri.get("high") or {}, pri.get("low") or {}
    if (high.get("created", 0) >= 3 and isinstance(high.get("rate"), (int, float))
            and high["rate"] < 0.5 and isinstance(low.get("rate"), (int, float))
            and high["rate"] < low["rate"] - 0.15):
        out.append(f"高优先级完成率 {round(high['rate'] * 100)}% 明显低于低优先级 "
                   f"{round(low['rate'] * 100)}%——先做轻松的、难事一拖再拖"
                   "（「要事第一」的信号）")

    # 2. 要事霸占拖延榜（榜首高优先级且改期 ≥3 次）
    top = (rhythm.get("top_deferred") or [{}])[0]
    if top.get("priority") == 1 and top.get("count", 0) >= 3:
        out.append(f"拖延榜首「{top['title']}」是高优先级事项且已改期 "
                   f"{top['count']} 次——可能在逃避要事，建议拆解启动或调低预期")

    # 3. 估时乐观（done 事件样本 ≥3 且平均超计划 ≥15 分钟）
    if (isinstance(behavior.get("avg_delta_min"), (int, float))
            and behavior["avg_delta_min"] >= 15
            and behavior.get("done_events", 0) >= 3):
        out.append(f"完成任务实际时长平均比计划多 {behavior['avg_delta_min']} 分钟——"
                   "估时偏乐观，建议排期预留缓冲或把大任务拆小")

    # 4. 计划执行脱节（近 14 天排 ≥5 项且勾选完成率 ≤30%）
    if (rhythm.get("planned_count", 0) >= 5
            and isinstance(rhythm.get("done_rate"), (int, float))
            and rhythm["done_rate"] <= 0.3):
        out.append(f"近 {rhythm.get('window_days', 14)} 天排了 {rhythm['planned_count']} 项"
                   f"只勾选完成 {rhythm.get('done_count', 0)} 项——排期与实际执行脱节，"
                   "建议少排实做或排期前先拆解")

    return out[:_PATTERN_MAX]


def _calculate_health_score(summary: dict, rhythm: dict | None) -> dict:
    """健康度评分：5 个维度各 0-100 分 + 加权总分（统计页雷达图与月报成长对比共用）。

    仅依赖 summary 的 overview/lag/behavior/ai 块（rhythm 参数保留为兼容占位，
    现不参与评分——报告周期模式的 summary 亦有这四块，可独立调用）。

    维度：
    1. 待办消化率：todo_digest_rate * 100（>75% = 满分）
    2. 负载健康度：100 - |load_rate - 0.75| * 200（最佳75%，偏离扣分）
    3. 排期提前量：归一化到0-100（>5天 = 满分）
    4. 执行节奏：基于拖延次数反向评分（0次 = 满分）
    5. AI协作度：采纳率 * 100
    """
    ov = summary["overview"]
    lag = summary["lag"]
    ai = summary["ai"]

    scores = {}

    # 1. 待办消化率（>75% = 100分）
    digest = ov.get("todo_digest_rate")
    if digest is not None:
        scores["digest"] = min(100, round(digest * 100 / 0.75))
    else:
        scores["digest"] = 50  # 无数据默认中等

    # 2. 负载健康度（最佳75%，偏离惩罚）
    load = ov.get("load_rate", 0)
    deviation = abs(load - 0.75)
    scores["load"] = max(0, round(100 - deviation * 200))

    # 3. 排期提前量（>5天 = 100分）
    advance_days = lag.get("events_days")
    if advance_days is not None and advance_days > 0:
        scores["advance"] = min(100, round(advance_days * 20))
    else:
        scores["advance"] = 50  # 无数据默认中等

    # 4. 执行节奏（拖延次数反向，0次=100分，每次-10分）
    reschedules = summary["behavior"].get("reschedules", 0)
    scores["rhythm"] = max(0, 100 - reschedules * 5)

    # 5. AI协作度（采纳率 * 100）
    adoption = ai.get("adoption", {})
    rate = adoption.get("rate")
    if rate is not None:
        scores["ai_collab"] = round(rate * 100)
    else:
        scores["ai_collab"] = 50  # 无数据默认中等

    # 计算总体健康度（加权平均）
    weights = {"digest": 0.25, "load": 0.2, "advance": 0.15, "rhythm": 0.25, "ai_collab": 0.15}
    total = sum(scores[k] * weights[k] for k in weights)

    return {
        "dimensions": scores,
        "total": round(total),
        "labels": {
            "digest": "待办消化",
            "load": "负载健康",
            "advance": "排期提前",
            "rhythm": "执行节奏",
            "ai_collab": "AI协作",
        }
    }


def _calculate_habits(db: Session, lo: datetime, hi: datetime) -> dict:
    """习惯模式分析：完成/改期时段分布、偏好项目、优先级完成率 + 热力图原始矩阵。

    分析维度：
    1. 最高效时段（按星期几 + 时段聚合完成数）
    2. 拖延高发期（按星期几 + 时段聚合改期数）
    3. 偏好项目类型（按项目聚合时间花费占比）
    4. 优先级倾向（按优先级计算完成率）
    """
    now = datetime.now()

    # 1. 完成任务时段分析（待办+日程，按星期几+时段聚合）
    done_rows = list(db.scalars(select(Task).where(
        Task.is_deleted.is_(False),
        Task.done_at.is_not(None),
        Task.done_at >= lo, Task.done_at < hi,
    )))

    weekday_hour_done = {}  # {(weekday, hour): count}
    for t in done_rows:
        wd = t.done_at.weekday()  # 0=周一
        h = t.done_at.hour
        key = (wd, h)
        weekday_hour_done[key] = weekday_hour_done.get(key, 0) + 1

    # 找最高效时段（完成数最多）
    best_slot = None
    if weekday_hour_done:
        best_key = max(weekday_hour_done.items(), key=lambda x: x[1])
        wd, h = best_key[0]
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        period = "上午" if h < 12 else "下午" if h < 18 else "晚上"
        best_slot = f"{weekday_names[wd]}{period}（完成 {best_key[1]} 项）"

    # 2. 改期事件时段分析（拖延高发期）
    reschedule_events = list(db.scalars(select(BehaviorEvent).where(
        BehaviorEvent.kind == "reschedule",
        BehaviorEvent.created_at >= lo, BehaviorEvent.created_at < hi,
    )))

    weekday_hour_defer = {}
    for e in reschedule_events:
        wd = e.created_at.weekday()
        h = e.created_at.hour
        key = (wd, h)
        weekday_hour_defer[key] = weekday_hour_defer.get(key, 0) + 1

    defer_slot = None
    if weekday_hour_defer:
        defer_key = max(weekday_hour_defer.items(), key=lambda x: x[1])
        wd, h = defer_key[0]
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        period = "上午" if h < 12 else "下午" if h < 18 else "晚上"
        defer_slot = f"{weekday_names[wd]}{period}（改期 {defer_key[1]} 次）"

    # 3. 偏好项目类型（已发生日程的时间花费占比）
    sched_rows = list_tasks(db, start=lo, end=hi)
    proj_names = {p.id: p.name for p in db.scalars(select(Project))}
    proj_minutes: dict[str, int] = {}
    total_minutes = 0

    for t in sched_rows:
        if getattr(t, "all_day", False):
            continue
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            if occ < now:
                name = proj_names.get(t.project_id, "未归类")
                proj_minutes[name] = proj_minutes.get(name, 0) + dur
                total_minutes += dur

    top_project = None
    top_project_pct = None
    if proj_minutes and total_minutes > 0:
        top = max(proj_minutes.items(), key=lambda x: x[1])
        top_project = top[0]
        top_project_pct = round(top[1] / total_minutes * 100)

    # 4. 优先级完成率（完成数 ÷ 创建数）
    priority_completion = {}
    for pri_val, pri_key in [(1, "high"), (2, "medium"), (3, "low")]:
        created = db.scalar(select(func.count()).select_from(Task).where(
            Task.is_deleted.is_(False),
            Task.priority == pri_val,
            Task.created_at >= lo, Task.created_at < hi,
        )) or 0
        done = db.scalar(select(func.count()).select_from(Task).where(
            Task.is_deleted.is_(False),
            Task.priority == pri_val,
            Task.done_at.is_not(None),
            Task.done_at >= lo, Task.done_at < hi,
        )) or 0
        priority_completion[pri_key] = {
            "created": created,
            "done": done,
            "rate": round(done / created, 2) if created > 0 else None,
        }

    # 找完成率最高的优先级
    best_priority = None
    best_priority_rate = None
    for key, val in priority_completion.items():
        if val["rate"] is not None and (best_priority_rate is None or val["rate"] > best_priority_rate):
            best_priority_rate = val["rate"]
            best_priority = key

    priority_labels = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}
    best_priority_text = None
    if best_priority and best_priority_rate:
        best_priority_text = f"{priority_labels[best_priority]}完成率最高（{round(best_priority_rate * 100)}%）"

    # 热力图原始矩阵（7×24，weekday 0-6 行、hour 0-23 列）
    heatmap_done = [[weekday_hour_done.get((wd, h), 0) for h in range(24)] for wd in range(7)]
    heatmap_defer = [[weekday_hour_defer.get((wd, h), 0) for h in range(24)] for wd in range(7)]

    return {
        "best_slot": best_slot,
        "defer_slot": defer_slot,
        "top_project": top_project,
        "top_project_pct": top_project_pct,
        "priority_completion": priority_completion,
        "best_priority_text": best_priority_text,
        "heatmap_done": heatmap_done,
        "heatmap_defer": heatmap_defer,
    }


def _period_buckets(db: Session, days: int, periods: int = 4) -> list[dict]:
    """同长周期分桶取数（环比/均值基线/分位基线共用）：过去 periods 个同长
    窗口的 done/created/held/mins 计数。性能口径与 stats_summary 的环比 delta
    同款加宽窗——待办完成/创建各一次查宽窗、日程一次 list_tasks 宽窗，
    Python 按周期分桶（消逐周期全量扫描与系列重复展开）。"""
    now = datetime.now()
    lo = _day0(now) - timedelta(days=days - 1)  # 当前窗口下界（各期紧贴其前）
    wide_lo = _day0(now) - timedelta(days=days * (periods + 1) - 1)

    def _bucket_of(ts: datetime) -> int | None:
        """时间戳 → 周期桶下标（0 = 最近一期）；当前窗内或宽窗外返回 None。"""
        if ts >= lo:
            return None
        for i in range(periods):
            if _day0(now) - timedelta(days=days * (i + 1) - 1) <= ts:
                return i
        return None

    buckets = [dict(done=0, created=0, held=0, mins=0) for _ in range(periods)]

    done_rows = db.scalars(select(Task).where(
        Task.is_deleted.is_(False),
        Task.start_time.is_(None),
        Task.done_at.is_not(None),
        Task.done_at >= wide_lo, Task.done_at < lo,
    ))
    for t in done_rows:
        i = _bucket_of(t.done_at)
        if i is not None:
            buckets[i]["done"] += 1

    created_rows = db.scalars(select(Task).where(
        Task.is_deleted.is_(False),
        Task.start_time.is_(None),
        Task.created_at >= wide_lo, Task.created_at < lo,
    ))
    for t in created_rows:
        i = _bucket_of(t.created_at)
        if i is not None:
            buckets[i]["created"] += 1

    # 日程侧一次宽窗展开分桶；occ < lo ≤ now 恒成立（宽窗全在过去），无需 now 过滤
    for t in list_tasks(db, start=wide_lo, end=lo):
        dur = _task_dur(t)
        for occ in expand_occurrences(t, wide_lo, lo):
            i = _bucket_of(occ)
            if i is not None:
                buckets[i]["mins"] += dur
                buckets[i]["held"] += 1
    return buckets


# 分位基线的最小样本数（<3 期不硬凑分位——冷启动保护）
_BASELINE_MIN_PERIODS = 3


def _calculate_baseline(db: Session, days: int, periods: int = 8) -> dict:
    """个人分位数基线（近 periods 个同长窗口的 P25/P50/P75）——洞察/报告的
    纵向注入与个性化预警数据源（「相对于自己的常态」而非通用阈值）。

    样本 <3 期的指标分位为 None；p50=0 视为该指标无历史信号（消费侧跳过
    该指标的个人预警，防空报告误报）。"""
    buckets = _period_buckets(db, days, periods)

    def _q(vals: list[float]) -> dict | None:
        vals = [v for v in vals if v is not None]
        # 样本不足或全零（冷启动——零无法区分"真空窗"与"没在用"，视为无信号）
        if len(vals) < _BASELINE_MIN_PERIODS or not any(v > 0 for v in vals):
            return None
        q1, q2, q3 = statistics.quantiles(vals, n=4)
        return {"p25": round(q1, 2), "p50": round(q2, 2), "p75": round(q3, 2)}

    capacity = get_settings(db).work_minutes_per_day * days
    digest = [b["done"] / b["created"] for b in buckets if b["created"] > 0]
    load = [b["mins"] / capacity for b in buckets if capacity > 0]
    return {
        "todos_done": _q([b["done"] for b in buckets]),
        "events_held": _q([b["held"] for b in buckets]),
        "todo_digest_rate": _q(digest),
        "load_rate": _q(load),
        "periods": periods,
    }


def _calculate_prev_periods_avg(db: Session, days: int, periods: int = 4) -> dict:
    """历史平均基线（过去 N 个同长周期的滚动平均，供 KPI 个性化对比箭头；
    页面契约形状不动）。分桶取数单源 _period_buckets。"""
    buckets = _period_buckets(db, days, periods)
    digest_rates = [b["done"] / b["created"] for b in buckets if b["created"] > 0]
    capacity = get_settings(db).work_minutes_per_day * days
    load_rates = [b["mins"] / capacity for b in buckets if capacity > 0]
    return {
        "todos_done": round(sum(b["done"] for b in buckets) / periods, 1),
        "events_held": round(sum(b["held"] for b in buckets) / periods, 1),
        "todo_digest_rate": round(sum(digest_rates) / len(digest_rates), 2) if digest_rates else None,
        "load_rate": round(sum(load_rates) / len(load_rates), 2) if load_rates else None,
    }


def stats_summary(db: Session, *, days: int = 30, lo: datetime | None = None,
                  hi: datetime | None = None) -> dict:
    """统计聚合（默认近 N 天含今天；显式 lo/hi 供报告周期复用，[lo, hi) 半开）。

    overview（含 delta 环比与负载率）/ daily_done（待办日程分列）/ by_project /
    by_priority / rhythm / behavior / ai / lag 各块，形状即统计页契约（routers 直出）。

    页面滚动窗口模式（lo/hi 缺省按 days 推得）额外附带 warnings（规则预警）/
    prev_avg（历史平均基线）/ habits（习惯模式）/ health（健康度评分）；报告
    周期模式（显式 lo/hi）不带这四块——留档报告只做周期聚合。"""
    now = datetime.now()
    if lo is None:
        lo = _day0(now) - timedelta(days=days - 1)
    if hi is None:
        hi = _day0(now) + timedelta(days=1)

    # —— 待办侧（勾选口径可信——收件箱有完成闭环）——
    # 完成行一次查宽到环比窗 [prev_lo, hi)（全表扫次数不变），Python 分桶拆
    # 当前/上一窗；完成待办 = done_at 落窗 ∧ start_time 空（排期后完成是弱信号，
    # 只经 BehaviorEvent 进洞察不作 KPI）
    prev_lo = lo - (hi - lo)
    done_rows_all = list(db.scalars(select(Task).where(
        Task.is_deleted.is_(False),
        Task.done_at.is_not(None),
        Task.done_at >= prev_lo, Task.done_at < hi,
    )))
    todos_done = [t for t in done_rows_all
                  if t.done_at >= lo and t.start_time is None]
    prev_todos_done = sum(1 for t in done_rows_all
                          if t.done_at < lo and t.start_time is None)

    # 新增待办 = 窗口内创建 ∧ 当前仍是待办形态（与完成待办对称排除排期项——
    # 出身待办不可判：create_task 带时间即置 ever_scheduled）
    todos_created = db.scalar(select(func.count()).select_from(Task).where(
        Task.is_deleted.is_(False),
        Task.start_time.is_(None),
        Task.created_at >= lo, Task.created_at < hi,
    )) or 0

    # —— 日程侧（日历世界观：已开始即发生，不依赖勾选）——
    # list_tasks 范围口径（单次按窗口 ∪ 系列全量附墓碑聚合）→ expand_occurrences
    # 逐实例展开；一次取宽窗，当前/上一窗各自展开（上一窗全在过去无需 now 过滤）
    sched_rows = list_tasks(db, start=prev_lo, end=hi)
    held: list[tuple[Task, datetime]] = []  # (任务行, 实例开始)，occ < now
    held_prev = 0
    scheduled_minutes = 0
    prev_scheduled_minutes = 0
    for t in sched_rows:
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            scheduled_minutes += dur
            if occ < now:
                held.append((t, occ))
        for occ in expand_occurrences(t, prev_lo, lo):
            prev_scheduled_minutes += dur
            held_prev += 1

    inbox_backlog = db.scalar(select(func.count()).select_from(Task).where(
        Task.is_deleted.is_(False),
        Task.start_time.is_(None), Task.done_at.is_(None),
    )) or 0

    # 每日动态：完成待办（done_at 落日）+ 发生日程（实例开始日落日）+ 当日
    # 时间花费（已发生日程实例的排期时长）；空日零行——趋势连续性
    by_day: dict[str, dict[str, int]] = {}
    for t in todos_done:
        b = by_day.setdefault(t.done_at.date().isoformat(),
                              {"count": 0, "todos": 0, "events": 0, "minutes": 0})
        b["count"] += 1
        b["todos"] += 1
    for task, occ in held:
        b = by_day.setdefault(occ.date().isoformat(),
                              {"count": 0, "todos": 0, "events": 0, "minutes": 0})
        b["count"] += 1
        b["events"] += 1
        # 累加该日的时间花费（全天任务不计入）
        if not getattr(task, "all_day", False):
            b["minutes"] += _task_dur(task)
    daily_done = []
    d = _day0(lo)
    while d < hi:
        b = by_day.get(d.date().isoformat(), {"count": 0, "todos": 0, "events": 0, "minutes": 0})
        daily_done.append({"date": d.date().isoformat(), **b})
        d += timedelta(days=1)

    # 时间花费 by 项目（已发生日程实例的排期时长；全天恒 0 不计；无项目归「未归类」）
    proj_names = {p.id: p.name for p in db.scalars(select(Project))}
    proj_minutes: dict[str, int] = {}
    for t, _occ in held:
        if getattr(t, "all_day", False):
            continue
        name = proj_names.get(t.project_id, "未归类")
        proj_minutes[name] = proj_minutes.get(name, 0) + _task_dur(t)
    by_project = sorted(
        ({"name": k, "minutes": v} for k, v in proj_minutes.items()),
        key=lambda r: -r["minutes"],
    )

    # 完成分布按优先级（完成待办口径——排期后完成不进 KPI，同完成待办口径）
    by_priority = {"high": 0, "medium": 0, "low": 0}
    for t in todos_done:
        key = {1: "high", 2: "medium", 3: "low"}.get(t.priority or 2, "medium")
        by_priority[key] += 1

    # 完成滞后（待办）= created→done 中位天数；排期提前量（日程）= created→实例
    # 开始中位天数（非系列——系列锚点创建日无提前量语义）
    def _median(vals: list[float]) -> float | None:
        return round(statistics.median(vals), 1) if vals else None

    lag = {
        "todos_days": _median([
            (t.done_at - t.created_at).total_seconds() / 86400
            for t in todos_done if t.created_at is not None]),
        "lead_days": _median([
            (occ - t.created_at).total_seconds() / 86400
            for t, occ in held
            if t.recurrence_rule is None and t.created_at is not None]),
    }

    # 行为洞察：改期/取消事件量与偏差（done 事件的 delta_min = 实际-计划——
    # BehaviorEvent 是历史真值快照，不受 duration 不变式重算影响）
    events = list(db.scalars(select(BehaviorEvent).where(
        BehaviorEvent.created_at >= lo, BehaviorEvent.created_at < hi,
    )))
    reschedules = sum(1 for e in events if e.kind == "reschedule")
    done_deltas = [e.delta_min for e in events
                   if e.kind == "done" and e.delta_min is not None]
    behavior = {
        "reschedules": reschedules,
        "avg_delta_min": round(sum(done_deltas) / len(done_deltas)) if done_deltas else None,
        "event_count": len(events),
        # done 事件样本数（avg_delta_min 的分母——行为推断「估时乐观」的门槛数据）
        "done_events": len(done_deltas),
    }

    rhythm = execution_rhythm(db, window_days=14)

    # 环比：上一同长滚动窗三数差（完成待办/日程发生/排期总分钟）
    delta = {
        "todos_done": len(todos_done) - prev_todos_done,
        "events_held": len(held) - held_prev,
        "scheduled_minutes": scheduled_minutes - prev_scheduled_minutes,
    }

    # 负载率：排期总分钟 ÷（每日工时 × 窗口天数）——与「排期总时长」同分子
    # 同口径（按存储行计，重复系列不展开实例）
    capacity_minutes = get_settings(db).work_minutes_per_day * (hi - lo).days
    load_rate = round(scheduled_minutes / capacity_minutes, 2) if capacity_minutes else 0.0

    # AI 协作：建议采纳率（adoption_stats 自带近 14 天窗，expired 双侧剔除）
    # + 窗口内排期建议数（Proposal 日志级小表）
    ai = {
        "adoption": adoption_stats(db),
        "schedule_proposals": db.scalar(select(func.count()).select_from(Proposal).where(
            Proposal.kind == "schedule",
            Proposal.created_at >= lo, Proposal.created_at < hi,
        )) or 0,
    }

    # 页面模式专属块（智能预警/历史基线/习惯/健康度）——见函数 docstring
    summary_base = {
        "range": {"from": lo.date().isoformat(), "to": hi.date().isoformat()},
        "overview": {
            "todos_done": len(todos_done),
            "todos_created": todos_created,
            "events_held": len(held),
            "scheduled_minutes": scheduled_minutes,
            "todo_digest_rate": round(len(todos_done) / todos_created, 2) if todos_created else None,
            "inbox_backlog": inbox_backlog,
            "capacity_minutes": capacity_minutes,
            "load_rate": load_rate,
            "delta": delta,
        },
        "daily_done": daily_done,
        "by_project": by_project,
        "by_priority": by_priority,
        "rhythm": rhythm,
        "behavior": behavior,
        "ai": ai,
        "lag": lag,
    }

    # 页面滚动窗口模式专属块（warnings/prev_avg/habits/health）——显式 lo/hi 的
    # 报告周期模式跳过；月报成长对比如需上期 health，另行单独计算（见
    # _calculate_growth_metrics）。warnings 先算个人分位基线（预警规则 6 消费）
    if lo == _day0(now) - timedelta(days=days - 1) and hi == _day0(now) + timedelta(days=1):
        summary_base["warnings"] = _calculate_warnings(
            summary_base, rhythm, baseline=_calculate_baseline(db, days))
        summary_base["prev_avg"] = _calculate_prev_periods_avg(db, days, periods=4)
        summary_base["habits"] = _calculate_habits(db, lo, hi)
        summary_base["health"] = _calculate_health_score(summary_base, rhythm)

    return summary_base


# —— 记忆注入（统计域三调用点共用的个性化上下文）———————————————————————

# 画像注入护栏：Tier 1 参考段 ≤20 行 + Tier 2 未过期事实 ≤10 条，整段 ≤1500 字符
_PROFILE_MAX_RULE_LINES = 20
_PROFILE_MAX_FACTS = 10
_PROFILE_MAX_CHARS = 1500


def _memory_profile_lines(db: Session) -> str:
    """用户画像文本段（洞察/报告/问答共用的个性化注入）：Tier 1 记忆参考段
    （低置信 AI 学习规则——习惯画像依据；core 铁律段是排期硬约束，统计域
    不消费）+ Tier 2 未过期情节事实（倒序取新）。空记忆返回空串——调用方
    据此跳过注入段，提示词形状稳定。"""
    _, ref = format_memory_lines(db)
    lines = list(ref)[:_PROFILE_MAX_RULE_LINES]
    now = datetime.now()
    facts = [f for f in list_episodic_facts(db, limit=15)
             if f.expires_at is None or f.expires_at > now][:_PROFILE_MAX_FACTS]
    for f in facts:
        suffix = f"（{f.expires_at:%m-%d} 前有效）" if f.expires_at else ""
        lines.append(f"[{f.topic}] {f.fact}{suffix}")
    return "\n".join(lines)[:_PROFILE_MAX_CHARS]


def _goal_lines(db: Session) -> list[str]:
    """改进目标规则（category=改进目标，设置 → 记忆画像手写——自由文本类目）：
    洞察/报告「目标进度」注入源，建议围绕目标对照解读。≤5 条。"""
    return [r.content for r in list_memory_rules(db)
            if (r.category or "") == "改进目标"][:5]


def _habits_text(habits: dict | None) -> str:
    """习惯分析 dict → 文字行（best_slot/defer_slot/top_project/best_priority 四项，
    热力矩阵大数组不进任何 LLM 注入）。"""
    if not habits:
        return ""
    parts = []
    if habits.get("best_slot"):
        parts.append(f"- 最高效时段：{habits['best_slot']}")
    if habits.get("defer_slot"):
        parts.append(f"- 拖延高发期：{habits['defer_slot']}")
    if habits.get("top_project") and habits.get("top_project_pct"):
        parts.append(f"- 偏好项目：{habits['top_project']} 占比 {habits['top_project_pct']}%")
    if habits.get("best_priority_text"):
        parts.append(f"- {habits['best_priority_text']}")
    return "\n".join(parts)


def _extract_review_parts(content: str) -> str:
    """报告正文 →（【总评】行 + 「## 建议」节）拼接文本；都提不出（如空文）
    返回空串。建议节 = `^##\\s*建议` 起、到下一个 `^## ` 或文末止。"""
    head = next((line.strip() for line in content.splitlines()
                 if line.strip().startswith("【总评】")), "")
    advice = ""
    m = re.search(r"^##\s*建议\s*$", content, re.M)
    if m:
        rest = content[m.end():]
        nxt = re.search(r"^##\s", rest, re.M)
        advice = (rest[:nxt.start()] if nxt else rest).strip()
    parts = [p for p in (head, advice) if p]
    return "\n".join(parts)


def _previous_report_row(db: Session, period: str, lo: datetime) -> StatsReport | None:
    """上一期同型报告行：(period_type, period_start) 无唯一约束 → 取
    period_start < lo 最新一条。"""
    return db.scalar(
        select(StatsReport)
        .where(StatsReport.period_type == period, StatsReport.period_start < lo)
        .order_by(StatsReport.period_start.desc())
        .limit(1)
    )


def _latest_report_row(db: Session) -> StatsReport | None:
    """最近生成的报告行（任意类型，created_at 降序）——面向滚动窗口的洞察/
    问答取最近一期即可（建议是否被执行对照当期数据点评）。"""
    return db.scalar(
        select(StatsReport).order_by(StatsReport.created_at.desc()).limit(1)
    )


def _advice_items_of(row: StatsReport) -> list[str]:
    """报告行 → 建议条目清单（≤5 条，注入用纯文本）：payload.advice_items
    结构化优先——dict 条目（打卡形态）按 status 渲染：completed 加「（你确认
    已完成）」标注（下期对照点评的反馈信号）、dropped 整条跳过（用户放弃的
    不再追问）、proposed/缺省原样；字符串条目（旧留档）原样。回落正文
    「## 建议」节按行拆（无 payload 的旧报告/降级模板兼容）。正文行剥
    Markdown 列表记号（"- "/"1. "）。"""
    payload = row.payload or {}
    items = payload.get("advice_items")
    if isinstance(items, list) and items:
        out: list[str] = []
        for it in items:
            if isinstance(it, dict):
                text = str(it.get("text") or "").strip()
                status = it.get("status")
                if not text or status == "dropped":
                    continue
                out.append(f"{text}（你确认已完成）" if status == "completed" else text)
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
            if len(out) >= 5:
                break
        return out
    m = re.search(r"^##\s*建议\s*$", row.content, re.M)
    if not m:
        return []
    rest = row.content[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    section = (rest[:nxt.start()] if nxt else rest).strip()
    out = []
    for line in section.splitlines():
        text = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
        if text:
            out.append(text)
        if len(out) >= 5:
            break
    return out


# 报告/洞察文末结构化块（复盘产出回流）：```json {"advice_items": [...],
# "keep_habits": [...], "fact_candidates": [...]}```——单次 LLM 调用产出
# Markdown 正文 + 结构化尾部（比 json_mode 全文编码更稳：正文自由写作，
# 只有尾部小块受 JSON 约束）
_ADVICE_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
# 单次留档的条数上限（指令要求建议 2-3 条 / 事实 0-3 条，5 是离谱值兜底）
_ADVICE_MAX_ITEMS = 5
_FACT_MAX_ITEMS = 3


def _parse_structured_tail(content: str) -> tuple[str, dict]:
    """报告/洞察 LLM 输出 →（剥离结构化块后的正文, 结构化字段 dict）。

    文末 ```json {"advice_items": [...], "keep_habits": [...], "fact_candidates":
    [...]} ``` 围栏块解析（json.loads 防御）；无块 / 解析失败 / 形状不符 →
    (原正文, {})——产物永远以纯 Markdown 形态可用。字段清洗：advice_items /
    keep_habits 滤空字符串截 5；fact_candidates 滤缺 topic/fact 条目截 3。"""
    m = _ADVICE_BLOCK_RE.search(content)
    if not m:
        return content, {}
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return content, {}
    if not isinstance(obj, dict):
        return content, {}
    advice = [s.strip() for s in obj.get("advice_items") or []
              if isinstance(s, str) and s.strip()][:_ADVICE_MAX_ITEMS]
    keeps = [s.strip() for s in obj.get("keep_habits") or []
             if isinstance(s, str) and s.strip()][:_ADVICE_MAX_ITEMS]
    facts = [f for f in obj.get("fact_candidates") or []
             if isinstance(f, dict) and str(f.get("topic") or "").strip()
             and str(f.get("fact") or "").strip()][:_FACT_MAX_ITEMS]
    body = (content[:m.start()] + content[m.end():]).rstrip()
    return body, {"advice_items": advice, "keep_habits": keeps,
                  "fact_candidates": facts}


def _persist_fact_candidates(db: Session, facts: list[dict]) -> int:
    """复盘事实沉淀（消费→产出闭环）：fact_candidates 逐条落 Tier 2 情节事实
    （source=stats_reflect、confidence 0.6、expires_days 钳 [1,90] 由 upsert_fact
    内部守）——日程助手排期/教练经 search_facts 自然可见。非法条目丢弃打日志
    外静默；同 topic+fact 重复生成自动印证不堆行（upsert 既有语义）。返回
    落库条数。"""
    n = 0
    for item in facts[:_FACT_MAX_ITEMS]:
        topic = str(item.get("topic") or "").strip()
        fact = str(item.get("fact") or "").strip()
        if not topic or not fact:
            continue
        days = item.get("expires_days")
        row = upsert_fact(db, topic=topic, fact=fact, source="stats_reflect",
                          confidence=0.6,
                          expires_days=days if isinstance(days, int) else None)
        if row is not None:
            n += 1
    return n


def build_review_context(db: Session, summary: dict, *, days: int = 30,
                         period: str | None = None) -> dict:
    """复盘上下文单源（洞察/报告/问答三调用点共用，防注入漂移）。

    输入 summary = 调用方已算好的 stats_summary（滚动页面模式自带
    prev_avg/health 四块；报告周期模式四块缺失，health 此处单算补齐）。
    period 给定（week/month）= 报告周期形态：上期报告取同型、另算周期成长
    对比；None = 滚动窗口形态：上期取最近一期任意类型、不算成长。

    返回 {"profile", "health", "baseline", "quantiles", "growth", "prev_report",
    "deviations", "goals", "patterns"}：
    - profile：用户画像文本（Tier 1 参考段 + Tier 2 事实；空 = 无记忆）
    - health：健康度评分（五维 + 总分）
    - baseline：过去 4 期历史基线 prev_avg（报告周期模式为 None）
    - quantiles：个人分位基线（近 8 期 P25/P50/P75，_calculate_baseline）
    - growth：周期成长对比（period=None 或上期数据不可用时 None）
    - prev_report：{"from", "digest", "advice", "keeps"} 上期报告结论
      （digest = 总评+建议文本；advice = 建议条目清单、keeps = 值得保持的
      好习惯，均 payload 结构化优先）——无留档 None
    - deviations：行为偏差聚类簇（窗口对齐 days，注入上限 5 簇）
    - goals：改进目标规则清单（category=改进目标；空 = 未设定）
    - patterns：行为模式推断（_calculate_behavior_insights——周期模式 summary
      无 habits 页面块时单算补齐，报告路径同享推断）

    定位红线：事后复盘专用（与日程助手的事前/事中辅导分工）；数据确定性、
    解读归 LLM。"""
    health = summary.get("health") or _calculate_health_score(summary, None)
    if period:
        row = _previous_report_row(
            db, period, datetime.fromisoformat(summary["range"]["from"]))
    else:
        row = _latest_report_row(db)
    prev_report = None
    if row is not None:
        digest = _extract_review_parts(row.content)[:_PREV_REVIEW_MAX_CHARS]
        if digest:
            keeps = [k for k in (row.payload or {}).get("keep_habits") or []
                     if isinstance(k, str) and k.strip()][:_ADVICE_MAX_ITEMS]
            prev_report = {
                "from": row.period_start.date().isoformat(),
                "digest": digest,
                "advice": _advice_items_of(row),
                "keeps": keeps,
            }
    # 行为推断（周期模式 summary 不带 habits 页面块——单算补齐使报告路径同享）
    habits = summary.get("habits")
    if habits is None:
        habits = _calculate_habits(
            db, datetime.fromisoformat(summary["range"]["from"]),
            datetime.fromisoformat(summary["range"]["to"]))
    return {
        "profile": _memory_profile_lines(db),
        "health": health,
        "baseline": summary.get("prev_avg"),
        "quantiles": _calculate_baseline(db, days),
        "growth": _calculate_growth_metrics(db, summary, period) if period else None,
        "prev_report": prev_report,
        "deviations": aggregate_deviations(db, days=days)[:5],
        "goals": _goal_lines(db),
        "patterns": _calculate_behavior_insights({**summary, "habits": habits}),
    }


_REPORT_INSTRUCTION = """你是日程助手的统计分析师。基于下面的 JSON 统计数据，写一份{label}。
要求：
- 正文第一行必须是【总评】开头的一句话总评（40 字内，概括本期最值得注意的一点，其后换行）——报告首行总评锚点约定
- Markdown 格式，结构为：## 概述 / ## 完成情况 / ## 时间花费 / ## 行为洞察 / ## 建议
- 概述 2-3 句；每节先给结论再列数据；建议 2-3 条可执行（引用数据支撑，不编造数字）；值得保持的好习惯也点名，不只挑毛病
- 关键结论与关键数字用 **加粗** 标出（每段至多 1-2 处）——前端会着色突出，方便读者扫读抓重点
- 数据含 health（健康度五维评分）、habits_text（习惯分析文字）与 deviations（行为偏差聚类）——行为洞察节优先引用；patterns（行为模式推断）是有数据依据的确定性观察，行为洞察节优先解读成用户能察觉的行为模式（不牵强才提）；有 growth 字段（与上一周期对比）时，概述与行为洞察优先引用对比结论（用百分比说明变化）；baseline_quantiles 是用户近 8 期个人分位基线（P25/P50/P75），本期值越出 P25~P75 区间时点名对比（相对于自己的常态）；用户画像与改进目标是个性化判断依据不是统计数据，不要原文复述
- 正文结束后另起一行输出结构化块（系统留档供下期对照追踪与长期记忆，不计入正文长度）：
```json
{{"advice_items": ["「## 建议」节每条建议的原文"], "keep_habits": ["本期数据中值得保持的好习惯"], "fact_candidates": [{{"topic": "主题词", "fact": "本期数据发现的稳定习惯规律", "expires_days": 90}}]}}
```
（advice_items 2-3 条；keep_habits 0-3 条；fact_candidates 0-3 条——与用户画像重复的规律不选，没有则空数组）
- 中文，总长 300-500 字，语气务实不煽情；数据里没有的维度不要提
{profile}{review}统计数据：{data}"""

_GROWTH_REPORT_INSTRUCTION = """你是日程助手的成长分析师。基于下面的 JSON 统计数据（包含本月与上月对比），写一份月度成长报告。

要求：
- 正文第一行必须是【总评】开头的一句话总评（40 字内，概括本月最大的成长或变化，其后换行）
- Markdown 格式，结构为：## 概述 / ## 能力进步 / ## 目标达成 / ## 行为改善 / ## 建议
- 概述：2-3 句总结本月核心数据与亮点
- 能力进步：重点对比健康度、完成数、消化率等核心指标的月度变化（用百分比说明增长/下降）
- 目标达成：分析预警问题数量、收件箱积压的改善情况
- 行为改善：对比改期次数、完成偏差的变化，指出进步点
- 建议：2-3 条可执行建议，基于本月数据和对比结果，给出下月改进方向；值得保持的好习惯也点名
- 关键结论与关键数字用 **加粗** 标出（每段至多 1-2 处）——前端会着色突出，方便读者扫读抓重点
- 数据含 health（健康度五维评分）、habits_text（习惯分析文字）与 deviations（行为偏差聚类）——行为改善节优先引用；patterns（行为模式推断）是有数据依据的确定性观察，行为改善节优先解读（不牵强才提）；baseline_quantiles 是用户近 8 期个人分位基线（P25/P50/P75），本期值越出 P25~P75 区间时点名对比；用户画像与改进目标是个性化判断依据不是统计数据，不要原文复述
- 正文结束后另起一行输出结构化块（系统留档供下期对照追踪与长期记忆，不计入正文长度）：
```json
{{"advice_items": ["「## 建议」节每条建议的原文"], "keep_habits": ["本期数据中值得保持的好习惯"], "fact_candidates": [{{"topic": "主题词", "fact": "本期数据发现的稳定习惯规律", "expires_days": 90}}]}}
```
（advice_items 2-3 条；keep_habits 0-3 条；fact_candidates 0-3 条——与用户画像重复的规律不选，没有则空数组）
- 中文，总长 400-600 字，语气鼓励但客观，强调数据对比；数据里没有的维度不要提
{profile}{review}
统计数据（含成长指标 growth 字段，包含 metrics_change/health_change/goal_achievement/behavior_improvement）：
{data}"""

_INSIGHT_INSTRUCTION = """你是日程助手的 AI 时间管理教练，帮用户复盘近期的时间使用：洞察习惯、察觉好坏、给出个性化建议。基于下面的统计数据、规则预警、习惯分析{extra_intro}，生成统计页顶部的一卡三段式洞察。

要求（Markdown，四段结构，段首用【】标记，不要 ## 标题）：
- 第一行【总评】：30 字内一句话，概括本期最核心的状态
- 【值得关注】：2-3 条，融合规则预警与数据异常（每条引用具体数据，如"工作类任务占比82%"）；patterns（行为模式推断）是有数据依据的确定性观察，优先引用并解读成用户能察觉的行为模式（数据不牵强才提，有自己的解读角度）；纵向数据（prev_avg 近 4 期均值 / baseline_quantiles 个人近 8 期分位 / health 健康度）偏离显著时点名对比（如"高于你的常态 P75"），平稳则不提
- 【本期建议】：2-3 条可执行建议，每条以 [高优]/[中优]/[低优] 标签开头，给出具体数字目标；若有上期结论，须先点评其执行情况（对照本期数据：执行了/没执行/看不出，照实说）；建议尽量结合用户画像个性化、有改进目标时关联目标对照进度
- 【习惯洞察】：1-2 条个性化建议（如"你在周三上午完成高优任务成功率最高，建议将重要任务排在该时段"）；值得保持的好习惯也要点名，不只挑毛病
- 关键结论与关键数字用 **加粗** 标出（每段至多 1-2 处）——前端会着色突出，方便读者扫读抓重点

语气：直接、务实、个性化（用"你"而非"用户"）；数据驱动，不编造数字；建议具体可执行，不说空话；用户画像与上期结论是判断依据不是统计数据，不要原文复述。
输出：列表用 "- " 开头，总长 250-450 字。
- 正文结束后另起一行输出结构化块（系统留档沉淀长期记忆，不计入正文长度）：
```json
{{"fact_candidates": [{{"topic": "主题词", "fact": "本期数据发现的稳定习惯规律", "expires_days": 90}}]}}
```
（fact_candidates 0-3 条——与用户画像重复的规律不选，没有则空数组）

统计数据：
{data}

规则预警：
{warnings}

习惯分析：
{habits}{extra}"""

# 上期注入截断护栏：报告全长 300-500 字，上期「总评 + 建议节」常态 <600 字，
# 1200 是离谱值兜底（防异常长留档把指令撑爆）
_PREV_REVIEW_MAX_CHARS = 1200


def _calculate_growth_metrics(db: Session, current_summary: dict, period: str) -> dict | None:
    """周期成长指标：当前周期与上一周期的对比，注入报告供 LLM 写成长分析。

    周报（week，本周 vs 上周）与月报（month，本月 vs 上月）同构；其他值返回
    None。计算维度：
    1. 核心指标变化：完成待办、日程发生、待办消化率、负载率（环比增长率）
    2. 能力进步：健康度总分变化（本/上期 summary 均为报告周期模式、不带
       health 块——两侧各自单算 _calculate_health_score，此前依赖不存在的
       块导致恒 None 的缺陷已修）
    3. 目标达成：预警问题数量变化、收件箱积压改善
    4. 行为改善：改期次数减少、完成偏差缩小
    """
    if period not in ("week", "month"):
        return None

    now = datetime.now()
    prev_hi = week_start_of(now) if period == "week" else month_start_of(now)
    # 上一周期：周报 = 恰 7 天前（同锚定）；月报 = 上一日历月（天数可变）
    prev_lo = (prev_hi - timedelta(days=7)) if period == "week" else month_start_of(
        prev_hi - timedelta(days=1))

    # 计算上一周期的统计数据
    try:
        prev_summary = stats_summary(db, lo=prev_lo, hi=prev_hi)
    except Exception:
        return None  # 上一周期数据不可用时返回 None

    current_ov = current_summary["overview"]
    prev_ov = prev_summary["overview"]

    # 1. 核心指标变化（环比增长率，百分比）
    def growth_rate(current: float, prev: float) -> float | None:
        if prev == 0:
            return None
        return round((current - prev) / prev * 100, 1)

    metrics_change = {
        "todos_done": {
            "current": current_ov["todos_done"],
            "prev": prev_ov["todos_done"],
            "growth": growth_rate(current_ov["todos_done"], prev_ov["todos_done"]),
        },
        "events_held": {
            "current": current_ov["events_held"],
            "prev": prev_ov["events_held"],
            "growth": growth_rate(current_ov["events_held"], prev_ov["events_held"]),
        },
        "todo_digest_rate": {
            "current": current_ov.get("todo_digest_rate"),
            "prev": prev_ov.get("todo_digest_rate"),
            "growth": growth_rate(
                current_ov.get("todo_digest_rate", 0) * 100,
                prev_ov.get("todo_digest_rate", 0) * 100,
            ) if current_ov.get("todo_digest_rate") and prev_ov.get("todo_digest_rate") else None,
        },
        "load_rate": {
            "current": current_ov.get("load_rate"),
            "prev": prev_ov.get("load_rate"),
            "growth": growth_rate(
                current_ov.get("load_rate", 0) * 100,
                prev_ov.get("load_rate", 0) * 100,
            ) if current_ov.get("load_rate") is not None and prev_ov.get("load_rate") is not None else None,
        },
    }

    # 2. 能力进步：健康度总分变化（报告周期模式的 summary 不带 health 块，
    #    两侧各自单算——_calculate_health_score 只依赖 base 四块，可独立调用）
    cur_health = current_summary.get("health") or _calculate_health_score(current_summary, None)
    prev_health = prev_summary.get("health") or _calculate_health_score(prev_summary, None)
    health_change = {
        "current": cur_health["total"],
        "prev": prev_health["total"],
        "diff": cur_health["total"] - prev_health["total"],
    }

    # 3. 目标达成：预警问题数量、收件箱积压改善（warnings 同样报告模式不带，
    #    两侧单算——规则函数只依赖 overview/lag/rhythm，base 块齐备）
    cur_warnings = (current_summary["warnings"] if "warnings" in current_summary
                    else _calculate_warnings(current_summary, current_summary.get("rhythm")))
    prev_warnings = (prev_summary["warnings"] if "warnings" in prev_summary
                     else _calculate_warnings(prev_summary, prev_summary.get("rhythm")))
    goal_achievement = {
        "warnings_reduced": len(prev_warnings) - len(cur_warnings),
        "backlog_reduced": prev_ov["inbox_backlog"] - current_ov["inbox_backlog"],
    }

    # 4. 行为改善：改期次数、完成偏差
    behavior_improvement = {
        "reschedules_reduced": (
            prev_summary["behavior"]["reschedules"] - current_summary["behavior"]["reschedules"]
        ),
        "avg_delta_improved": None,
    }
    if (current_summary["behavior"]["avg_delta_min"] is not None and
        prev_summary["behavior"]["avg_delta_min"] is not None):
        # 偏差绝对值缩小表示改善
        behavior_improvement["avg_delta_improved"] = (
            abs(prev_summary["behavior"]["avg_delta_min"]) -
            abs(current_summary["behavior"]["avg_delta_min"])
        )

    return {
        "metrics_change": metrics_change,
        "health_change": health_change,
        "goal_achievement": goal_achievement,
        "behavior_improvement": behavior_improvement,
        "prev_period": f"{prev_lo.date().isoformat()} ~ {prev_hi.date().isoformat()}",
    }


def _fallback_report(period_label: str, summary: dict) -> str:
    """LLM 不可用时的纯数据模板（保底产出，报告永远生成得出来）。"""
    ov = summary["overview"]
    top = summary["by_project"][:3]
    top_text = "、".join(f"{r['name']} {round(r['minutes'] / 60)}h" for r in top) or "无"
    rate = f"{round(ov['todo_digest_rate'] * 100)}%" if ov["todo_digest_rate"] is not None else "—"
    return (
        f"【总评】{period_label}：完成待办 {ov['todos_done']} 项、日程发生 {ov['events_held']} 次，"
        f"待办消化率 {rate}，收件箱积压 {ov['inbox_backlog']} 项。\n\n"
        f"## 概述\n\n{period_label}（{summary['range']['from']} ~ {summary['range']['to']}）："
        f"完成待办 {ov['todos_done']} 项、日程发生 {ov['events_held']} 次，"
        f"待办消化率 {rate}，"
        f"收件箱积压 {ov['inbox_backlog']} 项。\n\n"
        f"## 时间花费\n\n{top_text}。\n\n（AI 总结不可用，以上为数据摘要。）"
    )


def generate_report(db: Session, period: str) -> StatsReport:
    """生成周/月报并落库：周期聚合 → 复盘上下文（画像/纵向/上期结论/偏差聚类）
    → call_llm 总结 → Markdown + 结构化建议落库返回。手动（POST /api/stats/
    reports）与周日晚自动两入口共用。

    月报注入成长指标（月度对比）；周报注入周际对比（growth 同构）。LLM 输出
    文末的结构化建议块被解析剥离，存 payload.advice_items 供下期对照追踪。"""
    lo, hi = period_range(period)
    label = "本周报" if period == "week" else "本月报"
    summary = stats_summary(db, lo=lo, hi=hi)
    ctx = build_review_context(db, summary, days=(hi - lo).days, period=period)

    profile = ""
    if ctx["profile"] or ctx["goals"]:
        parts = []
        if ctx["profile"]:
            parts.append(f"用户画像（个性化判断依据）：\n{ctx['profile']}")
        if ctx["goals"]:
            parts.append("改进目标（用户设定——建议尽量关联目标，对照本期数据给进度解读）：\n"
                         + "\n".join(f"- {g}" for g in ctx["goals"]))
        profile = "\n\n".join(parts) + "\n\n"

    review = ""
    prev_report = ctx["prev_report"]
    if prev_report is not None:
        review = (
            f"上期报告（{prev_report['from']} 起）的总评与建议如下：\n{prev_report['digest']}\n"
            "请在「## 建议」节开头先用 1-2 句点评上期建议的实际执行情况"
            "（对照本期数据，执行了/没执行/数据看不出都照实说），再给本期建议。\n\n"
        )

    # 构建LLM输入数据（追加 health/habits 文字/偏差聚类/个人分位基线/行为推断；
    # 成长对比有值时注入）
    llm_data = {
        **summary,
        "health": ctx["health"],
        "habits_text": _habits_text(_calculate_habits(db, lo, hi)),
        "deviations": ctx["deviations"],
        "baseline_quantiles": ctx["quantiles"],
        "patterns": ctx["patterns"],
    }
    if ctx["growth"] is not None:
        llm_data["growth"] = ctx["growth"]

    content: str
    structured: dict = {}
    try:
        # 月报使用增强版指令（包含成长分析要求）
        instruction = _REPORT_INSTRUCTION if period == "week" else _GROWTH_REPORT_INSTRUCTION
        content = call_llm([{"role": "user", "content": instruction.format(
            label=label, profile=profile, review=review,
            data=json.dumps(llm_data, ensure_ascii=False, default=str),
        )}])
        content, structured = _parse_structured_tail(content)
    except Exception as e:  # NoActiveModelError / ContextOverflowError / 网络层
        logger.warning("AI 报告生成失败，回落纯数据模板：%s", e)
        content = _fallback_report(label, summary)
    # 复盘事实沉淀（Tier 2，source=stats_reflect）——失败不连坐报告主链
    n_facts = _persist_fact_candidates(db, structured.get("fact_candidates") or [])
    # 结构化留档：本期建议（打卡形态 {text, status}，下期对照追踪——用户
    # completed 的标注点评、dropped 的不再追问）+ 好习惯 + 健康度快照
    payload = {
        "advice_items": [{"text": s, "status": "proposed"}
                         for s in structured.get("advice_items") or []],
        "keep_habits": structured.get("keep_habits") or [],
        "health": {"total": ctx["health"]["total"],
                   "dimensions": ctx["health"]["dimensions"]},
    }
    report = StatsReport(period_type=period, period_start=lo,
                         content=content, payload=payload)
    db.add(report)
    db.commit()
    db.refresh(report)
    logger.info("生成 %s（周期起 %s，%d 字，建议 %d 条，沉淀事实 %d 条）",
                label, lo.date(), len(content), len(payload["advice_items"]), n_facts)
    return report


# 洞察缓存：低频场景——每窗口一天生成一次即可（TTL 86400s），反复进出页面/
# 来回切范围全部复用；要新内容走手动刷新（refresh=1 跳过缓存读仍刷新写），
# 大幅降低 LLM 调用。进程内 dict，竞态最坏重复生成一次，无害；降级文案不
# 缓存——下次调用重试 LLM 主线
_INSIGHT_CACHE: dict[int, tuple[str, float]] = {}
_INSIGHT_CACHE_TTL = 86400.0


def generate_insight(db: Session, days: int = 30, *, refresh: bool = False) -> str:
    """AI 动态洞察（统计页顶部一卡三段：总评/值得关注/本期建议/习惯洞察）。

    区别于周/月报（generate_report）：
    - 周/月报：完整周期数据报告，Markdown 落库留档
    - 动态洞察：滚动窗口教练式导读，Markdown 不落库，页面顶部实时展示

    规则预警与习惯分析注入 LLM 上下文；LLM 异常时回落规则文案拼三段
    （页面不空，规则引擎兜底）。refresh=True 跳过缓存读（手动重新生成）。"""
    if not refresh:
        cached = _INSIGHT_CACHE.get(days)
        if cached and time.time() - cached[1] < _INSIGHT_CACHE_TTL:
            return cached[0]

    summary = stats_summary(db, days=days)
    # 零数据早退（2026-11-09 反思批）：核心指标全零时 LLM 只会输出"暂无数据"
    # 类废话——固定引导文案替代，不进 LLM 不写缓存（有数据后自动恢复主线）
    ov0 = summary["overview"]
    if ov0["todos_done"] == 0 and ov0["events_held"] == 0 and ov0["scheduled_minutes"] == 0:
        return (f"【总评】近 {days} 天暂无完成待办与日程记录。\n\n"
                "先用几天 Clara——勾选完成待办、把事项排上日历后，这里会有基于"
                "你数据的个性化复盘（习惯洞察、纵向对比与针对性建议）。")
    ctx = build_review_context(db, summary, days=days)
    warnings = summary.get("warnings") or []
    warnings_text = "\n".join(f"- {w['message']}" for w in warnings)
    habits_text = _habits_text(summary.get("habits"))

    # 个性化与纵向注入（复盘上下文单源；空则整段不注入，提示词形状稳定）：
    # 画像 + 上期建议（执行情况对照指令），数据面带基线/健康度/偏差聚类
    extra_parts = []
    if ctx["profile"]:
        extra_parts.append(f"用户画像（个性化判断依据）：\n{ctx['profile']}")
    if ctx["goals"]:
        extra_parts.append("改进目标（用户设定——点评目标进度，建议关联目标）：\n"
                           + "\n".join(f"- {g}" for g in ctx["goals"]))
    prev_report = ctx["prev_report"]
    if prev_report and prev_report["advice"]:
        lines = "\n".join(f"- {a}" for a in prev_report["advice"])
        extra_parts.append(
            f"上期结论（最近一期报告 {prev_report['from']} 起的建议，点评其执行情况）：\n{lines}")
    extra = ("\n\n" + "\n\n".join(extra_parts)) if extra_parts else ""
    extra_intro = "、用户画像与上期结论" if extra_parts else ""

    try:
        raw = call_llm([{"role": "user", "content": _INSIGHT_INSTRUCTION.format(
            data=json.dumps({
                "overview": summary["overview"],
                "lag": summary["lag"],
                "behavior": summary["behavior"],
                "rhythm": summary["rhythm"],
                "prev_avg": ctx["baseline"],
                "baseline_quantiles": ctx["quantiles"],
                "health": ctx["health"],
                "by_project_top": summary["by_project"][:5],
                "deviations": ctx["deviations"],
                "patterns": ctx["patterns"],
            }, ensure_ascii=False),
            warnings=warnings_text or "无预警问题",
            habits=habits_text or "暂无习惯数据",
            extra_intro=extra_intro,
            extra=extra,
        )}])
        # 结构化尾部（fact_candidates）剥离后缓存正文；事实沉淀失败不连坐
        insight, structured = _parse_structured_tail(raw)
        _persist_fact_candidates(db, structured.get("fact_candidates") or [])
        _INSIGHT_CACHE[days] = (insight, time.time())
        return insight
    except Exception as e:
        logger.warning("AI 洞察生成失败，回落规则文案：%s", e)
        # 降级：规则预警 + 习惯结论拼三段（不缓存，下次调用重试 LLM）
        ov = summary["overview"]
        parts = [
            f"【总评】近 {days} 天完成待办 {ov['todos_done']} 项、日程发生 "
            f"{ov['events_held']} 次，消化率 "
            f"{'—' if ov['todo_digest_rate'] is None else round(ov['todo_digest_rate'] * 100)}%。"
        ]
        if warnings:
            parts.append("【值得关注】\n" + "\n".join(f"- {w['message']}" for w in warnings))
        fallback_habits = summary.get("habits")
        if fallback_habits and fallback_habits.get("best_slot"):
            parts.append(
                f"【习惯洞察】\n- 你的最高效时段是{fallback_habits['best_slot']}，"
                "建议将重要任务安排在该时段。")
        return "\n\n".join(parts)


_ASK_SYSTEM_PROMPT = (
    "你是 Clara 日程助手的复盘教练，基于用户近期的统计数据、记忆画像与报告结论，"
    "帮用户复盘时间使用、洞察习惯、给出个性化改进建议。\n"
    "要求：中文回答；先给结论再列依据；引用具体数字，数据里没有的不要编造；"
    "建议具体可执行、结合用户画像个性化；回答用 Markdown。"
)

# 复盘引导模式指令（mode=review 时追加到 system 尾部）：结构化块约定与
# 报告/洞察同源（_parse_structured_tail 单点解析）
_ASK_REVIEW_DIRECTIVE = (
    "本会话是复盘引导：围绕「本期最满意的一件事 → 最大卡点 → 下期想改进什么」"
    "逐项引导用户反思——每次只问一项，用户回答后先用统计数据简要呼应（有数据就"
    "引用，没有就不硬凑），再自然过渡到下一项。\n"
    "三项都问完后给总结：肯定进步、点出数据模式、给一条最小可行动建议；总结末尾"
    "另起一行输出结构化块（供用户确认后沉淀为长期记忆，不计入正文）：\n"
    '```json\n{"fact_candidates": [{"topic": "主题词", "fact": "用户反思得出的结论", '
    '"expires_days": 90}]}\n```\n'
    "（fact_candidates 0-3 条，来自用户的反思而非你的推断；没有则空数组）"
)


def build_ask_messages(db: Session, days: int, question: str,
                       history: list[dict] | None = None, *,
                       mode: str = "ask") -> list[dict]:
    """统计问答消息构建（POST /api/stats/ask 用）。

    独立轻通道：stats_summary 精简注入 system（剔除热力矩阵等 LLM 不需要的
    大数组）+ 复盘上下文（画像 / 按问题检索的情节事实 / 最近报告结论 / 行为
    偏差聚类，空段不注入），前端带最近几轮 history 实现多轮追问——无状态
    设计，不进 ChatTurn、不走意图路由、不注入人格（工具属性同统计报告）。
    mode=review 追加复盘引导指令（结构化块约定见 _ASK_REVIEW_DIRECTIVE）。"""
    summary = stats_summary(db, days=days)
    slim = {k: v for k, v in summary.items() if k != "habits"}
    if "habits" in summary:
        slim["habits"] = {k: v for k, v in summary["habits"].items()
                          if not k.startswith("heatmap")}
    ctx = build_review_context(db, summary, days=days)
    sections = [f"统计上下文（近 {days} 天，JSON）：\n"
                + json.dumps(slim, ensure_ascii=False, default=str)]
    if ctx["profile"]:
        sections.append(f"用户画像（个性化判断依据）：\n{ctx['profile']}")
    related = fact_lines(search_facts(db, question, k=3))
    if related:
        sections.append("与问题相关的近期事实：\n" + "\n".join(related))
    prev_report = ctx["prev_report"]
    if prev_report is not None:
        text = (f"最近报告结论（{prev_report['from']} 起，总评与建议）：\n"
                f"{prev_report['digest']}")
        if prev_report["keeps"]:
            text += "\n值得保持的好习惯：" + "；".join(prev_report["keeps"])
        sections.append(text)
    if ctx["deviations"]:
        sections.append("行为偏差聚类（近期重复出现的行为模式，JSON）：\n"
                        + json.dumps(ctx["deviations"], ensure_ascii=False))
    if ctx["patterns"]:
        sections.append("行为模式推断（有数据依据的确定性观察，解读时引用）：\n"
                        + "\n".join(f"- {p}" for p in ctx["patterns"]))
    system = _ASK_SYSTEM_PROMPT
    if mode == "review":
        system += "\n\n" + _ASK_REVIEW_DIRECTIVE
    messages = [{
        "role": "system",
        "content": system + "\n\n" + "\n\n".join(sections),
    }]
    for h in history or []:
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


def list_reports(db: Session, limit: int = 20) -> list[StatsReport]:
    """历史报告（最新在前；手动生成频率低，20 条足够回看）。"""
    return list(db.scalars(
        select(StatsReport).order_by(StatsReport.created_at.desc()).limit(limit)
    ))


def export_report_md(db: Session, report_id: str, path: str) -> dict:
    """报告导出 .md：UTF-8 落盘。

    路径来自前端原生保存对话框（非自由输入），校验同 services/backup.create_backup
    先例：绝对路径 + .md 后缀 + 父目录存在，违规 ValueError；报告不存在
    LookupError——router 各翻 422/404。service 放本模块（统计域产物，不混
    backup.py 的应用快照域）。"""
    report = db.get(StatsReport, report_id)
    if report is None:
        raise LookupError(f"报告不存在：{report_id}")
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("导出路径须为绝对路径")
    if p.suffix.lower() != ".md":
        raise ValueError("导出文件须以 .md 结尾")
    if not p.parent.is_dir():
        raise ValueError(f"目标目录不存在：{p.parent}")
    size = len(report.content.encode("utf-8"))
    p.write_text(report.content, encoding="utf-8")
    logger.info("导出报告 %s → %s（%d 字节）", report_id, p, size)
    return {"ok": True, "path": str(p), "size_bytes": size}


def set_advice_status(db: Session, report_id: str, index: int, status: str) -> StatsReport:
    """建议打卡（用户确认执行情况）：payload.advice_items[index].status 更新
    （proposed/completed/dropped）——下期生成对照时 completed 标注「你确认已
    完成」、dropped 不再追问（_advice_items_of 消费）。旧字符串条目顺带升级
    为打卡形态。index 越界 / 非法状态 ValueError（router 翻 422）；报告不存
    在 LookupError（404）。JSON 列整体替换（setattr 惯例）。"""
    report = db.get(StatsReport, report_id)
    if report is None:
        raise LookupError(f"报告不存在：{report_id}")
    if status not in ("proposed", "completed", "dropped"):
        raise ValueError(f"非法建议状态：{status}")
    items = list((report.payload or {}).get("advice_items") or [])
    if index < 0 or index >= len(items):
        raise ValueError(f"建议序号越界：{index}（共 {len(items)} 条）")
    normalized: list[dict] = []
    for i, it in enumerate(items):
        if i == index:
            text = it if isinstance(it, str) else str(it.get("text") or "").strip()
            normalized.append({"text": text, "status": status})
        elif isinstance(it, dict):
            normalized.append(it)
        else:
            normalized.append({"text": str(it).strip(), "status": "proposed"})
    payload = dict(report.payload or {})
    payload["advice_items"] = normalized
    report.payload = payload  # JSON 列整体替换（record_proposal_outcome 惯例）
    db.commit()
    db.refresh(report)
    logger.info("建议打卡：报告 %s 第 %d 条 → %s", report_id, index, status)
    return report


def upsert_review_fact(db: Session, topic: str, fact: str) -> EpisodicFact | None:
    """复盘会话反思结论的沉淀入口（用户点确认后前端调用，source=stats_chat、
    confidence 0.7——用户确认过的略高于 AI 自动归纳的 0.6）。非法输入 None
    （upsert_fact 内部校验）。"""
    return upsert_fact(db, topic=topic, fact=fact,
                       source="stats_chat", confidence=0.7)


def split_ask_reply(text: str) -> tuple[str, list[dict]]:
    """问答回复 →（剥离结构化尾部后的正文, fact_candidates）：复盘引导模式的
    总结轮按约定带文末 ```json 块（_parse_structured_tail 单点解析）；普通
    模式无块零影响。router 消费——reply 帧发剥离后正文，facts 帧带候选。"""
    body, structured = _parse_structured_tail(text)
    return body, structured.get("fact_candidates") or []
