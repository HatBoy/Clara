"""统一排期引擎（2026-10-20 增）：日程排期唯一时间决策内核。

设计动机（用户架构红线：全入口同一套混合引擎，效果统一、代码精简）：
- 收件箱单卡「智能安排」→ auto_schedule_run（routers/tasks.py 调；纯规则
  直落——2026-10-20 用户决策：单任务找空档规则层毫秒级足够，AI 微调等待
  不值；批量「安排全部」按钮退役，多任务批量排期全走对话草案卡）
- chat 撞期问询建议与 agent 草案时间 → 排期子图 finder（批 5 起纯规则自研，plan_slots
  调用方仅剩收件箱单卡 auto_schedule_run）
- 三层架构：①规则层（services.tasks.suggest_slot 确定性找空档：7 天窗 /
  15 分钟网格 / 避忙区午休 / 就医上午闸 / 每日工时预算——同日塞满工时
  自动顺延次日，批量不再全堆同一天）②AI 层（单次 SLOT_REFINE_PROMPT
  微调全批，贴负荷 / 天气 / 习惯哲学；任何异常整层静默跳过）③校验层
  （is_valid_slot 后验，非法回落规则槽）——AI 贴习惯但不可信，规则不聪明
  但可靠，永不空手。

同批互斥：决策层内部累积 busy 副本与每日负荷（每定一条即计入），批量条目
互相避让；落库层（apply_slot_decisions）fresh busy + 预算重验同口径，只是
并发纵深防御。

分层红线：engine 在 agents 层，读数据与写库一律调 services；LLM 只经
services.llm，提示词只引 prompts.py。plan_slots 纯决策（busy / settings 由
调用方传入，不读库不写库）；auto_schedule_run 是收件箱入口编排（读库 +
决策，不写库——落库由 router 调 apply_slot_decisions）。
"""

import json
import logging
from datetime import datetime, timedelta

from agents.parsing import loads_obj
from agents.prompts import SLOT_REFINE_PROMPT
from services.preference_slots import parse_avoid_windows
from services.tasks import (
    SLOT_MAX_DAYS,
    AlreadyScheduledError,
    NoSlotError,
    day_busy_minutes,
    is_valid_slot,
    suggest_slot,
    title_morning_only,
)

logger = logging.getLogger(__name__)


