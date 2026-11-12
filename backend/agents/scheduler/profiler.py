"""Node1 任务剖析器（批 2 骨架）：LLM 调用 + 旧 planner 确定性尾整段移植
（nodes.py:539-758，2026-10-21 拆批）。

批 2 与旧行为逐字等价：PLANNER_PROMPT 原文 + 同 payload + 同分流；批 4 把
提示词正式化为 PROFILER_PROMPT（六类剖析 / est_minutes / 软常识条款 / 两分钟
短路）。确定性尾留在本节点的原因（计划架构修正）：origin 分流与 pending
六形态消费的决议值产自 LLM——这些执行段必须跟在 LLM 之后，不能塞 intake。
"""

import json
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from agents.nodes import (
    _clock_from_text,
    _clean_draft_entry,
    _find_similar,
    _normalize_title,
    _status_emitter,
    rel_minutes_from_text,
)
from agents.parsing import call_llm_json, is_int
from agents.prompts import clip_rules
from agents.scheduler.finder import _needs_weekday
from services.preference_slots import hits_window, parse_avoid_windows
from services.tasks import overlaps as _overlaps

from .prompts import PROFILER_PROMPT
from .state import SchedulerState

# 用户原话时长表达（优化批 1）：命中的轮次不覆盖新建条目估值——用户设定优先
# 于同类历史（PROFILER §四.5 优先级链：用户 > 历史 > 经验）
_DURATION_IN_TEXT_RE = re.compile(
    r"[0-9两二三四五六七八九十半]+\s*(小时|钟头|分钟|min|h|H)|半个?小时|一个?(小时|钟头)")

logger = logging.getLogger(__name__)

# 批量排期意图三组词（2026-10-25 功能边界批）：范围 + 名词 + 动词全命中才判
# 批量——「把所有待办排了 / 帮我安排全部任务 / 没排期的都规划一下」等语序全
# 覆盖。改期动词在场即不判（「所有任务改排到周一」是改期语义，误判批量会弹
# 窗打断——漏判的批量语句走常规 LLM 管道，行为不坏只是不出弹窗，漏判代价小）
_BULK_SCOPE_RE = re.compile(r"所有|全部|全都|一切|每个|都|组|项目")
_BULK_NOUN_RE = re.compile(r"待办|日程|任务|事项|事情")
_BULK_VERB_RE = re.compile(r"排|安排|规划")
# insisted 确认词形（真机批）：完全匹配限定「(就/那就/还是/我)坚持安排/原时间」
# ——「还是坚持安排」曾被 LLM 判成新 fuzzy 请求，no_slot 消费死循环
_INSIST_CONFIRM_RE = re.compile(r"^(?:就|那就|还是|我)?坚持(?:安排|原时间|原来的时间)吧?$")
# 改期动词（真机批）：「把X改到Y」LLM 漏关联 task_id 时 similar 命中的转译信号
_RESCHED_VERB_RE = re.compile(r"改到|挪到|换到|移到|提前到|推迟到|改期|换个时间|改个时间")
_BULK_MUTATE_RE = re.compile(r"改|挪|移|取消|删")

# 政务/银行类标题关键词（2026-11-05 真机批）：周末提醒与画像工作日条款的
# 确定性兜底——办证事项客观上仅工作日窗口，LLM 画像条款生成不稳不依赖它
_GOV_TITLE_RE = re.compile(
    r"护照|签证|办证|出入境|车管所|社保|公积金|银行|政务|民政局|派出所|身份证|户口")


def _bulk_intent(text: str) -> bool:
    """批量排期意图判定（规则层零 LLM）。"""
    if _BULK_MUTATE_RE.search(text):
        return False
    return all(rx.search(text) for rx in (_BULK_SCOPE_RE, _BULK_NOUN_RE, _BULK_VERB_RE))

# 剖析画像的合法枚举（与 PROFILER_PROMPT §四一一对应；批 4 定稿六类规格）
_CATEGORIES = ("深度脑力", "机械杂事", "沟通协作", "外出体力", "医疗健康", "差旅通勤")
_ENERGY_LEVELS = ("高", "中", "低")
_ENV_DEPS = ("需要好天气", "需要查交通")


def _clean_profile(p) -> dict:
    """剖析画像防御性清洗（批 4）：形态错配字段丢弃、非法枚举归 other、
    越界值截断——模型画像不可信时下游硬约束见空即跳过（≈批 2 行为），
    排期永不因画像垃圾而变差。返回 {} 表示"无画像"。"""
    if not isinstance(p, dict):
        return {}
    out: dict = {}
    cat = p.get("category")
    if isinstance(cat, str) and cat.strip():
        out["category"] = cat.strip() if cat.strip() in _CATEGORIES else "other"
    drain = p.get("energy_drain")
    if isinstance(drain, str) and drain.strip() in _ENERGY_LEVELS:
        out["energy_drain"] = drain.strip()
    deps = p.get("env_deps")
    if isinstance(deps, list):
        ok = [d.strip() for d in deps if isinstance(d, str) and d.strip() in _ENV_DEPS]
        if ok:
            out["env_deps"] = ok
    rules = p.get("common_sense_rules")
    if isinstance(rules, list):
        ok = [r.strip()[:50] for r in rules if isinstance(r, str) and r.strip()]
        if ok:
            out["common_sense_rules"] = ok[:5]
    est = p.get("est_minutes")
    if is_int(est) and est > 0:
        out["est_minutes"] = est
    if p.get("important") is True:  # 仅用户明示/记忆画像明示（提示词 §四.6）
        out["important"] = True
    return out


