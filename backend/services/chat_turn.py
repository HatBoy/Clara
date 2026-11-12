"""对话轮次共享内核（2026-10-23 增，聊天通讯渠道前置批）。

从 routers/ai_coach.py 下沉的轮次管道公共件——桌面 SSE 壳与远程渠道 worker
（services/chat_channels.py）两条入口消费同一套逻辑，保证 initial_state 构造、
final state 出口消费序、消息落库口径零漂移（接缝契约见 docs/ai-agents.md §六；
分层红线：业务逻辑不留在 router）。ai_coach 保留 `_load_graph` 等再导出别名，
既有测试的 monkeypatch 锚点零破坏。
"""

import asyncio
import logging

from sqlalchemy.orm import Session

from agents.nodes import QUESTION_KINDS
from services import ai_data, data_ingest
from services.settings import get_settings

logger = logging.getLogger(__name__)


def load_graph():
    """延迟加载手写区主图入口（agents.graph.build_graph）。ImportError / 桩的
    NotImplementedError 由调用点分类处理（not_ready）；本函数是测试 monkeypatch
    的锚点——替身 graph 经 monkeypatch.setattr(chat_turn, "load_graph", ...) 注入
    （桌面壳走 ai_coach._load_graph 再导出别名，同值）。"""
    from agents.graph import build_graph

    return build_graph


def build_initial_state(db: Session, user_input: str) -> dict:
    """壳构造的 initial_state（接缝契约，docs/ai-agents.md §六）：memory_rules 为
    core+reference 合并单列表（分级标签格式，消费方契约不变），core_memory 为
    铁律段（2026-11-03 批 3 双段注入，qa/smalltalk 消费）；pending 为当前活跃
    问询快照（ai_data.active_pending，无则 None——"可以"这类确认回复靠它关联上
    一轮，2026-10-17 增）。节点需要读库时自行 with SessionLocal() as db（只调
    services，禁摸 ORM / 禁 import router）。"""
    # 人格规则前置（2026-10-20）：回复风格 + 管理哲学排最前——比记忆规则优先级高，
    # balanced 且全空时 personality_rules 返回 []，拼接后形状与旧版逐项一致
    settings = get_settings(db)
    from agents.prompts import personality_rules

    personality = personality_rules(
        settings.ai_style, settings.ai_philosophy, settings.ai_philosophy_custom
    )
    # 双段单源（2026-11-03 批 3）：core=铁律段（人格前置+user_manual/chat+带锚高
    # 置信，slot 分组序）/ ref=参考段（低置信 ai_reflect 头部切片）——格式收敛自
    # 此函数旧内联与 workflows._collect_priority_plan 两处（rule_label 单源）
    from services.ai_data import format_memory_lines

    core, ref = format_memory_lines(db, personality=personality)
    return {
        "user_input": user_input,
        "memory_rules": core + ref,
        "core_memory": core,
        "pending": ai_data.active_pending(db),
    }


