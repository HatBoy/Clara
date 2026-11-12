"""阶段二：AI 助手接口（对话 SSE / 历史 / 建议回写 / 记忆画像 CRUD / 工作流事件流）。

薄壳层——组装 initial_state、把手写区 graph 的输出翻译成 SSE 事件、持久化
会话与建议；推理逻辑全在手写区 agents/（docs/ai-agents.md 分工）。SSE 协议沿用
routers/reminders.py 先例（": connected" 注释行 + "data: {json}" 帧）。2026-10-16
起 qa / smalltalk 分支持 token 级流式（graph.stream 双通道 + 跨线程队列桥，见
chat 端点内注释）；json_mode 分支（排期/教练/删除/采集）输出结构化 JSON 无法
半截消费，保持整段事件。

POST /api/ai/chat 事件协议（data 载荷 type 字段）：
  message   {role:"user", content}          用户输入回显（已持久化）
  delta     {text}                          回复正文增量（打字机；message 后 reply 前
                                            到达，仅 qa / smalltalk 流式分支产生）
  reply     {content}                       final_reply 整段（权威文本，前端以它替换
                                            delta 累积）
  draft     {proposal_id, draft_plan}       排期草案（幽灵方块，kind=schedule 已建行）
  proposal  {proposal_id, kind, payload}    结构化建议卡片（preflight 等）
  applied   {proposal_id, tasks}            快录直落结果（origin=user 过全链校验已写库，
                                            kind=schedule 行直置 accepted + payload.auto，
                                            前端刷新画布；2026-10-17 增）
  done      {}                              流结束（正常与错误路径都收尾）
  error     {code, message}                 code ∈ no_model / not_ready / disabled / agent_error
    no_model    零激活模型（前端显示"配置模型后解锁"引导卡，点击直达模型接入设置）
    not_ready   手写区桩未实现（build_graph 就绪前的过渡期常态）
    disabled    AI 主开关关闭
    agent_error graph 运行异常（含 litellm 原生异常向上抛的路径）
"""

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    ChatIn,
    EpisodicFactOut,
    MemoryRuleCreate,
    MemoryRuleOut,
    MemoryRuleUpdate,
    MessageOut,
    MinutesIn,
    PhilosophyOut,
    ProposalOut,
    ProposalOutcomeIn,
    ProposalSettleItemsIn,
    RuleConflictCheckIn,
    RuleConflictCheckOut,
    SessionCompactOut,
)
from services import ai_data, data_ingest, episodic, workflows
from services import chat_turn
from services.ai_data import list_memory_rules
from services.chat_turn import (
    build_initial_state,
    finalize_turn as _finalize_turn,
    compact_history_async as _compact_history_async,
)
from services.llm import NoActiveModelError
from services.model_providers import get_active_model
from services.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

# 工作流事件流的无事件保活间隔（秒，同 reminders 先例）
_PING_SECONDS = 30


class _StreamFailure:
    """工作线程 → 消费循环的失败信封：图执行异常原样包装入队，消费侧 raise 后
    落入 chat 端点既有的异常翻译分支（no_model / not_ready / agent_error）。"""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException):
        self.exc = exc


class _StreamDone:
    """完成哨兵：携带图末个 values 帧（终态全量 state，与 invoke 返回值同源同值）。
    图零 values 帧（理论不发生——qa/smalltalk 必写 final_reply）时 final 为 None，
    _finalize_turn 的 `final or {}` 兜底。"""

    __slots__ = ("final",)

    def __init__(self, final):
        self.final = final


# 轮次任务出队哨兵（2026-10-23 批 3）：轮次体全部事件已投递完毕——身份比较用
_TURN_END = object()

# 轮次任务强引用池（2026-10-23 批 3）：断连后轮次继续跑（锁自释、消息照落），
# asyncio 只持任务弱引用，无人引用可能在完成前被 GC 静默蒸发——同
# _bg_compact_tasks 教训
_turn_tasks: set[asyncio.Task] = set()


def _load_graph():
    """兼容再导出（2026-10-23 轮次内核下沉 services/chat_turn.py）：测试 monkeypatch
    锚点 ai_coach._load_graph 保留——本模块内调用点写裸名，打模块属性即生效。"""
    return chat_turn.load_graph()