def _two_minute_hit(data: dict, philosophy_ids: list[str]) -> str | None:
    """两分钟法则短路（批 4）：全部条目为新建且预估 ≤2 分钟、用户启用
    two-minute 哲学 → 返回劝导文案（不排期不落库——立刻做完比排期更省事，
    GTD 两分钟法则）。条目预估取 profile.est_minutes，缺省回落 duration。
    返回 None = 不短路照常管道。"""
    if "two-minute" not in philosophy_ids:
        return None
    entries = _raw_entries(data)
    if not entries or any(e.get("task_id") for e in entries):
        return None  # 空/改既有任务排期不适用
    for e in entries:
        p = e.get("profile") if isinstance(e.get("profile"), dict) else {}
        est = p.get("est_minutes") if is_int(p.get("est_minutes")) else e.get("duration")
        if not (is_int(est) and 0 < est <= 2):
            return None
    titles = "、".join(f"「{(e.get('title') or '').strip()[:30]}」"
                      for e in entries if isinstance(e.get("title"), str))
    return (f"{titles}这类两分钟内的小事不用排进日历——现在立刻做掉更省心，"
            "做完随时告诉我。")


def _raw_entries(data: dict) -> list[dict]:
    """LLM 条目清单的过渡容错（批 4 schema 更名）：正式键 entries，旧键
    draft_plan 兜底（替身/模型残留旧口径时行为不漂移）。"""
    raw = data.get("entries")
    if not isinstance(raw, list):
        raw = data.get("draft_plan")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _subseq_in(s: str, t: str) -> bool:
    """有序子序列判定：s 的字符按序散布在 t 中（「开会」⊂「开个会」）。
    仅供快录复核的量词插入兜底（调用方已限 s ≤4 字）。"""
    it = iter(t)
    return all(ch in it for ch in s)


_REL_DAY = {"今天": 0, "今晚": 0, "明天": 1, "明晚": 1, "后天": 2, "大后天": 3}


