"""LangGraph 节点函数（阶段二实现，graph.py 七分支的执行体都在这）。

阅读路线：center_router（一切 chat 的第一跳：意图分类）→ 按分支读对应节点——
schedule → 排期子图 agents/scheduler/（强度标注已内联 task_intensity，2026-10-21 复查批 A
撤销 intensity_tagger 中转节点——其输出零消费还每轮白付一次收件箱全量查询）；
qa → qa_answerer；coach → strategist 子图 agents/strategist/（包装节点
strategist_node 在 graph.py）；smalltalk → smalltalk_node；
delete → delete_confirmer；ingest 分支的 collect_data / refine 在 graph.py
（与定时 run_ingest 同体）。

分层红线：读数据一律调 services/（如 services.tasks.list_tasks），
不在 node 里重写查询、不 import routers——阶段一的业务逻辑在此复用。

统一签名形状：def node_x(state: AgentState) -> dict（同步函数，返回增量 dict 由
LangGraph 合并进 State；内部 LLM 调用只经 services.llm——call_llm 整段 /
call_llm_stream 流式姊妹（qa / smalltalk 的 token 级打字机），LangGraph 自将
同步节点扔 executor，事件循环不被卡）。

异常分流口径（全节点一致，docs/ai-agents.md §五）：
- 解析失败是数据——loads_obj 返回 None / 字段非法 → 兜底降级（smalltalk、
  纯文本回复、空卡），绝不 raise；
- LLM 调用失败是故障——call_llm 裸奔不 try，异常原样上抛，由壳（ai_coach 译
  agent_error 帧 / workflows 记日志）统一兜底。
"""

import difflib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from agents.parsing import is_int, loads_obj
from agents.prompts import (
    DELETE_PROMPT,
    QA_PROMPT,
    ROUTER_PROMPT,
    SMALLTALK_PROMPT,
    assemble_system_prompt,
)
from agents.prompts import build_settings_prompt  # 独立行：函数依赖 services 层，构建时才解析（延迟闭环）
from agents.state import AgentState
from database import SessionLocal
from langgraph.config import get_stream_writer

# 节点流程打点（2026-10-22 真机排障需求）：[router] 意图判定 / 分支节点入口——
# 与 services 层数据日志、[llm] 调用日志拼出完整智能体运行轨迹
logger = logging.getLogger(__name__)

# 半开区间重叠判定是 planner 撞期/午休门的公共小件（graph.py 也引用
# nodes._overlaps）；找空档/校验/LLM 微调已收口 services.tasks 与 agents/engine
# （2026-10-20 统一引擎批清退时间决策别名，agents→services 是许可方向，无环）。
from services.tasks import overlaps as _overlaps

# 合法意图集（与 prompts.ROUTER_PROMPT 的七分类、graph._INTENT_EDGES 一一对应）
_INTENTS = frozenset({"schedule", "qa", "coach", "smalltalk", "ingest", "delete", "settings"})

# QA 输入上下文钳制：行数与 notes 长度（上下文溢出的前置拦截在 llm.py，
# 这里先从源头省——50 行紧凑清单足够回答绝大多数日程问题）
_QA_MAX_ROWS = 50
_QA_INBOX_ROWS = 20  # 收件箱分口径保底配额（真机批增，见 _qa_task_lines 注释）
_QA_NOTES_CHARS = 60
# 优先级 → 中文标签（1=高 2=中 3=低，与 schemas.PRIORITY_INT 一致）
_PRIORITY_LABELS = {1: "高", 2: "中", 3: "低"}
# 滚动摘要注入前缀（qa/smalltalk 经 _with_chat_history 同款拼接——2026-10-21
# 复查批 A 提常量，改文案只动一处）
_CHAT_SUMMARY_PREFIX = "（更早对话的脉络摘要，供衔接参考）"


def _delta_emitter() -> Callable[[str], None]:
    """构造 token 增量 → delta 帧的回调（qa / smalltalk 流式共用，2026-10-16 增）。

    返回的回调把每段正文增量包成 {"type": "delta", "text": ...} 交给
    langgraph 的 StreamWriter——壳（ai_coach）以 stream(stream_mode=["custom",
    "values"]) 驱动图时，custom 帧被原样转发为 SSE delta 事件（前端打字机）。

    两种退化形态都安全（langgraph 1.2.11 源码验证）：
    - 节点被直接调用（单测形态）→ get_stream_writer 抛 RuntimeError → 静默 no-op；
    - invoke() 驱动（练习脚本等）→ writer 是官方 no-op 函数，调用无副作用。
    即同一份节点代码在流式 / 非流式驱动下行为一致，仅差打字机体感。
    """
    try:
        writer = get_stream_writer()
    except Exception:
        return lambda _text: None
    return lambda text: writer({"type": "delta", "text": text})


def _status_emitter() -> Callable[[str], None]:
    """构造阶段文案 → status 帧的回调（过程反馈，2026-10-26 提速批 2 增）。

    非流式分支（schedule / coach / delete / ingest）json_mode 全程零输出——
    前端只有静态「思考中…」干等，一次 LLM 卡住即整段黑屏。重节点入口推
    {"type":"status","text":...} 一帧，前端把「思考中…」替换为当前阶段文案。
    SSE additive：壳对 custom 帧全量透传（ai_coach._drain），旧前端忽略未知
    帧无破坏；远程渠道 invoke 驱动 writer 是官方 no-op 零副作用。双退化
    防御同 _delta_emitter（直调 RuntimeError → no-op；invoke → no-op writer）。
    """
    try:
        writer = get_stream_writer()
    except Exception:
        return lambda _text: None
    return lambda text: writer({"type": "status", "text": text})


def _history_dicts(state: AgentState) -> tuple[str | None, list[dict]]:
    """读取对话上下文并转为消息字典（qa / smalltalk / coach 三节点共用，2026-10-20 增）：
    (滚动摘要 | None, [{"role","content"}…])——每条 content 截 CHAT_MSG_CHARS，
    exclude 本轮原话（壳在图运行前已落库，不剔则与 user 载荷重复注入）。
    注入窗按激活模型档位自适应（chat_recent_k，2026-10-26）。"""
    from services.ai_data import CHAT_MSG_CHARS, chat_context, chat_recent_k

    with SessionLocal() as db:
        summary, rows = chat_context(db, recent_k=chat_recent_k(db),
                                     exclude=state["user_input"])
    dicts = [{"role": m.role, "content": (m.content or "")[:CHAT_MSG_CHARS]} for m in rows]
    return summary, dicts


def _with_chat_history(state: AgentState, system_content: str, user_content: str) -> list[dict]:
    """自然语言节点的消息数组组装（qa / smalltalk 共用，2026-10-20 增）：
    [system(+滚动摘要)] + [近期历史（每条截 CHAT_MSG_CHARS）] + [user 本轮载荷]。

    摘要非空追加 system 尾部（不改 assemble_system_prompt 签名——那是记忆规则
    通道，滚动摘要是对话状态，语义不同）；历史经 chat_context 读取（exclude
    本轮原话——壳在图运行前已落库，不剔则与 user 载荷重复注入）。读库失败
    不另设防：与 list_tasks 同源同库，库坏则本轮本就无解，退化逻辑交给壳。
    call_llm_stream 与数组长度无耦合（逐条透传 provider），流式不受影响。"""
    summary, history = _history_dicts(state)
    if summary:
        system_content = f"{system_content}\n\n{_CHAT_SUMMARY_PREFIX}\n{summary}"
    return (
        [{"role": "system", "content": system_content}]
        + history
        + [{"role": "user", "content": user_content}]
    )


# —— 规则层预路由（2026-10-21 真机 bug 增）：排期强动词 ∧ 时间表达 ∧ 无疑问词 ——
# LLM 路由把"我安排明天下午15点爬山"误判 smalltalk 后，闲聊分支不读库不产卡，
# 模型凭空编"已安排/与现有日程无冲突"（谎报零防线）——确定性排期诉求规则层
# 兜住，零 LLM 零延迟。疑问句（"今天有什么安排？"）必含疑问词不误吞；弱表述
# （"明天上午去爬山"无动词）仍走 LLM 七分类。
#
# 预路由/短路优先级矩阵（2026-11-01 优化批 1 固化——七套正则的先后与互斥从
# 散落注释收拢为单源，改任何一条前先对照本表；test_agents_nodes 预路由组与
# 之互锁）：
# ⓪ confirm_shortcut（图第一跳，center_router 之前）：草案/删除确认短句，
#    $ 全锚防误吞调整语——最优先（消息锚点已锁定，语义唯一）
# ① schedule 预路由：强动词 ∧ 时间 ∧ 无疑问 ∧ 无删除，pending 空才判
#    （问询回复轮语境复杂，交 LLM 全量判断）
# ② 批量支路：范围+待办名词+动词 ∧ 无疑问 ∧ 无删除，pending 无关
#    （换意图语义，旧问询由 profiler 批量短路结算）
# ③ qa 预路由：疑问词 ∧ 域词 ∧ 无删除 ∧ 无排期动词形态（名词守卫），pending 空才判
# ④ 幕僚确认词表：origin=strategist 短句 $ 全锚直达 coach
# ⓪-④ 全不命中 → LLM 七分类（pending 前缀 / 近期对话注入照常拼）
# 互斥要点：疑问词优先于排期动词（①必无疑问）；删除动词优先于一切排期语义
# （①②③均排除）；批量词优先于问询 pending（②pending 无关）。
_SCHEDULE_VERB_RE = re.compile(
    r"安排|排|预约|订|记一下|记着|提醒我|加.{0,4}(日程|提醒|事项)")
_TIME_HINT_RE = re.compile(
    r"今天|明天|后天|大后天|下周|本周|这周|周[一二三四五六日天]|\d{1,2}\s*[点时:：]"
    r"|\d{1,2}\s*[号日]月?|上午|下午|中午|晚上|傍晚|早上|清晨|凌晨")
