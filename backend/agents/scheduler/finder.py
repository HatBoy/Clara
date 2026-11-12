"""Node3 规则硬约束初筛（批 5 正式化）：candidate_slots 多候选泛化 + 十道
硬约束（2026-10-21 计划落地）。

从批 2 的"委托 engine.plan_slots"切换为纯规则多候选——AI 微调职责移交
Node4 裁决器（批 6），SLOT_REFINE 单次微调随旧统一轮退役（收件箱单卡链路
不受影响——auto_schedule_run 仍走 plan_slots 纯规则形态）。

约束总表（每道可单测，详见 docs/ai-agents.md 排期子图节）：
1. 政务/银行（common_sense 含工作日类词）→ 候选限工作日（allowed_dates）
2. 就医上午：title_morning_only + 画像常识含"上午"并入（上界 12:00）
3. 户外 + 天气：降水概率 ≥60% 的日期整日剔除（allowed_dates 减雨日）
4. deep-work 哲学 ∧ 深度脑力 ∧ 高能耗 → 连续块下限 120 分钟（duration 抬升）
5. 缓冲：候选不紧贴 ≥3h 会议链尾后 30 分钟（busy 虚拟延长实现，与
   candidate_slots 五道闸同口径避让）
6. 通勤打包：差旅/查交通条目槽前 30 分钟须空闲（市内口径常量，跨城登记
   技术债不接实时路况）；通勤块出卡批 7 接
7. 日预算/午休/时段窗：candidate_slots 原生参数（与 suggest/is_valid 同源）
8. 低能耗短任务（≤30min）垃圾时间锚定（午休末与日末前 60 分钟起搜优先）
9. 频率防呆：常量表间隔内做过同类 → frequency 问询卡（条目剔出待排）
10. 零候选（agent 条目窗口筛空/通勤缓冲全滤掉）→ no_slot 问询卡（条目
    剔出待排；conflict 条目零候选保持旧口径——suggestion 置 None 的
    conflict 问询"改到什么时间"，不转 no_slot）
"""

import logging
from datetime import date, datetime, timedelta

from agents.nodes import _status_emitter
from services.preference_slots import parse_avoid_windows
from services.tasks import (
    candidate_slots,
    day_busy_minutes,
    overlaps,
    title_morning_only,
)

from .state import SchedulerState

logger = logging.getLogger(__name__)

# —— 常量表（批 5；跨城通勤/实时路况登记技术债，不接地图 API）——
COMMUTE_MINUTES = 30          # 差旅/外出条目前置通勤块（市内口径）
BUFFER_MIN = 30               # 长会链尾/差旅落地后的缓冲（分钟）
MEETING_TAIL_MIN = 15         # 单场会议尾缓冲（2026-10-22 批 5 场景用例泛化）
RAINY_PRECIP = 60             # 降水概率 ≥ 此值整日剔除（户外类，%）
DEEP_BLOCK_MIN = 120          # deep-work 哲学下深度条目连续块下限（分钟）
GARBAGE_DUR_MAX = 30          # 垃圾时间锚定的短任务上限（分钟）
FREQ_GUARD_DAYS = {"洗牙": 180, "体检": 365, "疫苗": 90}   # 频率防呆：标题子串 → 建议间隔（疫苗 90 天保守下限——多针程序内拆条目由 PROFILER 给日期，此处只拦"单提打疫苗"的近期重复）
_WEEKDAY_HINTS = ("工作日", "政务", "银行", "办证", "车管所", "社保")
_OUTDOOR_CATS = ("外出体力", "差旅通勤")  # 避雨约束域（户外=天气敏感）
_COMMUTE_CATS = ("差旅通勤",)  # 通勤块约束域（2026-11-02 真机三轮拆分：夜跑
# 「外出体力」曾误出「路上」条目——户外≠差旅，两个约束共用一个域混了语义；
# 避雨照旧看 _OUTDOOR_CATS，通勤前置只看本表）