def finalize_turn(db: Session, final: dict, *, source: str | None = None) -> list[dict]:
    """graph 最终 state → 持久化 + SSE 事件序列（同步纯函数，测试直测）。

    source（2026-10-23 增）：本条 assistant 回复的消息来源标记（None=桌面端 /
    "feishu"|"dingtalk"=远程渠道轮）——只写 messages.source 列作展示层徽标，
    不进任何提示词与上下文组装（统一上下文决策见 docs/chat-comm.md）。

    处理序（前三块 2026-10-17 增，顺序有语义）：
    ① resolved_proposal → 回写旧问询行 outcome（pending 被本轮消费的结算）；
    ② question_plan → 建 kind=question 行，零卡零事件，且优先占 first_proposal_id
    ——assistant 问询消息必须挂问询行（消息锚点是下轮 active_pending 的判定依据；
    mixed 轮次若被 applied/draft 行抢走锚点，问询将失活）；
    ③ applied_plan → tasks.apply_plan_entries 落库 + kind=schedule 行直置
    accepted（payload.auto=true）+ applied 事件（tasks 全量载荷，前端刷新画布）；
    落库 ValueError → 整批并入 draft_plan 原子降级为确认卡（无部分应用）；
    ④ 现状四块：draft_plan → kind=schedule 建议行（evidence 溯源并入 payload，
    M1-①）+ draft 事件；inbox_items → kind=inbox 建议行 + proposal 事件（M1-②）；
    preflight → proposal 事件；delete_plan → kind=delete 建议行 + proposal 事件
    （删除确认卡：用户点「确认删除」才经前端 DELETE 管线执行，AI 不直接删）；
    ⑤ final_reply → assistant 消息（关联首个建议行）+ reply 事件置顶。建卡后
    mark_items_proposed 回写采集台账（首次进卡时刻）。

    ingest 轮口径（2026-10-25 功能边界）：intent=ingest 时采集建议卡照建（补
    from_ingest 标记 → 数据源页可见）但不发 draft/proposal 卡事件、不占消息
    锚点——对话框只留 reply 综述与指引文本，数据源页是采集建议唯一视图；轻量
    ingest_done 事件通知前端刷新数据源徽标。"""
    final = dict(final or {})
    is_ingest = final.get("intent") == "ingest"
    is_bulk = final.get("bulk") is True
    events: list[dict] = []
    first_proposal_id: str | None = None
    reply = final.get("final_reply")
    reply = reply if isinstance(reply, str) else ""

    resolved = final.get("resolved_proposal")
    if isinstance(resolved, dict) and resolved.get("proposal_id"):
        ai_data.record_proposal_outcome(
            db, resolved["proposal_id"], resolved.get("outcome") or "ignored")

    question = final.get("question_plan")
    # 幕僚问询（strategist_clarify/memory）无排期条目——entries 空放行
    # （Track B 批 2）；scenario_type（clarify 恢复锚）与 rule（memory 提案）
    # 随 payload 落库，active_pending 快照消费侧读取
    if (isinstance(question, dict) and question.get("kind") in QUESTION_KINDS
            and (question.get("entries")
                 or question.get("kind") in ("strategist_clarify", "memory"))):
        payload = {k: question.get(k) for k in (
            "kind", "entries", "suggestion", "similar_to", "date_hint", "original_input")}
        payload.update({k: question[k] for k in ("scenario_type", "rule")
                        if question.get(k)})
        if reply:
            payload["ask_text"] = reply  # ROUTER 问询前缀与 active_pending 快照都读它
        q_row = ai_data.create_proposal(db, kind="question", payload=payload)
        first_proposal_id = q_row.id

    # —— 高可信直通拆分（2026-10-24，workflows._run_ingest 同款）——trusted 源
    # todo/calendar 域条目免确认直落（结构性数据可信；笔记/文档提炼的域为 doc
    # 恒建卡进数据源页，用户定案）。draft 直落独立落库（apply_plan_entries + 原子
    # 降级 fallback 形态）+ applied 事件刷新画布；inbox 直落逐条 create_task +
    # accepted 结算（无卡片事件，reply 注记可见）。非 ingest 轮（draft/inbox 均无
    # source_ref/evidence）split 自然全量回传零分支。
    split = data_ingest.split_trusted_direct(
        db, final.get("draft_plan") or [], final.get("inbox_items") or [],
        final.get("evidence") or [])
    direct_parts: list[str] = []
    if split["draft_direct"] or split["draft_rest"]:
        final["draft_plan"] = split["draft_rest"]
        final["evidence"] = split["evidence_rest"]
    if split["draft_direct"]:
        try:
            from schemas import TaskOut
            from services import tasks as tasks_svc
            created = tasks_svc.apply_plan_entries(db, split["draft_direct_entries"])
        except ValueError:
            # 原子降级：直落整批并回建卡组走确认卡（无部分应用）
            final["draft_plan"] = split["draft_fallback"]
            final["evidence"] = split["evidence_fallback"]
        else:
            payload = {"draft_plan": split["draft_direct"], "from_ingest": True,
                       "evidence": split["evidence_direct"], "auto": True}
            prop = ai_data.create_proposal(db, kind="schedule", payload=payload)
            ai_data.record_proposal_outcome(db, prop.id, "accepted")
            first_proposal_id = first_proposal_id or prop.id
            data_ingest.mark_items_proposed(
                db, data_ingest.payload_refs("schedule", payload))
            events.append({"type": "applied", "proposal_id": prop.id,
                           "tasks": [TaskOut.model_validate(t).model_dump(mode="json")
                                     for t in created]})
            direct_parts.append(f"{len(created)} 条日程直落日历")
    if split["inbox_direct"]:
        final["inbox_items"] = split["inbox_rest"]
        from schemas import TaskCreate
        from services import tasks as tasks_svc
        clean: list[dict] = []
        for it in split["inbox_direct"]:
            try:
                ref = it.get("source_ref")
                ref = ref if isinstance(ref, dict) else {}
                fp = ref.get("fingerprint")
                tasks_svc.create_task(db, TaskCreate(
                    title=str(it["title"])[:30],
                    duration_mins=it.get("duration"),
                    notes=it.get("notes") or None,
                    priority=it.get("priority"),
                    source_key=ref.get("source_key"),
                    source_fingerprint=fp if isinstance(fp, str) and fp else None,
                ))
                clean.append(it)
            except Exception:
                logger.warning("高可信直通单条失败，转建卡：%r", it.get("title"))
                final["inbox_items"] = (final.get("inbox_items") or []) + [it]
        if clean:
            prop = ai_data.create_proposal(db, kind="inbox",
                                           payload={"items": clean, "from_ingest": True})
            ai_data.record_proposal_outcome(db, prop.id, "accepted")
            data_ingest.mark_items_proposed(
                db, data_ingest.payload_refs("inbox", {"items": clean}))
            direct_parts.append(f"{len(clean)} 条待办直落收件箱")
    if direct_parts:
        reply += f" 高可信源直通：{'、'.join(direct_parts)}。"

    applied = [e for e in (final.get("applied_plan") or [])
               if isinstance(e, dict) and e.get("title")]
    if applied:
        try:
            from services import tasks as tasks_svc
            created = tasks_svc.apply_plan_entries(db, applied)
        except ValueError:
            # 原子降级：整批并入 draft 走确认卡——用户明确指令也不硬落半个批次
            final["draft_plan"] = applied + list(final.get("draft_plan") or [])
        else:
            from schemas import TaskOut
            payload = {"draft_plan": applied, "auto": True}
            prop = ai_data.create_proposal(db, kind="schedule", payload=payload)
            ai_data.record_proposal_outcome(db, prop.id, "accepted")
            first_proposal_id = first_proposal_id or prop.id
            events.append({
                "type": "applied", "proposal_id": prop.id,
                "tasks": [TaskOut.model_validate(t).model_dump(mode="json") for t in created],
            })

    draft_plan = final.get("draft_plan")
    ingest_card_count = 0
    if draft_plan:
        payload = {"draft_plan": draft_plan}
        evidence = final.get("evidence") or []
        if evidence:
            payload["evidence"] = evidence
        if is_ingest:
            payload["from_ingest"] = True  # 数据源页过滤依据（缺标即两处视图都不见）
        if is_bulk:
            payload["bulk"] = True  # 审计标记：弹窗确认的批量草案卡
        proposal = ai_data.create_proposal(db, kind="schedule", payload=payload)
        data_ingest.mark_items_proposed(db, data_ingest.payload_refs("schedule", payload))
        if is_ingest:
            ingest_card_count += len(draft_plan)
        elif is_bulk:
            # 批量排期轮（2026-10-25 功能边界）：草案不出对话框——bulk 事件驱动
            # 前端弹窗清单确认（时间建议由统一引擎产生），不挂消息锚点（同
            # ingest 先例：CopilotPanel 按锚渲染，无锚即零卡片）
            events.append({"type": "bulk", "proposal_id": proposal.id,
                           "draft_plan": draft_plan})
        else:
            first_proposal_id = first_proposal_id or proposal.id
            events.append(
                {"type": "draft", "proposal_id": proposal.id, "draft_plan": draft_plan}
            )

    inbox_items = [it for it in (final.get("inbox_items") or [])
                   if isinstance(it, dict) and it.get("title")]
    # 会后纪要轮（2026-11-10 批 2）：minutes_meta 与 inbox_items 同轮存在 →
    # kind=minutes 卡（payload 契约同浮层端点，前端渲染分支共用）；零行动项
    # 只回 reply 不建卡。互斥：不走下方 inbox 建卡段
    minutes_meta = final.get("minutes_meta")
    if inbox_items and isinstance(minutes_meta, dict):
        payload = {k: minutes_meta.get(k) for k in (
            "meeting_task_id", "meeting_title", "meeting_start")}
        payload["headline"] = minutes_meta.get("headline") or ""
        payload["items"] = inbox_items
        payload["source_text"] = minutes_meta.get("source_text") or ""
        proposal = ai_data.create_proposal(db, kind="minutes", payload=payload)
        first_proposal_id = first_proposal_id or proposal.id
        events.append(
            {"type": "proposal", "proposal_id": proposal.id, "kind": "minutes",
             "payload": payload}
        )
    elif inbox_items:
        proposal = ai_data.create_proposal(db, kind="inbox",
                                           payload={"items": inbox_items, "from_ingest": True})
        data_ingest.mark_items_proposed(db, data_ingest.payload_refs("inbox", {"items": inbox_items}))
        if is_ingest:
            ingest_card_count += len(inbox_items)
        else:
            first_proposal_id = first_proposal_id or proposal.id
            events.append(
                {"type": "proposal", "proposal_id": proposal.id, "kind": "inbox",
                 "payload": {"items": inbox_items}}
            )

    if is_ingest and ingest_card_count:
        # 采集建议不出现在对话框：指引文本 + 轻量事件（前端据此刷新数据源徽标，
        # 不弹通知——reply 气泡本身就是反馈，通知是定时任务离线场景的触达手段）
        reply += f"（{ingest_card_count} 条建议已放入「数据源」页，可前往确认、排期或转入收件箱）"
        events.append({"type": "ingest_done", "count": ingest_card_count})

    preflight = final.get("preflight")
    if preflight:
        proposal = ai_data.create_proposal(db, kind="preflight", payload=preflight)
        first_proposal_id = first_proposal_id or proposal.id
        events.append(
            {"type": "proposal", "proposal_id": proposal.id, "kind": "preflight", "payload": preflight}
        )

    # 删除确认卡（delete 分支）：delete_plan 非空才建卡——空清单是节点的"未命中/
    # 重复系列指引"形态，只有 final_reply 不出卡
    delete_plan = [it for it in (final.get("delete_plan") or [])
                   if isinstance(it, dict) and it.get("task_id")]
    if delete_plan:
        payload = {"tasks": delete_plan}
        proposal = ai_data.create_proposal(db, kind="delete", payload=payload)
        first_proposal_id = first_proposal_id or proposal.id
        events.append(
            {"type": "proposal", "proposal_id": proposal.id, "kind": "delete", "payload": payload}
        )

    if reply:
        ai_data.append_message(db, role="assistant", content=reply,
                               proposal_id=first_proposal_id, source=source)
        events.insert(0, {"type": "reply", "content": reply})
    # 本轮出口摘要（2026-10-22 排障打点）：非空出口键一览——结合 [router]/
    # [llm]/[scheduler]/[strategist] 各节点日志即可还原整轮智能体流程
    logger.info("[chat] 本轮出口：%s",
                "、".join(sorted(k for k, v in final.items() if v)) or "（空）")
    return events