_QUESTION_RE = re.compile(r"[?？]|什么|怎么|如何|哪|多少|几|吗|呢|有没有")
# 删除/否定动词排除：句中含删除诉求时动词形态是名词（"牙医预约"）或否定
# （"别安排了"），删除语义交 LLM——排期预路由不抢
_DELETE_VERB_RE = re.compile(r"删|取消|移除|退订|退掉|别.{0,3}(排|订|安排|预约)")
# 长期偏好/否定词守卫（2026-11-02 真机三轮）：「以后…不要安排」「周五下午
# 别再排事」类表达是 settings 意图（prefer/unprefer 才落库），预路由抢判
# schedule 后 profiler 的 llm_reply 会编造"已记下"零持久化——命中即交 LLM
_PREF_NEG_RE = re.compile(r"以后|长期|默认|从来|一向|不要|别再|别在|不准|不许")
# qa 预路由（2026-10-26 提速批 3）：疑问词 ∧ 日历域名词 → 直判 qa 零 LLM——
# 「今天有什么安排」「明天忙吗」这类高频查询句不再每轮烧 1-3s 路由分类。
# 疑问词表刻意不含「怎么/如何」与问号（求思路是 coach 语义，「怎么安排这个
# 任务？」会绕过实词排除——中文问句几乎都带问号，问号无区分力；「怎么样」
# 整词是询问状态非求思路，收进表）。排期强动词排除（2026-11-01 优化批 1）：
# 「安排」既是强动词又是域词——「帮我安排明天下午的会可以吗」疑问词+域词双
# 命中但语义是排期试探，直判 qa 会错过撞期检查与草案卡；含强动词的疑问句
# 交 LLM 七分类（与 ① 的边界由此闭合：无疑问有动词时间 → schedule 零 LLM；
# 有疑问有动词 → LLM；有疑问无动词 → qa 零 LLM）
_QA_HINT_RE = re.compile(r"什么|哪|多少|几|吗|呢|有没有|怎么样")
_QA_DOMAIN_RE = re.compile(r"安排|日程|任务|待办|忙|空|提醒|天气|行程")
# qa 预路由的排期动词排除表（2026-11-01 优化批 1）：「安排/预约」双词性——
# 「今天有什么安排」「我的预约」里是名词（qa 高频查询句），裸词排除会误伤；
# 负向后顾（什么/的）+ 前瞻（有）守卫名词形态，命中即动词形态交 LLM 判
# schedule（排期试探句直判 qa 会错过撞期检查与草案卡）
_QA_VERB_EXCLUDE_RE = re.compile(
    r"(?<!什么)(?<!的)安排(?!有)|(?<!什么)(?<!的)预约"
    r"|记一下|记着|提醒我|加.{0,4}(日程|提醒|事项)|帮我排|排[到进一]")
# 会后纪要形态（2026-11-10 批 2）：纪要词 + 长文本（含换行或超阈值）——
# 「开完会了，纪要：\n……」直通 minutes 提炼零 router LLM。长文本门槛防短句
# 误吞（「帮我写会议纪要」是代笔诉求走幕僚长，长度不达标不命中）；判定必须
# 在 schedule 预路由之前——纪要正文里高频出现「安排/明天」类词，先判 schedule
# 会把整段纪要当排期指令吞掉。阈值 80→30（2026-11-11 真机反馈）：简单会议
# 几条关键结论常不足 80 字被漏走 LLM 兜底；典型代笔诉求普遍 <30 字仍挡得住，
# 含换行不受限
_MINUTES_HINT_RE = re.compile(r"纪要|开完会|会后行动|会后安排")
_MINUTES_LONG_MIN = 30

# 幕僚长问询消费轮的确认/拒绝短句词表（Track B 批 5，pending origin=strategist
# 时零 LLM 直达 coach；$ 全锚——"算了，就这样吧"这类带尾巴的句子留给 LLM 判，
# 宁多一次调用不误吞新话题；_CHAT_ACK_RE 先例的幕僚版，拒绝词是幕僚特有：
# router 规则 2 会把"不用了/算了"硬判 schedule，对幕僚问询是误路由）
_STRATEGIST_ACK_RE = re.compile(
    r"^(同意|可以|好的?|行|嗯+|确认|没问题|写入?吧?|采纳|ok|OK)[的了啊呀哈~～!！.。\s]*$")
_STRATEGIST_REFUSE_RE = re.compile(
    r"^(不用|算了|不写|不要|取消|拒绝|先不|不用写)[了了吧啊呀~～!！.。\s]*$")


# —— 草案/删除确认短路（2026-10-23 聊天通讯渠道配套）——————————————————————
# 远程渠道（飞书/微信等）没有卡片按钮，草案/删除卡的确认走对话式消费；
# 桌面对话回「确认」同享（与点卡片采纳等价）。fullmatch 词表（$ 锚）防误吞
# 调整语——"可以，改成下午3点"不命中，留给排期管道出新草案顶替锚点。
_DRAFT_ACK_RE = re.compile(
    r"^(同意|可以|好的?|行|嗯+|确认|没问题|写入?吧?|采纳|就这么办|就这么安排|"
    r"安排吧|ok|OK)[的了啊呀哈~～!！.。\s]*$")
_DRAFT_DECLINE_RE = re.compile(
    r"^(不用写?|算了|取消|放弃|先不用|先不|(不|先不?)(排|删|写|要)了?)"
    r"[了了吧啊呀~～!！.。\s]*$")
_DELETE_ACK_RE = re.compile(
    r"^(确认删除|删除吧|删掉吧|删了吧|删了|删除|删掉|同意|可以|好的?|行|嗯+|确认|"
    r"没问题|ok|OK)[的了啊呀哈~～!！.。\s]*$")


def confirm_shortcut(state: AgentState) -> dict:
    """草案/删除确认短路节点（图第一跳，2026-10-23 增）：活跃草案卡/删除
    确认卡 + 确认或拒绝短句 → 零 LLM 秒回终态，完全复用壳既有出口——草案
    确认走 applied_plan（apply_plan_entries 同款落库）、拒绝走 resolved_proposal
    declined；删除确认在节点内调 services.tasks.delete_task 真删（锚点行已
    锁定目标，等价于桌面点「确认删除」；classifier memory-agree 节点内写库
    同款先例），行 outcome 由壳 finalize 回写。锚点唯一（消息锚点机制）保证
    draft 与 delete 互斥、不与问询 pending 抢轮。不命中返回 {} 走
    center_router 正常七分类。LLM 调用数 0。

    输入：state["user_input"]。输出：{resolved_proposal, applied_plan,
    final_reply} 或 {}。"""
    from services import ai_data, tasks as tasks_svc
    # SessionLocal 函数内 import（非模块级）：conftest autouse 替换的是
    # database 模块属性，调用时解析才吃到测试库（center_router 同款纪律）
    from database import SessionLocal

    raw = (state.get("user_input") or "").strip()
    with SessionLocal() as db:
        draft = ai_data.active_draft(db)
        pending_delete = ai_data.active_delete(db)
        if draft is not None:
            if _DRAFT_ACK_RE.match(raw):
                entries = draft["draft_plan"]
                titles = "、".join(str(e.get("title") or "").strip()
                                   for e in entries[:3] if e.get("title"))
                more = f"等 {len(entries)} 条" if len(entries) > 3 else ""
                logger.info("[shortcut] 草案确认短路：%d 条落库（行 %s）",
                            len(entries), draft["proposal_id"])
                return {
                    "resolved_proposal": {"proposal_id": draft["proposal_id"],
                                          "outcome": "accepted"},
                    "applied_plan": entries,
                    "final_reply": f"已安排：{titles}{more}，已写入日历。",
                }
            if _DRAFT_DECLINE_RE.match(raw):
                logger.info("[shortcut] 草案拒绝短路（行 %s）", draft["proposal_id"])
                return {
                    "resolved_proposal": {"proposal_id": draft["proposal_id"],
                                          "outcome": "declined"},
                    "final_reply": "好的，草案先撤下，需要时随时再说。",
                }
        if pending_delete is not None:
            if _DELETE_ACK_RE.match(raw):
                deleted, missing = [], []
                for t in pending_delete["tasks"]:
                    if tasks_svc.delete_task(db, t["task_id"]):
                        deleted.append(str(t.get("title") or t["task_id"]))
                    else:
                        missing.append(str(t.get("title") or t["task_id"]))
                logger.info("[shortcut] 删除确认短路：删 %d 条（行 %s）",
                            len(deleted), pending_delete["proposal_id"])
                reply = ""
                if deleted:
                    reply += f"已删除：{'、'.join(deleted)}。"
                if missing:
                    reply += f"未找到：{'、'.join(missing)}（可能已被删除）。"
                return {
                    "resolved_proposal": {"proposal_id": pending_delete["proposal_id"],
                                          "outcome": "accepted"},
                    "final_reply": reply or "没有可删除的任务。",
                }
            if _DRAFT_DECLINE_RE.match(raw):
                logger.info("[shortcut] 删除拒绝短路（行 %s）",
                            pending_delete["proposal_id"])
                return {
                    "resolved_proposal": {"proposal_id": pending_delete["proposal_id"],
                                          "outcome": "declined"},
                    "final_reply": "好的，不删了，任务保留。",
                }
    return {}


