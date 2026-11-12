"""排期子图（2026-10-21 私人秘书化重构批 2）：私人秘书式 4 节点管道。

拓扑（批 4 起条件边三岔）：
    START → intake（纯代码取数快照）→ profiler（LLM① 剖析 + 确定性尾分流）
          → 三岔：终态（降级/两分钟劝导）→ END；
                  待排清零（全直落/全问询）→ 直达 adjudicator（跳过
                  context/finder 空转——快录轮仅 router+profiler 两次 LLM）；
                  其余 → context（批 5 条件聚合）→ finder（引擎轮 → 批 5
                  十道硬约束）→ adjudicator（批 6 裁决 + 回复组装）→ END

langgraph 1.2.11 子图语义规避（计划风险表）：不用 compiled-as-node——主图
经 graph.py 的 scheduler_node 包装函数 invoke 本单例（防御性包装，显式
映射进出键）。
"""

from langgraph.graph import END, START, StateGraph

from .adjudicator import adjudicator_node
from .context import context_node
from .finder import finder_node
from .intake import intake_node
from .profiler import profiler_node
from .state import SchedulerState


def _route_after_profile(state: SchedulerState) -> str:
    """profiler 出口三岔（条件边，永不抛错）：
    - final_reply 已写（垃圾降级 / 两分钟劝导终态）→ END；
    - conflict_queue 与 agent_entries 全空（全直落/全问询）→ 直达
      adjudicator 组装回复——跳过 context/finder 空转（批 5 后 context 有
      真实取数成本，快录直落轮不该付）；
    - 其余 → context 续全管道。"""
    if state.get("final_reply"):
        return END
    if not (state.get("conflict_queue") or state.get("agent_entries")):
        return "adjudicator"
    return "context"


def build_scheduler():
    """编译排期子图（checkpointer 不挂——与主图裸编译同口径，包装节点
    invoke 零 config）。"""
    b = StateGraph(SchedulerState)
    b.add_node("intake", intake_node)
    b.add_node("profiler", profiler_node)
    b.add_node("context", context_node)
    b.add_node("finder", finder_node)
    b.add_node("adjudicator", adjudicator_node)
    b.add_edge(START, "intake")
    b.add_edge("intake", "profiler")
    b.add_conditional_edges("profiler", _route_after_profile)
    b.add_edge("context", "finder")
    b.add_edge("finder", "adjudicator")
    b.add_edge("adjudicator", END)
    return b.compile()


# 子图单例（scheduler_node 包装驱动；模块级编译一次，与主图 build_graph 由
# 壳按需调用的差异：子图无参数化编译需求）
scheduler_app = build_scheduler()