def _hint_to_not_before(hint: str, now: datetime) -> datetime | None:
    """date_hint → 引擎起点（2026-11-02 真机三轮）：「明天晚上七点」类字符串
    提示此前只服务 no_time 问询文案，改期兜底关联既有任务后交引擎全自主挑槽
    ——用户明给的时间被丢弃（真机：明天晚七点改期被排到 4 天后早八点）。
    只解析高频相对日词+钟点（今天/明天/后天/大后天 + _clock_from_text），
    周X 等复杂表达不解析保持现状（引擎从 now 起近期优先）。"""
    days = next((d for k, d in _REL_DAY.items() if k in hint), None)
    if days is None:
        return None
    day = (now + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    clock = _clock_from_text(hint)
    if clock is None:
        return day  # 只有日期 → 当天零点起（引擎在日窗内自挑）
    h, m = clock
    if ("晚上" in hint or "今晚" in hint or "明晚" in hint or "夜" in hint) and h < 12:
        h += 12  # 「晚上七点」7→19（_clock_from_text 已含词表，双保险）
    return day.replace(hour=h, minute=m)


def profiler_node(state: SchedulerState) -> dict:
    """剖析节点（职责/输入/输出/设计原因见模块 docstring，LLM 调用数 1）。

    输入：intake 的 context 快照 + 入口三键。输出：task_profile（LLM 原始
    解析）+ 分流产物（applied/question/resolved/conflict_queue/agent_entries/
    dropped/insist_clash/llm_reply/busy 增量）——conflict 与 agent 条目留待
    finder 引擎轮，early-exit 条件边批 4 增（当前线性贯通，行为与旧单节点
    完全一致：finder 空队列即空转）。垃圾输出 → 终态降级文案直接写
    final_reply，下游节点透传不再组装。
    """
    ctx = state["context"]
    now = state["now"]
    candidates = ctx["candidates"]
    valid_ids = set(ctx["valid_ids"])
    pending = state.get("pending") or None
    # 批量排期短路（2026-10-25 功能边界，零剖析 LLM）：批量意图 + ≥2 条待排
    # 候选 → 全量直交统一混合排期引擎出 bulk 草案，前端弹窗清单确认（对话
    # 框只沟通）。scheduler 系 pending 活跃轮不触发——回复语义是结算问询；
    # strategist 系（clarify/memory）放行——批量语句是明确换意图，旧问询由
    # 短路体结算 ignored 防幽灵问询抢后续轮（真机：批量请求被 LLM 误判 coach
    # 出 clarify"把具体待办发给我"后批量再也进不来）；候选 <2 条时弹窗无
    # 意义，走常规管道出单条草案卡
    if ((pending is None or pending.get("origin") == "strategist")
            and len(candidates) >= 2 and _bulk_intent(state["user_input"])):
        return _bulk_shortcut(state, ctx, now, pending)
    payload = {
        "today": now.strftime("%Y-%m-%d"),
        "now": now.strftime("%Y-%m-%dT%H:%M"),
        "user_input": state["user_input"],
        "pending": pending,
        "tasks_unscheduled": candidates,
        "tasks_scheduled": ctx["scheduled"],
        "load_summary": ctx["load_summary"],
        "settings": ctx["settings"],
        "weather": ctx["weather"],
        "recent_chat": ctx["recent_chat"],
        "chat_summary": ctx.get("chat_summary"),
        # 载荷双闸（优化批 4）：全量直通（理论上限 120 条）→ clip_rules 头部
        # 切片 40 条/3000 字符——画像库涨满后 profiler/裁决器载荷失控的溢出
        # 隐患堵上（assemble_system_prompt / reflection 同款单源口径）
        "memory_rules": clip_rules(state.get("memory_rules") or []),
        "recent_done": ctx.get("recent_done"),  # 近 7 天完成（批 4 补键）
        # 执行节奏量化 + 近期拒绝理由（优化批 5 数据回流）：估时对照真实产能、
        # 建议避开用户上周明确拒绝过的同类时间/理由
        "execution_rhythm": ctx.get("execution_rhythm"),
        "recent_rejects": ctx.get("recent_rejects"),
    }
    _status_emitter()("正在剖析任务画像…")  # LLM① 过程反馈（提速批 2）
    # insisted 确定性短路（2026-11-01 真机批）：「还是坚持安排」类变体 LLM
    # 判定不稳（glm 曾把「还是坚持安排」当新 fuzzy 请求重新剖析——no_slot
    # 问询消费失败死循环）。pending 活跃 + 词形完全匹配 → 零 LLM 直判
    # insisted（_DRAFT_ACK_RE 确认词表短路先例）；限定三种核心形态+语气
    # 前缀防误吞（「坚持每天跑步」类新表达不含在内），条目从 pending 快照取
    if pending and _INSIST_CONFIRM_RE.match(state.get("user_input") or ""):
        logger.info("[scheduler] profiler：坚持确认词短路 insisted（零 LLM）")
        data = {"resolution": {"action": "insisted", "entries": []},
                "draft_plan": [], "reply": ""}
    else:
        # call_llm_json（优化批 4）：解析失败窄域修复重试一次——qwen 思考污染的
        # 「排期推理失败」真机痛点，降级文案不是等价体验
        data = call_llm_json(
            PROFILER_PROMPT, json.dumps(payload, ensure_ascii=False), max_tokens=1200)
    if data is None:  # 垃圾输出（重试后仍失败）→ 零草案降级（不 raise）
        return {"task_profile": {},
                "draft_plan": [],
                "final_reply": "排期推理失败（模型输出无法解析），已降级——请稍后重试或手动排期。"}
    logger.info("[scheduler] profiler 原始输出：%s",
                json.dumps(_raw_entries(data), ensure_ascii=False)[:600])
    # 新建条目个性化耗时（2026-11-01 优化批 1）：PROFILER §四.5「同类历史耗时
    # 优先于经验值」此前只对候选清单条目生效（intake est_hint），对话新建条目
    # （task_id null）无 hint——这里按 LLM 提炼后的标题补查一次（小表查询
    # 毫秒级零 LLM），est_minutes 与 duration 同步对齐。用户原话给了时长的
    # 条目不覆盖（用户设定 > 历史 > 经验）
    if _DURATION_IN_TEXT_RE.search(state["user_input"] or ""):
        pass  # 用户明说时长：LLM 已照抄，历史不越权
    else:
        entries = data.get("entries")
        if isinstance(entries, list) and entries:
            from database import SessionLocal
            from services.tasks import similar_done_durations

            with SessionLocal() as db:
                for e in entries:
                    if not isinstance(e, dict) or e.get("task_id") is not None:
                        continue
                    hint = similar_done_durations(db, str(e.get("title") or ""))
                    if hint and isinstance(e.get("profile"), dict):
                        e["profile"]["est_minutes"] = hint
                        if isinstance(e.get("duration"), int) and e["duration"] > 0:
                            e["duration"] = hint
    if pending is None:
        # 两分钟短路先于一切分流（用户明说时间也劝导直接做——两分钟法则的
        # 精神就是"别为小事花排期成本"）；pending 活跃轮不触发（回复语义是
        # 结算问询，2026-10-21 批 4 取舍）
        shortcut = _two_minute_hit(data, state.get("philosophy_ids") or [])
        if shortcut:
            return {"task_profile": data, "draft_plan": [], "final_reply": shortcut}

    # busy 拷贝再追加：直落条目的占用进流水 busy（finder 引擎轮避让），
    # 不改写 context 快照（state 更新语义清晰，回放调试友好）
    busy: list[tuple[datetime, datetime]] = list(ctx["busy"])
    similar_pool = ctx["similar_pool"]

    applied: list[dict] = []
    dropped: list[str] = []
    insist_clash: list[str] = []  # insisted 落库但撞既有日程的标题（回复附注用）
    # 偏好避让窗（批 5）：直落条目撞窗不拦（用户明给时间，意志优先）但附注
    # 提醒——引擎/finder 侧才是硬闸（模糊排期不再落窗内）
    avoid = parse_avoid_windows(state.get("memory_rules") or [])
    prefer_clash: list[str] = []
    conflict_queue: list[dict] = []  # 撞期条目（建议时间统一由引擎一轮算出）
    agent_entries: list[dict] = []   # 时间待引擎定的 agent 草案
    question: dict | None = None
    resolved: dict | None = None
    displace_drafts: list[dict] = []  # displace 挡路者改期卡条目（adjudicator 并入出口）
    displace_notes: list[str] = []    # 系列/无槽挡路者附注（回复组装消费）
    displace_done = False             # 本轮走了 displace 消费（llm_reply 前置判据）

    def raise_question(kind: str, entries: list[dict], **extra) -> None:
        """单轮多触发合并为一条问询（kind 记首个）——一次只问一件事。"""
        nonlocal question
        if question is None:
            question = {"kind": kind, "entries": entries, **extra}

    def route_user_entry(entry: dict, *, insist: bool = False) -> None:
        """origin=user 候选的三问分流：过去→past、撞 busy→conflict（入队待
        统一引擎算改期建议，问题在引擎轮后抛出）、撞相似→similar，全过→
        applied（午休对用户意志豁免——原话指定优先于默认偏好）。insist=True
        （resolution=insisted，"体检约好了的，不能改时间"）：跳过 busy/similar
        再拦——用户明确坚持，意志优先（午休豁免同款先例），撞 busy 如实记
        insist_clash 供回复附注；past 仍拦（过去时间物理上不能落）。start 为
        空（问询"加待办"的落点）直接进未排期 applied。"""
        start_raw = entry.get("start")
        if not start_raw:
            applied.append({**entry, "start": None, "origin": "user"})
            return
        start = datetime.fromisoformat(start_raw)
        duration = entry["duration"]
        # 政务/银行类落周末附注（2026-11-01 真机批）：用户明给周末时间照落
        # （意志优先同 insisted 先例），但政务窗口客观上仅工作日开放——
        # prefer_clash 同款「照落不拦+提醒」（真机「约下周六办护照」直落无提示）
        if (start.weekday() >= 5
                and _needs_weekday(entry.get("profile") or {})):
            prefer_clash.append(
                f"「{entry['title']}」落在周末——政务/银行类窗口通常仅工作日"
                "开放，建议先确认网点营业时间")
        if start < now:
            raise_question("past", [entry])
        elif insist:
            if any(_overlaps(start, start + timedelta(minutes=duration), b0, b1)
                   for b0, b1 in busy):
                insist_clash.append(entry["title"])
            w = hits_window(start, duration, avoid)
            if w:
                prefer_clash.append(f"「{entry['title']}」与你的偏好「{w['rule']}」相冲")
            applied.append({**entry, "origin": "user"})
            busy.append((start, start + timedelta(minutes=duration)))
        elif any(_overlaps(start, start + timedelta(minutes=duration), b0, b1)
                 for b0, b1 in busy):
            # 改期建议不再就地产生——入队，统一引擎一轮算出后再抛问询；
            # _clash 记撞到的既有任务名（问询文案"已安排「X」"，2026-10-22，
            # 内部键随 _shell_entry 出口剥离）
            entry["_clash"] = [m["title"] for m in ctx.get("busy_meta") or []
                               if _overlaps(start, start + timedelta(minutes=duration),
                                            m["start"], m["end"])][:2]
            conflict_queue.append(entry)
        else:
            hit = _find_similar(entry["title"], similar_pool)
            if hit:
                raise_question("similar", [entry], similar_to=hit[0])
            else:
                w = hits_window(start, duration, avoid)
                if w:  # 照落不拦，附注提醒（同 insisted 撞 busy 先例）
                    prefer_clash.append(f"「{entry['title']}」与你的偏好「{w['rule']}」相冲")
                applied.append({**entry, "origin": "user"})
                busy.append((start, start + timedelta(minutes=duration)))

    def user_origin_verified(entry: dict) -> bool:
        """origin=user 三重复核：explicit_time 是原话子串、title 与原话有子串
        关系、原话钟点与 start 一致——任一不过由调用方降级 agent 走确认卡。
        2026-10-23 扩围：原话候选从本轮输入扩到近期对话的用户消息——省略式
        确认轮（"安排一下/帮我安排"）时间在历史原话里；时间与标题必须真实
        出现在用户自己说过的话里才算数，幻觉防线不弱化。"""
        raw_texts = [state["user_input"]] + [
            m.get("content") or "" for m in (ctx.get("recent_chat") or [])
            if m.get("role") == "user"]
        explicit = entry.get("explicit_time")
        if not (isinstance(explicit, str) and explicit.strip()):
            return False
        clock = _clock_from_text(explicit)
        start = datetime.fromisoformat(entry["start"])
        # 相对分钟形态（2026-11-05 真机批）：「两分钟后」无绝对钟点，一致性
        # 改为对照 now+N（调用方已把 start 确定性换算成该值，LLM 原猜时刻被覆盖）
        rel = rel_minutes_from_text(explicit) if clock is None else None
        if clock is None and rel is None:
            return False
        if clock is not None and clock != (start.hour, start.minute):
            return False
        if rel is not None and abs(
                (start - (now + timedelta(minutes=rel[0]))).total_seconds()) > 180:
            return False
        n_title = _normalize_title(entry["title"])
        for raw in raw_texts:
            if not raw or explicit.strip() not in raw:
                continue
            n_input = _normalize_title(raw)
            if n_title and (n_title in n_input or n_input in n_title):
                return True
            # 补全式扩写（2026-11-01 真机批）：LLM 常把口语补成规范标题
            # （"去理发店"→"去理发店理发"），双向子串都不中但共享长公共段
            # ——最长公共子串 ≥ max(4, 标题 2/3) 视为同一活动（幻觉标题
            # 与原话无此特征，防线不弱化；真机教训：曾因扩写降级 agent，
            # 撞 busy 被挪槽而用户明说的时间应出 conflict 问询）
            if n_title and len(n_title) >= 4:
                m = SequenceMatcher(None, n_title, n_input).find_longest_match(
                    0, len(n_title), 0, len(n_input))
                if m.size >= max(4, int(len(n_title) * 2 / 3)):
                    return True
            # 量词插入形态（同日真机批）：口语「开个会」拆散标题「开会」
            # （双向子串与 LCS 均不中）——短标题 ≤4 字做有序子序列判定
            # （标题字符按序散布在原话即认）；「牙医复诊」类幻觉无此特征。
            # 真机教训：「昨天下午3点开个会」曾因此降级 agent，past 拦截
            # 失效被静默排到明天
            if n_title and len(n_title) <= 4 and _subseq_in(n_title, n_input):
                return True
        return False

    llm_reply = data.get("reply")
    llm_reply = llm_reply.strip() if isinstance(llm_reply, str) else ""

    # ① pending 消费：resolution 七形态（unrelated/形态错配 → 忽略，按新请求走）
    turn_consumed = False
    if pending:
        res = data.get("resolution")
        action = res.get("action") if isinstance(res, dict) else None
        if action == "displace" and pending.get("kind") != "conflict":
            action = None  # 形态错配：非 conflict 问询没有"挤开"对象
        if action in ("inbox", "cancelled") \
                and _RESCHED_VERB_RE.search(state.get("user_input") or ""):
            # 改期话术守卫（2026-11-01 真机批）：similar 问询挂起时「把X改到Y」
            # 被 LLM 误判 inbox 吞掉改期意图（真机「技术预研改到周五」进了待办）
            # ——轻动作与改期动词互斥，降为新请求走②分流，改期兜底接住
            action = None
        if action in ("confirmed", "insisted", "displace", "rescheduled",
                      "inbox", "cancelled"):
            turn_consumed = True
            if action == "cancelled":
                resolved = {"proposal_id": pending["proposal_id"], "outcome": "ignored"}
            else:
                resolved = {"proposal_id": pending["proposal_id"], "outcome": "accepted"}
                p_entries = [dict(e) for e in pending.get("entries") or []]
                if action == "displace":
                    # 挤开冲突（2026-10-22）：新任务按原时间照排（用户已表态
                    # 优先级，busy/similar 不再拦——撞的是即将被挪走的挡路者；
                    # 不走 route_user_entry，撞 busy 入队就死循环），挡路者从
                    # busy_meta 识别、非系列自动找新槽出改期卡
                    for e in p_entries:
                        raw = e.get("start")
                        if raw and datetime.fromisoformat(raw) >= now:
                            s0 = datetime.fromisoformat(raw)
                            dur = e.get("duration")
                            dur = dur if isinstance(dur, int) and dur > 0 else 60
                            applied.append({**e, "origin": "user"})
                            busy.append((s0, s0 + timedelta(minutes=dur)))
                        else:
                            route_user_entry(e)  # past/缺 start 防御回落常规分流
                    blocked = _blocked_tasks(p_entries, ctx)
                    if blocked:
                        start_from = max(
                            (datetime.fromisoformat(e["start"])
                             + timedelta(minutes=e.get("duration") or 60)
                             for e in p_entries if e.get("start")),
                            default=now)
                        drafts, notes = _displace_shifts(blocked, busy, ctx,
                                                         start_from)
                        displace_drafts.extend(drafts)
                        displace_notes.extend(notes)
                    displace_done = True
                    entries = []  # 新条目已在分支内处理，公共分流循环不复用
                elif action in ("confirmed", "insisted"):
                    if pending.get("kind") in ("frequency", "no_slot"):
                        # 批 5 两类问询的确认 ≠ 拿快照直落：条目当初被防呆/
                        # 零候选剔出待排，确认后要重进管道找槽（条目回
                        # agent_entries 走 finder）。_freq_ok 豁免二次频率防呆
                        # ——用户已拍板再拦就是死循环；insisted（"坚持安排"）
                        # 额外 _no_budget：日预算豁免重找（finder 消费端）
                        entries = []
                        for e in p_entries:
                            if e.get("start"):
                                e["_from"] = e.pop("start")
                            e["_freq_ok"] = True
                            if action == "insisted":
                                e["_no_budget"] = True
                            agent_entries.append(e)
                    elif (action == "confirmed" and pending.get("kind") == "conflict"
                          and pending.get("suggestion")):
                        # confirmed = 同意建议：conflict 问询把起点换成主荐时间，
                        # 其余保持原条目；insisted = 坚持原时间（"体检约好了的，
                        # 不能改"）：快照条目原样落库，busy/similar 由
                        # route_user_entry(insist=True) 豁免（撞期附注）——
                        # 两者都不需要模型给新时间
                        for e in p_entries:
                            e["start"] = pending["suggestion"]
                        entries = p_entries
                    else:
                        entries = p_entries
                elif action == "rescheduled":
                    entries = []
                    # 画像沿用（2026-11-01 真机批）：重建条目此前只带 LLM 的
                    # title/start/duration——快照 profile 丢失致 finder 政务/
                    # 就医/户外约束在消费轮全部失效（真机「办护照」改时间后
                    # 冲突建议排周六下午）。事项没变只是时间变，画像按原标题
                    # 从快照合并回来
                    prof_by_title = {p.get("title"): p.get("profile")
                                     for p in p_entries
                                     if isinstance(p.get("profile"), dict)}
                    for r in res.get("entries") or []:
                        if not (isinstance(r, dict) and isinstance(r.get("title"), str)):
                            continue
                        # 单条目问询标题以快照为准：事项本身没变，变的只是时间
                        title = (p_entries[0].get("title") if len(p_entries) == 1
                                 else r["title"].strip())
                        entry, _why = _clean_draft_entry(
                            {**r, "task_id": None, "title": title}, valid_ids, origin="user")
                        if entry is not None:
                            prof = prof_by_title.get(r["title"].strip())
                            if prof:
                                entry["profile"] = prof
                            entries.append(entry)
                    entries = entries or p_entries  # 模型没给可用新时间 → 回落快照再过校验
                else:  # inbox：收进待办不排期
                    base = p_entries[0] if p_entries else {}
                    entries = [{"task_id": None, "title": base.get("title") or "待办",
                                "start": None, "duration": base.get("duration")}]
                before, agent_before = len(applied), len(agent_entries)
                for e in entries:
                    route_user_entry(e, insist=(action == "insisted"))
                if ((question is not None or conflict_queue)
                        and len(applied) == before
                        and len(agent_entries) == agent_before):
                    # 主荐/新时间未过校验 → 链式收敛为新问询顶替，旧问询按被
                    # 顶替结算（agent_entries 增长不算——frequency/no_slot 确认
                    # 轮条目回管道，落成与否由 finder 裁决）
                    resolved = {"proposal_id": pending["proposal_id"], "outcome": "ignored"}

    # ② entries 逐条分流（resolution 已消费本轮时跳过）；清洗后条目附
    # 剖析画像（批 4）——conflict_queue/agent_entries 带 profile 供批 5 硬
    # 约束消费，出口层（adjudicator）剥离保证九键契约零漂移
    if not turn_consumed:
        _raw = _raw_entries(data)
        # 单条目轮 + 原话相对分钟（2026-11-05 真机二批）：「提醒我两分钟后关火」
        # LLM 常判 agent/fuzzy 且时刻从缺（真机草案排到当天 08:00）——轮级确定性
        # 改写：origin 升 user、start=now+N、explicit_time=命中原话（三重复核
        # 天然过）。仅单条目轮：多条目口述里的"X分钟后"只属于其中一件
        _rel_round = (rel_minutes_from_text(state["user_input"] or "")
                      if len(_raw) == 1 else None)
        for d in _raw:
            if _rel_round and not d.get("task_id"):
                d = {**d, "origin": "user",
                     "start": (now + timedelta(minutes=_rel_round[0])).isoformat(),
                     "explicit_time": _rel_round[1]}
                d.pop("date_hint", None)
            profile = _clean_profile(d.get("profile"))
            origin = d.get("origin")
            origin = origin if origin in ("user", "fuzzy", "agent") else "agent"
            if origin in ("user", "fuzzy") and d.get("task_id"):
                origin = "agent"  # 改既有任务排期第一版不自动（系列语义不可逆）
            if origin == "fuzzy":
                title = d.get("title")
                title = title.strip() if isinstance(title, str) else ""
                if title and len(title) <= 30:
                    duration = d.get("duration")
                    hint = d.get("date_hint")
                    entry0 = {
                        "task_id": None, "title": title, "start": None,
                        "duration": duration if is_int(duration) and 0 < duration <= 1440 else None,
                        "profile": profile,
                    }
                    if profile.get("important"):
                        entry0["priority"] = 1  # 同上：明示信号 → 高优直判
                    # 相似检查优先于缺钟点——"X 日安排体检"若未来一周已有同款，
                    # 先提醒重排/确认（非高频活动撞名大概率是忘了已安排，
                    # 2026-10-18 真机教训），再谈几点
                    hit = _find_similar(title, similar_pool)
                    if hit:
                        raise_question("similar", [entry0], similar_to=hit[0])
                    else:
                        # 政务/银行类落周末（2026-11-05 真机批）：缺钟点先问几点
                        # 是正路，但窗口仅工作日开放——问的时候就提醒（问询文案
                        # _question_text weekend_note 消费），不等答完才在附注说。
                        # 判据 = 画像工作日条款 ∨ 标题政务关键词（画像条款由 LLM
                        # 生成不稳，真机二轮漏判——标题词是确定性兜底）
                        wk_note = None
                        if ((_needs_weekday(profile)
                                 or _GOV_TITLE_RE.search(title)) and re.search(
                                r"周六|周日|周末|星期六|星期天|礼拜六|礼拜天",
                                f"{hint or ''} {state['user_input']}")):
                            wk_note = "政务/银行类窗口通常仅工作日开放，建议改约工作日"
                        raise_question("no_time", [entry0],
                                       date_hint=hint if isinstance(hint, str) and hint.strip() else None,
                                       weekend_note=wk_note)
                continue
            entry, why = _clean_draft_entry(d, valid_ids, origin=origin)
            if entry is None:
                dropped.append(why)
                continue
            if profile:
                entry["profile"] = profile
                if profile.get("important"):
                    entry["priority"] = 1  # 用户明示/记忆画像明示 → 高优直判
            if origin == "user" and entry.get("start"):
                _rel_e = rel_minutes_from_text(entry.get("explicit_time") or "")
                if _rel_e is not None:
                    # 相对分钟（2026-11-05 真机批）：「两分钟后关火」确定性换算
                    # now+N 覆盖 LLM 猜的时刻——原话即此刻+N，LLM 时刻猜测在
                    # 此形态下从不可信（真机曾把"两分钟后"排到当天下午）
                    entry["start"] = (
                        now + timedelta(minutes=_rel_e[0])).isoformat()
            if origin == "user" and not user_origin_verified(entry):
                origin = "agent"  # 三重复核不过 → 降级 agent 走确认卡
                entry.pop("explicit_time", None)
                entry["_from"] = entry.pop("start")  # LLM 时刻猜测降级为引擎起点偏好
                # 用户明说钟点但 title 复核未过（同义词改写「与/和」、补全扩写）：
                # 钟点仍是原话意志——finder 侧撞 busy 转 conflict 问询、其余闸按
                # 用户直落同口径豁免（绝不静默挪槽，真机「明天下午三点开个会」
                # 曾被挪到后天早上 8 点）
                entry["_from_user"] = True
            if origin == "agent":
                # agent 草案时间统一交引擎：新建条目（task_id None）撞未来 7 天
                # 相似 → similar 问询——周一已排体检、agent 周二再排体检明显不
                # 合理，转问询让用户定夺。改既有任务排期（task_id 非空）不触发：
                # confirmed 后 apply_plan_entries 对 task_id 草案卡由用户手动
                # 确认（语义雷区）。问询条目不占 busy（用户没确认）；旧版
                # "重叠/午休丢弃"门退役——引擎校验层等价替代（2026-10-20）
                hit = (_find_similar(entry["title"], similar_pool)
                       if entry.get("task_id") is None else None)
                if hit and _RESCHED_VERB_RE.search(state.get("user_input") or ""):
                    # 改期语义兜底（2026-11-01 真机批）：「把X改到Y」LLM 常产
                    # 新条目漏关联 task_id，similar 命中被误问「已安排还要再
                    # 排吗」——改期动词在场即查库关联既有任务转改期草案
                    # （引擎按 not_before 找槽，前端 task_id 改期路径）；
                    # 系列不出卡（this/following 语义雷区，displace 先例）
                    from database import SessionLocal as _SL
                    from models import Task as _Task

                    with _SL() as db:
                        row = db.query(_Task).filter(
                            _Task.title == hit[0],
                            _Task.is_deleted.is_(False),
                            _Task.recurrence_rule.is_(None),
                        ).first()
                    if row is not None:
                        entry["task_id"] = row.id
                        # date_hint → not_before（真机三轮 #7）：明给时间随改期
                        # 关联进引擎起点，防全自主挑槽漂移 4 天。hint 取自原始
                        # 输出 d——_clean_draft_entry 白名单不含 date_hint（清洗
                        # 后 entry 恒无此键，首版曾在此断链）
                        nb = _hint_to_not_before(str(d.get("date_hint") or ""), now)
                        if nb is not None:
                            entry["not_before"] = nb.isoformat()
                        hit = None
                if hit:
                    raise_question("similar", [entry], similar_to=hit[0])
                else:
                    if entry.get("start"):
                        entry["_from"] = entry.pop("start")
                    agent_entries.append(entry)
            else:
                route_user_entry(entry)

    logger.info("[scheduler] profiler：直落 %d / 冲突入队 %d / agent 草案 %d / "
                "丢弃 %d / 问询 %s / displace 改期 %d", len(applied),
                len(conflict_queue), len(agent_entries), len(dropped),
                question["kind"] if question else "无", len(displace_drafts))
    return {"task_profile": data, "busy": busy, "applied": applied,
            "question": question, "resolved": resolved,
            "conflict_queue": conflict_queue, "agent_entries": agent_entries,
            "dropped": dropped, "insist_clash": insist_clash,
            "prefer_clash": prefer_clash,
            "llm_reply": llm_reply,
            "displace_drafts": displace_drafts, "displace_notes": displace_notes,
            "displace_done": displace_done}


def _bulk_shortcut(state: SchedulerState, ctx: dict, now: datetime,
                   pending: dict | None = None) -> dict:
    """批量排期短路体：全量待排候选直交统一混合排期引擎（plan_slots 三层：
    规则找槽 + SLOT_REFINE 批量微调 + 后验回落——时间决策单点收口红线，
    docs/ai-agents.md §2.3b），产 bulk 草案终态（条件边 final_reply 即 END）。

    条目全部带 task_id（改既有任务排期而非新建）——前端弹窗确认走
    applyDraftItems 改期路径，零新落库通路；无槽条目不进草案，回复附注手动
    处理。bulk 标记随出口透传，壳改发 bulk 事件驱动前端弹窗（对话框零草案卡，
    2026-10-25 功能边界）。strategist 系 pending 在场即结算 ignored——换意图
    语义，行挂着会继续抢锚点（下轮"可以"误消费旧 clarify）。"""
    from agents.engine import plan_slots  # 函数内 import 防环（engine 依赖 services）
    from database import SessionLocal
    from services import ai_data

    if pending is not None:
        with SessionLocal() as db:
            ai_data.record_proposal_outcome(db, pending["proposal_id"], "ignored")

    entries = [{"key": c["id"], "title": c["title"],
                "priority": c.get("priority") or 2,
                "duration": (c.get("duration") or 0) or 60, "from": now}
               for c in ctx["candidates"]]
    finals = plan_slots(
        entries=entries, busy=list(ctx["busy"]), now=now,
        day_start_min=ctx["day_start_min"], day_end_min=ctx["day_end_min"],
        lunch=ctx["lunch"], weather=ctx.get("weather"),
        rules=state.get("memory_rules") or [], day_budget=ctx.get("day_budget"))
    draft_plan: list[dict] = []
    missed: list[str] = []
    for c, e in zip(ctx["candidates"], entries):
        slot = finals.get(e["key"])
        if slot is None:
            missed.append(c["title"])
            continue
        draft_plan.append({"task_id": c["id"], "title": c["title"],
                           "start": slot.isoformat(), "duration": e["duration"]})
    if draft_plan:
        reply = f"已为 {len(draft_plan)} 条待办生成排期建议，请在弹窗中核对确认。"
        if missed:
            shown = "、".join(f"「{t[:20]}」" for t in missed[:3])
            more = "等" if len(missed) > 3 else ""
            reply += f"（{shown}{more}共 {len(missed)} 条近一周没有空档，请手动处理）"
    else:
        reply = f"{len(missed)} 条待办近一周都没有可用空档——告诉我放宽时段，或到日历手动安排。"
    logger.info("[scheduler] profiler：批量排期短路 %d 条草案（无槽 %d）",
                len(draft_plan), len(missed))
    return {"task_profile": {}, "draft_plan": draft_plan,
            "final_reply": reply, "bulk": True}


def _blocked_tasks(entries: list[dict], ctx: dict) -> list[dict]:
    """displace 挡路者识别：新条目区间在 busy_meta（intake 的区间→任务映射）
    里撞到的既有任务，按任务 id 去重（两条快照撞同一个只挪一次）。"""
    blocked: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        raw = e.get("start")
        if not (isinstance(raw, str) and raw):
            continue
        try:
            s0 = datetime.fromisoformat(raw)
        except ValueError:
            continue
        dur = e.get("duration")
        dur = dur if isinstance(dur, int) and dur > 0 else 60
        s1 = s0 + timedelta(minutes=dur)
        for m in ctx.get("busy_meta") or []:
            if m.get("id") not in seen and _overlaps(s0, s1, m["start"], m["end"]):
                seen.add(m["id"])
                blocked.append(m)
    return blocked


def _displace_shifts(blocked: list[dict], busy: list, ctx: dict,
                     start_from: datetime) -> tuple[list[dict], list[str]]:
    """挡路者挪移找槽（displace 专属，零 LLM）：非系列逐个 candidate_slots
    从新任务最晚结束顺延扫（网格步进天然同日优先），找到即占位（同批互斥）。

    系列挡路者不出卡——改期语义雷区（this/following 不可逆），附注指引手动
    挪；无槽同理。挪移不施加日预算闸：挡路者当日总量守恒（同日挪）或用户
    主动挤（跨日），预算对挪移是伪约束（口径登记 docs/tech-debt.md）。"""
    from services.tasks import candidate_slots

    drafts: list[dict] = []
    notes: list[str] = []
    busy_v = list(busy)
    for m in blocked:
        if m.get("recur"):
            notes.append(f"「{m['title']}」是重复日程，请在日历上手动挪动")
            continue
        dur = int((m["end"] - m["start"]).total_seconds() // 60) or 30
        cands = candidate_slots(
            start_from, dur, busy_v,
            day_start_min=ctx["day_start_min"], day_end_min=ctx["day_end_min"],
            lunch=ctx["lunch"], same_clock_first=False, k=1)
        if not cands:
            notes.append(f"「{m['title']}」近一周没找到空档，请手动改期")
            continue
        slot = cands[0]
        drafts.append({"task_id": m["id"], "title": m["title"],
                       "start": slot.isoformat(), "duration": dur})
        busy_v.append((slot, slot + timedelta(minutes=dur)))
    return drafts, notes
