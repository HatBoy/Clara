"""Node0 前置门（纯代码）：排期轮的确定性前置取数（2026-10-21 批 2）。

旧 schedule_planner 的 LLM 前取数段整段移植（nodes.py:516-592）——读库四次 +
天气 + 近期对话 + busy/similar_pool 构建；这些确定性事实与 LLM 输出无关，
前置到 intake 让 profiler 专心组合推理。批 4 起部分取数将按 Node1 剖析结果
条件化（天气仅户外/通勤类才调），此处先保行为等价。
"""

import logging
from datetime import datetime, timedelta

from agents.nodes import (
    _PLANNER_HISTORY_CHARS,
    _PLANNER_HISTORY_K,
    _PLANNER_MAX_CANDIDATES,
    _PLANNER_MAX_SCHEDULED,
    _PLANNER_NOTES_CHARS,
    _SIMILAR_WINDOW_DAYS,
    settings_payload,
    task_intensity,
)
from database import SessionLocal
from services.tasks import similar_done_durations

from .state import SchedulerState

logger = logging.getLogger(__name__)


def intake_node(state: SchedulerState) -> dict:
    """intake 节点：raw_input 归一 + 取数快照组装（见模块 docstring）。

    输出：raw_input / now / philosophy_ids / context（含 profiler 与 finder
    消费的全部确定性输入）。ORM 对象在会话内转 dict——state 只过纯数据，
    跨节点不再摸 detached ORM（人工阅读与批 5 重构都更直观）。
    """
    from services.ai_data import chat_context, recent_reject_reasons
    from services.settings import get_settings
    from services.tasks import (
        _task_dur,
        expand_occurrences,
        execution_rhythm,
        list_tasks,
        recent_done,
    )
    from services.weather import forecast_for_city

    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lo, hi = day0 - timedelta(days=1), day0 + timedelta(days=15)  # -1~+14 天全盘视野
    with SessionLocal() as db:
        unscheduled = list_tasks(db, unscheduled=True)
        scheduled = list_tasks(db, start=lo, end=hi)
        s = get_settings(db)
        # 执行节奏 + 近期拒绝理由（优化批 5 数据回流，批 6 起拒绝理由抽
        # ai_data.recent_reject_reasons 与 qa 共用）：前者给「实际执行风格」
        # 量化对照；后者是用户上周明确拒绝过的时间/理由——同类建议当场避开
        rhythm = execution_rhythm(db)
        recent_rejects = recent_reject_reasons(db)
        # 近期对话（窄窗取舍见 nodes._PLANNER_HISTORY_K 注释）：exclude 本轮
        # 原话防重复注入（壳在图运行前已落库）；滚动摘要一并带出（2026-10-26
        # 增——排期分支此前丢弃摘要，上午闲聊交代的偏好下午排期接不住）
        chat_summary, recent_msgs = chat_context(db, recent_k=_PLANNER_HISTORY_K,
                                                 exclude=state["user_input"])

    # 天气注入：城市未配置/网络失败恒 None——排期不依赖天气，提示词层降级
    weather = forecast_for_city(s.weather_city)

    candidates = unscheduled[:_PLANNER_MAX_CANDIDATES]
    # 占用区间（系列按实例展开——补上旧版"系列基准日在窗外挡不住撞期"的盲区）；
    # 相似查重池取未来 7 天窗口内的实例；时长统一 _task_dur（0 视同缺失）。
    # busy_meta 同循环产区间→任务映射（2026-10-22 冲突让位：conflict 问询带
    # 挡路者名字 + displace 挡路者识别的唯一数据源，零额外读库）
    busy: list[tuple[datetime, datetime]] = []
    busy_meta: list[dict] = []
    similar_pool: list[tuple[str, datetime | None]] = []
    for t in scheduled:
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            busy.append((occ, occ + timedelta(minutes=dur)))
            busy_meta.append({"id": t.id, "title": t.title, "start": occ,
                              "end": occ + timedelta(minutes=dur),
                              "recur": bool(t.recurrence_rule),
                              "priority": t.priority})  # 负荷聚合消费（批 3 双产）
            if now <= occ <= now + timedelta(days=_SIMILAR_WINDOW_DAYS):
                similar_pool.append((t.title, occ))

    # 单查询双产（2026-11-01 优化批 3）：日负荷从 busy_meta 本地聚合——旧版
    # calendar_load_summary 在同一窗口独立再查一次 list_tasks + 全量
    # expand_occurrences（系列多时 O(2N)）。口径逐项对齐 services 原实现：
    # day 行集 [-1,+14) 零行也出 / 实例计 start 日 / high = 时长≥90 ∨ 高优
    load_summary: list[dict] = []
    _by_day: dict[str, dict] = {}
    _d = day0 - timedelta(days=1)
    while _d < day0 + timedelta(days=14):
        _row = {"date": _d.date().isoformat(), "scheduled_count": 0,
                "total_mins": 0, "high_count": 0}
        load_summary.append(_row)
        _by_day[_row["date"]] = _row
        _d += timedelta(days=1)
    for m in busy_meta:
        row = _by_day.get(m["start"].date().isoformat())
        if row is None:
            continue
        dur = int((m["end"] - m["start"]).total_seconds() // 60)
        row["scheduled_count"] += 1
        row["total_mins"] += dur
        if dur >= 90 or (m.get("priority") or 3) <= 1:
            row["high_count"] += 1

    philosophy_ids = [p for p in (s.ai_philosophy or "").split(",") if p]
    context = {        # 待排候选（真实 id 清单防幻觉，坑 ④）——payload 字段照旧 planner 形状
        "candidates": [{
            "id": t.id, "title": t.title, "duration": t.duration_mins,
            "priority": t.priority,
            # 改期次数（2026-10-26 批 4 补键）：频繁改期的任务画像里是"这件事
            # 一直被挤"的信号，剖析可提示优先给它锁定整块时间
            "reschedule_count": t.reschedule_count,
            "intensity": task_intensity(t.title, t.duration_mins, t.priority),
            # 同类历史耗时（2026-10-26 批 5）：近 90 天相似已完成任务时长均值
            # ——剖析估时的个性化证据（PROFILER §四.5 历史值优先于经验值）
            "est_hint": similar_done_durations(db, t.title),
            "notes": (t.notes or "").strip()[:_PLANNER_NOTES_CHARS],
        } for t in candidates],
        "valid_ids": sorted({t.id for t in candidates}),
        "prio_by_id": {t.id: (t.priority or 2) for t in candidates},
        "scheduled": [{
            "title": t.title,
            "start": t.start_time.isoformat() if t.start_time else None,
            "end": t.end_time.isoformat() if t.end_time else None,
            "priority": t.priority,
        } for t in scheduled[:_PLANNER_MAX_SCHEDULED]],
        # 完成节奏（2026-10-26 批 4 补键）：近 7 天 done 前 10——真实产能与
        # 节奏的证据（"上周完成了 X Y Z"），剖析估时/精力判断的对照源
        "recent_done": recent_done(db),
        # 执行节奏量化 + 近期拒绝理由（优化批 5）：见 intake 取数注释
        "execution_rhythm": rhythm,
        "recent_rejects": recent_rejects,
        "settings": settings_payload(s),
        "lunch": (s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None,
        "day_start_min": s.calendar_start_min,
        "day_end_min": s.calendar_end_min,
        "day_budget": s.work_minutes_per_day,
        "load_summary": load_summary,
        "recent_chat": [{"role": m.role,
                          "content": (m.content or "")[:_PLANNER_HISTORY_CHARS]}
                         for m in recent_msgs],
        # 更早对话的滚动摘要（None=尚未压缩；跨天偏好/久前话题线索供剖析参考）
        "chat_summary": chat_summary,
        # 记忆规则透传（2026-10-22 补：裁决器提示词声明的 memory_rules 输入此前在
        # 快照里恒缺、恒空数组——习惯偏好在 Node4 从不可见，排期建议与画像脱节）
        "memory_rules": state.get("memory_rules") or [],
        "weather": weather,
        "busy": busy,
        "busy_meta": busy_meta,
        "similar_pool": similar_pool,
    }
    logger.info("[scheduler] intake：候选待排 %d / 已排任务 %d（busy 区间 %d）/"
                "历史 %d 条 / 天气 %s", len(candidates), len(scheduled), len(busy),
                len(recent_msgs), "有" if weather else "无")
    return {"raw_input": state["user_input"], "now": now,
            "philosophy_ids": philosophy_ids, "context": context}
