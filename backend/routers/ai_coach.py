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
    ProposalAcceptItemIn, ProposalDecideIn, ProposalDecideOut, ProposalSettleItemsIn,
    RuleConflictCheckIn,
    RuleConflictCheckOut,
    SessionCompactOut,
    _to_naive_local,
)
from services import ai_data, data_ingest, episodic, workflows
from services import proposal_decisions
from services import chat_turn
from services.ai_data import list_memory_rules
from services.chat_turn import (
    build_initial_state,
    finalize_turn as _finalize_turn,
    compact_history_async as _compact_history_async,
)
from services import sse_kit
from services.llm import NoActiveModelError, check_ai

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

def _load_graph():
    """兼容再导出（2026-10-23 轮次内核下沉 services/chat_turn.py）：测试 monkeypatch
    锚点 ai_coach._load_graph 保留——本模块内调用点写裸名，打模块属性即生效。"""
    return chat_turn.load_graph()


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
        gate = check_ai(db)
        if gate is not None:
            yield sse_kit.sse_frame({"type": "error", "code": gate.reason,
                                "message": gate.message})
            yield sse_kit.sse_frame({"type": "done"})
            return
        try:
            graph = _load_graph()
        except (ImportError, AttributeError):
            graph = None
        if graph is None:
            yield sse_kit.sse_frame({"type": "error", "code": "not_ready", "message": "智能体尚未就绪（agents/graph.py 手写区未实现）"})
            yield sse_kit.sse_frame({"type": "done"})
            return
        # 轮次编排下沉 chat_turn.stream_turn_events（2026-11-17 架构批 5）——
        # graph 在本层加载后传入：ai_coach._load_graph 的 monkeypatch 锚点保持
        # 命中（test_ai_coach 替身 graph 注入路径），router 只剩闸门/加载/帧序列化
        async for event in chat_turn.stream_turn_events(body.message, graph):
            yield sse_kit.sse_frame(event)
        yield sse_kit.sse_frame({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/meeting-minutes", response_model=ProposalOut, status_code=201)
def meeting_minutes(body: MinutesIn, db: Session = Depends(get_db)):
    """会后纪要提炼（2026-11-10 增）：纪要原文 → LLM 提炼行动项 → minutes
    建议卡 + 面板消息 + minutes 广播。def 端点（同步单次 LLM 秒级返回，跑
    线程池不卡事件循环，同 QuickAdd 复核同步先例）；浮层 / 聊天入口共用。
    AI 关 / 零激活 / 提炼失败（解析全空）→ 503；会议 id 无效 → 404。
    items 空仍建卡（"未识别出行动项"也是有效回执）。"""
    try:
        replay = proposal_decisions.invite_result(db, body)
    except KeyError as e:
        raise HTTPException(404, '邀约不存在') from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    if replay is not None:
        return replay
    gate = check_ai(db)
    if gate is not None:
        raise HTTPException(503, gate.message)
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
    payload = {
        "meeting_task_id": meeting.get("task_id"),
        "meeting_title": meeting.get("title"),
        "meeting_start": meeting.get("start"),
        "headline": headline,
        "items": items,
        # 纪要原文留档：proposals 行不删天然持久，浮层回看区块直接消费
        "source_text": body.text,
    }
    try:
        proposal, created = proposal_decisions.save_minutes(db, body, payload, reply or headline or '会后纪要已提炼')
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, '邀约不存在') from e
    if not created:
        return proposal
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
    # 位点统一归一为本地 naive（2026-11-16 优化批 1）：旧前端存的是 UTC 带 Z
    # 的 ISO 串，与 Message.created_at（本地 naive）直接比较会偏一个时区。
    return ai_data.list_messages(
        db, limit=limit, after=_to_naive_local(after) if after is not None else None
    )


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

    gate = check_ai(db)
    if gate is not None:
        return SessionCompactOut(compacted=0, reason=gate.reason)

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


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: str, db: Session = Depends(get_db)):
    row = ai_data.get_proposal(db, proposal_id)
    if row is None:
        raise HTTPException(404, '建议不存在')
    return row


@router.post("/proposals/{proposal_id}/decide", response_model=ProposalDecideOut)
def decide_proposal(proposal_id: str, body: ProposalDecideIn, db: Session = Depends(get_db)):
    try:
        return proposal_decisions.decide(db, proposal_id, [it.model_dump(exclude_none=True) for it in body.items])
    except KeyError as e:
        raise HTTPException(404, '建议不存在') from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/proposals/{proposal_id}/outcome", response_model=ProposalOut)
def resolve_proposal(proposal_id: str, body: ProposalOutcomeIn, db: Session = Depends(get_db)):
    try:
        return proposal_decisions.resolve(db, proposal_id, body.outcome, body.reason)
    except KeyError as e:
        raise HTTPException(404, '建议不存在') from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/proposals/{proposal_id}/accept-item", response_model=ProposalOut)
def accept_item(proposal_id: str, body: ProposalAcceptItemIn, db: Session = Depends(get_db)):
    # 老协议有完整原始快照：可安全转换成稳定标识，拒绝错位请求。
    card = ai_data.get_proposal(db, proposal_id)
    if card is None:
        raise HTTPException(404, '建议不存在')
    stable = proposal_decisions.ids(proposal_id, body.expected)
    original = (card.payload.get(proposal_decisions.STATE) or {}).get('ids', card.item_ids)
    if body.index >= len(stable) or stable != original:
        raise HTTPException(409, '建议已变更')
    try:
        result = proposal_decisions.decide(db, proposal_id, [{'item_id': stable[body.index], 'action':'accept'}])
        return result['proposal']
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, '建议不存在') from e


@router.post("/proposals/{proposal_id}/settle-items", deprecated=True)
def settle_proposal_items(proposal_id: str, body: ProposalSettleItemsIn):
    raise HTTPException(409, '旧版条目序号不能安全重试，请刷新客户端后按条目标识结算')


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
    """工作流事件流（晨间对齐 / 夜间整理 / 负荷熔断），协议同提醒流
    （帧与退订时序收在 sse_kit.subscription_response 单源）。"""
    return sse_kit.subscription_response(workflows.subscribe, workflows.unsubscribe)