def center_router(state: AgentState) -> dict:
    """中枢路由节点：用户输入 → 意图七分类，图的第一跳（一切 chat 请求经此分发）。

    输入：state["user_input"]（壳注入）；state["pending"] 非空（活跃问询）时
    把问询原句拼进 user content 前缀——"（待确认问询：…）"让路由器把"可以/
    改到周三/不用了"这类短回复正确判进 schedule（2026-10-17 增）。
    另注近期对话前缀（2026-10-23 增）：走 LLM 分类时读近 6 条历史（与 intake
    recent_chat 同窗）拼进 user content——"安排一下/帮我安排"这类省略式确认
    单看本轮无法判 intent，qa/smalltalk 早已注入历史而第一跳路由看不见，
    用户明确确认排期却三连落闲聊（真机教训）；只注近程历史不注滚动摘要
    （七分类只需衔接不需脉络，第一跳调用保持小）。
    输出：{"intent": str}——只写 intent 一个键，由 graph 的条件边
    _route_by_intent 消费（schedule / qa / coach / smalltalk / ingest / delete / settings）。
    设计原因：自然语言的意图多样性只能靠 LLM 分类（关键词枚举永远不全），
    但分类只需短输出，max_tokens=100 的小调用。LLM 调用数 1（分支节点另计）。
    解析失败/非法值兜底 smalltalk——路由错了最坏是答非所问的闲聊，不能炸链
    （坑 ⑥）；call_llm 本身异常（零激活/网络）则原样上抛交壳翻译——故障就是
    故障，不静默吞。规则层预路由（2026-10-21 增）：无活跃问询且原话命中
    "排期强动词 + 时间表达 + 无疑问词"时直接判 schedule 零 LLM——堵 LLM 误判
    smalltalk 后模型凭空谎报"已安排/无冲突"的通路（见 _SCHEDULE_VERB_RE 注释）；
    问询回复（pending 非空）语义复杂不走规则。批量意图支路（2026-10-25 真机
    修复）：「把所有待办排期」无时间表达罩不住 → LLM 误判 coach → 幕僚长出
    clarify 追问"把具体待办发给我"死循环——批量词全命中即直判 schedule 且
    pending 无关（换意图语义，旧问询由 profiler 批量短路结算）。
    """
    from services.llm import call_llm_aux

    content = state["user_input"]
    pending = state.get("pending")
    if not pending:
        raw = state["user_input"].strip()
        # 会后纪要形态（2026-11-10 批 2）：必须在 schedule 预路由之前——纪要
        # 正文高频出现「安排/明天」类词，后判会被 schedule 吞走整段纪要；长文本
        # 门槛防短句误吞（「帮我写会议纪要」是代笔诉求走幕僚长）；删除动词在场
        # 交 LLM（「把刚才的纪要删了」）
        if (_MINUTES_HINT_RE.search(raw)
                and ("\n" in raw or len(raw) > _MINUTES_LONG_MIN)
                and not _DELETE_VERB_RE.search(raw)):
            logger.info("[router] 规则预路由 → minutes（零 LLM）")
            return {"intent": "minutes"}
        # 危机词守卫（2026-11-05 真机批）：「项目今天下午就要交付…全乱了，帮我
        # 重新安排」动词+时间双命中被预路由 schedule → 幕僚长的危机重组（classifier
        # _CRISIS_RE）永远接不到——带危机情绪的排期句交 LLM 全量判定（ROUTER
        # 规则：危机情绪一律 coach），词表单源在 classifier
        from agents.strategist.classifier import _CRISIS_RE

        if (_SCHEDULE_VERB_RE.search(raw) and _TIME_HINT_RE.search(raw)
                and not _QUESTION_RE.search(raw) and not _DELETE_VERB_RE.search(raw)
                # 长期偏好守卫（2026-11-02 真机三轮）：「以后工作日早上八点之前
                # 不要给我安排事情」动词+时间双命中被预路由 schedule → profiler
                # 把它当非具体排期、llm_reply 编造"已记下作息规则"零落库——
                # 长期/否定词在场交 LLM 全量判定（settings 意图才落库）
                and not _PREF_NEG_RE.search(raw)
                and not _CRISIS_RE.search(raw)):
            logger.info("[router] 规则预路由 → schedule（零 LLM）")
            return {"intent": "schedule"}
    # 批量排期意图支路（2026-10-25 真机修复，pending 无关）：「把所有待办排期」
    # 无时间表达，上面的强动词+时间预路由罩不住 → LLM 误判 coach → 幕僚长当
    # "任务拆解但没给任务"出 clarify 追问"把具体待办发给我"（真机死循环：批量
    # 请求再也进不了排期管道）。批量语句（范围+名词+动词全命中）语义明确——
    # 既非问询结算短句也非确认/拒绝，pending 活跃也直判（换意图语义，旧问询
    # 由 profiler 批量短路结算 ignored）
    from agents.scheduler.profiler import _bulk_intent  # 函数内 import 防环

    raw = content.strip()
    if (_bulk_intent(raw) and not _QUESTION_RE.search(raw)
            and not _DELETE_VERB_RE.search(raw)):
        logger.info("[router] 规则预路由 → schedule（批量意图，零 LLM）")
        return {"intent": "schedule"}
    # qa 预路由（2026-10-26 提速批 3）：pending 活跃轮语义复杂不走规则（问询
    # 回复短句「可以吗」可能带疑问词）；删除动词在场交 LLM 判 delete；排期
    # 排期动词形态在场交 LLM 判 schedule（2026-11-01 批 1——名词守卫见
    # _QA_VERB_EXCLUDE_RE 注释；裸「安排」名词查询句不受影响照走 qa 零 LLM）
    if (not pending and _QA_HINT_RE.search(raw) and _QA_DOMAIN_RE.search(raw)
            and not _DELETE_VERB_RE.search(raw)
            and not _QA_VERB_EXCLUDE_RE.search(raw)):
        logger.info("[router] 规则预路由 → qa（零 LLM）")
        return {"intent": "qa"}
    if isinstance(pending, dict) and pending:
        if pending.get("origin") == "strategist":
            # 幕僚长问询消费轮（Track B 批 5）：确认/拒绝短句确定性直达 coach
            # 零 LLM——router 规则 2 会把"算了/不用了"硬判 schedule（scheduler
            # pending 语义），对幕僚问询是误路由；其余（补充信息/疑似新话题）
            # 走 LLM 且换幕僚前缀，提示词规则 6 引导多数归 coach、新话题放行
            raw = content.strip()
            if _STRATEGIST_ACK_RE.match(raw) or _STRATEGIST_REFUSE_RE.match(raw):
                logger.info("[router] 幕僚长问询确认/拒绝词 → coach（零 LLM）")
                return {"intent": "coach"}
            ask = pending.get("ask_text") or pending.get("original_input") or ""
            content = f"（待幕僚长确认的问询：{ask}）\n用户回复：{content}"
        else:
            ask = pending.get("ask_text") or pending.get("original_input") or ""
            content = f"（待确认问询：{ask}）\n用户回复：{content}"
    # 近期对话注入（2026-10-23 增，见 docstring）：省略式确认句的意图在上文里。
    # 每条截 _PLANNER_HISTORY_CHARS（路由只要意图线索不需全文）；无历史零注入
    # ——首轮对话与旧行为逐字节一致。滚动摘要前缀（2026-10-26 增）：上午闲聊
    # 交代的偏好被挤出近期窗后靠摘要接住——路由层是"遗忘"的第一道闸。
    # SessionLocal 函数内 import（非模块级）：conftest autouse 替换的是
    # database 模块属性，调用时解析才吃到测试库
    from services.ai_data import chat_context
    from database import SessionLocal

    with SessionLocal() as db:
        chat_summary, hist = chat_context(db, recent_k=_PLANNER_HISTORY_K,
                                          exclude=state["user_input"])
    prefixes: list[str] = []
    if chat_summary:
        prefixes.append(f"（更早对话摘要，供衔接）\n{chat_summary}")
    if hist:
        lines = "\n".join(
            f"{'用户' if m.role == 'user' else '助手'}：{(m.content or '').strip()[:_PLANNER_HISTORY_CHARS]}"
            for m in hist)
        prefixes.append(f"（近期对话，供衔接判读）\n{lines}")
    if prefixes:
        content = "\n".join(prefixes) + f"\n（本轮输入）\n{content}"
    _status_emitter()("正在理解请求…")  # LLM 分类 1-3s：过程反馈（提速批 2）
    out = call_llm_aux(
        [{"role": "system", "content": ROUTER_PROMPT},
         {"role": "user", "content": content}],
        json_mode=True, max_tokens=100)
    data = loads_obj(out)
    intent = data.get("intent") if data else None
    intent = intent if intent in _INTENTS else "smalltalk"
    logger.info("[router] LLM 判定意图=%s", intent)
    _maybe_side_facts(state["user_input"], intent)  # 原话判（content 带历史前缀会误触发）
    return {"intent": intent}


# 临时状况关键词闸（2026-11-05 真机批）：状况陈述被 router 判进 coach/smalltalk
# 时 settings 官的 facts 同跳不发生——regex 闸住"每轮都跑"的成本，只在疑似
# 状况话里多花一次小调用（max 120）
_FACT_HINT_RE = re.compile(
    r"感冒|发烧|生病|不舒服|生理期|大姨妈|出差|休假|放假|度假|隔离|阳了|"
    r"受伤|骨折|扭伤|康复|住院|体检结果|赶项目|冲刺期|倒时差")


def _maybe_side_facts(raw: str, intent: str) -> None:
    """临时状况旁路落事实（2026-11-05 真机批）：facts 提取只挂在 settings 官，
    router 把「我这周感冒了」判成 coach/smalltalk 时 Tier-2 事实直接丢（真机
    D3 场景 0->0）——状况陈述是情节事实的主入口。settings 意图自己会提（不
    重复跑）；regex 命中才调一次小 LLM（复用设置官提示词的 facts 段，action
    判成什么都不执行），落库静默不注记（旁路产物本轮回复无处挂脚注，透明性
    弱化登记 tech-debt），任何异常吞掉不挡对话。"""
    if intent not in ("coach", "smalltalk", "qa"):
        return
    text = raw if isinstance(raw, str) else ""
    if not _FACT_HINT_RE.search(text):
        return
    try:
        import json as _json

        from database import SessionLocal
        from services.episodic import upsert_fact
        from services.llm import call_llm_aux

        out = call_llm_aux(
            [{"role": "system", "content": build_settings_prompt()},
             {"role": "user", "content": _json.dumps(
                 {"user_input": text[-400:], "recent_chat": []},
                 ensure_ascii=False)}],
            json_mode=True, max_tokens=120)
        data = loads_obj(out)
        facts = data.get("facts") if isinstance(data, dict) else None
        if isinstance(facts, list) and facts:
            with SessionLocal() as db:
                kept = [upsert_fact(db, topic=f.get("topic"), fact=f.get("fact"),
                                    source="chat", expires_days=f.get("expires_days"))
                        for f in facts[:3] if isinstance(f, dict)]
                if any(k is not None for k in kept):
                    logger.info("[chat] 旁路落事实 %d 条（intent=%s）", len(kept), intent)
    except Exception:
        logger.warning("[chat] 旁路事实提取失败（不挡本轮对话）", exc_info=True)