def compact_history_async() -> None:
    """滚动上下文压缩的后台执行体（chat 端点 done 帧前 fire-and-forget 挂起，
    2026-10-20 增）：自开 SessionLocal——本函数跑在 to_thread 工作线程，线程
    隔离铁律禁止复用请求级 Session；全异常吞为 warning——压缩失败 = 本轮退化
    为"无摘要 + 近期原文"，下轮自然重试，绝不影响对话主链路。

    开关 gate（2026-10-26 增）：chat_autocompact_enabled 关 → 轮末早退零 LLM
    （摘要不再自动更新）。gate 放这里单点罩住桌面 SSE 与远程渠道轮两个调用点；
    手动 /compact 端点不经此函数，天然不受开关影响（开关语义 = 只关自动）。"""
    from agents.graph import compact_chat_history
    from database import SessionLocal

    try:
        with SessionLocal() as background_db:
            if not get_settings(background_db).chat_autocompact_enabled:
                return
            compact_chat_history(background_db)
    except Exception:
        logger.warning("对话上下文压缩失败（静默降级，下轮重试）", exc_info=True)


# 后台压缩任务引用池（2026-10-21 复查批 C 增）：asyncio 只持任务弱引用，裸
# create_task 无人引用时可能在完成前被 GC 静默蒸发——强引用挂池 + done 回调移除
_bg_compact_tasks: set[asyncio.Task] = set()


