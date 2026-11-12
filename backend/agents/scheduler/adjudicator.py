"""Node4 AI 裁决器（批 6 正式化，2026-10-21）。

build_adjudicator_prompt 动态组 Prompt（精力/哲学/天气/通勤按画像裁剪——
见 prompts.py）+ selections 消费 + is_valid_slot 后验回落。两条铁律：
① LLM 不产卡片结构（draft/question 卡由确定性代码组装，杜绝 schema 漂移）；
② 永不空手（裁决失败/垃圾输出 → clean 保持 finder 主荐，finder 主荐本来就
过了同一套闸）。回复组装纪律恒定：applied→question→clean→insist_clash
固定序、草案摘要确定性拼装（谎报修复不回退）——不可让渡给 LLM 的部分。
"""

import logging
from datetime import datetime, timedelta

from agents.nodes import _fmt_when, _question_text, _status_emitter
from agents.parsing import call_llm_json

from .state import SchedulerState

logger = logging.getLogger(__name__)

_ADJUST_MAX_MIN = 90  # adjust_start 同日微调上界（分钟）


def _shell_entry(e: dict) -> dict:
    """条目出子图剥离（profile + 下划线内部键 _from/_no_budget/_freq_ok 等）。"""
    return {k: v for k, v in e.items()
            if k != "profile" and not k.startswith("_")}


def adjudicator_node(state: SchedulerState) -> dict:
    """裁决节点（职责/输入/输出/设计原因见模块 docstring，LLM 调用数 0~1——
    slots 非空才调）。

    输入：profiler/finder 的全部流水键（含 candidate_slots/busy_acc）。输出：
    九键出口子集 + final_decision（规格⑤——selections 落键与 explanation，
    子图内观测件不出壳）。profiler 已写终态（解析失败降级文案）时透传不组装。
    """
    clean = state.get("clean") or []
    question = state.get("question")
    slots = state.get("candidate_slots") or []
    conflict_queue = state.get("conflict_queue") or []
    final_decision: dict = {}
    explanation: str | None = None

    if slots and (clean or (question is not None and question.get("kind") == "conflict")):
        _status_emitter()("正在权衡最佳时刻…")  # LLM② 过程反馈（提速批 2）
        explanation = _adjudicate(state, clean, conflict_queue, slots, question,
                                  final_decision)
        _insert_commute_blocks(clean)

    if state.get("final_reply"):
        return {"final_decision": final_decision}  # profiler 降级终态，零产物组装

    applied = state.get("applied") or []
    insist_clash = state.get("insist_clash") or []
    dropped = state.get("dropped") or []
    displace = state.get("displace_drafts") or []
    llm_reply = state.get("llm_reply") or ""

    # 回复组装：问询模板 > 落库摘要 > 草案摘要 > LLM reply；agent 丢弃附注仅
    # 无问询时拼。草案摘要确定性组装——llm_reply 只在零产物时兜底（模型常在
    # "只是待确认草案"时谎称"已安排"，确定性拼装杜绝；displace 轮例外前置
    # ——它是口吻确认"明白，X 优先"，事实仍由确定性文案给出）
    parts: list[str] = []
    if applied:
        bits = []
        for e in applied:
            if e.get("start"):
                bits.append(f"「{e['title']}」{_fmt_when(datetime.fromisoformat(e['start']))}")
            else:
                bits.append(f"「{e['title']}」进待办")
        parts.append("已安排：" + "、".join(bits) + "。")
    if displace:
        bits = [f"「{e['title']}」调整到 {_fmt_when(datetime.fromisoformat(e['start']))}"
                for e in displace]
        parts.append("已把挡路的挪开：" + "、".join(bits) + "，确认后完成改期。")
    for note in state.get("displace_notes") or []:
        parts.append(f"（{note}）")
    if question is not None:
        parts.append(_question_text(question))
    if clean:
        bits = [f"「{e['title']}」{_fmt_when(datetime.fromisoformat(e['start']))}" for e in clean]
        parts.append("已出草案：" + "、".join(bits) + "，确认后写入日历。")
        if explanation:  # 裁决器秘书口吻"为何此时段"附注（一句话说人话）
            parts.append(f"（{explanation}）")
    if insist_clash:
        # insisted 落库但撞既有日程：不拦（用户意志优先），如实附注提醒
        parts.append(f"（按你的坚持安排；注意「{'、'.join(insist_clash[:3])}」与既有日程时间重叠）")
    for note in state.get("prefer_clash") or []:  # 直落撞偏好窗（批 5）：照落提醒
        parts.append(f"（{note}，下次帮你避开）")
    reply = "".join(parts) or llm_reply
    if state.get("displace_done") and llm_reply and parts:
        reply = llm_reply + reply  # displace 轮口吻前置（见上方组装纪律注）
    if not reply:
        reply = "这轮没能产出可用的排期草案，请换个说法或手动排期。"
    if dropped and question is None:
        reply += f"\n（{len(dropped)} 条草案未通过校验被丢弃：{'；'.join(sorted(set(dropped)))}）"

    # draft_plan 出口剥内部键（_commute——主体条目带标记进卡会污染契约）；
    # displace 改期条目并入出卡（task_id 非空 → 前端 applyDraftItems 走改期）
    result: dict = {"draft_plan": [_shell_entry(e) for e in clean + displace],
                    "final_reply": reply, "final_decision": final_decision}
    if applied:
        # 剖析画像（批 4）与流水内部键（批 5：_from/_no_budget/_freq_ok——下划线
        # 前缀约定）是子图内部件——出口九键契约零漂移，出子图前统一剥离
        # （壳落库/建行只认契约键）
        result["applied_plan"] = [_shell_entry(e) for e in applied]
    if question is not None:
        question["original_input"] = state["user_input"]
        if isinstance(question.get("entries"), list):
            question["entries"] = [
                _shell_entry(e) if isinstance(e, dict) else e
                for e in question["entries"]]
        result["question_plan"] = question
    resolved = state.get("resolved")
    if resolved is not None:
        result["resolved_proposal"] = resolved
    logger.info("[scheduler] adjudicator：草案 %d / 直落 %d / 问询 %s / 附注 %s",
                len(result["draft_plan"]), len(result.get("applied_plan") or []),
                question["kind"] if question is not None else "无",
                "有" if explanation else "无")
    return result