def plan_slots(*, entries: list[dict], busy: list[tuple[datetime, datetime]],
               now: datetime, day_start_min: int, day_end_min: int,
               lunch: tuple[int, int] | None,
               load_summary=None, weather=None, rules=None,
               use_ai: bool = True,
               day_budget: int | None = None) -> dict[str, datetime | None]:
    """为一批条目定时间：规则找空档 → AI 微调 → 校验回落（返回 {key: 时刻|None}）。

    entries 条目：{"key": str, "title": str, "priority": int, "duration": int,
    "from": datetime | None（该条目的时间起点，缺省用 now）,
    "same_clock_first": bool（缺省 False——收件箱一键安排与草案语义是"最早
    优先"；conflict 改期建议传 True 保留"明天同钟点"语感）}。key 是调用方的
    身份键（task_id 或草案合成键）。返回的每个时刻都已过 is_valid_slot 校验
    （含同批互斥），None 表示该条目 7 天窗内无空档。纯函数：不读库不写库，
    AI 层异常内部消化整层跳过，永不因 AI 故障空手。

    day_budget = work_minutes_per_day（每日工时预算）：传入后决策层维护每日
    负荷累计（既有 busy 按天拆分 + 同批已分配条目），当日塞满工时即顺延次日
    ——批量分散的规则层硬保证（2026-10-20 真机教训：无预算时最早优先全堆
    同一天）。None = 不限（既有调用方默认，行为不变）。

    模式参数化是产品语义不构成引擎分裂：conflict 同钟点优先 / 一键与草案
    最早优先，时间决策单点收口不变（docs/ai-agents.md §二）。
    """
    now = now.replace(second=0, microsecond=0)
    busy_acc = list(busy)  # 累积副本：同批条目互不重叠（不改动调用方列表）
    day_load = day_busy_minutes(busy) if day_budget is not None else None
    # 偏好避让窗（批 5）：rules 同时喂 AI 微调层（软指引）与规则/校验层
    # （硬闸）——「周五下午别排事」不再只靠模型自觉
    avoid = parse_avoid_windows(list(rules or []))
    # 尾随缓冲（2026-11-01 优化批 6 收口引擎）：长会/高强度条目（≥90 分钟或
    # priority≤1）后 +15 分钟虚拟延长——同批后续条目不再贴身排（finder 会议尾
    # 缓冲的引擎级统一，夜间预排/单卡/批量全入口继承；PRD §7.2）。缓冲不进
    # day_load（休息不是工时，finder 先例）；AI 微调槽生效时缓冲随条目搬移
    _BUFFER_MIN = 15
    tails: dict[str, int] = {}
    rule_slots: dict[str, datetime | None] = {}
    for e in entries:
        slot = suggest_slot(
            e.get("from") or now, e["duration"], busy_acc,
            day_start_min=day_start_min, day_end_min=day_end_min, lunch=lunch,
            morning_only=title_morning_only(e["title"]),
            same_clock_first=bool(e.get("same_clock_first")),
            day_load=day_load, day_budget=day_budget, avoid_windows=avoid)
        rule_slots[e["key"]] = slot
        if slot is not None:
            tail = (_BUFFER_MIN if e["duration"] >= 90
                    or (e.get("priority") or 2) <= 1 else 0)
            tails[e["key"]] = tail
            busy_acc.append((slot, slot + timedelta(
                minutes=e["duration"] + tail)))
            if day_load is not None:
                day_load[slot.date()] = day_load.get(slot.date(), 0) + e["duration"]

    finals: dict[str, datetime | None] = dict(rule_slots)
    candidates = {e["key"]: e for e in entries if rule_slots[e["key"]] is not None}
    if use_ai and candidates:
        try:
            refined = _refine_with_llm(
                candidates=candidates, rule_slots=rule_slots, now=now,
                load_summary=load_summary, weather=weather, rules=rules)
        except Exception as exc:  # AI 层永不抬升故障：NoActiveModelError / litellm /
            # 解析失败一律整层跳过，全批回落规则槽（引擎兜底性）。2026-11-01
            # 优化批 1 补 warning——此前静默回落零日志，AI 微调层是否生效排障靠猜
            logger.warning("[engine] AI 微调层整层跳过（%s：%s），全批回落规则槽",
                           type(exc).__name__, exc)
            refined = None
        if refined:
            for e in entries:  # 按 entries 顺序消费微调，同批互斥照样累积
                key = e["key"]
                cand = refined.get(key)
                if key not in candidates or not isinstance(cand, datetime):
                    continue
                # 微调校验前剔除自身的规则槽占用（挪动前的旧位置不该挡自己；
                # 含尾随缓冲的整段区间一起剔——缓冲是自己留的不该挡自己）
                rule = rule_slots[key]
                dur_total = e["duration"] + tails.get(key, 0)
                busy_eff = [(b0, b1) for b0, b1 in busy_acc
                            if not (b0 == rule and b1 == rule + timedelta(minutes=dur_total))]
                day_eff = None
                if day_load is not None:  # 每日负荷同步剔除自身旧位置贡献
                    day_eff = dict(day_load)
                    day_eff[rule.date()] = day_eff.get(rule.date(), 0) - e["duration"]
                if is_valid_slot(
                        cand, e["duration"], busy_eff, now=now,
                        day_start_min=day_start_min, day_end_min=day_end_min,
                        lunch=lunch, morning_only=title_morning_only(e["title"]),
                        day_load=day_eff, day_budget=day_budget,
                        avoid_windows=avoid):
                    finals[key] = cand
                    # 微调槽生效则旧规则槽同步退场（2026-10-21 复查批 A 修——
                    # 旧版只 append 新槽，旧槽残影双计日预算还挡后续条目的微调
                    # 候选，把条目推向更差时刻；remove 恒命中：规则槽在上方
                    # 建槽段刚 append 过；缓冲区间随条目一起搬移）
                    busy_acc.remove((rule, rule + timedelta(minutes=dur_total)))
                    busy_acc.append((cand, cand + timedelta(minutes=dur_total)))
                    if day_load is not None:
                        day_load[rule.date()] = day_load.get(rule.date(), 0) - e["duration"]
                        day_load[cand.date()] = day_load.get(cand.date(), 0) + e["duration"]
    return finals