def _extend_busy_buffers(busy: list, long_chain_ends: list[datetime],
                         busy_meta: list[dict] | None = None) -> list:
    """缓冲虚拟延长（约束 5）：≥3h 链尾后 30 分钟并入 busy——延长后的 busy
    即约束本身，candidate_slots 五道闸与批 6 裁决后验 is_valid_slot 用同一份
    就天然同口径（不必感知哪段是链）。批 5 泛化（2026-10-22 场景用例）：单场
    会议（busy_meta 标题命中会议词，MEETING_KEYWORDS_RE 单源）尾 +15 分钟
    ——散会后立刻塞新事是真实教训；与链尾 30 分钟同区间叠加时后者覆盖前者
    （重叠区间对避让判定无差）。"""
    out = list(busy)
    for end in long_chain_ends:
        out.append((end, end + timedelta(minutes=BUFFER_MIN)))
    if busy_meta:
        from services.job_rules import MEETING_KEYWORDS_RE

        for m in busy_meta:
            end = m.get("end")
            if (isinstance(end, datetime)
                    and MEETING_KEYWORDS_RE.search(m.get("title") or "")):
                out.append((end, end + timedelta(minutes=MEETING_TAIL_MIN)))
    return out


def _rainy_dates(weather) -> set[date]:
    """天气预报（forecast_for_city 裸 list[dict] 形态——行含 date/precip）→
    降水概率 ≥60% 的日期集合（户外剔日口径；None/形态异常恒空集——未配置
    城市不构成约束）。"""
    if not isinstance(weather, list):
        return set()
    out: set[date] = set()
    for r in weather:
        if not isinstance(r, dict):
            continue
        precip, d = r.get("precip"), r.get("date")
        if isinstance(precip, (int, float)) and precip >= RAINY_PRECIP and d:
            try:
                out.add(date.fromisoformat(str(d)))
            except ValueError:
                continue
    return out


def _needs_weekday(profile: dict) -> bool:
    """政务/银行类硬约束判定（约束 1）：画像常识条款命中工作日类词——高频
    硬场景进代码（分层落地决策），语义泛化留给提示词层。"""
    rules = profile.get("common_sense_rules") or []
    return any(any(h in r for h in _WEEKDAY_HINTS) for r in rules if isinstance(r, str))


def _commute_needed(profile: dict) -> bool:
    """通勤前置判定（约束 6）：差旅通勤类或声明需要查交通（避雨域不含——
    约束域拆分见 _COMMUTE_CATS 注释）。"""
    return (profile.get("category") in _COMMUTE_CATS
            or "需要查交通" in (profile.get("env_deps") or []))


def _entry_morning_only(e: dict) -> bool:
    """就医上午闸判定（约束 2，finder 与裁决器后验共用口径）：标题关键词或
    医疗类画像常识含"上午"。"""
    profile = e.get("profile") if isinstance(e.get("profile"), dict) else {}
    return title_morning_only(e["title"]) or (
        profile.get("category") == "医疗健康"
        and any("上午" in r for r in (profile.get("common_sense_rules") or [])
                if isinstance(r, str)))


def _eff_duration(e: dict, philosophy: set[str]) -> int:
    """条目实效时长（约束 4 单点实现）：deep-work 哲学 ∧ 深度脑力 ∧ 高能耗
    → 连续块下限 120 分钟抬升（草案条目 duration 仍报用户原值，抬升只影响
    占用与找槽——"写方案 90 分钟"按 120 分钟块占位防打断）。"""
    profile = e.get("profile") if isinstance(e.get("profile"), dict) else {}
    duration = e["duration"] or 60
    if ("deep-work" in philosophy and profile.get("category") == "深度脑力"
            and profile.get("energy_drain") == "高"):
        return max(DEEP_BLOCK_MIN, duration)
    return duration