def _expand_qa_rows(tasks, lo: datetime, hi: datetime) -> list:
    """已排期任务 → 窗口内实例行（qa 清单专用，2026-10-21 复查批 A 增）。

    重复系列按 expand_occurrences 展开（planner busy 构建同款先例）——旧版
    直接打系列基准日 start_time，问"今天有什么安排"时模型看到的是数周前的
    基准日期，与同载荷 load_summary（按实例展开计入）自相矛盾。单次任务恒
    一实例，行为不变。返回行按时刻排序（清单时间序），形状鸭子兼容
    _qa_task_lines（title/priority/notes/start_time/end_time）。
    """
    from types import SimpleNamespace

    from services.tasks import _task_dur, expand_occurrences

    rows: list = []
    for t in tasks:
        dur = _task_dur(t)
        for occ in expand_occurrences(t, lo, hi):
            rows.append(SimpleNamespace(
                title=t.title, priority=t.priority, notes=t.notes,
                start_time=occ,
                end_time=occ + timedelta(minutes=dur) if dur else None))
    rows.sort(key=lambda r: r.start_time)
    return rows


def _qa_task_lines(tasks, limit: int | None = None) -> list[str]:
    """把任务对象压成一行一条的紧凑清单（qa_answerer 专用）。

    形如 "[高] 2026-10-17 09:00-10:30 周报评审｜带打印版"——优先级、时间、标题、
    notes 摘要一行齐活；未排期待办时间位标"未排期"。行数与 notes 长度双钳制。
    limit 可传更小配额（2026-11-01 真机批：已排期展开 + 收件箱分口径注入，
    防重复系列展开挤占——真机 60+16 行超 50 上限头部截断，收件箱整段不可见，
    「我记了哪些待办」被答成「收件箱是空的」）。
    """
    lines: list[str] = []
    for t in tasks[: min(limit, _QA_MAX_ROWS) if limit is not None else _QA_MAX_ROWS]:
        prio = _PRIORITY_LABELS.get(t.priority, "中")
        if t.start_time and t.end_time:
            when = f"{t.start_time:%Y-%m-%d %H:%M}-{t.end_time:%H:%M}"
        elif t.start_time:
            when = f"{t.start_time:%Y-%m-%d %H:%M}"
        else:
            when = "未排期"
        note = (t.notes or "").strip()[:_QA_NOTES_CHARS]
        lines.append(f"[{prio}] {when} {t.title}" + (f"｜{note}" if note else ""))
    return lines


def qa_answerer(state: AgentState) -> dict:
    """日程问答节点（qa 分支）：读真实任务清单回答用户的查询。

    输入：state["user_input"] / state["memory_rules"]；读库两次
    services.tasks.list_tasks——①今天起 14 天已排期（窗口口径，2026-10-21 复查
    批 A 由 7 天对齐 load_summary 的 14 天——旧版问"下周三有什么安排"时清单
    缺该日而负荷摘要有，两处口径打架；重复系列经 _expand_qa_rows 按实例展开，
    清单打实例时刻而非系列基准日）②收件箱待办
    （unscheduled 口径），三口径互斥故必须分两次调（见 services/tasks.py）；
    另取 settings.weather_city 的未来 7 天天气预报（"明天适合爬山吗"类问题
    对照据实回答，未配置/失败恒 None 不阻塞，2026-10-18 增）+
    calendar_load_summary 按日负荷（"这周忙不忙"类问题的全盘视角，
    2026-10-20 私人秘书化批 1 增）；另经
    _with_chat_history 注入近期对话历史 + 滚动摘要（2026-10-20 增——多轮
    追问"那后天呢"靠它接住上文，exclude 本轮原话防重复注入）。
    输出：{"final_reply": str}——仅文字回答，零建议卡（查询不是动作）。
    设计原因：QA 的核心是"只依据真实数据回答"，先确定性取数再让模型组织语言，
    从根上杜绝编造；记忆规则经 assemble_system_prompt 注入 system（自然语言型
    节点通道，见 prompts.py 模块头约定）。LLM 调用数 1——流式形态：call_llm_stream
    逐 token 经 _delta_emitter 推 delta 帧（chat 壳转发为 SSE 打字机；invoke/
    直调驱动下静默退化，见 _delta_emitter 注释）。
    """
    from datetime import datetime, timedelta

    from services.ai_data import recent_reject_reasons
    from services.llm import call_llm_stream
    from services.settings import get_settings
    from services.tasks import calendar_load_summary, execution_rhythm, list_tasks, recent_done
    from services.weather import forecast_for_city

    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win_hi = day0 + timedelta(days=14)  # 与 calendar_load_summary 同口径（14 天）
    with SessionLocal() as db:
        scheduled = list_tasks(db, start=day0, end=win_hi)
        inbox = list_tasks(db, unscheduled=True)
        city = get_settings(db).weather_city
        load_summary = calendar_load_summary(db)  # "这周忙不忙"的负荷视角
        done_recent = recent_done(db)  # 过去 7 天完成窗（批 4：产能口径）
        # 优化批 6 三键：执行节奏量化（「这周完成率怎么样」有据可答）+ 近期
        # 拒绝理由（建议口径当场对照）+ 静态溯源摘录（「我上次记的那个想法」
        # 答得出素材——_related_pool 打分管线复用，不接 probe 回源零延迟）
        rhythm = execution_rhythm(db)
        rejects = recent_reject_reasons(db)
        from agents.strategist.context_fetcher import _source_context

        source_ctx = _source_context(db, state["user_input"], day0, probe=False)
        # Tier 2 相关事实（2026-11-03 记忆重构批 6）：「这周感冒」「下周出差」类
        # 临时事实 BM25 命中注入（零命中零注入——载荷键空数组提示词自然忽略）
        from services.episodic import fact_lines, search_facts

        related_facts = fact_lines(search_facts(db, state["user_input"]))
    from services.holidays import upcoming_holidays

    holidays = upcoming_holidays(days=14)
    weather = forecast_for_city(city)  # None=未配置/失败，提示词层据实降级
    # 分口径配额注入（2026-11-01 真机批）：已排期展开行多（重复系列一天一行），
    # 拼接后统一截断会让收件箱整段不可见——两段各自钳制，收件箱保底 20 行
    lines = (_qa_task_lines(_expand_qa_rows(scheduled, day0, win_hi),
                            _QA_MAX_ROWS - _QA_INBOX_ROWS)
             + _qa_task_lines(list(inbox), _QA_INBOX_ROWS))
    if not lines:
        lines = ["（当前没有已排期日程，也没有待办任务）"]
    logger.info("[qa] 问答轮：14 天窗任务 %d / 收件箱 %d / 天气 %s / 溯源 %d 条",
                len(scheduled), len(inbox), "有" if weather else "无", len(source_ctx))
    reply = call_llm_stream(
        _with_chat_history(
            state,
            assemble_system_prompt(state.get("memory_rules") or [], QA_PROMPT,
                                   core_rules=state.get("core_memory")),
            json.dumps(
                {"today": now.strftime("%Y-%m-%d"),
                 "user_input": state["user_input"],
                 "weather": weather,
                 "load_summary": load_summary,
                 "recent_done": done_recent,
                 "execution_rhythm": rhythm,
                 "recent_rejects": rejects,
                 "holidays": holidays,
                 "source_context": source_ctx,
                 "related_facts": related_facts,
                 "tasks": lines}, ensure_ascii=False)),
        on_chunk=_delta_emitter(),
        # 2000（<2048 思考闸）显式免思考：思考型模型下流式首字前不再生成数千
        # reasoning token（2026-10-26 提速批 1）；中文 ≈1300 字对 QA 足够
        max_tokens=2000)
    return {"final_reply": reply.strip() or "（模型没有返回内容，请稍后再试）"}


def smalltalk_node(state: AgentState) -> dict:
    """闲聊节点（smalltalk 分支）：最轻路径——一次调用直接回，不产卡。

    输入：state["user_input"] / state["memory_rules"]（闲聊也注入规则——
    "称呼偏好"之类的规则对闲聊同样有效，成本为零：同一次调用）。
    输出：{"final_reply": str}。另经 _with_chat_history 注入近期对话历史 +
    滚动摘要（2026-10-20 增——"刚说的那个"之类的指代靠它接住；无历史时消息
    数组退化为 [system, user] 两则，与旧形状逐字节一致）。
    今日概览注入（2026-10-26 批 4）：user 消息尾附一行今日负荷（一次轻读库
    calendar_load_summary——秘书语境感"今天还有几件事"，只陈述不承诺；无
    日程时省略整行，闲聊零数据形态不回退）。
    设计原因：闲聊是意图兜底目标（router 拿不准、解析失败、条件边非法值都
    落到这），必须最简单最稳；空回复兜底一句固定话术保证 final_reply 恒非空
    （壳只持久化非空 reply）。LLM 调用数 1——流式形态同 qa_answerer
    （call_llm_stream + _delta_emitter，打字机体感）。
    """
    from services.llm import init_llm, stream_model
    from services.settings import get_settings
    from services.tasks import calendar_load_summary
    from services.weather import forecast_for_city

    today = datetime.now().strftime("%Y-%m-%d")
    with SessionLocal() as db:
        row = next((r for r in calendar_load_summary(db) if r["date"] == today), None)
        s = get_settings(db)
        # Tier 2 相关事实（2026-11-03 记忆重构批 6）：共情语境——「我感冒了好难受」
        # 接得住健康事实；零命中零拼接（同今日概览省略形态）
        from services.episodic import fact_lines, search_facts

        facts = fact_lines(search_facts(db, state["user_input"]))
    brief = (f"（今日概览：{row['scheduled_count']} 件事共 {row['total_mins']} 分钟"
             f"{'，其中高强度 ' + str(row['high_count']) + ' 件' if row['high_count'] else ''}）"
             if row and row["scheduled_count"] else "")
    if facts:
        brief += f"（近期相关：{'；'.join(facts)}）"
    # 今日天气一行（2026-11-01 优化批 1）：与 QA 口径对齐——「今天真冷」「周末
    # 好天气」接得住预报；6h 缓存零网络成本，未配置城市/无预报省略整行
    wx = next((w for w in (forecast_for_city(s.weather_city) or [])
               if w.get("date") == today), None) if s.weather_city else None
    if wx:
        brief += (f"（今日天气：{wx.get('text')}，{wx.get('tmin')}~"
                  f"{wx.get('tmax')}℃，降水 {wx.get('precip')}%）")
    logger.info("[smalltalk] 闲聊轮（负荷 %s / 天气 %s，一次流式调用）",
                "有" if row and row["scheduled_count"] else "无", "有" if wx else "无")
    # 温度 0.7 直用（优化批 6，解禁项 5）：闲聊/共情恒 0 是「机械感」来源——
    # llm.py 参数策略注释预留的「闲聊可显式调高」首次落地；init_llm 直用 +
    # stream_model 共享 <think> 三态过滤（与 call_llm_stream 单源不复制）。
    # max_tokens 2000 同 qa（<2048 思考闸，闲聊最该秒回）
    reply = stream_model(
        init_llm(temperature=0.7, max_tokens=2000),
        _with_chat_history(
            state,
            assemble_system_prompt(state.get("memory_rules") or [], SMALLTALK_PROMPT,
                                   core_rules=state.get("core_memory")),
            state["user_input"] + brief),
        on_chunk=_delta_emitter())
    return {"final_reply": reply.strip() or "我在。日程上的事随时说。"}