def _insert_commute_blocks(clean: list[dict]) -> None:
    """通勤块出卡：带 _commute 标记的条目在最终槽前插一条"路上"时间块（随
    草案出卡，采纳即落库——赶高铁要预留提前量的可见形态）。裁决微调后的
    通勤窗不复验：通勤块本身是显示性提醒，撞了用户看得到自行挪（取舍登记
    docs/tech-debt.md）。_commute 内部键随 _shell_entry 出口剥离。"""
    i = 0
    while i < len(clean):
        mins = clean[i].get("_commute")
        if mins:
            start = datetime.fromisoformat(clean[i]["start"])
            clean.insert(i + 1, {
                "task_id": None,
                "title": f"去「{clean[i]['title'][:12]}」路上",
                "start": (start - timedelta(minutes=mins)).isoformat(),
                "duration": mins})
            i += 1
        i += 1


def _adjudicate(state, clean, conflict_queue, slots, question,
                final_decision: dict) -> str | None:
    """LLM 精修一轮：selections 消费（key→候选槽、adjust 同日 ±90min）→
    is_valid_slot 后验（失败回落该条 finder 主荐——主荐本就过同套闸）→
    clean 换槽 / conflict 首条 suggestion 换槽。垃圾输出静默跳过（零行为
    变化）。final_decision 就地写入（selections 落键 + explanation）。"""
    from services.preference_slots import parse_avoid_windows
    from services.tasks import day_busy_minutes, is_valid_slot

    from .finder import _eff_duration, _entry_morning_only, _extend_busy_buffers
    from .prompts import build_adjudicator_prompt

    ctx = state["context"]
    now = state["now"]
    philosophy = set(state.get("philosophy_ids") or [])
    entries = list(clean) + list(conflict_queue)
    # 偏好避让窗（2026-11-03 记忆重构批 4）：微调槽 ±90min 后验补查——旧版
    # valid() 不带 avoid_windows，LLM 微调可跳出用户偏好窗（finder.py:155 同款
    # 解析、preference_slots 整表签名模块缓存命中零成本）
    avoid = parse_avoid_windows(ctx.get("memory_rules") or [])
    system, user = build_adjudicator_prompt(entries, slots, ctx,
                                            list(philosophy))
    # call_llm_json（优化批 4）：解析失败窄域修复重试一次；仍失败返回 None
    # 静默保持 finder 主荐（引擎兜底性不变）
    data = call_llm_json(system, user, max_tokens=600)
    if not isinstance(data, dict):
        return None

    slot_by_key = {s["key"]: s for s in slots}
    chosen: dict[str, datetime] = {}
    for sel in data.get("selections") or []:
        if not isinstance(sel, dict):
            continue
        s = slot_by_key.get(sel.get("key"))
        if s is None:
            continue  # 编造 key 丢弃（key 只能引用候选表）
        target = datetime.fromisoformat(s["slot"])
        adj = sel.get("adjust_start")
        if isinstance(adj, str):
            try:
                adj_dt = datetime.fromisoformat(adj)
            except ValueError:
                adj_dt = None
            # 微调仅同日 ±90 分钟内生效；越界/跨日丢弃回落候选原值
            if (adj_dt and adj_dt.date() == target.date()
                    and abs((adj_dt - target).total_seconds()) <= _ADJUST_MAX_MIN * 60):
                target = adj_dt
        chosen[s["key"]] = target

    def valid(e: dict, slot: datetime, busy_v: list,
              load_v: dict | None) -> tuple[bool, int]:
        duration = _eff_duration(e, philosophy)
        return is_valid_slot(
            slot, duration, busy_v, now=now,
            day_start_min=ctx["day_start_min"], day_end_min=ctx["day_end_min"],
            lunch=ctx["lunch"], morning_only=_entry_morning_only(e),
            day_load=dict(load_v) if load_v is not None else None,
            day_budget=None if e.get("_no_budget") else ctx["day_budget"],
            avoid_windows=avoid), duration

    # clean 换槽：后验 busy 从 profiler 流水 busy 重建（finder 的 busy_acc 含
    # 各条主荐自身占用——重验自己会撞自己），选定一条追加一条（同批互斥）
    if clean:
        busy_v = _extend_busy_buffers(state.get("busy") or [],
                                      ctx.get("long_chain_ends") or [],
                                      ctx.get("busy_meta"))
        load_v = (day_busy_minutes(state.get("busy") or [])
                  if ctx["day_budget"] is not None else None)
        for i, e in enumerate(clean):
            key = f"a{i}"
            target = chosen.get(key)
            if target is None:
                target = datetime.fromisoformat(slot_by_key[key]["slot"])
            ok, duration = valid(e, target, busy_v, load_v)
            if not ok:
                base = datetime.fromisoformat(slot_by_key[key]["slot"])
                if base != target:
                    ok, duration = valid(e, base, busy_v, load_v)
                    if ok:
                        target = base
            if not ok:
                continue  # 主荐也过不了（同批竞态）→ 保持该条主荐原样不追加
            e["start"] = target.isoformat()
            busy_v.append((target, target + timedelta(minutes=duration)))
            if load_v is not None:
                load_v[target.date()] = load_v.get(target.date(), 0) + duration

    # conflict 首条 suggestion 换槽（问询文案只挂首条建议；conflict 不占 busy）
    if (question is not None and question.get("kind") == "conflict"
            and conflict_queue and "c0" in chosen):
        busy_v = _extend_busy_buffers(state.get("busy") or [],
                                      ctx.get("long_chain_ends") or [],
                                      ctx.get("busy_meta"))
        ok, _ = valid(conflict_queue[0], chosen["c0"], busy_v, None)
        if ok:
            question["suggestion"] = chosen["c0"].isoformat()

    exp = data.get("explanation")
    explanation = exp.strip()[:200] if isinstance(exp, str) and exp.strip() else None
    final_decision["selections"] = [{"key": k, "slot": v.isoformat()}
                                    for k, v in sorted(chosen.items())]
    final_decision["explanation"] = explanation
    return explanation
