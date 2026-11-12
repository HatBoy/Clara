"""chat 主图与采集编排（阶段二手写区，docs/ai-agents.md §六接缝契约）。

图结构（build_graph，七分支意图路由）：

                          ┌─(schedule)─→ scheduler（排期子图包装）──────────┐
                          ├─(qa)───────→ qa_answerer ────────────────────────┤
START → confirm_shortcut ─┼─(coach)────→ strategist（幕僚长子图包装）────────┼──→ END
  （草案/删除确认短路，    ├─(smalltalk)→ smalltalk_node ─────────────────────┤
   命中直达 END 零 LLM）   ├─(delete)───→ delete_confirmer ────────────────────┤
                          ├─(settings)─→ settings_handler ────────────────────┤
                          └─(ingest)───→ collect_data → refine ──────────────┘

节点执行体在 agents/nodes.py（qa/smalltalk/delete/settings 四分支；schedule 与 coach
分别包装 agents/scheduler/ 与 agents/strategist/ 两条子图，包装节点在本模块）；
collect_data / refine 留在本模块——它们与 run_ingest 同体共用 _map_phase /
_reduce_phase 管道。

双入口同体——共用同一条 _map_phase / _reduce_phase 管道，不复制：
- run_ingest(db, day)：定时/手动采集工作流入口（services/workflows.py 经
  asyncio.to_thread 调用），返回 {"draft_plan", "inbox_items", "evidence",
  "final_reply"} 四键，由壳 _run_ingest 消费（建议卡 / 自动收录 / ingest 广播）。
- build_graph()：chat 意图路由图。center_router 先分类（ingest 意图才走采集
  分支——不再每轮 chat 无条件采集，非 ingest 意图更快更省）；collect_data
  确定性采集先行——直调 service（必调的取数不经 @tool、不经 LLM 仲裁），
  refine 跑同一管道，四键出口由壳 _finalize_turn 消费（零壳改动）。

编排纪律：确定性编排直调 service（可测、不依赖 LLM 工具循环）；agents/tools.py
的 @tool 壳留作补采 ReAct 与工具循环练习。节点内 LLM 只经 services.llm.call_llm；
services 一律函数内 import——调用时点才解析模块属性，测试 monkeypatch 源模块
即可拦截替身。

——接缝契约（docs/ai-agents.md §六）——
build_graph()：返回编译后的图；壳以 graph().invoke(initial_state) 驱动（零 config，
故默认不挂 checkpointer，见 build_graph 注释）。initial_state 由壳构造：
{"user_input": str, "memory_rules": list[str]}（memory_rules 已按 user_manual 优先 +
confidence 降序排好，预算截断直接头部切片）。
run_ingest(db, day) / run_preflight(db, day)：工作流独立入口，签名冻结。
"""

import json
import logging
import re
from datetime import date, datetime, timedelta

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy.orm import Session

from agents import nodes
from agents.parsing import is_int as _is_int, loads_obj as _loads_obj
from agents.prompts import MERGE_PROMPT, SOURCE_DIGEST_PROMPT
from agents.state import AgentState
from database import SessionLocal

logger = logging.getLogger(__name__)

# Map 上限：源数（防首采爆炸，超出并入综述）与每源候选数（解析层截断）
_MAX_SOURCES = 8
_MAP_CONCURRENCY = 3          # Map 并发提炼上限（2026-10-26 提速批 3；provider 限速保护）
_MAX_CANDIDATES_PER_SOURCE = 10
# Reduce 输入候选总数截断（模板 §五：candidates[:80]）
_MAX_REDUCE_CANDIDATES = 80
# Map/Reduce 输出 token 上限（模板 §五 定稿：逐源摘要短、全局合并长）
_MAP_MAX_TOKENS = 600
_REDUCE_MAX_TOKENS = 1200
# evidence 条目键（idx + 六溯源键；溯源键值由编排侧附加，LLM 只照抄）
_EVIDENCE_KEYS = ("source", "source_key", "item_id", "title", "time", "fingerprint")

# 三层防御解析公共件（agents/parsing.py，与 nodes.py / reflection.py 共用防复制漂移；
# 本模块内沿用 _loads_obj / _is_int 内部名，见顶部 import as 别名）


# —— 纯函数：解析（红线清单第 2 条，宁可不学不可学错） ————————————————————————


def _parse_source(out: str) -> tuple[str, list[dict]]:
    """Map 输出解析：{"source_note", "candidates"} → (note, candidates)。
    整体解析失败 → ("（源摘要解析失败）", [])，不 raise（单源失败是数据）。
    candidate 宽进（素材层不做严格校验，出口校验在 _parse_ingest）：dict 且含
    非空 title；重建为恰好五键，item_id 非 str 一律置 None。"""
    data = _loads_obj(out)
    if data is None:
        return "（源摘要解析失败）", []
    note = data.get("source_note")
    if not isinstance(note, str) or not note.strip():
        note = "（源摘要缺失）"
    candidates = []
    for c in data.get("candidates") or []:
        if not (isinstance(c, dict) and isinstance(c.get("title"), str) and c["title"].strip()):
            continue
        item_id = c.get("item_id")
        when_hint = c.get("when_hint")
        evidence = c.get("evidence")
        confidence = c.get("confidence")
        candidates.append({
            "title": c["title"],
            "item_id": item_id if isinstance(item_id, str) and item_id else None,
            "when_hint": when_hint if isinstance(when_hint, str) else None,
            "evidence": evidence if isinstance(evidence, str) else None,
            "confidence": confidence if isinstance(confidence, (int, float)) else None,
        })
    return note, candidates


def _parse_ingest(merged: str) -> tuple[list[dict], list[dict], list[dict], str]:
    """Reduce 输出解析 → (draft_plan, inbox_items, evidence, reply)。
    整体解析失败 → ([], [], [], 原始文本)——垃圾输出零卡零落库，原文交壳兜底。
    逐条校验：非法条目丢弃不弃卡；draft 先过滤，evidence.idx 以过滤后的下标域
    校验（LLM 的 idx 基于它自己输出的草案序，错位对齐只能靠丢弃终结）。"""
    data = _loads_obj(merged)
    if data is None:
        return [], [], [], merged or ""

    draft_plan = []
    for d in data.get("draft_plan") or []:
        if not (isinstance(d, dict) and isinstance(d.get("title"), str)):
            continue
        title = d["title"].strip()
        start = d.get("start")
        duration = d.get("duration")
        if not title or len(title) > 30:
            continue
        if not isinstance(start, str):
            continue
        try:  # naive 本地时间契约：带时区后缀（含 Z）即丢——一查双防
            if datetime.fromisoformat(start).tzinfo is not None:
                continue
        except ValueError:
            continue
        if not _is_int(duration) or duration <= 0:
            continue
        draft_plan.append({
            "task_id": None,  # 编排语境无既有任务可挂，非 None 即幻觉
            "title": title,
            "start": start,
            "duration": duration,
        })

    inbox_items = []
    for it in data.get("inbox_items") or []:
        if not (isinstance(it, dict) and isinstance(it.get("title"), str)):
            continue
        title = it["title"].strip()
        if not title or len(title) > 30:
            continue
        duration = it.get("duration")
        notes = it.get("notes")
        source_ref = it.get("source_ref")
        inbox_items.append({
            "title": title,
            # duration None 合法（估不出）；非法值降 None 不弃条（可选项宽宥）
            "duration": duration if _is_int(duration) and 1 <= duration <= 1440 else None,
            "notes": notes.strip()[:200] if isinstance(notes, str) else None,
            "source_ref": source_ref if isinstance(source_ref, dict) else None,
        })

    evidence = []
    for e in data.get("evidence") or []:
        if not isinstance(e, dict):
            continue
        idx = e.get("idx")
        if not _is_int(idx) or not 0 <= idx < len(draft_plan):
            continue
        if any(k not in e for k in _EVIDENCE_KEYS):
            continue  # 六溯源键齐全，缺指纹等整条丢——台账对不上
        if not isinstance(e.get("fingerprint"), str) or not e["fingerprint"]:
            continue
        entry = {"idx": idx}
        entry.update({k: e.get(k) for k in _EVIDENCE_KEYS})
        evidence.append(entry)

    reply = data.get("summary")
    reply = reply if isinstance(reply, str) else ""
    return draft_plan, inbox_items, evidence, reply