def _event_bytes(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(body: ChatIn, db: Session = Depends(get_db)):
    """对话入口：SSE 事件流（前端 fetch 流消费——EventSource 不支持 POST）。

    2026-10-23 批 3 起：轮次跑在独立 asyncio 任务里、全程持有 chat_turn.TURN_LOCK
    （与远程渠道轮串行，防 pending 锚点与消息追加序竞争）；**断连不再丢本轮**——
    客户端断开只断 SSE 帧消费，轮次任务继续跑完落库（自开 SessionLocal：请求级
    Session 会随依赖清理关闭，断连后不可复用），锁在轮次 finally 释放。轮次事件
    经 out 队列投递到本生成器，断连后队列无人消费（无界队列，轮次不阻塞）。"""

    async def event_stream():
        yield ": connected\n\n"
        # 前置快查（纯读库毫秒级，留在事件循环线程；秒回路径不进锁）
        if not get_settings(db).ai_enabled:
            yield _event_bytes({"type": "error", "code": "disabled", "message": "AI 功能已在设置中关闭"})
            yield _event_bytes({"type": "done"})
            return
        if get_active_model(db) is None:
            yield _event_bytes({"type": "error", "code": "no_model", "message": "未激活任何模型，请到设置 → 模型接入中激活"})
            yield _event_bytes({"type": "done"})
            return
        try:
            graph = _load_graph()
        except (ImportError, AttributeError):
            graph = None
        if graph is None:
            yield _event_bytes({"type": "error", "code": "not_ready", "message": "智能体尚未就绪（agents/graph.py 手写区未实现）"})
            yield _event_bytes({"type": "done"})
            return

        out: asyncio.Queue = asyncio.Queue()
        lock = chat_turn.get_turn_lock()
        await lock.acquire()

        async def _turn() -> None:
            """轮次体：持锁跑完「构造 state → 落 user 消息 → 图执行 → 收尾落库」
            全程，事件（含错误帧）逐个投 out 队列，finally 释放锁 + 投哨兵。"""
            from database import SessionLocal

            try:
                import time as _time

                _t0 = _time.perf_counter()
                with SessionLocal() as turn_db:
                    state = build_initial_state(turn_db, body.message)
                    ai_data.append_message(turn_db, role="user", content=body.message)
                    logger.info("[chat] 新对话轮：%s", body.message[:60])
                    out.put_nowait({"type": "message", "role": "user", "content": body.message})

                    try:
                        # —— 图执行：stream 驱动 + 跨线程队列桥（2026-10-16 token 级流式改造） ——
                        # graph 同步阻塞（节点内 LLM 走 litellm 同步客户端）——仍在 to_thread
                        # 工作线程跑，免得卡住事件循环拖迟同进程的提醒调度。stream_mode 双通道
                        # （langgraph 1.2.11 源码验证过形态）：
                        # - "custom"：节点经 get_stream_writer 推的 delta 帧（nodes._delta_emitter
                        #   构造的 {"type":"delta","text":...}）——转发 out 队列，打字机体感，
                        #   且逐 token 实时到达（custom 模式的 waiter 机制，不积压到 superstep 末）；
                        # - "values"：每 superstep 后的全量 state，末帧 == invoke 返回值同源同值，
                        #   记为 final 交 _finalize_turn。
                        # 桥接：asyncio.Queue 非线程安全——工作线程经 call_soon_threadsafe 投递；
                        # 事件循环先关的极端窗口投递失败（RuntimeError）静默吞，工作线程自然退出。
                        # turn_db 全程只在事件循环线程使用（线程隔离铁律）。
                        queue: asyncio.Queue = asyncio.Queue()
                        loop = asyncio.get_running_loop()

                        def _drain() -> None:
                            final_local = None
                            try:
                                for mode, payload in graph().stream(state, stream_mode=["custom", "values"]):
                                    if mode == "custom":
                                        loop.call_soon_threadsafe(queue.put_nowait, payload)
                                    else:
                                        final_local = payload
                            except BaseException as exc:  # 消费侧 raise 后落入下方翻译分支
                                try:
                                    loop.call_soon_threadsafe(queue.put_nowait, _StreamFailure(exc))
                                except RuntimeError:
                                    pass
                            else:
                                try:
                                    loop.call_soon_threadsafe(queue.put_nowait, _StreamDone(final_local))
                                except RuntimeError:
                                    pass

                        producer = asyncio.create_task(asyncio.to_thread(_drain))
                        graph_exc: BaseException | None = None
                        final = None
                        try:
                            while True:
                                item = await queue.get()
                                if isinstance(item, _StreamFailure):
                                    graph_exc = item.exc
                                    break
                                if isinstance(item, _StreamDone):
                                    final = item.final
                                    break
                                out.put_nowait(item)  # custom 帧 = delta 事件
                            if graph_exc is not None:
                                raise graph_exc
                        finally:
                            producer.cancel()  # 图故障防任务泄漏；图线程不可中断，自然跑完丢弃
                    except NoActiveModelError:
                        out.put_nowait({"type": "error", "code": "no_model", "message": "未激活任何模型"})
                    except NotImplementedError:
                        out.put_nowait({"type": "error", "code": "not_ready", "message": "智能体尚未就绪（build_graph 未实现）"})
                    except Exception:
                        logger.exception("智能体运行异常（chat: %s）", body.message[:50])
                        out.put_nowait({"type": "error", "code": "agent_error", "message": "智能体运行异常，详见后端日志"})
                    else:
                        for event in _finalize_turn(turn_db, final):
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
                        # 对话时延；任务挂引用池防 GC（chat_turn.schedule_compaction 注释）
                        chat_turn.schedule_compaction()
            except Exception:
                # 外层兜底：state 构造 / user 落库等图执行之前的意外故障（图执行
                # 段已有自己的翻译分支）——同样投错误帧，绝不静默 done
                logger.exception("轮次前置异常（chat: %s）", body.message[:50])
                out.put_nowait({"type": "error", "code": "agent_error", "message": "智能体运行异常，详见后端日志"})
            finally:
                lock.release()
                out.put_nowait(_TURN_END)

        task = asyncio.create_task(_turn())
        _turn_tasks.add(task)
        task.add_done_callback(_turn_tasks.discard)
        try:
            while True:
                item = await out.get()
                if item is _TURN_END:
                    break
                yield _event_bytes(item)
        finally:
            # 断连（GeneratorExit）或正常退出：轮次任务不取消——跑完落库、锁自释
            pass
        yield _event_bytes({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/meeting-minutes", response_model=ProposalOut, status_code=201)
def meeting_minutes(body: MinutesIn, db: Session = Depends(get_db)):
    """会后纪要提炼（2026-11-10 增）：纪要原文 → LLM 提炼行动项 → minutes
    建议卡 + 面板消息 + minutes 广播。def 端点（同步单次 LLM 秒级返回，跑
    线程池不卡事件循环，同 QuickAdd 复核同步先例）；浮层 / 聊天入口共用。
    AI 关 / 零激活 / 提炼失败（解析全空）→ 503；会议 id 无效 → 404。
    items 空仍建卡（"未识别出行动项"也是有效回执）。"""
    s = get_settings(db)
    if not s.ai_enabled:
        raise HTTPException(503, "AI 功能已在设置中关闭")
    if get_active_model(db) is None:
        raise HTTPException(503, "未激活任何模型，请到设置 → 模型接入中激活")
    if body.task_id:
        from models import Task

        t = db.get(Task, body.task_id)
        if t is None or t.is_deleted:
            raise HTTPException(404, "会议日程不存在")
    from agents.graph import run_meeting_minutes

    result = run_meeting_minutes(db, body.task_id, body.text)
    headline = result.get("headline") or ""
    items = result.get("items") or []
    reply = result.get("final_reply") or ""
    if not headline and not items and not reply:
        # 解析全空 = 提炼失败——不建空壳卡（用户贴了纪要应有可消费产物）
        logger.warning("会后纪要提炼失败（解析全空），text=%s…", body.text[:50])
        raise HTTPException(503, "纪要提炼失败，请稍后重试")
    meeting = result.get("meeting") or {}
    proposal = ai_data.create_proposal(db, kind="minutes", payload={
        "meeting_task_id": meeting.get("task_id"),
        "meeting_title": meeting.get("title"),
        "meeting_start": meeting.get("start"),
        "headline": headline,
        "items": items,
        # 纪要原文留档：proposals 行不删天然持久，浮层回看区块直接消费
        "source_text": body.text,
    })
    ai_data.append_message(db, role="assistant",
                           content=reply or headline or "会后纪要已提炼",
                           proposal_id=proposal.id)
    title_part = f"《{meeting['title']}》" if meeting.get("title") else ""
    workflows.broadcast({
        "type": "minutes",
        "headline": f"{title_part}纪要已提炼 {len(items)} 条行动项",
        "proposal_id": proposal.id,
    })
    logger.info("会后纪要提炼完成：%d 条行动项，建议卡 %s", len(items), proposal.id)
    return proposal


@router.get("/messages", response_model=list[MessageOut])
def get_messages(limit: int = 200, after: datetime | None = None,
                 db: Session = Depends(get_db)):
    """面板启动加载会话历史（时间正序）。after：未读位点恢复（2026-11-04 通知
    分轨——前端 localStorage 存「上次面板可见时刻」，启动时取此后到达的消息计
    未读徽标初始值，重启不丢感知）。"""
    return ai_data.list_messages(db, limit=limit, after=after)


@router.post("/session/clear", status_code=204)
async def clear_session(db: Session = Depends(get_db)):
    """清空会话（前端 /clear 命令直调，不经 LLM）：消息全删 + 滚动摘要游标重置；
    建议行保留（待办建议不是对话内容）。持轮次锁执行——防清库撞上在途轮的
    消息追加与锚点判定（2026-10-23 批 3）。"""
    async with chat_turn.get_turn_lock():
        ai_data.clear_chat_session(db)


def _compact_force() -> int:
    """/compact 线程体（测试锚点，同 _compact_history_async 再导出先例）：自开
    SessionLocal 跑强制压缩——LLM 同步调用走 to_thread 工作线程，线程隔离铁律
    禁止复用请求级 Session。"""
    from agents.graph import compact_chat_history
    from database import SessionLocal

    with SessionLocal() as db:
        return compact_chat_history(db, force=True)


@router.post("/session/compact", response_model=SessionCompactOut)
async def compact_session(db: Session = Depends(get_db)):
    """手动压缩（前端 /compact 命令直调，2026-10-26 增）：强制把全部未摘要消息
    并入滚动摘要——不受自动压缩开关与双阈值限制（手动指令语义），切片口径不变
    仍保留最近 K 条原文。持轮次锁与在途轮串行；进锁前秒查 disabled / no_model
    （照 chat 前置快查先例）。端点永不 5xx：块级中断翻译 partial（部分成果已
    落库，再执行从游标断点续压），其余异常翻译 error。"""
    from services.ai_data import CompactPartialError

    if not get_settings(db).ai_enabled:
        return SessionCompactOut(compacted=0, reason="disabled")
    if get_active_model(db) is None:
        return SessionCompactOut(compacted=0, reason="no_model")

    async with chat_turn.get_turn_lock():
        try:
            compacted = await asyncio.to_thread(_compact_force)
        except CompactPartialError as e:
            return SessionCompactOut(compacted=e.done, reason="partial")
        except NoActiveModelError:  # 秒查与压缩执行之间的去激活竞态
            return SessionCompactOut(compacted=0, reason="no_model")
        except Exception:
            logger.exception("手动压缩执行异常")
            return SessionCompactOut(compacted=0, reason="error")
    return SessionCompactOut(compacted=compacted)


@router.get("/philosophies", response_model=list[PhilosophyOut])
def get_philosophies():
    """内置管理哲学预设清单（个性化设置页卡片；text 不出——提示词只留后端单源）。"""
    from agents.prompts import PHILOSOPHIES

    return [PhilosophyOut(id=p["id"], name=p["name"], desc=p["desc"],
                          conflicts=p.get("conflicts", [])) for p in PHILOSOPHIES]


@router.get("/proposals", response_model=list[ProposalOut])
def get_proposals(unresolved: bool = False, limit: int = 50, db: Session = Depends(get_db)):
    """建议列表（倒序最新在前；unresolved=true 只看待处理——晨间确认夜间草案用）。"""
    return ai_data.list_proposals(db, unresolved_only=unresolved, limit=limit)


@router.post("/proposals/{proposal_id}/outcome", response_model=ProposalOut)
def resolve_proposal(proposal_id: str, body: ProposalOutcomeIn, db: Session = Depends(get_db)):
    """建议回写（采纳/修改后采纳/拒绝/忽略）；拒绝可选填理由（习惯学习信号）。
    拒绝的采集类建议同步落台账永静默（on_proposal_settled——转发不是业务，同
    settings 先例）。"""
    row = ai_data.record_proposal_outcome(db, proposal_id, body.outcome, reason=body.reason,
                                          applied_entries=body.applied_entries)
    if row is None:
        raise HTTPException(status_code=404, detail="建议不存在")
    data_ingest.on_proposal_settled(db, row.kind, row.payload or {}, body.outcome)
    return row


@router.post("/proposals/{proposal_id}/settle-items", response_model=ProposalOut)
def settle_proposal_items(proposal_id: str, body: ProposalSettleItemsIn, db: Session = Depends(get_db)):
    """条目级部分结算（2026-10-24 数据源页单条操作）：从待处理卡移除指定条目
    （payload 收缩，卡清空才按 outcome 回写整卡）；拒绝只静默被拒条目的指纹
    （on_proposal_settled 喂 mini payload——同卡其余条目不受牵连）。"""
    try:
        result = ai_data.settle_proposal_items(db, proposal_id, body.indexes, body.outcome,
                                               applied_entries=body.applied_entries)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail="建议已结算或非待处理卡" if str(e) == "settled" else "条目序号越界",
        )
    if result is None:
        raise HTTPException(status_code=404, detail="建议不存在")
    row, mini = result
    data_ingest.on_proposal_settled(db, row.kind, mini, body.outcome)
    return row


# —— 记忆画像 CRUD（设置 → 记忆画像白盒界面；PRD §4.5） ——


@router.get("/memory-rules", response_model=list[MemoryRuleOut])
def list_rules(db: Session = Depends(get_db)):
    return list_memory_rules(db)


@router.post("/memory-rules/conflict-check", response_model=RuleConflictCheckOut)
def check_rule_conflict(body: RuleConflictCheckIn, db: Session = Depends(get_db)):
    """手动新增/编辑保存前的冲突预检（2026-11-03 记忆重构，用户拍板「冲突提示
    用户确认」）：纯规则 difflib 相似度（≥0.6，复用排期链查重口径）零 LLM 秒回；
    字面量路由先于参数路由注册（同 data-cli 先例）。"""
    return RuleConflictCheckOut(
        conflicts=ai_data.find_rule_conflicts(
            db, category=body.category, content=body.content, exclude_id=body.exclude_id))


@router.post("/memory-rules", response_model=MemoryRuleOut, status_code=201)
def create_rule(body: MemoryRuleCreate, db: Session = Depends(get_db)):
    try:
        return ai_data.create_memory_rule(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.patch("/memory-rules/{rule_id}", response_model=MemoryRuleOut)
def update_rule(rule_id: str, body: MemoryRuleUpdate, db: Session = Depends(get_db)):
    row = ai_data.update_memory_rule(db, rule_id, body)
    if row is None:
        raise HTTPException(status_code=404, detail="记忆规则不存在")
    return row


@router.delete("/memory-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    # dismiss=True：用户主动删除落纠偏账（AI 不得再推同义规则）
    if not ai_data.delete_memory_rule(db, rule_id, dismiss=True):
        raise HTTPException(status_code=404, detail="记忆规则不存在")


# —— 情节事实（Tier 2 白盒清单，2026-11-03 记忆重构批 6）——
# 不建 POST/PATCH：事实只由 AI 链（对话同跳提取 / 夜间反思兜底）落库，手动只删
#（同 GET /philosophies「text 不出提示词」白盒边界先例）


@router.get("/episodic-facts", response_model=list[EpisodicFactOut])
def list_facts(db: Session = Depends(get_db)):
    return episodic.list_episodic_facts(db)


@router.delete("/episodic-facts/{fact_id}", status_code=204)
def delete_fact(fact_id: str, db: Session = Depends(get_db)):
    if not episodic.delete_fact(db, fact_id):
        raise HTTPException(status_code=404, detail="事实不存在")


@router.get("/stream")
async def stream_workflow_events():
    """工作流事件流（晨间对齐 / 夜间整理 / 负荷熔断），协议同提醒流。"""
    q = workflows.subscribe()

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _event_bytes(event)
        finally:
            workflows.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
