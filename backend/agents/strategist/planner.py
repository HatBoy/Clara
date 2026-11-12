"""Node4 规则直排器（批 5 正式化，2026-10-21）：action_items → draft/inbox 条目。

用户决策（2026-10-21）：action_items 不级联排期子图（LLM 成本不叠加）——
纯规则找槽出 draft 卡，用户点卡确认落库（前端 applyDraftItems 复用，
reschedule 条目天然走改期语义）。三流向：

- schedule + duration → 引擎找槽（same_clock_first=False 最早优先，窗口
  即引擎默认 SLOT_MAX_DAYS）命中 → draft 条目；无槽 → inbox 附注降级；
- reschedule + task_id → 以原任务时长（deep_context 快照，不裸信 LLM）从
  明日 0 点起找槽（not_before 语义），产 {task_id, title 原任务名, start,
  duration}；无槽/快照缺时长 → inbox 附注；
- inbox / schedule 缺 duration → inbox 条目。

找槽统一经 engine.plan_slots(use_ai=False)（2026-10-26 批 5，时间决策单点
收口红线）：全套闸一次继承——morning_only 就医上午闸 / 午休 / 日预算 /
偏好避让窗（rules 传入即硬筛），教练说「明早去医院复诊」直排不再落下午、
撞偏好窗的槽自动避开；此前手拼 suggest_slot 漏闸的坑（批 4 补 morning_only）
从结构上消失。同批已落条目计回 busy（防挪槽后旧槽幽灵占用——排期链修过的
坑，此处从第一天就防）。纯函数零 LLM、不读库（busy/settings/rules 由
dispatcher 组装传入，本模块可独立单测）。
"""

from datetime import datetime, timedelta


def plan_actions(actions: list[dict], *, now: datetime,
                 busy: list[tuple[datetime, datetime]],
                 day_start_min: int, day_end_min: int,
                 lunch: tuple[int, int] | None,
                 day_budget: int | None,
                 known: dict[str, dict],
                 rules: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """三流向编排 → (draft_plan 条目, inbox_items 条目)。

    known：deep_context 已知任务快照 {task_id: {"title", "duration"}}——
    reschedule 的标题与时长唯一可信源（brain 钳制只保 task_id 白名单）。
    rules：memory_rules 列表（偏好避让窗解析源，engine 内部解析）。
    """
    from agents.engine import plan_slots

    busy = list(busy)  # 拷贝再追加：同批已落条目计回 busy 供后续条目避让
    drafts: list[dict] = []
    inbox: list[dict] = []

    def find_slot(title: str, duration: int, from_dt: datetime) -> datetime | None:
        """单条经统一引擎规则层找槽——与 chat 排期/收件箱一键安排同一决策内核。"""
        return plan_slots(entries=[{"key": "k", "title": title, "duration": duration,
                                    "from": from_dt, "same_clock_first": False}],
                          busy=busy, now=now, day_start_min=day_start_min,
                          day_end_min=day_end_min, lunch=lunch,
                          day_budget=day_budget, rules=rules,
                          use_ai=False).get("k")

    for it in actions:
        action, title, duration = it.get("action"), it.get("title"), it.get("duration")
        if action == "schedule" and duration:
            slot = find_slot(title, duration, now)
            if slot is not None:
                drafts.append({"task_id": None, "title": title,
                               "start": slot.isoformat(timespec="minutes"),
                               "duration": duration})
                busy.append((slot, slot + timedelta(minutes=duration)))
            else:
                inbox.append(_entry(title, duration, "近期找不到空档，已放收件箱"))
        elif action == "reschedule":
            snap = known.get(it.get("task_id")) if it.get("task_id") else None
            if not snap or not snap.get("duration") or not snap.get("title"):
                inbox.append(_entry(title, None, "未能自动改期（信息不足），请手动调整"))
                continue
            # 明日 0 点 = not_before 语义：改期不给"今天更晚"，从明天起找
            not_before = now.replace(hour=0, minute=0, second=0, microsecond=0) \
                + timedelta(days=1)
            slot = find_slot(snap["title"], snap["duration"], not_before)
            if slot is not None:
                drafts.append({"task_id": it["task_id"], "title": snap["title"],
                               "start": slot.isoformat(timespec="minutes"),
                               "duration": snap["duration"]})
                busy.append((slot, slot + timedelta(minutes=snap["duration"])))
            else:
                inbox.append(_entry(snap["title"], snap["duration"],
                                    "未来无空档，未能自动改期，请手动调整"))
        else:
            inbox.append(_entry(title, duration, None))
    return drafts, inbox


def _entry(title: str, duration: int | None, note: str | None) -> dict:
    """inbox 条目（_parse_ingest 同形：title/duration/notes/source_ref）。"""
    return {"title": title, "duration": duration, "notes": note, "source_ref": None}