def _refine_with_llm(*, candidates: dict[str, dict], rule_slots: dict, now: datetime,
                     load_summary, weather, rules) -> dict[str, datetime]:
    """AI 微调层（plan_slots 内部步）：单次 SLOT_REFINE_PROMPT 调整全批。

    活跃调用方登记（2026-11-01 优化批 6 收编注记，防误删误改——tech-debt 曾
    登记「use_ai=True 无 chat 调用方」的孤儿状态已过时）：夜间重要预排
    （workflows._collect_priority_plan，ai_on 条件）与批量排期短路轮
    （profiler._bulk_shortcut）；chat 排期轮的时间决策走排期子图
    finder/adjudicator（§2.3c），不经本层——两条路径并存是收口架构的既定
    地形，不是冗余。

    返回 {key: datetime}（只含解析成功且在候选集内的条目——合法性统一由调用
    方 is_valid_slot 后验，此处只管解析与搬运）；解析失败 / 响应形状不对 /
    输出非法 id 全部静默丢弃（返回空或部分 dict，调用方按无微调处理）。异常
    一律上抛（call_llm 的 NoActiveModelError / litellm 原生异常），整层跳过
    的决策在 plan_slots 做，这里只管跑。
    """
    from agents.prompts import clip_rules  # 延迟 import；批 3 补——engine 是唯一不 clip 的 JSON 入口
    from services.llm import call_llm  # 函数内 import 防环（house style）

    payload = {
        "now": f"{now:%Y-%m-%dT%H:%M:%S}",
        "tasks": [
            {"id": e["key"], "title": e["title"], "priority": e.get("priority") or 2,
             "minutes": e["duration"],
             "rule_start": f"{rule_slots[e['key']]:%Y-%m-%dT%H:%M:%S}"}
            for e in candidates.values()
        ],
        "load_summary": load_summary,
        "weather": weather,
        "memory_rules": clip_rules(list(rules or [])),
    }
    raw = call_llm(
        [{"role": "system", "content": SLOT_REFINE_PROMPT},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        json_mode=True, max_tokens=800,  # 全库唯一漏配上限的调用点，2026-10-21 补
    )
    obj = loads_obj(raw)
    if not (isinstance(obj, dict) and isinstance(obj.get("slots"), list)):
        return {}
    refined: dict[str, datetime] = {}
    for item in obj["slots"]:
        if not (isinstance(item, dict) and item.get("id") in candidates
                and isinstance(item.get("start"), str)):
            continue
        try:  # naive 本地时间契约：带时区后缀即弃（is_valid_slot 双保险）
            refined[item["id"]] = datetime.fromisoformat(item["start"])
        except ValueError:
            continue
    return refined


def auto_schedule_run(db, task_id: str) -> dict:
    """收件箱单卡「智能安排」入口（routers/tasks.py 调）：目标校验 → 纯规则定时间。

    2026-10-20 用户决策：①单卡降级纯规则（use_ai=False，毫秒级直落——单
    任务找空档规则层足够，AI 微调等待不值，事后拖拽即改）；②批量「安排
    全部」按钮退役，多任务批量排期全走对话（planner 草案卡确认后落库，
    确认心智单一）。签名随之单卡化（原 task_ids 列表 + None 批量形态删除）。

    返回单 decision {"task_id", "title", "slot"}（slot 已过校验层；不写库
    ——落库由 router 调 apply_slot_decisions）。目标校验：不存在 raise
    KeyError→404、已排期 raise AlreadyScheduledError→409；引擎无槽 raise
    NoSlotError（422 文案可直显）省一次落库。

    每日工时预算照常生效：当日既有 busy 已塞满工时 → 空档顺延次日（负荷
    语义与批量/对话入口一致）。
    """
    from services.settings import get_settings  # 函数内 import 防环（house style）
    from services.tasks import busy_intervals, get_task

    t = get_task(db, task_id)
    if t is None:
        raise KeyError(task_id)
    if t.start_time is not None:
        raise AlreadyScheduledError(f"待办「{t.title}」已有排期")

    s = get_settings(db)
    now = datetime.now().replace(second=0, microsecond=0)
    busy = busy_intervals(db, days_back=0, days_fwd=SLOT_MAX_DAYS + 1)
    lunch = (s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None
    duration = t.duration_mins if t.duration_mins and t.duration_mins > 0 else 60
    # 记忆接入（2026-11-03 记忆重构批 4）：旧版不传 rules——用户设了「周五下午
    # 别排事」偏好，单卡智能安排照样排进禁区；plan_slots 规则层（avoid_windows
    # 硬筛）+ 校验层一次继承，三入口（对话/批量/单卡）偏好口径归一
    from services.ai_data import list_memory_rules, rule_label

    rules = [rule_label(r) for r in list_memory_rules(db)]
    finals = plan_slots(
        entries=[{"key": t.id, "title": t.title, "priority": t.priority or 2,
                  "duration": duration, "from": now, "same_clock_first": False}],
        busy=busy, now=now,
        day_start_min=s.calendar_start_min, day_end_min=s.calendar_end_min,
        lunch=lunch, use_ai=False, day_budget=s.work_minutes_per_day,
        rules=rules)
    slot = finals.get(t.id)
    if slot is None:
        raise NoSlotError(f"未来 {SLOT_MAX_DAYS} 天没有「{t.title}」的可用空档")
    return {"task_id": t.id, "title": t.title, "slot": slot}
