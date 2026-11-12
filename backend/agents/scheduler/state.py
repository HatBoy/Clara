"""排期子图 State 定义（2026-10-21 私人秘书化重构批 2）。

规格五键（用户定稿架构）：raw_input / task_profile / context /
candidate_slots / final_decision——四节点管道的主干数据。批 2 骨架形态：
- raw_input / context 已由 intake 填充（context 含旧 planner 取数段全量快照）；
- task_profile 暂存 LLM 原始解析（旧 PLANNER schema），批 4 正式化为六类剖析；
- candidate_slots 暂为引擎槽位快照（每条目单槽），批 5 泛化多候选 + 硬约束；
- final_decision 批 6 落（动态组 Prompt 裁决 + 后验回落）。

契约提醒：
- 出口键（draft_plan/final_reply/applied_plan/question_plan/resolved_proposal）
  值形态与主图 AgentState 九键完全一致——子图由 graph.py 的 scheduler_node
  包装节点驱动，只回填非空出口键（接缝契约冻结，docs/ai-agents.md §6.2）；
- 节点间流水键（busy/applied/question/...）是子图内部实现细节，壳永不消费。
"""

from datetime import datetime
from typing import TypedDict


class SchedulerState(TypedDict, total=False):
    # —— 入口（graph.py scheduler_node 包装注入，与主图三键同形）——
    user_input: str
    memory_rules: list[str]
    pending: dict | None

    # —— 规格① raw_input：intake 置 = user_input（后续节点一律读它，
    #    与 user_input 分离为批 4 归一改写留位）——
    raw_input: str

    # —— 规格③ context：intake 基础取数快照（candidates/scheduled/settings/
    #    lunch/day_budget/load_summary/recent_chat/weather/busy/similar_pool/
    #    valid_ids/prio_by_id/day_start_min/day_end_min），批 5 Node2 条件聚合扩展 ——
    context: dict
    now: datetime                    # intake 冻结的轮基准时刻（全管道共用，不再各自 now()）
    philosophy_ids: list[str]        # intake：启用哲学 id 清单（批 4 两分钟短路消费）

    # —— 规格② task_profile：Node1 LLM 解析产物（批 2 = 旧 PLANNER 原始 dict）——
    task_profile: dict

    # —— profiler 确定性尾的分流产物（流水键）——
    busy: list                       # profiler 追加直落条目占用后的 busy（finder 引擎轮消费）
    applied: list[dict]              # 快录直落条目（adjudicator 转 applied_plan 出口）
    question: dict | None            # 首个问询（similar/past/no_time；first-wins 跨节点延续）
    resolved: dict | None            # pending 结算回写 {proposal_id, outcome}
    conflict_queue: list[dict]       # 撞期条目（finder 统一引擎算改期建议后转 conflict 问询）
    agent_entries: list[dict]        # 时间待引擎定的 agent 草案条目
    dropped: list[str]               # 校验丢弃附注（finder 追加无空档条目）
    insist_clash: list[str]          # insisted 落库但撞既有日程的标题（回复附注用）
    prefer_clash: list[str]          # 直落撞偏好避让窗的附注（批 5：照落不拦，提醒用）
    llm_reply: str                   # LLM 零产物兜底文案
    displace_drafts: list[dict]      # displace 挡路者改期条目（adjudicator 并入 draft_plan 出口）
    displace_notes: list[str]        # displace 系列/无槽挡路者附注（回复组装消费）
    displace_done: bool              # 本轮走了 displace 消费（adjudicator llm_reply 前置判据）

    # —— 规格④ candidate_slots：Node3 输出 [{key, title, slot}]（批 5 泛化）——
    candidate_slots: list[dict]
    clean: list[dict]                # finder 从引擎槽位产的 draft 草案条目

    # —— 规格⑤ final_decision：Node4 输出（批 6 落）——
    final_decision: dict

    # —— 出口键（与主图 AgentState 契约一致，adjudicator 组装）——
    draft_plan: list[dict]
    final_reply: str
    applied_plan: list[dict]
    question_plan: dict
    resolved_proposal: dict
    bulk: bool                      # 批量排期短路标记（profiler 写，2026-10-25 功能
                                    # 边界批）：壳改发 bulk 事件驱动前端弹窗清单确认