def schedule_compaction() -> None:
    """轮次结束后调度后台压缩（须在事件循环协程内调用；远程渠道轮复用同款）。

    fire-and-forget 不占对话时延；任务挂引用池防 GC（见 _bg_compact_tasks 注释）。
    """
    bg = asyncio.create_task(asyncio.to_thread(compact_history_async))
    _bg_compact_tasks.add(bg)
    bg.add_done_callback(_bg_compact_tasks.discard)


# —— 轮次串行锁（2026-10-23 聊天通讯批 3 增）—————————————————————————————

_TURN_LOCK: asyncio.Lock | None = None


def get_turn_lock() -> asyncio.Lock:
    """桌面 SSE 轮与远程渠道轮（chat_channels.handle_incoming）共用的串行锁：
    同一时刻只允许一个轮次在跑，防 pending 消息锚点（active_pending 倒序判锚）
    与 messages 追加序在并发轮下竞争。懒建——TestClient 每测试新事件循环，
    模块级裸锁会跨循环绑定 Future 报错；测试经 reset_turn_lock 复位。

    锁的所有权归「轮次」不归「SSE 连接」：轮次任务在 finally 里释放，客户端
    断连只断帧消费、轮次照常跑完落库——孤儿图线程与下一轮并发写的竞态窗口
    因此关闭（docs/chat-comm.md）。无看门狗：LLM 卡死则锁被占死，依赖 llm
    客户端自身 120s 超时兜底（tech-debt 登记：引入看门狗会制造"超时放锁但
    孤儿仍在写"的同型竞态，不做）。"""
    global _TURN_LOCK
    if _TURN_LOCK is None:
        _TURN_LOCK = asyncio.Lock()
    return _TURN_LOCK


def reset_turn_lock() -> None:
    """测试专用：丢弃旧锁（下个事件循环重建），防跨循环绑定。"""
    global _TURN_LOCK
    _TURN_LOCK = None