# —— 编排：Map 逐源摘要 / Reduce 全局合并（双入口共用） ——————————————————————


def _map_phase(db: Session, raw: dict) -> tuple[list[dict], list[str]]:
    """Map 逐源摘要（模板 §五）：单源失败隔离零候选；源数上限防首采爆炸。
    诚实注记先行——采集层 catastrophic 信封 error / 预算截断 / 源数超限，如实
    进综述不静默。溯源字段编排侧附加：LLM 只认 item_id，source/source_key/
    item_title/time/fingerprint 一律从该源 items 按 id 照抄，键值不生成。

    并发提炼（2026-10-26 提速批 3）：≤8 源 LLM 曾串行 for 循环（取数层早 gather
    了、提炼层没有）——改 ThreadPoolExecutor 限 3 并发（provider 限速保护），
    结果按源序归位零漂移。预算先在主线程原子预扣（并发下逐源 claim 有竞态
    窗口），失败源逐个返还（refund_llm_budget），预算按实际消耗计。线程体
    纯 LLM + 解析零库操作（Session 不跨线程铁律）。"""
    from concurrent.futures import ThreadPoolExecutor

    from services import data_ingest
    from services.llm import call_llm_aux

    notes: list[str] = []
    if raw.get("error"):
        notes.append(f"采集层异常：{raw['error']}")
    if raw.get("truncated"):
        notes.append("部分源数据被预算截断")
    sources = raw.get("sources") or []
    if len(sources) > _MAX_SOURCES:
        notes.append(f"另有 {len(sources) - _MAX_SOURCES} 个源超出本轮上限未提炼")

    # 结构化分流（2026-11-05 采集零 LLM 批，用户拍板）：todo/calendar 域条目
    # （滴答/飞书日历待办/钉钉待办等——本来就是日程/待办）零 LLM 直映射候选，
    # 时间照抄源数据（真机铁证：滴答「时间待定」待办被 Reduce LLM 编造成次日
    # 09:30 日历事件直通落库）；doc/未标域条目（本地文件夹/Obsidian/Notion/
    # 飞书文档）才进 SOURCE_DIGEST 提炼。按条目拆（lark 一源两域自然分流），
    # doc 条目非空才构造 LLM 源（浅拷贝换 items）
    active: list[dict] = []
    structured_cands: list[dict] = []
    source_notes: list[str] = []
    for src in sources[:_MAX_SOURCES]:
        if not (src.get("ok") and src.get("items")):
            source_notes.append(f"{src['source']}: {src.get('error') or '无新条目'}")
            continue
        struct_items, doc_items = data_ingest.split_structured_items(src)
        for it in struct_items:
            structured_cands.append({
                "title": str(it.get("title") or "")[:30] or "未命名条目",
                "item_id": it.get("id"), "when_hint": None,
                "evidence": None, "confidence": None,
                "source": src["source"], "source_key": src["key"],
                "item_title": it.get("title"), "time": it.get("time"),
                "fingerprint": it.get("fingerprint"), "structured": True,
                "raw_priority": it.get("priority"),  # 源原始优先级（装配层映射）
            })
        if struct_items:
            logger.info("[ingest] 直映射 源=%s：结构化条目 %d 条（零 LLM）",
                        src["source"], len(struct_items))
            source_notes.append(
                f"{src['source']}: 结构化直映射 {len(struct_items)} 条（零 LLM）")
        if doc_items:
            active.append({**src, "items": doc_items})
    granted = 0
    while granted < len(active) and data_ingest.claim_llm_budget(db):
        granted += 1
    todo = active[:granted]

    # seen_count 消费（2026-11-01 优化批 5）：反复出现（≥3 次）且从未进过建议卡
    # 的素材标注低优先——台账数据从只写不读变闭环（采集降噪最后一环）。一次
    # 批量查询覆盖本轮全部指纹，带键条目进 Map 输入（提示词层降优先）
    from models import IngestionItem
    from sqlalchemy import select as _sa_select

    _fps = {it.get("fingerprint") for src in todo for it in src["items"]
            if it.get("fingerprint")}
    _seen: dict[str, bool] = {}
    if _fps:
        for fp, seen, proposed in db.execute(
            _sa_select(IngestionItem.fingerprint, IngestionItem.seen_count,
                       IngestionItem.proposed_at).where(
                IngestionItem.fingerprint.in_(_fps))):
            _seen[fp] = (seen or 0) >= 3 and proposed is None
    for src in todo:
        for it in src["items"]:
            if _seen.get(it.get("fingerprint")):
                it["seen_count"] = True  # 布尔标记（具体次数对提炼无增量信息）

    def _digest_one(src: dict) -> tuple[str, list[dict], bool]:
        """单源 LLM 摘要（线程体）：返回 (源注记, 候选, 是否成功)。"""
        try:
            out = call_llm_aux(
                [{"role": "system", "content": SOURCE_DIGEST_PROMPT},
                 {"role": "user", "content":
                     f"<<<DATA\n{json.dumps(src, ensure_ascii=False)}\nDATA>>>"}],
                json_mode=True, max_tokens=_MAP_MAX_TOKENS)
            note, cands = _parse_source(out)  # 三层防御取 (note, candidates)
            return f"{src['source']}: {note}", cands, True
        except Exception:
            return f"{src['source']}: 提炼调用失败，已跳过", [], False

    results = ([_digest_one(todo[0])] if len(todo) == 1
               else list(ThreadPoolExecutor(max_workers=_MAP_CONCURRENCY)
                         .map(_digest_one, todo)))  # map 保源序

    candidates: list[dict] = []
    for (note, cands, ok), src in zip(results, todo):
        source_notes.append(note)
        if not ok:
            data_ingest.refund_llm_budget()  # 失败源额度还回（按实际消耗计）
            logger.info("[ingest] 提炼 源=%s：调用失败跳过", src["source"])
            continue
        by_id = {it["id"]: it for it in src["items"]}
        kept: list[str] = []
        for c in cands[:_MAX_CANDIDATES_PER_SOURCE]:  # 每源候选 ≤10（解析层截断）
            meta = by_id.get(c.get("item_id"))
            c.update(source=src["source"], source_key=src["key"],
                     item_title=meta["title"] if meta else None,
                     time=meta["time"] if meta else None,
                     fingerprint=meta["fingerprint"] if meta else None)
            candidates.append(c)
            kept.append(str(c.get("title") or c.get("item_title") or "?")[:20])
        # 逐源提炼明细（2026-11-04 用户需求）：源名 + 候选标题清单——采集了
        # 什么一眼可追，下游去向（建卡/直通/丢弃）由各环节日志接续
        logger.info("[ingest] 提炼 源=%s：候选 %d 条%s", src["source"], len(kept),
                    f"（{'、'.join(kept)}）" if kept else "")
    for src in active[granted:]:
        source_notes.append(f"{src['source']}: 当日预算用尽未提炼")
    candidates.extend(structured_cands)
    return candidates, notes + source_notes


