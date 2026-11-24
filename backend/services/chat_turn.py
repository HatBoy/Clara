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

from agents.common import QUESTION_KINDS
from services import ai_data, data_ingest, sse_kit
from services.llm import AI_NO_MODEL_SHORT_MSG, NoActiveModelError
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
    # 注入上下文单源（2026-11-17 架构批 1）：人格前置 + 双段分流（core=铁律段 /
    # ref=参考段）的顺序约束收在 ai_data.injected_rules（原 9 处配方逐字拷贝收敛）
    inj = ai_data.injected_rules(db)
    return {
        "user_input": user_input,
        "memory_rules": inj.all,
        "core_memory": inj.core,
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
        outcome = resolved.get("outcome") or "ignored"
        from services import proposal_decisions
        from models import Proposal
        row = db.get(Proposal, resolved['proposal_id'])
        if row and row.kind in ('schedule', 'delete') and outcome in ('accepted', 'rejected'):
            action = 'accept' if outcome == 'accepted' else 'reject'
            stable = resolved.get('item_ids') or row.item_ids
            # 对话／渠道确认使用原卡回执，而不是另建一张已采纳卡。
            final.pop('applied_plan', None)
            try:
                result = proposal_decisions.decide(db, row.id, [{'item_id': i, 'action':action} for i in stable])
            except ValueError:
                reply = '这张建议已发生变化，本次没有写入，请刷新后核对。'
            else:
                if action == 'accept' and row.kind == 'schedule':
                    from schemas import TaskOut
                    from services.tasks import get_task
                    applied_tasks = [get_task(db, r['task_id']) for r in result['receipts'] if r['task_id']]
                    events.append({'type':'applied', 'proposal_id':row.id, 'tasks':[
                        TaskOut.model_validate(t).model_dump(mode='json') for t in applied_tasks if t]})
                first_proposal_id = row.id
        else:
            settled = ai_data.record_proposal_outcome(db, resolved['proposal_id'], outcome)
            if settled is not None:
                data_ingest.on_proposal_settled(db, settled.kind, settled.payload, outcome)

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

    # —— 高可信直通拆分（2026-10-24，workflows.run_ingest_now 同款）——trusted 源
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
            # 原子降级：整批并入 draft 走确认卡——用户明确指令也不硬落半个批次。
            # 同步纠正回复文案（2026-11-16 优化批 2）：草案确认短路预先写好了
            # 「已安排」，降级后必须改成待确认，避免"假采纳"文案。
            final["draft_plan"] = applied + list(final.get("draft_plan") or [])
            final["final_reply"] = "这批调整需要你确认，已转为确认卡，请查看后决定。"
            reply = final["final_reply"]  # 局部 reply 已在上方捕获，须同步
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
        # from_ingest 只标采集轮（2026-10-25 功能边界；2026-11-17 二轮批 1 修正：
        # 早版无条件恒打——幕僚长教练行动项被误标采集建议、徽标虚高、错用 7 天
        # 过期窗。对照 draft 分支的条件注入同构）
        inbox_payload: dict = {"items": inbox_items}
        if is_ingest:
            inbox_payload["from_ingest"] = True
        proposal = ai_data.create_proposal(db, kind="inbox", payload=inbox_payload)
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

    for event in events:
        if event["type"] in ("draft", "proposal", "bulk"):
            prop = ai_data.get_proposal(db, event["proposal_id"])
            event["item_ids"] = prop.item_ids
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


def schedule_compaction() -> None:
    """轮次结束后调度后台压缩（须在事件循环协程内调用；远程渠道轮复用同款）。

    fire-and-forget 不占对话时延；强引用池托管（sse_kit.spawn 单源，
    2026-10-21 复查批 C 的 GC 坑修法收编）。
    """
    sse_kit.spawn(asyncio.create_task(asyncio.to_thread(compact_history_async)))


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


# —— 桌面端轮次编排（2026-11-17 架构批 5：从 routers/ai_coach.py 下沉）———————

# 轮次任务出队哨兵：轮次体全部事件已投递完毕——身份比较用（消费侧内部化，
# router 只见事件流）
TURN_END = object()


async def stream_turn_events(message: str, graph):
    """桌面端对话轮次的 SSE 事件编排：产出事件 dict（帧序列化归 router）。

    轮次跑在独立 asyncio 任务里、全程持有轮次锁（get_turn_lock，与远程渠道轮
    串行防 pending 锚点与消息追加序竞争）；**断连不丢本轮**——客户端断开只断
    帧消费，轮次任务继续跑完落库（自开 SessionLocal：请求级 Session 会随依赖
    清理关闭，断连后不可复用），锁在轮次 finally 释放。事件经无界队列投递，
    断连后无人消费轮次也不阻塞。graph 由调用方加载传入（ai_coach._load_graph
    的测试锚点留在 router 层——patch 语义保留）；帧序契约（message → delta* →
    reply/draft/... → done）由语句顺序结构性保证，改动须对照 test_ai_coach 的
    帧序断言。
    """
    out: asyncio.Queue = asyncio.Queue()
    lock = get_turn_lock()
    await lock.acquire()

    async def _turn() -> None:
        """轮次体：持锁跑完「构造 state → 落 user 消息 → 图执行 → 收尾落库」
        全程，事件（含错误帧）逐个投 out 队列，finally 释放锁 + 投哨兵。"""
        from database import SessionLocal

        try:
            import time as _time

            _t0 = _time.perf_counter()
            with SessionLocal() as turn_db:
                state = build_initial_state(turn_db, message)
                ai_data.append_message(turn_db, role="user", content=message)
                logger.info("[chat] 新对话轮：%s", message[:60])
                out.put_nowait({"type": "message", "role": "user", "content": message})

                try:
                    # —— 图执行：stream 驱动 + 跨线程队列桥（2026-10-16 token 级流式改造） ——
                    # graph 同步阻塞（节点内 LLM 走 litellm 同步客户端）——仍在 to_thread
                    # 工作线程跑，免得卡住事件循环拖迟同进程的提醒调度。stream_mode 双通道
                    # （langgraph 1.2.11 源码验证过形态）：
                    # - "custom"：节点经 get_stream_writer 推的 delta 帧（common.delta_emitter
                    #   构造的 {"type":"delta","text":...}）——转发 out 队列，打字机体感，
                    #   且逐 token 实时到达（custom 模式的 waiter 机制，不积压到 superstep 末）；
                    # - "values"：每 superstep 后的全量 state，末帧 == invoke 返回值同源同值，
                    #   记为 final 交 finalize_turn。
                    # 桥接/收尾信封收敛 sse_kit.stream_worker（deliver + StreamDone/
                    # StreamFailure + 强引用池托管，线程不可中断自然跑完出池）。
                    # turn_db 全程只在事件循环线程使用（线程隔离铁律）。
                    def _consume_graph(deliver):
                        final_local = None
                        for mode, payload in graph().stream(state, stream_mode=["custom", "values"]):
                            if mode == "custom":
                                deliver(payload)
                            else:
                                final_local = payload
                        return final_local

                    queue = sse_kit.stream_worker(_consume_graph)
                    graph_exc: BaseException | None = None
                    final = None
                    while True:
                        item = await queue.get()
                        if isinstance(item, sse_kit.StreamFailure):
                            graph_exc = item.exc
                            break
                        if isinstance(item, sse_kit.StreamDone):
                            final = item.payload
                            break
                        out.put_nowait(item)  # custom 帧 = delta 事件
                    if graph_exc is not None:
                        raise graph_exc
                except NoActiveModelError:
                    out.put_nowait({"type": "error", "code": "no_model",
                                    "message": AI_NO_MODEL_SHORT_MSG})
                except NotImplementedError:
                    out.put_nowait({"type": "error", "code": "not_ready", "message": "智能体尚未就绪（build_graph 未实现）"})
                except Exception as exc:
                    logger.exception("智能体运行异常（chat: %s）", message[:50])
                    # 用户可读分类（2026-11-16 优化批 2）：401/429/超时/超限
                    # 分别给操作建议，不再一律「详见后端日志」。
                    from services.model_providers import describe_llm_error
                    out.put_nowait({"type": "error", "code": "agent_error",
                                    "message": describe_llm_error(exc)})
                else:
                    for event in finalize_turn(turn_db, final):
                        out.put_nowait(event)
                    # 轮耗时观测（优化批 4）：速度问题从体感变数字——总耗时
                    # + 出口键一览（各 LLM 调用耗时已由 [llm] 单点日志覆盖）
                    logger.info(
                        "[chat] 轮完成：%.1fs，出口 %s",
                        _time.perf_counter() - _t0,
                        "、".join(sorted(k for k in (final or {}) if k in (
                            "final_reply", "draft_plan", "question_plan",
                            "applied_plan", "inbox_items", "delete_plan",
                            "preflight")) or "（空）"))
                    # 滚动上下文压缩（2026-10-20 增）：轮末 fire-and-forget，不占
                    # 对话时延；强引用池托管（schedule_compaction 裸名调用——
                    # test_chat_channels 的模块属性 patch 锚点）
                    schedule_compaction()
        except Exception:
            # 外层兜底：state 构造 / user 落库等图执行之前的意外故障（图执行
            # 段已有自己的翻译分支）——同样投错误帧，绝不静默 done
            logger.exception("轮次前置异常（chat: %s）", message[:50])
            out.put_nowait({"type": "error", "code": "agent_error", "message": "智能体运行异常，详见后端日志"})
        finally:
            lock.release()
            out.put_nowait(TURN_END)

    # 强引用池托管（sse_kit.spawn）：断连后轮次继续跑（锁自释、消息照落）
    sse_kit.spawn(asyncio.create_task(_turn()))
    while True:
        item = await out.get()
        if item is TURN_END:
            break
        yield item
