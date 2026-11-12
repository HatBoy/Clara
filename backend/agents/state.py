"""LangGraph 智能体的 State 定义（阶段二实现）。

State 是贯穿整张图的共享数据（TypedDict），节点读写它、边按它路由。
字段随实现扩展，典型形态：用户输入、检索到的日程上下文、AI 中间输出、最终回复。

契约提醒（TypedDict 漏键不报错，全靠定义齐全）：
- initial_state 由壳注入，恒含 user_input / memory_rules 两键（docs/ai-agents.md §6.1），
  排期问询待决时另注入 pending 快照（2026-10-17 增）
- final state 壳消费九键：final_reply / draft_plan / preflight / inbox_items / evidence
  （§6.2）+ applied_plan / question_plan / resolved_proposal（2026-10-17 增，见各键注释），
  其余键忽略
- memory_rules 已按注入序排好，预算截断直接头部切片，不要重排
"""

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    user_input: str                 #用户输入
    memory_rules: list[str]         #用户记忆画像规则（core+reference 合并单列表，消费方契约不变）
    core_memory: list[str]          #铁律段（2026-11-03 批 3 双段注入：壳注入，qa/smalltalk 经 assemble_system_prompt(core_rules=) 双段渲染；缺省视同空）
    intent: str                     #用户意图条件边（schedule / qa / coach / smalltalk / ingest / delete 六分类，center_router 写入）
    schedule_context: dict          #fetch_recent 完整信封（collect_data 确定性采集写入，refine 消费）
    dedup_titles: list[str]         #Reduce 语义去重对照集（collect_data 写入，refine 消费）
    draft_plan: list[dict]          #日程规划草稿 → 出口（壳建 schedule 建议卡，agent 推荐流；用户明确时间的走 applied_plan 直落）
    inbox_items: list[dict]         #收件箱建议清单（无时间线索的提炼事项）→ 出口（壳建 inbox 卡）
    minutes_meta: dict              #会后纪要元信息（minutes_extractor 写入，2026-11-10 批 2：meeting_*/headline/source_text）→ 出口（inbox_items 同轮存在时壳改建 kind=minutes 卡）
    evidence: list[dict]            #日程草案溯源数组（idx 对齐 draft_plan 序）→ 出口（并入卡 payload）
    preflight: dict                 #任务预先准备，strategist 子图 → 出口
    final_reply: str                #终节点 → SSE 出口
    delete_plan: list[dict]         #删除确认清单（delete_confirmer 写入 → 壳建 kind=delete 确认卡，用户确认才真删）
    pending: dict                   #活跃问询快照（壳注入：最后一条带建议行的 assistant 消息挂 kind=question 未决行；无则缺省——"可以"这类确认回复靠它关联上一轮，2026-10-17 增）
    applied_plan: list[dict]        #可直接落库条目（origin=user 明确时间且全过校验）→ 出口（壳调 services/tasks 落库 + applied 事件 + 行直置 accepted）
    question_plan: dict             #待确认问询（conflict/similar/no_time/past）→ 出口（壳建 kind=question 行挂消息锚点，零卡零事件）
    resolved_proposal: dict         #本轮消费的 pending 结算 {proposal_id, outcome} → 出口（壳回写旧问询行）
    bulk: bool                      #批量排期短路标记（scheduler 子图 profiler 写入，2026-10-25）→ 出口（壳改发 bulk 事件驱动前端弹窗，草案不出对话框）