def _assemble_structured(cands: list[dict], existing_titles: list[str],
                         db=None) -> tuple[list[dict], list[dict], list[dict], str]:
    """结构化候选确定性装配（2026-11-05 采集零 LLM 批）→ (draft, inbox, evidence,
    note)。todo/calendar 域条目不经 LLM：时间照抄源数据（有可解析开始时间 → 日历
    草案，这是同步不是排期；无时间/解析失败 → 收件箱待办——「时间待定」绝不编
    钟点）；查重用归一化标题全等对照 existing_titles（同实异名双订的零 LLM
    防线，口径轻于 LLM 语义去重——结构化源标题通常逐字一致）。evidence 六键照抄
    溯源（结构化候选指纹来自台账自身，白名单后滤天然通过）。

    2026-11-05 真机批增：①draft 打 `_sync` 标记——_postvalidate_drafts 跳过
    （后验是 LLM 自排时间的兜底，源数据时间权威：偏好避让窗/作息是 Clara 排期
    建议的软偏好，不拒同步——真机用户快照的避让窗把全部同步日程丢弃即此坑）；
    ②指纹溯源防重复：条目指纹命中既有 Task.source_fingerprint → 跳过（曾以
    其他形态落库的同一源条目，重复同步不做——时间升级自动化登记 tech-debt）；
    ③优先级映射：源 raw 值（滴答 0/1/3/5）→ 1高/2中/3低，无优先级默认低
    （用户定案）。"""
    from agents.nodes import _normalize_title
    from services.data_ingest import mapped_priority

    seen = {_normalize_title(t) for t in existing_titles if isinstance(t, str)}
    known_fps: set[str] = set()
    if db is not None:
        from sqlalchemy import select as _sa_select

        from models import Task

        fps = {c.get("fingerprint") for c in cands if c.get("fingerprint")}
        if fps:
            known_fps = set(db.scalars(
                _sa_select(Task.source_fingerprint).where(
                    Task.source_fingerprint.in_(fps),
                    Task.is_deleted.is_(False))))
    drafts: list[dict] = []
    inbox: list[dict] = []
    evidence: list[dict] = []
    dropped: list[str] = []
    for c in cands:
        title = str(c.get("title") or "").strip()[:30]
        fp = c.get("fingerprint")
        if fp in known_fps:
            logger.info("[ingest] 直映射跳过「%s」（指纹已有落库任务，防重复同步）",
                        title[:20])
            continue
        if not title or _normalize_title(title) in seen:
            if title:
                dropped.append(title)
                logger.info("[ingest] 直映射查重丢弃「%s」（与既有任务同名）", title[:20])
            continue
        seen.add(_normalize_title(title))
        start = None
        raw = c.get("time")
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                # 双保险转本地 naive（_iso 出口已统一，防 time 从别路进来带时区）
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                # 有可解析开始时间即进日历（同步语义），过去未来都照抄——
                # 窗口钳制（≤168h）保证过去的最多一周内
                start = dt.isoformat(timespec="seconds")
            except ValueError:
                start = None
        prio = mapped_priority(c.get("raw_priority")) or 3  # 无优先级默认低（用户定案）
        if start:
            drafts.append({"task_id": None, "title": title,
                           "start": start, "duration": 60,  # 源无时长字段，缺省 1h
                           "priority": prio, "_sync": True})
            evidence.append({"idx": len(drafts) - 1, "source": c.get("source"),
                             "source_key": c.get("source_key"),
                             "item_id": c.get("item_id"),
                             "title": c.get("item_title"), "time": c.get("time"),
                             "fingerprint": c.get("fingerprint")})
        else:
            # source_ref 六键补全（2026-11-05 真机显示批）：数据源页组名与来源
            # 徽标读 source_ref.source（LLM 产物由 MERGE 提示词约定六键，直映射
            # 首版只给 source_key/fingerprint 两键 → 组头与徽标空白）
            inbox.append({"title": title, "duration": None, "notes": None,
                          "priority": prio,
                          "source_ref": {
                              "source": c.get("source"),
                              "source_key": c.get("source_key"),
                              "item_id": c.get("item_id"),
                              "title": c.get("item_title"),
                              "time": c.get("time"),
                              "fingerprint": c.get("fingerprint")}})
    parts = []
    if inbox:
        parts.append(f"{len(inbox)} 条待办入收件箱")
    if drafts:
        parts.append(f"{len(drafts)} 条带时间日程入日历")
    note = (f"结构化源直映射：{'、'.join(parts)}（零 LLM）" if parts
            else "结构化源无新条目")
    if dropped:
        note += f"；{len(dropped)} 条与既有任务同名跳过（{'、'.join(dropped[:3])}）"
    return drafts, inbox, evidence, note


def _reduce_phase(candidates: list[dict], existing_titles: list[str],
                  memory_rules: list[str], source_notes: list[str], day: date, *,
                  load_summary: list[dict] | None = None,
                  settings: dict | None = None,
                  weather: list[dict] | None = None,
                  chat_summary: str | None = None,
                  db=None) -> dict:
    """Reduce 全局合并（模板 §五）：语义去重 + 置信度分流 → 四键。
    2026-11-05 采集零 LLM 批：结构化候选（structured 标记，_map_phase 直映射）
    走 _assemble_structured 确定性装配不进 LLM；LLM 只喂 doc 域候选——全结构化
    轮（无 doc 候选）跳过 Reduce 保底调用，综述确定性拼装（毫秒级零成本）。
    call_llm 裸奔不 try（异常上抛：chat 壳译 agent_error 帧、workflows 统一
    logger 兜底）、不扣预算（Reduce 保底一次）。解析后按候选白名单后滤溯源：
    evidence / inbox.source_ref 的 (source_key, fingerprint) 不在候选集内的
    视为 LLM 编造——无来源条目整条丢弃（2026-10-26 修订，曾置 None 条目保留：
    溯源被证伪 = 条目本身可疑，且产品规则要求数据源页每条必有明确来源）。
    2026-10-17 增 load_summary / settings 两注入键（秘书视野——晨间批量与
    chat 提炼同体共用，缺省空值不影响旧调用形态）；2026-10-18 增 weather
    注入键（未配置城市恒 None——批量排时对照天气，同 planner 口径）；
    2026-11-01 优化批 4 增 chat_summary（滚动摘要 ≤500 字有界——采集提炼
    知道用户最近在忙什么，「最近在准备 X」的相关素材自然优先，零新调用）。"""
    from services.llm import call_llm

    structured = [c for c in candidates if c.get("structured")]
    llm_cands = [c for c in candidates if not c.get("structured")]
    if llm_cands or not structured:
        # doc 候选在 → LLM 合并；全零候选（Map 全失败等）也保底跑一次——旧
        # 语义：零候选 Reduce 仍出综述兜底，不因零 LLM 化收窄
        merged = call_llm(
            [{"role": "system", "content": MERGE_PROMPT},
             {"role": "user", "content": json.dumps(
                 {"candidates": llm_cands[:_MAX_REDUCE_CANDIDATES],
                  "existing_titles": existing_titles,
                  "memory_rules": memory_rules,
                  "source_notes": source_notes,
                  "load_summary": load_summary or [],
                  "settings": settings or {},
                  "weather": weather,
                  "today": day.isoformat(),
                  "chat_summary": chat_summary}, ensure_ascii=False)}],
            json_mode=True, max_tokens=_REDUCE_MAX_TOKENS)
        draft_plan, inbox_items, evidence, reply = _parse_ingest(merged)
    else:
        # 全结构化轮（零 doc 候选）：跳过 Reduce LLM，确定性装配
        draft_plan, inbox_items, evidence, reply = [], [], [], ""
    known = {(c.get("source_key"), c.get("fingerprint"))
             for c in llm_cands if c.get("fingerprint")}
    evidence = [e for e in evidence
                if (e.get("source_key"), e.get("fingerprint")) in known]
    # 无来源条目丢弃（口径见 docstring）：inbox 看 source_ref 白名单命中；
    # draft_plan 看 evidence per idx 覆盖（无覆盖 = 无来源），丢弃后 idx 重排
    # 对齐（同 run_ingest 撞日历后验的重排惯例，evidence 溯源契约不破）。
    # 丢弃逐条日志（2026-11-04 用户需求）——LLM 编造溯源/无来源条目去了哪可追
    for it in inbox_items:
        ref = it.get("source_ref") if isinstance(it.get("source_ref"), dict) else {}
        if (ref.get("source_key"), ref.get("fingerprint")) not in known:
            logger.info("[ingest] 丢弃（溯源不在候选集，疑编造）：待办「%s」",
                        str(it.get("title") or "?")[:20])
    inbox_items = [it for it in inbox_items
                   if isinstance(it.get("source_ref"), dict)
                   and (it["source_ref"].get("source_key"),
                        it["source_ref"].get("fingerprint")) in known]
    keep = [i for i in range(len(draft_plan)) if any(e["idx"] == i for e in evidence)]
    for i, d in enumerate(draft_plan):
        if i not in keep:
            logger.info("[ingest] 丢弃（无溯源证据行）：草案「%s」",
                        str(d.get("title") or "?")[:20])
    remap = {old: new for new, old in enumerate(keep)}
    draft_plan = [draft_plan[i] for i in keep]
    evidence = [e for e in evidence if e["idx"] in remap]  # 越界 idx（无对应草案）同步丢
    for e in evidence:
        e["idx"] = remap[e["idx"]]
    # 溯源素材回填（2026-10-23 静态溯源地基）：inbox 条目按指纹对上候选时把候选的
    # evidence 摘录写进条目——采纳落库（自动收录/手动卡/快录直落）随 TaskCreate
    # 透传三列，幕僚长教练咨询该任务时零成本回放素材
    by_fp = {c["fingerprint"]: c for c in llm_cands if c.get("fingerprint")}
    for it in inbox_items:
        ref = it.get("source_ref")
        if isinstance(ref, dict) and not it.get("evidence"):
            cand = by_fp.get(ref.get("fingerprint"))
            if cand and cand.get("evidence"):
                it["evidence"] = cand["evidence"]
    # 结构化装配产物并入（evidence idx 偏移到 LLM 草案之后；时间照抄源数据、
    # 白名单天然通过——不进上方后滤段）
    if structured:
        s_draft, s_inbox, s_ev, note = _assemble_structured(
            structured, existing_titles, db)
        offset = len(draft_plan)
        for e in s_ev:
            e["idx"] += offset
        draft_plan = draft_plan + s_draft
        inbox_items = inbox_items + s_inbox
        evidence = evidence + s_ev
        reply = f"{reply}\n{note}".strip() if reply else note
    if structured and not llm_cands:
        # 全结构化轮跳过了 Reduce LLM——失败/异常注记（lark 认证过期等）旧版由
        # LLM 综述进 reply，现在确定性直列尾部（诚实注记不静默，零 LLM 不零信息）
        problems = [n for n in source_notes
                    if not n.endswith("无新条目") and "直映射" not in n]
        if problems:
            reply = (f"{reply}\n请注意：{'；'.join(problems[:3])}").strip()
    return {"draft_plan": draft_plan, "inbox_items": inbox_items,
            "evidence": evidence, "final_reply": reply}