def finder_node(state: SchedulerState) -> dict:
    """初筛节点：逐条目组装约束参数 → candidate_slots 取 k 候选 → 通勤后滤
    → 首个存活槽为主荐；频率防呆与零候选转问询（first-wins 跨节点延续）。
    零 LLM；conflict 条目 from=原时间、same_clock_first=True 保"明天同钟点"
    语感，agent 条目最早优先；问询条目不占 busy（用户没确认）。"""
    ctx = state["context"]
    now = state["now"]
    conflict_queue = state.get("conflict_queue") or []
    agent_entries = state.get("agent_entries") or []
    question = state.get("question")
    dropped = list(state.get("dropped") or [])
    philosophy = set(state.get("philosophy_ids") or [])
    rainy = _rainy_dates(ctx["weather"])
    # 缓冲只影响找槽的 busy；日预算负荷按真实占用算（缓冲是休息不是工时）
    busy_ext = _extend_busy_buffers(ctx["busy"], ctx.get("long_chain_ends") or [],
                                    ctx.get("busy_meta"))
    day_load = (day_busy_minutes(ctx["busy"]) if ctx["day_budget"] is not None
                else None)
    busy_acc: list = list(busy_ext)  # 同批互斥：agent 选定即占用（含缓冲口径）
    last_medical = ctx.get("last_medical") or {}
    # 偏好避让窗（批 5 硬闸）：「周五下午别排事」进 candidate_slots 筛维——
    # 纯函数解析带缓存，每轮一次微秒级
    avoid = parse_avoid_windows(ctx.get("memory_rules") or [])

    def raise_question(kind: str, entries: list[dict], **extra) -> None:
        nonlocal question
        if question is None:
            question = {"kind": kind, "entries": entries, **extra}

    def pick_slot(e: dict, *, same_clock: bool, user_will: bool = False) -> datetime | None:
        """单条目约束组装（1/2/3/4/7/8）+ 多候选取首个过通勤闸（6）的槽。
        user_will（2026-11-05 真机批）：条目钟点是用户原话（_from_user 降级），
        午休/日预算/偏好避让按用户直落同口径豁免（照排草案待确认）——只有
        busy 是硬边界（撞了走 conflict 问询，由调用方先行转换）。"""
        profile = e.get("profile") if isinstance(e.get("profile"), dict) else {}
        duration = _eff_duration(e, philosophy)
        # 约束 1/3：日期白名单（工作日限定为底、雨日从中剔除）
        horizon = {now.date() + timedelta(days=i) for i in range(8)}
        allowed = {d for d in horizon if d.weekday() < 5} \
            if (_needs_weekday(profile) and not user_will) else None
        if ((profile.get("category") in _OUTDOOR_CATS
                or "需要好天气" in (profile.get("env_deps") or []))
                and rainy & (allowed or horizon)):
            allowed = (allowed or horizon) - rainy
        # 约束 2：就医上午闸（标题关键词 + 医疗类画像常识"上午"并入）；
        # insisted（"坚持安排"）连本闸一并豁免——上午被 daily 系列切碎时
        # 2h 医疗项在上午域永远零候选，重找仍 no_slot 死循环（2026-11-01
        # 真机教训：insisted 后原样重复"回复坚持安排"文案）；豁免后引擎可
        # 荐下午槽，adjudicator 附注仍会提醒就医建议上午
        morning = None if (user_will or e.get("_no_budget")) else _entry_morning_only(e)
        # 约束 8：低能耗短任务垃圾时间锚定（午休末与日末前 60 分钟优先）
        anchors = None
        if (not user_will and profile.get("energy_drain") == "低"
                and duration <= GARBAGE_DUR_MAX):
            anchors = [a for a in (ctx["lunch"][1] if ctx["lunch"] else None,
                                   ctx["day_end_min"] - 60) if a is not None] or None
        # 约束 7 + 预算豁免（no_slot"坚持安排"的回落通路）
        budget = None if (user_will or e.get("_no_budget")) else ctx["day_budget"]
        from_raw = e.get("_from") or e.get("start") or e.get("not_before")
        start_from = datetime.fromisoformat(from_raw) if from_raw else now
        if start_from <= now:
            start_from = now
        cands = candidate_slots(
            start_from, duration, busy_acc,
            day_start_min=ctx["day_start_min"], day_end_min=ctx["day_end_min"],
            lunch=None if user_will else ctx["lunch"], morning_only=morning,
            same_clock_first=same_clock,
            day_load=dict(day_load) if (day_load is not None and not user_will) else None,
            day_budget=budget, allowed_dates=allowed, anchor_mins=anchors,
            avoid_windows=None if user_will else avoid, k=5)
        # 约束 6：通勤前置（槽前 30 分钟须空闲——含同批已占与缓冲区）
        commute = _commute_needed(profile)
        for s in cands:
            if commute and any(
                    overlaps(s - timedelta(minutes=COMMUTE_MINUTES), s, b0, b1)
                    for b0, b1 in busy_acc):
                continue
            return s
        return None

    # —— 约束 9：频率防呆（先于找槽——防呆成立时无需找槽）——
    if conflict_queue or agent_entries:  # 零 LLM 毫秒级，但有活干时推一帧
        _status_emitter()("正在检索日历与空档…")  # 过程反馈（提速批 2）
    for e in agent_entries:
        if e.get("_freq_ok"):
            continue  # 用户已确认重排（resolution 消费轮置位）——再拦就是死循环
        title = e.get("title") or ""
        key = next((k for k in FREQ_GUARD_DAYS if k in title), None)
        if key and last_medical.get(key):
            last = datetime.fromisoformat(last_medical[key])
            if (now - last).days < FREQ_GUARD_DAYS[key]:
                raise_question("frequency", [e], last_done=last_medical[key],
                               interval_days=FREQ_GUARD_DAYS[key])
                e["_freq_blocked"] = True  # 剔出待排（下方循环跳过）

    slots_out: list[dict] = []  # 规格④ candidate_slots（Node4 裁决消费）
    clean: list[dict] = []

    # —— 用户明示钟点降级条目（_from_user，2026-11-05 真机批）：三重复核
    # title 未过降级 agent，但钟点来自用户原话——钟点即意志：撞既有 busy 转
    # conflict 问询（绝不静默挪槽，真机「明天下午三点开个会」曾被挪到后天
    # 早上 8 点、长口述"明天上午复诊"被顺延到周六）；past 仍拦（物理不可落）。
    # 不撞 busy 的留在 agent 走草案（pick_slot user_will 豁免软闸，落原钟点）
    for e in list(agent_entries):
        if not (e.get("_from_user") and e.get("_from")):
            continue
        s0 = datetime.fromisoformat(e["_from"])
        dur = _eff_duration(e, philosophy)
        if s0 < now:
            raise_question("past", [e])
            agent_entries.remove(e)
            continue
        if any(overlaps(s0, s0 + timedelta(minutes=dur), b0, b1)
               for b0, b1 in ctx["busy"]):
            e["_clash"] = [m["title"] for m in ctx.get("busy_meta") or []
                           if overlaps(s0, s0 + timedelta(minutes=dur),
                                       m["start"], m["end"])][:2]
            conflict_queue.append(e)
            agent_entries.remove(e)
            logger.info("[scheduler] finder：用户钟点撞 busy 转 conflict 问询「%s」",
                        e.get("title"))

    # —— conflict 条目：主荐即改期建议（不占 busy、不进草案——用户没确认）——
    sugg: datetime | None = None
    for i, e in enumerate(conflict_queue):
        slot = pick_slot(e, same_clock=True)
        if i == 0:
            sugg = slot
        if slot is not None:
            slots_out.append({"key": f"c{i}", "title": e["title"],
                              "slot": slot.isoformat()})
    # 全量条目进问询、建议取首条主荐（旧版只问首条其余无痕消失——复查批 A
    # 口径；零候选时 suggestion=None 走"改到什么时间"文案，不转 no_slot）；
    # clash_titles 取首条撞到的既有任务名（profiler 入队时从 busy_meta 查得，
    # 问询文案"已安排「X」"用，2026-10-22）
    if conflict_queue and question is None:
        raise_question("conflict", conflict_queue,
                       suggestion=sugg.isoformat() if sugg else None,
                       clash_titles=conflict_queue[0].get("_clash") or [])

    # —— agent 条目：主荐进草案 / 零候选 no_slot（约束 10）——
    # a 键按 clean 序编（跳过 _freq_blocked 的不占号）——裁决器按 key↔clean
    # 索引一一对应消费，跳位错位即错条目
    for e in agent_entries:
        if e.get("_freq_blocked"):
            continue
        slot = pick_slot(e, same_clock=False, user_will=bool(e.get("_from_user")))
        if slot is None:
            raise_question("no_slot", [e])
            dropped.append(f"「{e['title']}」未来 7 天没有可用空档")
        else:
            duration = _eff_duration(e, philosophy)
            slots_out.append({"key": f"a{len(clean)}", "title": e["title"],
                              "slot": slot.isoformat()})
            entry = {"task_id": e["task_id"], "title": e["title"],
                     "start": slot.isoformat(), "duration": e["duration"]}
            if _commute_needed(e.get("profile") if isinstance(e.get("profile"), dict) else {}):
                # 通勤条目内部标记：裁决器出口在最终槽前插"路上"块随卡出
                entry["_commute"] = COMMUTE_MINUTES
            clean.append(entry)
            busy_acc.append((slot, slot + timedelta(minutes=duration)))
            if day_load is not None:
                day_load[slot.date()] = day_load.get(slot.date(), 0) + duration

    logger.info("[scheduler] finder：十约束后 clean %d 条 / 候选槽 %d / 问询 %s / "
                "丢弃 %d", len(clean), len(slots_out),
                question["kind"] if question else "无", len(dropped))
    return {"candidate_slots": slots_out, "clean": clean, "dropped": dropped,
            "question": question, "busy_acc": busy_acc, "day_load": day_load}