def task_intensity(title: str, duration_mins: int | None, priority: int | None) -> str:
    """任务强度规则映射（排期子图 intake 内联消费的唯一实现）。

    duration≥90 分钟或高优（priority=1）→ high（深度工作/关键事项）；
    duration≤15 分钟或低优（priority=3）→ low（碎片事务）；否则 medium。
    title 当前不参与（语义级标注留待 TAGGER_PROMPT 启用，见 prompts.py 注记）。
    """
    if (duration_mins or 0) >= 90 or priority == 1:
        return "high"
    if 0 < (duration_mins or 0) <= 15 or priority == 3:
        return "low"
    return "medium"


# —— 排期子图的输入钳制与防御（后验校验与 _parse_ingest 口径对齐） ——

# 待排期待办进提示词的上限（防上下文膨胀；超出的照常留在收件箱，下轮再排）
_PLANNER_MAX_CANDIDATES = 20
# 已排期对照清单上限（7 天窗口内通常远少于此）
_PLANNER_MAX_SCHEDULED = 30
# notes 进提示词的截断长度
_PLANNER_NOTES_CHARS = 60
# 排期轮的对话历史窄窗（2026-10-21 复查批 A 定案）：刻意小于 qa/coach 的
# K=10×200 全量窗口（ai_data.CHAT_RECENT_K/CHAT_MSG_CHARS）——任务数据是排期
# 载荷大头，历史只服务"把它改到明天"类指代衔接，窄窗够用且防 8K 档输入膨胀
_PLANNER_HISTORY_K = 6
_PLANNER_HISTORY_CHARS = 80


