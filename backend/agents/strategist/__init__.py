"""幕僚长子图（2026-10-21 Track B 重构批 1）：麦肯锡式私人顾问 4 节点管道。

拓扑（条件边三岔）：
    START → classifier（Node1 场景嗅探：cron 直打 / pending 消费 / 危机正则 /
            LLM 分类 + clarify 挂起）
          → 三岔：终态（final_reply 已写：消费轮 / 降级）→ END；
                  挂起（question_plan 已写：clarify / memory 问询）→ END；
                  其余 → context_fetcher（Node2 条件取数）→ brain（Node3 七场景
                  Prompt 路由）→ dispatcher（Node4 动作分发 + 记忆固化）→ END

langgraph 1.2.11 子图语义规避（同 scheduler 先例）：不用 compiled-as-node——
主图经 graph.py 的 strategist_node 包装函数 invoke 本单例（防御性包装，显式
映射进出键）。
"""

from langgraph.graph import END, START, StateGraph

from .brain import brain_node
from .classifier import classifier_node
from .context_fetcher import context_fetcher_node
from .dispatcher import dispatcher_node
from .state import StrategistState


def _route_after_classify(state: StrategistState) -> str:
    """classifier 出口三岔（条件边，永不抛错）：
    - final_reply 已写（pending 消费终态 / 批 4 垃圾降级）→ END；
    - question_plan 已写（clarify 追问 / memory 确认问询挂起）→ END；
    - 其余 → context_fetcher 续全管道。"""
    if state.get("final_reply") or state.get("question_plan"):
        return END
    return "context_fetcher"


def build_strategist():
    """编译幕僚长子图（checkpointer 不挂——与主图裸编译同口径，包装节点
    invoke 零 config）。"""
    b = StateGraph(StrategistState)
    b.add_node("classifier", classifier_node)
    b.add_node("context_fetcher", context_fetcher_node)
    b.add_node("brain", brain_node)
    b.add_node("dispatcher", dispatcher_node)
    b.add_edge(START, "classifier")
    b.add_conditional_edges("classifier", _route_after_classify)
    b.add_edge("context_fetcher", "brain")
    b.add_edge("brain", "dispatcher")
    b.add_edge("dispatcher", END)
    return b.compile()


# 子图单例（strategist_node 包装与 run_preflight cron 轮驱动；模块级编译一次）
strategist_app = build_strategist()