def _postvalidate_drafts(result: dict, *, busy: list[tuple[datetime, datetime]],
                         day_start_min: int, day_end_min: int,
                         lunch: tuple[int, int] | None,
                         avoid_windows: list[dict] | None = None) -> None:
    """采集草案后验（两入口同体，2026-11-01 优化批 1）：run_ingest 原有的
    busy 撞期后验升级为全闸后验，chat refine 补齐同款——MERGE 让 LLM 自排
    start，合法性在此兜底：撞既有日历 / 落在可排时段之外 / 进午休，任一命中
    即丢弃该条草案（services.tasks._gates_ok 五道闸共享实现，不带 now 检查
    ——采集草案的日期语义由 Reduce 提示词约束，后验只管作息与撞车）。evidence
    idx 重排对齐、reply 附注；同批草案互撞不在后验范围（Reduce 语义去重的
    职责，既有口径不扩）。原地修改 result 三个键。
    avoid_windows（2026-11-03 记忆重构批 4）：偏好避让窗透传——旧版后验不查
    偏好窗，采集草案可落进用户「周五下午别排事」禁区；调用方从注入 rules 解析
    （None = 不查，直调用例兼容）。"""
    from services.tasks import _gates_ok  # 单点闸共享（第 4 个消费方）

    kept: list[dict] = []
    remap: dict[int, int] = {}
    dropped: list[str] = []
    for i, d in enumerate(result.get("draft_plan") or []):
        if d.get("_sync"):
            # 同步草案跳过（2026-11-05 真机批）：时间来自源数据是权威值——
            # 偏好避让窗/作息/撞期是「Clara 排期建议」的约束，不拒同步（真机
            # 用户快照的避让窗曾把全部同步日程丢弃，前端永远看不到）
            remap[i] = len(kept)
            kept.append(d)
            continue
        start = datetime.fromisoformat(d["start"])
        if any(nodes._overlaps(start, start + timedelta(minutes=d["duration"]),
                               b0, b1) for b0, b1 in busy):
            dropped.append(d["title"])
            continue
        if not _gates_ok(start, d["duration"], busy, day_start_min=day_start_min,
                         cap_min=day_end_min, lunch=lunch,
                         day_load=None, day_budget=None,
                         avoid_windows=avoid_windows):
            dropped.append(d["title"])
            continue
        remap[i] = len(kept)
        kept.append(d)
    if dropped:
        result["draft_plan"] = kept
        result["evidence"] = [{**e, "idx": remap[e["idx"]]} for e in result.get("evidence") or []
                              if e.get("idx") in remap]
        note = (f"{len(dropped)} 条草案与既有日程冲突或违反作息约束已丢弃：{'、'.join(dropped[:5])}")
        result["final_reply"] = (result.get("final_reply") or "").rstrip() + f"\n（{note}）"


# —— 入口 A：工作流独立入口（签名冻结，docs/ai-agents.md §6.3） ——————————————