def _clean_draft_entry(d: dict, valid_ids: set[str], *, origin: str):
    """单条草案的基础后验（宁可不排，不可排错）——返回 (清化条目, None) 或
    (None, 拒绝原因)。

    校验链与 PROFILER_PROMPT 硬约束一一对齐（提示词先讲对，这里再
    兜底）：task_id 白名单 / naive 无时区 / duration 正整数 / title≤30。
    start 允许 None 仅限 origin=agent——具体时刻由统一排期引擎决定
    （2026-10-20 统一引擎批）；origin=user 且带非空 explicit_time 时透传
    （快录落库的审计线索）；origin=agent 的 not_before（日期偏好）一并透传，
    由排期子图换算成引擎的起点。
    """
    if not (isinstance(d, dict) and isinstance(d.get("title"), str)):
        return None, "条目形状非法"
    title = d["title"].strip()
    if not title or len(title) > 30:
        return None, "标题为空或超 30 字"
    task_id = d.get("task_id")
    if task_id is not None and task_id not in valid_ids:
        return None, "task_id 不在待排清单（幻觉）"
    start = None
    if d.get("start") is not None:
        start_raw = d.get("start")
        if not isinstance(start_raw, str):
            return None, "start 非字符串"
        try:  # naive 本地时间契约：带时区后缀（含 Z）即丢——与 _parse_ingest 一查双防
            start = datetime.fromisoformat(start_raw)
            if start.tzinfo is not None:
                return None, "start 带时区后缀"
        except ValueError:
            return None, "start 无法解析"
    elif origin != "agent":
        return None, "start 缺失（仅 agent 条目可省，时间由引擎定）"
    duration = d.get("duration")
    if not is_int(duration) or duration <= 0:
        return None, "duration 非正整数"
    entry = {"task_id": task_id, "title": title,
             "start": start.isoformat() if start else None, "duration": duration}
    if origin == "user":
        explicit = d.get("explicit_time")
        if isinstance(explicit, str) and explicit.strip():
            entry["explicit_time"] = explicit.strip()
    elif origin == "agent":
        nb = d.get("not_before")
        if isinstance(nb, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", nb.strip()):
            entry["not_before"] = nb.strip()
    return entry, None


# —— 排期分流纯函数（2026-10-17：快录自动落库 / 问询判定 / 相似查重。
#    2026-10-20 推荐空档规则层提升 services.tasks 公开实现，此处别名引用） ——

# 相似日程判定阈值：标题归一化后 difflib 相似度 ≥ 此值视为"同一件事"——
# 中文短标题（"体检" vs "年度体检"）0.6 是"重复安排"与"碰巧像"的经验分界
_SIMILAR_RATIO = 0.6
# 相似查重窗口（天）：短期内近名事项大概率是重复安排（如一周内两次体检）
_SIMILAR_WINDOW_DAYS = 7


def _normalize_title(title: str) -> str:
    """标题归一化（相似查重前置）：去首尾空白、剔除标点符号、转小写。

    "年度体检！"与"年度体检"、"Write report."与"write report" 归一后一致——
    标点噪声不剔除会把 difflib 相似度拖到阈值下，漏掉真正的重复安排。
    """
    return re.sub(r"[\W_]+", "", title, flags=re.UNICODE).lower()


def _find_similar(title: str,
                  existing: list[tuple[str, datetime | None]]) -> tuple[str, float] | None:
    """在既有日程里找相似标题（返回 (命中标题, 相似度) 或 None）。

    difflib.SequenceMatcher 对归一化串做规则层近似比对——查重目标是"用户短时间
    重复安排同一件事"，标题高度重合已是强信号，不需要嵌入模型。existing 为
    (标题, 开始时间) 二元组列表，窗口过滤（未来 _SIMILAR_WINDOW_DAYS 天）由
    调用方负责；多命中取相似度最高者。
    """
    norm = _normalize_title(title)
    if not norm:
        return None
    best: tuple[str, float] | None = None
    for other, _dt in existing:
        ratio = difflib.SequenceMatcher(None, norm, _normalize_title(other)).ratio()
        if ratio >= _SIMILAR_RATIO and (best is None or ratio > best[1]):
            best = (other, ratio)
    return best


_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str | None) -> int | None:
    """中文数字钟点解析（一到二十几，2026-11-01 真机批增）：口语「今晚十点」
    「下午三点」的「十」「三」此前正则提不出来 → 复核恒挂降级草案。只覆盖
    钟点实用域（1-2X），不做通用中文数字转换。"""
    if not s:
        return None
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[s[1]]
    if s == "二十":
        return 20
    if len(s) == 3 and s[:2] == "二十" and s[2] in _CN_DIGITS:
        return 20 + _CN_DIGITS[s[2]]
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def _clock_from_text(text: str) -> tuple[int, int] | None:
    """从用户原话时间表达式提取钟点（返回 (时, 分) 或 None）。

    覆盖常见写法："上午10点"、"10:30"、"下午3点半"、"14点"、"晚上9点"、
    "今晚十点"（中文数字，2026-11-01 真机批增）——上下文词（下午/晚上等）
    参与 12 小时制换算。这是 origin=user 落库前的第三道复核（前两道：
    explicit_time 是原话子串、title 与原话有子串关系）：提取出的钟点必须与
    条目 start 一致，防 LLM 把"上午10点"抄成 22:00 之类的语义漂移；提取失败
    也返回 None，调用方按复核不通过降级走确认卡——宁可多确认不误落库。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    m = re.search(
        r"(\d{1,2}|[一两二三四五六七八九十]{1,3})\s*[:：点时]\s*"
        r"(\d{1,2}|[一两二三四五六七八九十]{1,3}|半)?", t)
    if not m:
        return None
    g1, g2 = m.group(1), m.group(2)
    hour = int(g1) if g1.isdigit() else _cn_num(g1)
    if g2 is None or g2 == "半":
        minute = 30 if g2 == "半" else 0
    elif g2.isdigit():
        minute = int(g2)
    else:
        minute = _cn_num(g2) or 0
    if hour is None:
        return None
    if g2 is None and t[m.end():m.end() + 1] == "半":  # 「半」紧跟钟点后缀形态
        minute = 30
    if any(w in t for w in ("下午", "中午", "傍晚", "晚上", "今晚")) and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


_REL_MIN_RE = re.compile(
    r"([0-9]{1,3}|[一两二三四五六七八九十]{1,3})\s*个?\s*(?:分|分钟)后"
    r"|(半小时后)|(马上|立刻)")


def rel_minutes_from_text(text: str) -> tuple[int, str] | None:
    """相对分钟解析（2026-11-05 真机批）：「两分钟后关火」「半小时后出发」
    「马上开始」——用户以"现在 + N 分钟"指定时刻。返回 (分钟数, 命中原话)
    或 None（0 分钟 = 现在）。只认"…分钟后"封闭形态与"马上/立刻"（裸"现在"
    上下文噪声多，误认会把非时间句当相对时刻）；「十几分钟后」类模糊量不
    硬猜返回 None。真机教训：「提醒我两分钟后关火」无此解析，LLM 时刻
    猜测被当真，排到了当天下午。"""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _REL_MIN_RE.search(text.strip())
    if not m:
        return None
    if m.group(2):  # 半小时后
        return 30, m.group(2)
    if m.group(3):  # 马上 / 立刻
        return 0, m.group(3)
    g = m.group(1)
    minutes = int(g) if g.isdigit() else _cn_num(g)
    return (minutes, m.group(0)) if minutes is not None else None


# 问询四类（question_plan.kind 合法域；壳建 kind=question 行时的形态契约）
# frequency（频率防呆）/ no_slot（零候选）2026-10-21 批 5 增——payload 内部
# kind（proposals 表恒 kind=question，前端对 question 行整体跳过渲染，不破契约）
# strategist_clarify（幕僚追问）/ memory（记忆确认）2026-10-21 Track B 批 2 增
# ——同款内部 kind，幕僚问询无排期条目（entries 恒空，壳对这两类放行空 entries）
QUESTION_KINDS = ("conflict", "similar", "no_time", "past", "frequency", "no_slot",
                  "strategist_clarify", "memory")


def settings_payload(s) -> dict:
    """设置对象 → 排期提示词的 settings 键（排期子图与 Reduce 共用
    同一形状，2026-10-17 提取——两入口的秘书视野输入不漂移）。"""
    return {
        "work_minutes_per_day": s.work_minutes_per_day,
        "day_start": f"{s.calendar_start_min // 60:02d}:{s.calendar_start_min % 60:02d}",
        "day_end": f"{s.calendar_end_min // 60:02d}:{s.calendar_end_min % 60:02d}",
        "lunch": ([s.lunch_start_min, s.lunch_end_min] if s.lunch_enabled else None),
        # 提前提醒量（2026-10-26 批 4 补键）：分钟；新排任务的提醒由系统按此
        # 生成，重要事项的"提前准备"建议可对照它（提前量不够时值得点一句）
        "reminder_lead_min": s.reminder_lead_min,
    }


def _fmt_when(dt: datetime) -> str:
    """问询文案的时刻格式（"2026-10-19 14:00"——带年份免跨年歧义，可照抄回复）。"""
    return f"{dt:%Y-%m-%d %H:%M}"


def _question_text(q: dict) -> str:
    """问询文案模板（凝练口径：一句问清 + 指路两种回复方式，不罗嗦）。

    多条目时 extra 列出其余标题（2026-10-21 复查批 A 改——同轮多条撞期全量
    进问询后，只给计数不给标题，用户无从知道"另 1 件"是什么）。"""
    entries = q.get("entries") or []
    e0 = entries[0] if entries else {}
    title = e0.get("title", "这件事")
    if len(entries) > 1:
        names = "、".join(str(e.get("title") or "?") for e in entries[1:])
        extra = f"（另有 {len(entries) - 1} 件待确认：{names}）"
    else:
        extra = ""
    kind = q.get("kind")
    if kind == "conflict":
        start = e0.get("start")
        when = _fmt_when(datetime.fromisoformat(start)) if start else "该时间"
        sugg = q.get("suggestion")
        clash = [str(t) for t in (q.get("clash_titles") or []) if t]
        # 撞到的既有任务直说（"已安排「写作时间」"——用户才好判断让不让位；
        # 无名回落旧文案），至多列 2 个防刷屏
        busy_txt = f"已安排「{'」「'.join(clash)}」" if clash else "已有安排"
        if sugg:
            return (f"「{title}」要的 {when} {busy_txt}，建议改到 "
                    f"{_fmt_when(datetime.fromisoformat(sugg))}，可以吗？也可以直接给新时间，"
                    f"不能改就回复“坚持原时间”。{extra}")
        return f"「{title}」要的 {when} {busy_txt}，近一周没找到空档——改到什么时间？{extra}"
    if kind == "similar":
        return (f"未来一周已有相似日程「{q.get('similar_to') or '同题事项'}」，"
                f"还要再安排「{title}」吗？回复“坚持安排”或给新时间。{extra}")
    if kind == "past":
        start = e0.get("start")
        when = _fmt_when(datetime.fromisoformat(start)) if start else "该时间"
        return f"「{title}」的时间 {when} 已经过去——改到什么时间？{extra}"
    if kind == "frequency":
        # 批 5 频率防呆：距上次同类太近（extra_payload 带 last=上次时刻 /
        # interval_days=建议间隔天数，文案直说）
        last = q.get("last_done")
        when = _fmt_when(datetime.fromisoformat(last)) if last else "不久前"
        gap = q.get("interval_days")
        head = f"距上次（{when}）间隔不足" + (f"建议的 {gap} 天" if gap else "常规间隔")
        return (f"「{title}」{head}——确认还要再安排一次吗？"
                f"回复“确认安排”或“先不排”。{extra}")
    if kind == "no_slot":
        # 批 5 零候选：窗口/硬约束筛到没有任何可用槽（文案给两条出路：
        # 收待办 或 用户点名挪走低优先级日程——自动挪期登记技术债不做）
        return (f"未来一周的空档实在挤不出「{title}」的位置——回复“加待办”先收进"
                f"收件箱，或回复“坚持安排”忽略每日工时上限再找一次；也可以告诉"
                f"我挪走哪件低优先级的事。{extra}")
    hint = q.get("date_hint")
    head = f"「{title}」安排在{hint}几点？" if hint else f"「{title}」安排在哪天几点？"
    # 政务/银行类落周末提醒（2026-11-05 真机批）：no_time 问询本身不拦（缺
    # 钟点先问几点是正路），但窗口客观上仅工作日开放——秘书该在问时就提醒，
    # 不等用户答完时间才在直落附注里说（真机「这周六办护照」问几点零提示）
    note = f"另外提醒：{q['weekend_note']}。" if q.get("weekend_note") else ""
    return f"{head}{note}也可以回复“加待办”先收进收件箱。{extra}"


# —— 会前准备（prep 定时流，2026-10-20 增）：夜间报点后多会议合并单次 LLM ————————

# prep 输出预算：多会议 × checklist 合并一次返回，比教练单卡略宽
_PREP_MAX_TOKENS = 1200


def _parse_prep_out(out: str, valid_ids: set[str]) -> tuple[list[dict], str]:
    """会前准备 LLM 输出解析 → (meetings 条目, reply)。

    防御口径：task_id 不在输入白名单 → 丢条（幻觉防御，同 MERGE 溯源白名单
    后滤先例）；headline 非法或 checklist 全空的会议 → 整条丢（提示词"宁空
    勿凑"的落地——无实质准备点的会议不出卡条）；整体非 JSON → 空列表交
    调用方静默收场（prep 是后台流，无兜底卡形态）。"""
    data = loads_obj(out)
    if data is None:
        return [], ""
    meetings: list[dict] = []
    for m in data.get("meetings") or []:
        if not isinstance(m, dict):
            continue
        tid = m.get("task_id")
        headline = m.get("headline")
        if not isinstance(tid, str) or tid not in valid_ids:
            continue
        if not isinstance(headline, str) or not headline.strip():
            continue
        checklist = []
        for item in (m.get("checklist") or [])[:6]:
            if isinstance(item, str) and item.strip():
                checklist.append(item.strip()[:40])
        if not checklist:
            continue
        meetings.append({"task_id": tid, "headline": headline.strip()[:30], "checklist": checklist})
    reply = data.get("reply")
    return meetings, (reply.strip() if isinstance(reply, str) else "")


def build_prep_brief(meetings_payload: list[dict], memory_rules: list[str],
                     target_day_iso: str, *, core_rules: list[str] | None = None
                     ) -> tuple[list[dict], str]:
    """会前准备核心（prep 定时流专用，与 chat 无关）：多会议合并单次 LLM →
    (meetings 条目, reply)。识别已由 job_rules 纯规则把关，这里只做内容生成；
    记忆规则经 assemble_system_prompt 注入（同教练通道；core_rules 双段
    2026-11-03 批 3，None 兼容旧调用）。call_llm 裸奔不 try
    （故障上抛交壳记日志）；解析失败由 _parse_prep_out 返回空交调用方静默。
    LLM 调用数 1（多会议合并，防 N 场会议 N 次调用）。"""
    from agents.prompts import PREP_PROMPT
    from services.llm import call_llm

    system_content = assemble_system_prompt(memory_rules, PREP_PROMPT,
                                            core_rules=core_rules)
    out = call_llm(
        [{"role": "system", "content": system_content},
         {"role": "user", "content": json.dumps(
             {"target_date": target_day_iso, "meetings": meetings_payload},
             ensure_ascii=False)}],
        json_mode=True, max_tokens=_PREP_MAX_TOKENS)
    return _parse_prep_out(out, {m["task_id"] for m in meetings_payload})


# —— 会后纪要行动项提炼（minutes 流，2026-11-10 增）：用户主动提交，单次 LLM ——
# 与 prep 镜像（会前备会后落）；提取不生成——只从用户纪要原文挑行动项。

_MINUTES_MAX_TOKENS = 1200


def _parse_minutes_out(out: str) -> tuple[str, list[dict], str]:
    """会后纪要 LLM 输出解析 → (headline, items, reply)。

    防御口径：title 非法（非 str / 空）丢条；duration 非整数或 <1 → None
    （未估时），>1440 钳 1440；priority 非法回落 2（中）；notes 非法/空白 →
    None；条目 >8 截断（宁少勿多）。items 空时 headline 仍保留（纯纪要也有
    总结价值）。整体非 JSON → 全空交调用方按解析失败处理（不建空壳卡）。"""
    data = loads_obj(out)
    if data is None:
        return "", [], ""
    headline = data.get("headline")
    headline = headline.strip()[:30] if isinstance(headline, str) else ""
    items: list[dict] = []
    for it in (data.get("items") or [])[:8]:
        if not isinstance(it, dict):
            continue
        title = it.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        dur = it.get("duration")
        if isinstance(dur, bool) or not isinstance(dur, int) or dur < 1:
            dur = None
        else:
            dur = min(dur, 1440)
        prio = it.get("priority")
        prio = prio if isinstance(prio, int) and prio in (1, 2, 3) else 2
        notes = it.get("notes")
        notes = (notes.strip()[:60]
                 if isinstance(notes, str) and notes.strip() else None)
        items.append({"title": title.strip()[:30], "duration": dur,
                      "priority": prio, "notes": notes})
    reply = data.get("reply")
    return headline, items, (reply.strip() if isinstance(reply, str) else "")


def build_minutes_brief(meeting_payload: dict | None, minutes_text: str,
                        memory_rules: list[str],
                        *, core_rules: list[str] | None = None,
                        existing_titles: list[str] | None = None,
                        ) -> tuple[str, list[dict], str]:
    """会后行动项提炼核心（浮层 / 聊天入口共用，非定时流）：会议元信息 +
    纪要原文 → (headline, items, reply)。只提取不生成（反定位红线）；
    existing_titles（批 2 去重对照）：收件箱既有未完成待办标题清单，prompt
    据此跳过语义重复的行动项；记忆规则经 assemble_system_prompt 注入（同
    prep 通道；core_rules 双段 2026-11-03 批 3，None 兼容旧调用）。
    call_llm 裸奔不 try（故障上抛交壳记日志）；解析失败由 _parse_minutes_out
    返回空交调用方处理。LLM 调用数 1。"""
    from agents.prompts import MINUTES_PROMPT
    from services.llm import call_llm

    system_content = assemble_system_prompt(memory_rules, MINUTES_PROMPT,
                                            core_rules=core_rules)
    out = call_llm(
        [{"role": "system", "content": system_content},
         {"role": "user", "content": json.dumps(
             {"meeting": meeting_payload, "minutes_text": minutes_text,
              "existing_titles": existing_titles or []},
             ensure_ascii=False)}],
        json_mode=True, max_tokens=_MINUTES_MAX_TOKENS)
    return _parse_minutes_out(out)


def minutes_extractor(state: AgentState) -> dict:
    """会后纪要提炼节点（2026-11-10 批 2，chat minutes 分支）：整条 user_input
    即纪要原文（预路由保证纪要词 + 长文本形态），调 build_minutes_brief（含
    记忆注入与收件箱去重对照）→ inbox_items + minutes_meta（壳据此建
    kind=minutes 卡，payload 契约同浮层端点）+ final_reply。聊天入口不关联
    会议（meeting 恒 None——自动匹配最近结束会议有误关联风险，宁缺勿滥，
    关联诉求走日程浮层入口）。解析失败只回提示不建卡。LLM 调用数 1。"""
    from agents.prompts import personality_rules
    from database import SessionLocal
    from services import tasks as tasks_svc
    from services.ai_data import format_memory_lines
    from services.settings import get_settings

    raw = (state.get("user_input") or "").strip()
    with SessionLocal() as db:
        s = get_settings(db)
        core, ref = format_memory_lines(
            db, personality=personality_rules(
                s.ai_style, s.ai_philosophy, s.ai_philosophy_custom))
        # 去重对照：收件箱未完成待办标题（list_tasks unscheduled 口径，≤20 条）
        existing = [t.title for t in tasks_svc.list_tasks(db, unscheduled=True)[:20]]
    headline, items, reply = build_minutes_brief(
        None, raw, core + ref, core_rules=core, existing_titles=existing)
    if not headline and not items and not reply:
        return {"final_reply": "纪要提炼失败了，请稍后重试或换个格式再发。"}
    return {
        "inbox_items": items,
        "minutes_meta": {
            "meeting_task_id": None, "meeting_title": None, "meeting_start": None,
            "headline": headline, "source_text": raw[:8000],
        },
        "final_reply": reply or "已收到纪要。",
    }


# —— 高难协助与自定义简报（hardtask / brief 定时流，2026-10-20 增）：单次 LLM ———————

_HARDTASK_MAX_TOKENS = 800
_BRIEF_MAX_TOKENS = 800


def _parse_hardtask_out(out: str) -> dict | None:
    """高难协助 LLM 输出解析 → {headline, steps, advice} 或 None（skip/解析失败）。

    防御口径：skip=true → None（提示词"宁缺勿滥"）；task_id 不采信 LLM 回传
    （单任务流无幻觉过滤面，建卡直接以输入 id 为准）；headline 非法或 steps
    全空 → None（拆不出可执行步骤就不打扰）；整体非 JSON → None 交调用方静默
    收场。"""
    data = loads_obj(out)
    if not isinstance(data, dict) or data.get("skip") is True:
        return None
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        return None
    steps = []
    for item in (data.get("steps") or [])[:6]:
        if isinstance(item, str) and item.strip():
            steps.append(item.strip()[:30])
    if not steps:
        return None
    advice = data.get("advice")
    return {
        "headline": headline.strip()[:30],
        "steps": steps,
        "advice": advice.strip()[:80] if isinstance(advice, str) else "",
    }


def build_hardtask_brief(task_payload: dict, memory_rules: list[str],
                         today_iso: str, *, core_rules: list[str] | None = None
                         ) -> dict | None:
    """高难协助核心（hardtask 定时流专用）：单任务拆解单次 LLM → 内容 dict 或
    None。候选已由 job_rules.pick_hard_task 纯规则把关，这里只做拆解生成；
    call_llm 裸奔不 try（故障上抛交壳记日志）。LLM 调用数 1。"""
    from agents.prompts import HARDTASK_PROMPT
    from services.llm import call_llm

    system_content = assemble_system_prompt(memory_rules, HARDTASK_PROMPT,
                                            core_rules=core_rules)
    out = call_llm(
        [{"role": "system", "content": system_content},
         {"role": "user", "content": json.dumps(
             {"task": task_payload, "today": today_iso}, ensure_ascii=False)}],
        json_mode=True, max_tokens=_HARDTASK_MAX_TOKENS)
    return _parse_hardtask_out(out)


def _parse_brief_out(out: str) -> dict | None:
    """自定义简报 LLM 输出解析 → {headline, body} 或 None（解析失败）。
    brief 是用户主动订阅的流，无 skip 语义——解析失败返回 None 交调用方静默。"""
    data = loads_obj(out)
    if not isinstance(data, dict):
        return None
    headline = data.get("headline")
    body = data.get("body")
    if not isinstance(headline, str) or not headline.strip():
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    return {"headline": headline.strip()[:30], "body": body.strip()}


def build_custom_brief(instruction: str, tasks_payload: list[dict], load_ratio: float | None,
                       unscheduled_count: int, memory_rules: list[str],
                       today_iso: str, *, core_rules: list[str] | None = None) -> dict | None:
    """自定义简报核心（brief:<uuid> 定时流专用）：指令 + 当日数据单次 LLM →
    {headline, body} 或 None。数据收集在调用方（graph 入口）纯规则完成，这里
    只做生成；call_llm 裸奔不 try。LLM 调用数 1。"""
    from agents.prompts import BRIEF_PROMPT
    from services.llm import call_llm

    system_content = assemble_system_prompt(memory_rules, BRIEF_PROMPT,
                                            core_rules=core_rules)
    out = call_llm(
        [{"role": "system", "content": system_content},
         {"role": "user", "content": json.dumps(
             {"instruction": instruction, "tasks": tasks_payload,
              "load_ratio": load_ratio, "unscheduled_count": unscheduled_count,
              "today": today_iso}, ensure_ascii=False)}],
        json_mode=True, max_tokens=_BRIEF_MAX_TOKENS)
    return _parse_brief_out(out)


# —— delete 分支：删除定位器（只定位不执行，执行走前端确认卡） ——————————————————

# 候选清单上限（7 天窗口 + 收件箱全量合并后截断）。20→40（2026-10-22 真机
# 修复：实测库 118 条任务，目标任务排 20 名外时模型根本看不到 →「没能确定
# 你指的是哪个任务」；40 行 payload 紧凑 token 可控，确定性预定位兜住长尾）
_DELETE_MAX_CANDIDATES = 40

# 指令壳词（预定位前剥离）：删除动词 + 高频时间/量词修饰——剥离后剩余才是
# 目标短语。壳词表不全没关系：漏剥只稀释相似度，落回 LLM 语义定位路径。
_DELETE_SHELL_RE = re.compile(
    r"删掉|删除|删了|取消|移除|去掉|今天|明天|后天|下周|本周|周一|周二|周三|"
    r"周四|周五|周六|周日|那个|这个|一下|帮我|麻烦|请|把|了|的")


def _locate_by_title(raw: str, candidates) -> list:
    """确定性标题预定位（2026-10-22 真机修复增）：剥壳词后与全部候选标题
    归一化比对（difflib ratio ≥ 0.6——与排期查重同口径），返回命中列表按
    分数降序；恰一条命中时调用方可零 LLM 直接定位，多条则置顶候选交 LLM
    消歧。全量扫描不受 _DELETE_MAX_CANDIDATES 截断影响。"""
    norm = _normalize_title(_DELETE_SHELL_RE.sub("", raw))
    if not norm:
        return []
    hits = []
    for t in candidates:
        ratio = difflib.SequenceMatcher(None, norm, _normalize_title(t.title)).ratio()
        if ratio >= 0.6:
            hits.append((ratio, t))
    hits.sort(key=lambda h: -h[0])
    return [t for _, t in hits]


def delete_confirmer(state: AgentState) -> dict:
    """删除确认节点（delete 分支）：定位用户所指任务 → 出删除确认卡，只定位不执行。

    输入：state["user_input"]；读库两次 list_tasks——①今天起 7 天已排期（按
    开始时刻升序，近的在前）②收件箱待办（按优先级升序；删除对象可能是任一
    形态，三口径互斥分两次调）。
    定位两级（2026-10-22 真机修复：确定性预定位先行——删壳词后标题归一化比对
    全量候选，恰一条 ≥0.6 命中直接定位零 LLM；未命中/多条歧义才走 LLM，且
    高分条目置顶候选防 _DELETE_MAX_CANDIDATES 截断丢目标）：
    - 命中且目标是非重复任务 → {"delete_plan": [{"task_id", "title"}],
      "final_reply"}——壳 _finalize_turn 建 kind=delete 建议卡，用户点「确认删除」
      才经前端既有 DELETE 管线真删（P0 节点零写库红线：AI 永不直接删数据）；
    - 未命中 / 命中的是重复系列 → delete_plan 空数组 + 文字指引（重复系列六
      场景删除复杂度高，指引去日历卡操作——范围收窄取舍，登记 tech-debt）。
    设计原因：删除不可逆，模型只做"定位"这一件小事（DELETE_PROMPT 契约：
    task_id 清单内 id 或 null）；幻觉 id 防御与排期链同款——不在候选清单视为
    未命中。LLM 调用数 0-1（预定位命中省一次；+router 1）。
    """
    from services.llm import call_llm_aux
    from services.tasks import list_tasks

    raw = state["user_input"]
    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        scheduled = sorted(list_tasks(db, start=day0, end=day0 + timedelta(days=7)),
                           key=lambda t: t.start_time or now)
        inbox = sorted(list_tasks(db, unscheduled=True),
                       key=lambda t: t.priority or 2)
    candidates = scheduled + inbox
    if not candidates:
        return {"delete_plan": [], "final_reply": "当前没有可删除的任务。"}

    hits = _locate_by_title(raw, candidates)
    target = hits[0] if len(hits) == 1 else None
    logger.info("[delete] 定位轮：候选 %d 条，预定位命中 %d 条%s",
                len(candidates), len(hits),
                "（唯一命中零 LLM）" if target is not None else "，转 LLM 消歧")
    if target is None:  # 预定位未命中/多条歧义 → LLM 语义定位（高分条目置顶）
        _status_emitter()("正在查找相关日程…")  # 过程反馈（提速批 2）
        payload = [{"id": t.id, "title": t.title,
                    "start": t.start_time.isoformat() if t.start_time else None,
                    "recurrence": t.recurrence_rule is not None}
                   for t in (hits + [t for t in candidates if t not in hits])
                   [:_DELETE_MAX_CANDIDATES]]
        # 近期对话补注（2026-11-01 优化批 1）：「删掉明天那个」类指代此前零
        # 上下文无法消解——router 同窗近 6 条随载荷进 LLM（预定位命中轮零
        # LLM 不需要）
        recent = _recent_chat_lines(db, raw)
        out = call_llm_aux(
            [{"role": "system", "content": DELETE_PROMPT},
             {"role": "user", "content": json.dumps(
                 {"tasks": payload, "user_input": raw, "recent_chat": recent},
                 ensure_ascii=False)}],
            json_mode=True, max_tokens=100)
        data = loads_obj(out)
        task_id = data.get("task_id") if data else None
        target = {t.id: t for t in candidates}.get(
            task_id if isinstance(task_id, str) else None)
    if target is None:
        return {"delete_plan": [],
                "final_reply": "没能确定你指的是哪个任务——带上更准确的时间或标题再说一次；"
                               "也可以直接在日历卡上删除。"}
    if target.recurrence_rule is not None:
        return {"delete_plan": [],
                "final_reply": f"「{target.title}」是重复系列，删整条/仅本次/删后续请到日历卡上"
                               f"操作（删除菜单三选一），对话删除暂不支持系列。"}
    return {"delete_plan": [{"task_id": target.id, "title": target.title}],
            "final_reply": f"已定位「{target.title}」，请在下方确认删除。"}


# 撤销语义正则（settings_handler 零 LLM 短路）：短语（≤8 字）含撤销动词即直走
# 反演——"撤销刚才的设置"/"反悔"/"取消刚才那条"都命中；长句（如"撤销那条周五
# 不排事的偏好"）语义复杂留给 LLM 分流（泛撤销 → undo、点名撤某条 → unprefer）
_UNDO_HINT_RE = re.compile(r"撤销|反悔|回滚|取消刚才")


def _recent_chat_lines(db, exclude: str, k: int = 6) -> list[str]:
    """近 k 条对话行（2026-11-01 优化批 1）：delete/settings 载荷的指代消解
    线索——「删掉明天那个」「撤销刚才那条」零上下文判不了；router 同窗先例
    （每条截 80，user_input 本身排除）。"""
    from services.ai_data import chat_context

    _summary, hist = chat_context(db, recent_k=k, exclude=exclude)
    return [f"{'用户' if m.role == 'user' else '助手'}：{(m.content or '').strip()[:80]}"
            for m in hist]


def settings_handler(state: AgentState) -> dict:
    """设置官节点（settings 分支，2026-10-22 批 3）：对话自然语言 → 偏好增删 /
    数值设置 / 撤销，变更即时生效、确定性回执（谎报纪律：生效口径由 services
    返回拼装，不透传 LLM 自由文本当事实）。

    输入：state["user_input"]。流程（delete_confirmer 形态——LLM 抽取、
    services 执行）：
    - 短撤销句正则命中 → chat_config.undo_last_change 零 LLM（反演落库的
      settings_change proposal，跨重启有效）
    - 其余单次 LLM（build_settings_prompt 动态组入可改设置白名单）→ action
      分发 prefer / unprefer / setting / undo；unclear 回追问；解析失败兜底
      一句澄清（配置表达判错最坏是多问一句，不炸链）
    - 成功动作 journal 落 settings_change proposal（撤销锚 + 审计行，创建即
      accepted、非待确认卡）
    输出：{"final_reply"}——零卡零事件（已生效无待确认物）。
    LLM 调用数 0-1（撤销短路省一次；+router 1）。
    """
    from services import chat_config
    from services.llm import call_llm_aux

    raw = state["user_input"].strip()
    with SessionLocal() as db:
        if len(raw) <= 8 and _UNDO_HINT_RE.search(raw):
            logger.info("[settings] 撤销短路（零 LLM）")
            return {"final_reply": chat_config.undo_last_change(db)["reply"]}
        out = call_llm_aux(
            [{"role": "system", "content": build_settings_prompt()},
             {"role": "user", "content": json.dumps(
                 {"user_input": raw, "recent_chat": _recent_chat_lines(db, raw)},
                 ensure_ascii=False)}],
            json_mode=True, max_tokens=150)
        data = loads_obj(out)
        action = data.get("action") if isinstance(data, dict) else None
        # Tier 2 同跳事实提取（2026-11-03 记忆重构批 6，用户拍板「对话提取为主」）：
        # 无论 action 判成什么，临时状态（感冒/出差）先落 episodic_facts——零新增
        # LLM 调用（搭 settings 这跳的便车）；非法条目 upsert 内部丢弃
        facts_note = ""
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            from services.episodic import upsert_fact

            kept = [upsert_fact(db, topic=f.get("topic"),
                                fact=f.get("fact"), source="chat",
                                expires_days=f.get("expires_days"))
                    for f in data["facts"][:3]
                    if isinstance(f, dict)]
            kept = [k for k in kept if k is not None]
            if kept:
                facts_note = "（已记下这条近期状况，到时会自动失效）"
                logger.info("[settings] 同跳落事实 %d 条", len(kept))
        if action == "undo":  # 长撤销句（如"把刚才那个改动撤了"）
            logger.info("[settings] LLM 判定 undo")
            return {"final_reply": chat_config.undo_last_change(db)["reply"]}
        if action == "unclear" or action not in ("prefer", "unprefer", "setting"):
            question = (data.get("question") or "").strip() if isinstance(data, dict) else ""
            reply = question or "没完全听懂——想改哪项设置，或让我记住什么习惯？"
            return {"final_reply": reply + facts_note if facts_note else reply}
        logger.info("[settings] LLM 判定动作=%s", action)
        if action == "prefer":
            content = (data.get("content") or "").strip()
            if not content:
                return {"final_reply": "没听清这条偏好——把规则本身说完整一点？"}
            category = (data.get("category") or "其他").strip()
            result = chat_config.apply_preference(db, raw=raw, category=category, content=content)
        elif action == "unprefer":
            ref = (data.get("rule_ref") or "").strip()
            if not ref:
                return {"final_reply": "想撤掉哪条偏好？把那条规则的大意说一下。"}
            result = chat_config.remove_preference(db, rule_ref=ref)
        else:  # setting（单键或成对 pairs——午休起止/日历起止一句话两键，
        # 2026-11-01 真机批增：旧单 key schema 下「午休改12点到1点」被判 unclear）
            pairs = data.get("pairs")
            pairs = ([{"key": p.get("key"), "value": p.get("value")}
                      for p in pairs
                      if isinstance(p, dict) and isinstance(p.get("key"), str)
                      and "value" in p][:2]) if isinstance(pairs, list) else []
            if not pairs:
                if not isinstance(data.get("key"), str) or "value" not in data:
                    return {"final_reply": "没确定要改的设置项——说明白要改哪一项？"}
                pairs = [{"key": data.get("key"), "value": data.get("value")}]
            results = []
            for p in pairs:
                key, value = p["key"], p["value"]
                # 换算后验（2026-11-01 真机批）：「每天排25小时」被 LLM 错算成
                # 150 分钟（2.5h 挪小数点）且 150 落在 60-720 合法区间校验拦不
                # 住——原话明示 N 小时而分钟数对不上 N*60 时按原话数字纠偏
                #（25h→1500 由 update_settings 超上限 ValueError 原样回执）
                if key == "work_minutes_per_day" and isinstance(value, int):
                    m = re.search(r"(\d+(?:\.\d+)?)\s*个?小时", raw)
                    if m:
                        want = round(float(m.group(1)) * 60)
                        if value != want:
                            value = want
                results.append(chat_config.apply_setting(db, key=key, value=value))
            ok = [r for r in results if r.get("ok")]
            if ok:  # 回执拼接；成功的条逐 journal 并入（撤销锚按条反演）
                result = {"ok": True,
                          "reply": "；".join(r["reply"] for r in results),
                          "journals": [r["journal"] for r in ok]}
            else:
                result = {"ok": False, "reply": results[0]["reply"],
                          "journals": []}
        if result.get("ok") and result.get("journals"):
            chat_config.journal_change(db, result["journals"])
        elif result.get("ok"):  # prefer/unprefer 单动作路径
            chat_config.journal_change(db, [result["journal"]])
        reply = result["reply"]
        if facts_note:  # 事实附注（知情权：记了什么让用户看得见）
            reply = f"{reply}{facts_note}"
        return {"final_reply": reply}
