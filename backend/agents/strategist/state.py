"""幕僚长子图 State 定义（2026-10-21 Track B 重构批 1）。

规格六键（用户定稿架构）：trigger_input / scenario_type / deep_context /
strategy_output / action_items / memory_updates——四节点管道的主干数据。
批 1 骨架形态：六键均未填充（节点占位），批 2 起 classifier 填
trigger_input/scenario_type、批 3 起 context_fetcher 填 deep_context、
批 4 起 brain 填 strategy_output/action_items/memory_updates。

契约提醒：
- 出口键（final_reply/preflight/draft_plan/inbox_items/resolved_proposal/
  question_plan）值形态与主图 AgentState 九键完全一致——子图由 graph.py 的
  strategist_node 包装节点驱动，只回填非空出口键（九键子集，接缝契约冻结，
  docs/ai-agents.md §6.2）；
- 流水即规格键本身（不设下划线中间键）——classifier 早退路径直接回填出口键
  由条件边接 END，条目级下划线键出口前由 dispatcher 统一剥离。
"""

from typing import TypedDict


class StrategistState(TypedDict, total=False):
    # —— 入口四键（graph.py strategist_node 包装注入；前三键与主图同形，
    #    run_preflight cron 轮注入 trigger=morning/nightly）——
    user_input: str                  # chat 轮 = 用户原文；cron 轮 ""
    memory_rules: list[str]          # 人格前置 personality_rules 后的记忆快照
    pending: dict | None             # active_pending 快照（origin 派生键随行）；cron 轮恒 None
    trigger: str                     # "chat" | "morning" | "nightly"

    # —— 规格① trigger_input：classifier 置（chat=user_input；cron=tag 本身；
    #    clarify 恢复轮 = 原始诉求 + "\n（补充）" + 用户补充）——
    trigger_input: str

    # —— 规格② scenario_type：七场景枚举（SCENARIOS 单源）——
    scenario_type: str

    # —— 规格③ deep_context：Node2 按场景条件取数快照（键集随场景变化，
    #    ORM 已转 dict——跨节点不摸 detached ORM）——
    deep_context: dict
    now: str                          # 轮基准 ISO 时刻（取数窗口与文案共用）

    # —— 规格④ strategy_output：Node3 麦肯锡大脑 Markdown 长文 ——
    strategy_output: str
    checklist: list[str]              # Node3 钳制后要点清单（≤6 条 ≤40 字，preflight 卡专用）

    # —— 规格⑤ action_items：LLM 意图清单（未转卡）
    #    [{title≤30, duration?, task_id?, action: schedule|inbox|reschedule}] ——
    action_items: list[dict]

    # —— 规格⑥ memory_updates：LLM 记忆提案
    #    [{category≤30, content≤200, evidence≤200}]（夜间复盘场景产出）——
    memory_updates: list[dict]

    # —— 出口键（九键子集，dispatcher 白名单回填非空）——
    final_reply: str                  # chat 轮 = strategy_output + 产物附注；消费轮 = 确认文案
    preflight: dict                   # {headline, checklist}（晨/夜 cron 轮必出；chat 轮 checklist 非空也出）
    draft_plan: list[dict]            # planner 规则直排产出的排期草案条目
    inbox_items: list[dict]           # 无估时 / 无槽条目（kind=inbox 卡）
    resolved_proposal: dict           # pending 结算 {proposal_id, outcome}
    question_plan: dict               # clarify 追问 / memory 确认问询