def run_ingest(db: Session, day: date) -> dict:
    """定时/手动采集提炼（services/workflows.py 消费）：确定性采集先行 → Map
    逐源 → Reduce 合并 → 四键返回。采集层已信封化（失败是数据不是异常）；
    Reduce 的 LLM 调用异常按模板上抛，由壳兜底。2026-10-17 增两处：Reduce 注入
    load_summary + settings（秘书视野——批量推荐看得见周围日程密度与作息）；
    产出 draft 过真实日历冲突后验（_parse_ingest 只查形状，撞已排期的草案
    丢弃 + reply 附注 + evidence idx 重排对齐——批量推荐不再给撞车时间）。
    2026-10-18 增 weather 注入（settings.weather_city 未来 7 天预报，失败
    恒 None 不阻塞——两入口输入形状零漂移）。2026-10-23 增静默轮（silent 键
    additive）：零候选零异常时跳过 Reduce 保底调用，由壳静默收场——提频后
    多数轮次无事发生，不该每轮都耗一次 LLM 并刷一条综述消息。"""
    from services import data_ingest
    from services.ai_data import dedup_context, list_memory_rules
    from services.settings import get_settings
    from services.tasks import busy_intervals, calendar_load_summary
    from services.weather import forecast_for_city

    raw = data_ingest.fetch_recent(db, hours=72)  # ① 原始数据（直调 service，不经 @tool）
    # 无依赖段与 Map 并行（2026-11-01 优化批 3）：dedup/rules/settings/load 均不
    # 依赖 raw 与 Map 产物——Map 是最长段（逐源 LLM 并发 3），纯库取数挪进同
    # 窗口白拿；各开 SessionLocal（Session 线程隔离铁律）。天气仍留静默轮之后
    # （SWR 缓存命中不阻塞，静默轮不白付网络——首拉无缓存才同步等待的既有语义）
    from concurrent.futures import ThreadPoolExecutor

    def _prep_aux():
        # SessionLocal 函数内 import（非模块级）：conftest 替换的是 database 模块
        # 属性，调用时解析才吃到测试库（run_ingest 测试不 patch graph.SessionLocal）
        from database import SessionLocal as _SL

        from services.ai_data import format_memory_lines, get_chat_state

        with _SL() as db2:
            _core, _ref = format_memory_lines(db2)  # 无人格（tech-debt 登记）
            return (dedup_context(db2),
                    _core + _ref,
                    get_settings(db2), calendar_load_summary(db2),
                    get_chat_state(db2).summary or None)

    with ThreadPoolExecutor(max_workers=1) as _pool:
        _aux = _pool.submit(_prep_aux)
        candidates, source_notes = _map_phase(db, raw)  # ② Map 逐源提炼（主线程预算预扣）
        existing, rules, s, _load, _chat_sum = _aux.result()
    # 静默轮判定（2026-10-23 频率档位配套）：零候选 + 采集层无异常 + 各源全部 ok +
    # 注记全是「无新条目」尾串（Map 对 ok 且零 items 的源写死该文案，见 _map_phase）
    # → 跳过 Reduce 保底调用直接返回；预算用尽/提炼失败/认证类/源数超限注记都算
    # 「有事」照走 Reduce 出综述。壳（workflows._run_ingest）按 silent 键静默收场。
    if (not candidates and not raw.get("error") and not raw.get("truncated")
            and all(src.get("ok") for src in (raw.get("sources") or []))
            and all(n.endswith("无新条目") for n in source_notes)):
        return {"silent": True, "draft_plan": [], "inbox_items": [],
                "evidence": [], "final_reply": ""}
    result = _reduce_phase(candidates, existing, rules, source_notes, day,
                           load_summary=_load,
                           settings=nodes.settings_payload(s),
                           weather=forecast_for_city(s.weather_city),
                           chat_summary=_chat_sum, db=db)
    # 后验（2026-11-01 批 1 升级）：busy 撞期 → 全闸（busy/时段窗/午休），
    # 见 _postvalidate_drafts——批量推荐不给撞车时间也不违反作息；
    # avoid_windows（批 4）：rules 即注入同款文本，解析与硬闸单源共享缓存
    from services.preference_slots import parse_avoid_windows

    _postvalidate_drafts(result, busy=busy_intervals(db),
                         day_start_min=s.calendar_start_min,
                         day_end_min=s.calendar_end_min,
                         lunch=(s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None,
                         avoid_windows=parse_avoid_windows(rules))
    return result


# 生日/纪念日前瞻（2026-10-22 批 5 场景用例）：晨间扫描标题命中 → 零 LLM 模板
# 出 kind=inbox「准备礼物」待办卡（礼物后置倒推的场景化形态——不建 LLM 循环）
_BIRTHDAY_RE = re.compile(r"生日|纪念日")
_BIRTHDAY_LOOKAHEAD_DAYS = 7


def _birthday_inbox_items(db: Session, day: date) -> list[dict]:
    """未来 7 天已排期任务标题命中生日/纪念日 → 礼物准备待办条目（去重：近
    7 天已出的 kind=inbox 卡里有同名条目不再重出——晨跑连出 7 天卡会刷屏；
    proposals 拉回 Python 比对，量小）。纯规则零 LLM。"""
    from models import Proposal
    from services.tasks import list_tasks
    from sqlalchemy import select

    lo = datetime(day.year, day.month, day.day)
    hits = []
    for t in list_tasks(db, start=lo, end=lo + timedelta(days=_BIRTHDAY_LOOKAHEAD_DAYS + 1)):
        if not _BIRTHDAY_RE.search(t.title or "") or not t.start_time:
            continue
        hits.append({
            "title": f"为「{t.title[:22]}」准备礼物",
            "duration": 30,
            "notes": f"{t.start_time:%m-%d} {t.title}，提前选购与包装",
        })
    if not hits:
        return []
    recent = db.scalars(select(Proposal).where(
        Proposal.kind == "inbox",
        Proposal.created_at >= datetime.now() - timedelta(days=_BIRTHDAY_LOOKAHEAD_DAYS),
    )).all()
    seen = {it.get("title") for p in recent
            for it in (p.payload or {}).get("items") or [] if isinstance(it, dict)}
    return [h for h in hits if h["title"] not in seen]


def run_preflight(db: Session, day: date) -> dict:
    """晨间简报 / 前夜综述独立入口（services/workflows.py 消费，签名冻结）：
    strategist 子图 cron 直调（2026-10-21 Track B 批 6 收编——旧
    build_coach_brief 链退役，晨夜形态改由子图场景段承担）。

    保留：静好分支（day 无任务零 LLM 早退，坑 ⑦ final_reply 恒有意义）、
    人格前置 rules、is_eve 判定（day>今天 → nightly 前夜综述 / morning 晨间
    启航）。旧版代码层高优筛选退役——morning 场景段「挑 1 件高能耗硬骨头」
    方法论承担选择职责（秘书化方向：给全盘视野让模型判断）。
    LLM 调用数 1（子图大脑单次，与旧链持平）。

    新增：cron 轮 memory 问询承接——dispatcher 出 question_plan（kind=memory）
    时由本入口落 question 行（cron 消费端无 pending 通道，落库归此单点），
    ask 已由 dispatcher 拼进 final_reply；返回 question_proposal_id 键供消费端
    把 assistant 消息锚点挂问询行——不挂则消息锚点落在 preflight 行上，
    active_pending 找不到 kind=question 的锚点，问询次日失活（返回键增量，
    {"preflight", "final_reply"} 原两键不变）。
    新增：生日/纪念日前瞻（2026-10-22 批 5）——晨间形态零 LLM 扫描未来 7 天
    标题命中 → kind=inbox 礼物准备待办卡（近 7 天已出同名条目不重出）。"""
    from services.ai_data import create_proposal
    from services.tasks import list_tasks

    lo = datetime(day.year, day.month, day.day)  # day 0 点 → +1 天，一次窗口查询
    tasks = list(list_tasks(db, start=lo, end=lo + timedelta(days=1)))
    is_eve = day > date.today()
    # 生日/纪念日前瞻（批 5）：晨间形态才扫（夜综述聚焦明日复盘，礼物提醒
    # 放早上更贴习惯）；出卡不并简报卡——kind=inbox 独立收件箱清单卡，采纳后
    # 逐条入收件箱，消息锚点挂生日卡自身
    birthday_note = ""
    if not is_eve:
        # 低介入档跳过生日前瞻（优化批 5 信任闸）：近 14 天采纳率 <30% 时主动
        # 推送物降频——简报本体照出（被动应答保留），礼物卡这类加餐先撤
        from services.ai_data import trust_tier

        if trust_tier(db) == "low":
            logger.info("[preflight] 低介入档：生日前瞻卡跳过（近14天采纳率<30%）")
        else:
            gifts = _birthday_inbox_items(db, day)
            if gifts:
                gift_prop = create_proposal(db, kind="inbox", payload={"items": gifts})
                from services.ai_data import append_message

                append_message(db, role="assistant",
                               content=f"未来 {_BIRTHDAY_LOOKAHEAD_DAYS} 天内有生日/纪念日事项，已出「准备礼物」待办卡，记得提前安排～",
                               proposal_id=gift_prop.id)
                birthday_note = f"（另：未来 {len(gifts)} 项生日/纪念日，已出礼物准备待办卡）"
    if not tasks:
        quiet = f"{day.month}月{day.day}日没有已排期日程，节奏放松"
        return {"preflight": {"headline": f"{quiet}～", "checklist": []},
                "final_reply": f"{quiet}的一天，无需特别准备。{birthday_note}".rstrip()}

    # 双段注入单源（2026-11-03 批 3）：分级格式统一（旧版此处无置信分级）；
    # 人格前置保持（2026-10-20 晨/夜简报同受回复风格与管理哲学引导）
    from services.settings import get_settings

    s = get_settings(db)
    from agents.prompts import personality_rules
    from services.ai_data import format_memory_lines

    core, ref = format_memory_lines(
        db, personality=personality_rules(
            s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
    rules = core + ref
    from agents.strategist import strategist_app

    result = strategist_app.invoke({
        "user_input": "", "memory_rules": rules, "pending": None,
        "trigger": "nightly" if is_eve else "morning",
    })
    out = {"preflight": result.get("preflight") or {"headline": "简报生成失败",
                                                    "checklist": []},
           "final_reply": result.get("final_reply") or ""}
    if birthday_note:  # 生日卡附注并入简报回复（静好分支已拼，此处覆盖常态）
        out["final_reply"] += birthday_note
    question = result.get("question_plan")
    if isinstance(question, dict) and question.get("kind") == "memory":
        row = create_proposal(db, kind="question", payload={
            "kind": "memory", "entries": [],
            "rule": question.get("rule") or {},
            "ask_text": question.get("ask_text") or out["final_reply"]})
        out["question_proposal_id"] = row.id
    return out


def run_meeting_prep(db: Session, day: date) -> dict:
    """会前准备独立入口（services/workflows.py prep 流消费，签名冻结）：
    规则层识别 day 重要会议（job_rules.pick_important_meetings，纯规则零 LLM，
    单次+重复系列两路合流）→ 空清单零 LLM 直接返回 → build_prep_brief 单次
    调用产各会议准备清单 → 条目回填 title/start 展示字段（LLM 只回 task_id/
    headline/checklist，白名单后滤防幻觉）→
    {"meetings": [{task_id, title, start, headline, checklist}], "final_reply"}。
    解析失败/全部免备 → meetings 空数组（调用方静默零卡零消息）。LLM 调用数 0-1。
    """
    from services.job_rules import pick_important_meetings
    from services.tasks import _task_dur

    meetings = pick_important_meetings(db, day)
    if not meetings:
        return {"meetings": [], "final_reply": ""}
    # 建议史回放（2026-11-01 优化批 7）：该会议此前的 prep 卡处理痕迹——重推
    # 清单记得「上次你删了哪条准备项」，不再重复推被拒项（近 30 天 kind=prep
    # 行里按 task_id 匹配，取最近一次已决）
    from models import Proposal
    from sqlalchemy import select as _sel

    hist: dict[str, dict] = {}
    for p in db.scalars(_sel(Proposal).where(
            Proposal.kind == "prep",
            Proposal.created_at >= datetime.now() - timedelta(days=30)).limit(20)):
        if not p.outcome:
            continue
        for m in (p.payload or {}).get("meetings") or []:
            tid = m.get("task_id")
            if isinstance(tid, str) and tid not in hist:
                pl = p.payload or {}
                hist[tid] = {"outcome": p.outcome,
                             "reject_reason": pl.get("reject_reason"),
                             "modified_diff": (pl.get("modified_diff")
                                               or pl.get("item_diffs"))}

    def _minutes_carryover(task_id: str) -> dict | None:
        """该会议上期纪要行动项完成情况（批 3 prep 联动）：source_key 前缀
        匹配的行动项行数与 done 计数，遗留标题 ≤5 条——系列会议（同主行
        task_id）与单次会议同口径；无行动项返回 None。"""
        from models import Task as _Task

        rows = list(db.scalars(_sel(_Task).where(
            _Task.source_key == f"minutes:{task_id}",
            _Task.is_deleted.is_(False)).limit(20)))
        if not rows:
            return None
        done = sum(1 for r in rows if r.done_at is not None)
        leftover = [r.title for r in rows if r.done_at is None][:5]
        return {"done": done, "total": len(rows), "leftover": leftover}

    payload = []
    for t, occ in meetings:
        entry = {
            "task_id": t.id,
            "title": t.title,
            "start": occ.isoformat(),
            "priority": t.priority,
            "duration_mins": _task_dur(t),
            "notes": (t.notes or "").strip()[:60],
            "recurrence": t.recurrence_rule is not None,
        }
        if t.id in hist:
            entry["history"] = hist[t.id]
        carry = _minutes_carryover(t.id)
        if carry:
            entry["minutes_carryover"] = carry
        payload.append(entry)
    # 双段注入单源（2026-11-03 批 3）：分级格式统一 + core 传 build 双段渲染
    from services.settings import get_settings

    s = get_settings(db)
    from agents.prompts import personality_rules
    from services.ai_data import format_memory_lines

    core, ref = format_memory_lines(
        db, personality=personality_rules(
            s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
    parsed, reply = nodes.build_prep_brief(payload, core + ref, day.isoformat(),
                                           core_rules=core)
    by_id = {p["task_id"]: p for p in payload}
    meetings_out = [
        {
            "task_id": m["task_id"],
            "title": by_id[m["task_id"]]["title"],
            "start": by_id[m["task_id"]]["start"],
            "headline": m["headline"],
            "checklist": m["checklist"],
        }
        for m in parsed
    ]
    return {"meetings": meetings_out, "final_reply": reply}


def run_meeting_minutes(db: Session, task_id: str | None, text: str) -> dict:
    """会后纪要提炼独立入口（routers/ai_coach.py meeting-minutes 端点消费，
    2026-11-10 增）：task_id 给定则回读会议元信息（存在性由壳前置 404 判定，
    这里信任传入 id；已删/查无按未关联处理），build_minutes_brief 单次 LLM
    提炼行动项 →
    {"headline", "items", "final_reply", "meeting": {task_id, title, start} | None}。
    提炼失败（非 JSON 全空）原样上抛语义由壳判（不建空壳卡）。LLM 调用数 1。"""
    from models import Task
    from services.tasks import _task_dur

    meeting_out: dict | None = None
    meeting_payload: dict | None = None
    if task_id:
        t = db.get(Task, task_id)
        if t is not None and not t.is_deleted:
            meeting_payload = {
                "title": t.title,
                "start": t.start_time.isoformat() if t.start_time else None,
                "duration_mins": _task_dur(t),
                "priority": t.priority,
                "notes": (t.notes or "").strip()[:60],
            }
            meeting_out = {"task_id": t.id, "title": t.title,
                           "start": meeting_payload["start"]}
    # 双段注入单源（同 run_meeting_prep）+ 去重对照（批 2：收件箱既有未完成待办）
    from services.settings import get_settings

    s = get_settings(db)
    from agents.prompts import personality_rules
    from services.ai_data import format_memory_lines

    core, ref = format_memory_lines(
        db, personality=personality_rules(
            s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
    from services import tasks as tasks_svc

    existing = [t.title for t in tasks_svc.list_tasks(db, unscheduled=True)[:20]]
    headline, items, reply = nodes.build_minutes_brief(
        meeting_payload, text, core + ref, core_rules=core,
        existing_titles=existing)
    return {"headline": headline, "items": items, "final_reply": reply,
            "meeting": meeting_out}


def _compact_chunks(msgs: list) -> list[list[dict]]:
    """压缩片段切块（纯函数，2026-10-26 增）：单条超长截 CHAT_COMPACT_MSG_CHARS
    加"（超长截断）"标记，再按 CHAT_COMPACT_CHUNK_CHARS 贪心组块——防单次压缩
    调用输入超长（8K 档压缩调用自身也会 ContextOverflowError）。"""
    from services.ai_data import CHAT_COMPACT_CHUNK_CHARS, CHAT_COMPACT_MSG_CHARS

    compact: list[dict] = []
    for m in msgs:
        text = m.content or ""
        if len(text) > CHAT_COMPACT_MSG_CHARS:
            text = text[:CHAT_COMPACT_MSG_CHARS] + "（超长截断）"
        compact.append({"role": m.role, "content": text})
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for item in compact:
        if cur and cur_chars + len(item["content"]) > CHAT_COMPACT_CHUNK_CHARS:
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(item)
        cur_chars += len(item["content"])
    if cur:
        chunks.append(cur)
    return chunks


def compact_chat_history(db: Session, *, force: bool = False) -> int:
    """对话滚动上下文压缩（2026-10-20 增；2026-10-26 重写为切块循环 + force）：
    旧摘要 + 片段并给 CHAT_SUMMARY_PROMPT → 落新摘要与游标。返回本轮并入摘要
    的消息条数（0 = 无事可做零 LLM）。与图运行互不相干：自动路径由壳在轮末
    fire-and-forget 触发，不占对话时延。

    - force=True（/compact 手动指令）：跳过 compaction_due 阈值判定，把全部
      未摘要消息并入（切片口径不变，仍保留最近 K 条原文作活记忆窗口）；
    - 切块循环：片段经 _compact_chunks 切块逐块合并，每成功一块即落库推游标
      ——部分成果保留，失败块抛 CompactPartialError（done = 已并入条数）。
      自动路径的壳照旧全吞（下轮从游标续压）；空输出同款处理（不落不推）。"""
    from agents.prompts import CHAT_SUMMARY_PROMPT
    from services.ai_data import (
        CompactPartialError,
        compaction_due,
        get_chat_state,
        pending_compaction_slice,
        save_chat_summary,
    )
    from services.llm import call_llm

    fragment = pending_compaction_slice(db) if force else compaction_due(db)
    if not fragment:
        return 0
    merged = 0
    for chunk in _compact_chunks(fragment):
        prev = get_chat_state(db).summary
        try:
            out = call_llm(
                [{"role": "system", "content": CHAT_SUMMARY_PROMPT},
                 {"role": "user", "content": json.dumps(
                     {"prev_summary": prev, "messages": chunk}, ensure_ascii=False)}],
                # 900 容纳 ≤500 字摘要扩容（2026-10-26，中文≈1.5 token/字余量）
                max_tokens=900)
        except Exception as exc:  # 超窗/超时/断连等一切调用失败：块级中断
            raise CompactPartialError(merged) from exc
        summary = (out or "").strip()
        if not summary:  # 空输出不落库不推游标（下次窗口不变自然重试）
            raise CompactPartialError(merged)
        save_chat_summary(db, summary=summary,
                          count=get_chat_state(db).summarized_count + len(chunk))
        merged += len(chunk)
    return merged


# —— chat 采集分支节点（ingest 意图到达；壳 routers/ai_coach.py 经 build_graph().invoke 驱动） ———


def collect_data(state: AgentState) -> dict:
    """chat 节点①：确定性采集先行——直调 service（必调的取数不经 LLM 仲裁）。
    memory_rules 不重查：壳 build_initial_state 已注入 state，refine 直接用。"""
    from services import data_ingest
    from services.ai_data import dedup_context

    nodes._status_emitter()("正在采集各源数据…")  # 过程反馈（提速批 2）
    with SessionLocal() as db:
        raw = data_ingest.fetch_recent(db, hours=72)
        existing = dedup_context(db)
    return {"schedule_context": raw, "dedup_titles": existing}


def refine(state: AgentState) -> dict:
    """chat 节点②：与 run_ingest 同体——共用 _map_phase/_reduce_phase 管道
    （含 load_summary/settings/weather 秘书视野注入，两入口输入形状零漂移）+
    草案全闸后验（2026-11-01 优化批 1 补齐——此前 chat 轮零后验，LLM 自排的
    草案可撞真实日历或违反作息，run_ingest 有防御而本节点没有是同体管道的
    防御缺口）；四键出口由壳 _finalize_turn 消费（reply / draft / proposal
    事件，零壳改动）。"""
    from services.preference_slots import parse_avoid_windows
    from services.settings import get_settings
    from services.tasks import busy_intervals, calendar_load_summary
    from services.weather import forecast_for_city

    nodes._status_emitter()("正在汇总提炼建议…")  # 过程反馈（提速批 2）
    with SessionLocal() as db:  # claim_llm_budget 预算代理 + 负荷摘要用
        from services.ai_data import get_chat_state

        candidates, source_notes = _map_phase(db, state.get("schedule_context") or {})
        s = get_settings(db)
        result = _reduce_phase(candidates, state.get("dedup_titles") or [],
                               state.get("memory_rules") or [], source_notes, date.today(),
                               load_summary=calendar_load_summary(db),
                               settings=nodes.settings_payload(s),
                               weather=forecast_for_city(s.weather_city),
                               chat_summary=get_chat_state(db).summary or None,
                               db=db)
        _postvalidate_drafts(result, busy=busy_intervals(db),
                             day_start_min=s.calendar_start_min,
                             day_end_min=s.calendar_end_min,
                             lunch=(s.lunch_start_min, s.lunch_end_min) if s.lunch_enabled else None,
                             avoid_windows=parse_avoid_windows(
                                 state.get("memory_rules") or []))
        return result


# —— chat 主图：七分支意图路由（入口 B） —————————————————————————————————————


# 意图 → 分支首节点映射（与 nodes._INTENTS、prompts.ROUTER_PROMPT 七分类一一对应）
_INTENT_EDGES = {
    # 排期子图包装节点（2026-10-21 私人秘书化批 3——旧 schedule_planner 单节点
    # 整链迁入 agents/scheduler/ 四节点管道，接缝契约与 LLM 调用数零变化）
    "schedule": "scheduler",
    "qa": "qa_answerer",
    # 幕僚长子图包装节点（2026-10-21 Track B 批 5——旧 task_coach 单节点整链
    # 迁入 agents/strategist/ 四节点管道，coach 意图分流幕僚长七场景）
    "coach": "strategist",
    "smalltalk": "smalltalk_node",
    "ingest": "collect_data",
    "delete": "delete_confirmer",
    # 设置官（2026-10-22 批 3）：对话自然语言改设置/写长期偏好/撤销
    "settings": "settings_handler",
    # 会后纪要提炼（2026-11-10 批 2）：纪要词 + 长文本预路由直通，inbox_items
    # + minutes_meta 出口（壳建 kind=minutes 卡）
    "minutes": "minutes_extractor",
}

# 排期子图出口键（回填主图 AgentState；与旧 schedule_planner 返回键面一致）
_SCHEDULER_EXITS = ("resolved_proposal", "question_plan", "applied_plan",
                    "final_reply", "draft_plan", "bulk")


def scheduler_node(state: AgentState) -> dict:
    """排期分支包装节点（批 3 接线）：invoke 排期子图并显式映射出口键。

    langgraph 1.2.11 子图语义规避：不用 compiled-as-node——普通函数节点内
    invoke 子图单例，进出键显式映射（子图 SchedulerState 与主图 AgentState
    只有三键同形入口 / 五键出口交集，靠包装收窄不靠合并语义）。schedule
    分支本就非流式（json_mode 四分支不流式），invoke 阻塞无影响。"""
    from agents.scheduler import scheduler_app  # 延迟 import 防环

    result = scheduler_app.invoke({
        "user_input": state.get("user_input", ""),
        "memory_rules": state.get("memory_rules") or [],
        "pending": state.get("pending"),
    })
    return {k: result[k] for k in _SCHEDULER_EXITS if result.get(k) is not None}


# 幕僚长子图出口键（九键子集：无 applied_plan/delete_plan/evidence）
_STRATEGIST_EXITS = ("resolved_proposal", "question_plan", "final_reply",
                     "draft_plan", "preflight", "inbox_items")


def strategist_node(state: AgentState) -> dict:
    """教练分支包装节点（Track B 批 5 翻转）：invoke 幕僚长子图并映射出口键。

    同 scheduler_node 先例（invoke 子图单例 + 出口收窄）；入口多一个
    trigger 键（chat 恒 "chat"，cron 形态由 run_preflight 直调子图不走主图）。
    coach 分支非流式（json_mode），invoke 阻塞无影响。"""
    from agents.strategist import strategist_app  # 延迟 import 防环

    result = strategist_app.invoke({
        "user_input": state.get("user_input", ""),
        "memory_rules": state.get("memory_rules") or [],
        "pending": state.get("pending"),
        "trigger": "chat",
    })
    return {k: result[k] for k in _STRATEGIST_EXITS if result.get(k) is not None}


def _route_by_intent(state: AgentState) -> str:
    """条件边路由：center_router 写入的 intent → 下一节点名。

    非法/缺失值兜底 smalltalk_node——与 center_router 内部的解析兜底构成双保险
    （节点被测试直调 / 图被部分驱动时 state 可能没有合法 intent，条件边永远
    不能抛错——图结构层的最后一道闸）。
    """
    return _INTENT_EDGES.get(state.get("intent"), "smalltalk_node")


def _route_shortcut(state: AgentState) -> str:
    """草案/删除确认短路路由（图第一跳，2026-10-23 增）：confirm_shortcut
    命中终态（final_reply 已置）直达 END 由壳消费出口键；否则进 center_router
    正常七分类。final_reply 此刻非空 ⇔ 短路命中（initial_state 无此键，后续
    分支节点尚未跑），判定无歧义。"""
    return END if state.get("final_reply") else "center_router"


def run_hard_task(db: Session, task_id: str) -> dict | None:
    """高难协助入口（workflows hardtask 流消费，签名冻结）：规则层复核资格
    （认领与执行间任务可能刚被排期/删除）→ 不符静默 None → build_hardtask_brief
    单次调用产分解步骤 → skip/解析失败 None（调用方静默零卡）。LLM 调用数 0-1。
    """
    from datetime import datetime as _dt
    from models import Task
    from services.job_rules import hard_task_score

    task = db.get(Task, task_id)
    if task is None or task.is_deleted or hard_task_score(task, _dt.now()) is None:
        return None
    payload = {
        "task_id": task.id,
        "title": task.title,
        "duration_mins": task.duration_mins,
        "priority": task.priority,
        "notes": (task.notes or "").strip()[:60],
        "created_at": task.created_at.date().isoformat(),
        "reschedule_count": task.reschedule_count,
    }
    # 双段注入单源（2026-11-03 批 3）：分级格式统一 + core 传 build 双段渲染
    from services.settings import get_settings
    from agents.prompts import personality_rules
    from services.ai_data import format_memory_lines

    s = get_settings(db)
    core, ref = format_memory_lines(
        db, personality=personality_rules(
            s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
    parsed = nodes.build_hardtask_brief(payload, core + ref, _dt.now().date().isoformat(),
                                        core_rules=core)
    if parsed is None:
        return None
    return {
        "task_id": task.id, "title": task.title,
        "headline": parsed["headline"], "steps": parsed["steps"],
        "advice": parsed["advice"],
    }


def run_custom_brief(db: Session, brief_id: str) -> dict | None:
    """自定义简报入口（workflows brief 流消费，签名冻结）：行已删/停用 →
    静默 None（认领后窗口内停用，停用意图优先）；纯规则组当日数据
    （今日任务截 20 条 / 负荷率 / 未排期计数，零 LLM）→ build_custom_brief
    单次调用 → 解析失败 None。LLM 调用数 1。
    """
    from models import CustomBrief
    from services.settings import get_settings
    from services.workflows import load_ratio
    from services.tasks import list_tasks
    from agents.prompts import personality_rules

    brief = db.get(CustomBrief, brief_id)
    if brief is None or not brief.enabled:
        return None
    day0 = datetime.combine(date.today(), datetime.min.time())
    tasks_today = list_tasks(db, start=day0, end=day0 + timedelta(days=1))[:20]
    unscheduled = list_tasks(db, unscheduled=True)
    tasks_payload = [
        {
            "title": t.title,
            "start": t.start_time.isoformat() if t.start_time else None,
            "priority": t.priority,
            "duration_mins": t.duration_mins,
        }
        for t in tasks_today
    ]
    s = get_settings(db)
    # 双段注入单源（2026-11-03 批 3）：分级格式统一 + core 传 build 双段渲染
    from services.ai_data import format_memory_lines

    core, ref = format_memory_lines(
        db, personality=personality_rules(
            s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
    parsed = nodes.build_custom_brief(
        brief.instruction, tasks_payload, load_ratio(db, date.today()),
        len(unscheduled), core + ref, date.today().isoformat(), core_rules=core)
    if parsed is None:
        return None
    return {"headline": parsed["headline"], "body": parsed["body"]}


def build_graph(checkpointer=None):
    """chat 主图（模块头 ASCII 图）：START → center_router 意图分类 → 七分支
    条件边 → 各分支终点 → END。schedule 分支经 scheduler_node 包装进排期
    子图（agents/scheduler/ 四节点管道），ingest 分支两跳（确定性采集 →
    Map/Reduce 提炼），其余分支一跳直达。

    接缝契约：返回编译后的图，由壳以 graph().invoke(initial_state) 驱动（零
    config）。默认不挂 checkpointer——langgraph 1.2.11 实证：挂 MemorySaver 后
    无 thread_id 的 invoke 必抛 ValueError；需要检查点（中断挂起/恢复练习）时
    显式传 checkpointer=MemorySaver() 并附 thread_id（见 __main__）。"""
    b = StateGraph(AgentState)

    # 七个认知分支节点在 agents/nodes.py；collect_data/refine 留本模块（与
    # run_ingest 同体，见模块头"双入口同体"）
    b.add_node("confirm_shortcut", nodes.confirm_shortcut)
    b.add_node("center_router", nodes.center_router)
    b.add_node("scheduler", scheduler_node)
    b.add_node("qa_answerer", nodes.qa_answerer)
    b.add_node("strategist", strategist_node)
    b.add_node("smalltalk_node", nodes.smalltalk_node)
    b.add_node("delete_confirmer", nodes.delete_confirmer)
    b.add_node("settings_handler", nodes.settings_handler)
    b.add_node("minutes_extractor", nodes.minutes_extractor)
    b.add_node("collect_data", collect_data)
    b.add_node("refine", refine)

    b.add_edge(START, "confirm_shortcut")
    b.add_conditional_edges("confirm_shortcut", _route_shortcut)
    b.add_conditional_edges("center_router", _route_by_intent)
    b.add_edge("scheduler", END)
    b.add_edge("qa_answerer", END)
    b.add_edge("strategist", END)
    b.add_edge("smalltalk_node", END)
    b.add_edge("delete_confirmer", END)
    b.add_edge("settings_handler", END)
    b.add_edge("collect_data", "refine")
    b.add_edge("refine", END)

    return b.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    # 手动冒烟（backend 目录下 python -m agents.graph；真联网：采集 + LLM）；
    # 正式驱动方是 ai_coach 壳（零 config invoke）。此处同时演示检查点脚手架：
    # 显式传 MemorySaver + thread_id（interrupt 挂起/恢复练习的载体）。
    agent = build_graph(checkpointer=MemorySaver())
    outputs = agent.invoke(
        {"user_input": "看看接入源里有什么要做的", "memory_rules": []},
        {"configurable": {"thread_id": "smoke"}},
    )
    print("=== final_reply ===")
    print(outputs.get("final_reply"))
    print("=== draft_plan / inbox_items / evidence ===")
    print(json.dumps({k: outputs.get(k) for k in ("draft_plan", "inbox_items", "evidence")},
                     ensure_ascii=False, indent=1))
