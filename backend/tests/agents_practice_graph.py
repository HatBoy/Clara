"""手写区参考实现：LangGraph ReAct 工具循环 + 人类中断（init_llm 版，2026-10-11）。

由用户练习代码 graph_test2.py 迁移而来——模型初始化换成 services/llm.py 的
init_llm()（读库内激活模型，无硬编码 key），其余结构（五节点 / 四分支条件路由 /
interrupt + Command(resume=...) / MemorySaver 检查点）与原练习一一对应，可作为
agents/ 手写起步的对照模板。

运行（需先激活模型：设置 → 模型接入；同 agents_practice.py 先例不以 test_ 前缀
命名，pytest 全库扫描不收集，直接 python 跑）：

    cd backend && python tests/agents_practice_graph.py

对照要点（原练习 → 本文件）：
- init_chat_model(model=..., api_key=..., base_url=...)  → init_llm()（key/地址全在库里）
- langchain.messages / langchain.tools                    → langchain_core.*（同一套对象，规范入口）
- 无护栏的工具循环                                          → should_continue 分支 0 加 llm_calls 上限
- input() 控制台交互                                        → 演示脚本以常量 "3" 恢复（生产对应前端/API 传值）
"""

import sys
from pathlib import Path
from typing import Literal

# 直接 python 运行时把 backend/ 挂进 sys.path（pytest 运行不需要，多一行无害）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from services.llm import init_llm

# ── 状态定义 ──────────────────────────────────────────────
# 继承官方 MessagesState（自带 add_messages reducer：新消息按 key 追加合并），加自定义字段


class AgentState(MessagesState):
    llm_calls: int        # LLM 调用计数（护栏 + 观测）
    tool_call_count: int  # 工具调用轮数（路由分支 3 依据）
    human_input: str      # ""=未中断过 / "processed"=人类指令已注入（防路由死循环）
    final_result: str     # 收尾出口（对应壳消费的 final_reply 之位）


# ── 模型初始化（与原练习的唯一实质差异）────────────────────
# init_llm 返回 LangChain BaseChatModel：invoke / bind_tools / stream 与
# init_chat_model 产物完全同构；激活模型、key、思考开关全由设置里的 ModelProvider 行决定
# 参数策略：temperature 默认 0（稳定可复现），要活泼/限制输出显式传参覆盖
model = init_llm()


# ── 工具定义（docstring 即工具描述，会随 schema 发给模型）──
@tool
def add(a: int, b: int) -> int:
    """将 a 和 b 相加。"""
    print("  [tool] add", a, b)
    return a + b


@tool
def multiply(a: int, b: int) -> int:
    """将 a 和 b 相乘。"""
    print("  [tool] multiply", a, b)
    return a * b


tools = [add, multiply]
model_with_tools = model.bind_tools(tools)  # 与官方 init_chat_model 产物同款用法

# ── 节点定义 ──────────────────────────────────────────────
SYSTEM_PROMPT = """你是一位专门负责算术运算的助理。
用户输入题目后，你必须调用工具进行计算，严禁自己直接输出答案。
每次只能调用一个工具，拿到工具结果后再决定下一步。"""

# 防死循环护栏（原练习未设，手写区建议保留：LLM 卡在工具循环时强制收尾）
_MAX_LLM_CALLS = 8


def llm_call(state: AgentState):
    """LLM 调用：重组 system + 历史，决定是否调用工具。"""
    print("[node] llm_call")
    response = model_with_tools.invoke([SystemMessage(content=SYSTEM_PROMPT)] + state["messages"])
    return {"messages": [response], "llm_calls": state.get("llm_calls", 0) + 1}


_tool_node = ToolNode(tools)  # 官方工具节点：读 AIMessage.tool_calls，产出 ToolMessage


def tool_node(state: AgentState):
    """包装官方 ToolNode，同时累加工具轮数（原练习同款手法）。"""
    print("[node] tool_node")
    result = _tool_node.invoke(state)
    return {"messages": result["messages"], "tool_call_count": state.get("tool_call_count", 0) + 1}


def should_continue(state: AgentState) -> Literal["tool_node", "human_input", "llm_call", "store_final_result"]:
    """四分支条件路由（llm_call 与 apply_coefficient 两个节点共用）。"""
    last_message = state["messages"][-1]

    # 0. 护栏：LLM 次数超限强制收尾
    if state.get("llm_calls", 0) >= _MAX_LLM_CALLS:
        print("[route] 超次数上限，强制收尾")
        return "store_final_result"

    # 1. 模型给出工具调用 → 执行工具
    if getattr(last_message, "tool_calls", None):
        return "tool_node"

    # 2. 人类刚发完新指令（apply_coefficient 注入）→ 送回 LLM
    if isinstance(last_message, HumanMessage) and state.get("human_input") == "processed":
        return "llm_call"

    # 3. 前半场工具已跑、尚未中断过 → 进人类中断
    if state.get("tool_call_count", 0) > 0 and not state.get("human_input"):
        return "human_input"

    # 4. 算完 / 无工具诉求 → 收尾
    return "store_final_result"


def human_input(state: AgentState):
    """中断等待人类输入系数：interrupt() 暂停图，等 Command(resume=值) 恢复。"""
    print("[node] human_input（中断，等待恢复）")
    user_input = interrupt({"prompt": "请输入最终结果系数X（输入q退出，其他默认系数1）: "})
    return {"human_input": user_input}


def apply_coefficient(state: AgentState):
    """把人类输入转成新 HumanMessage 注入消息流，强迫模型再调一轮工具。"""
    print("[node] apply_coefficient")
    user_input = str(state.get("human_input", "1")).strip()
    if user_input.lower() == "q":
        return {"final_result": "用户退出"}
    new_instruction = HumanMessage(content=f"请将上一步得到的计算结果，乘以系数 {user_input}，并给出最终答案。")
    # 重置 human_input="processed"：既让分支 2 把路由送回 llm_call，又防分支 3 再次触发中断
    return {"messages": [new_instruction], "human_input": "processed"}


def store_final_result(state: AgentState):
    """收尾节点：把最后一条 AI 文本同步到 final_result（对应壳消费的出口键）。"""
    print("[node] store_final_result")
    return {"final_result": state["messages"][-1].content}


# ── 构建图 ────────────────────────────────────────────────
agent_builder = StateGraph(AgentState)

agent_builder.add_node("llm_call", llm_call)
agent_builder.add_node("tool_node", tool_node)
agent_builder.add_node("human_input", human_input)
agent_builder.add_node("apply_coefficient", apply_coefficient)
agent_builder.add_node("store_final_result", store_final_result)

agent_builder.add_edge(START, "llm_call")
agent_builder.add_conditional_edges("llm_call", should_continue, ["tool_node", "human_input", "store_final_result"])
agent_builder.add_edge("tool_node", "llm_call")  # 工具执行完回到 LLM（ReAct 循环回边）
agent_builder.add_edge("human_input", "apply_coefficient")
# apply_coefficient 后由 should_continue 决定去向（注入 HumanMessage 后走分支 2 回 llm_call）
agent_builder.add_conditional_edges("apply_coefficient", should_continue, ["llm_call", "store_final_result"])
agent_builder.add_edge("store_final_result", END)

checkpointer = MemorySaver()  # 内存检查点：中断挂起/恢复的载体（重启丢失，正式接 SQLite 再换）
agent = agent_builder.compile(checkpointer=checkpointer)
config = {"configurable": {"thread_id": "user1"}}


# ── 运行（两段式：跑到中断 → resume 恢复）─────────────────
if __name__ == "__main__":
    inputs = {"messages": [HumanMessage(content="3加4，结果再乘以5")]}

    print("=== 第一次启动（跑到 interrupt 暂停）===")
    outputs = agent.invoke(inputs, config)
    for m in outputs["messages"]:
        m.pretty_print()

    # 生产中此处对应前端/API 传回的人类输入；演示脚本以常量恢复
    user_val = "3"
    print(f"\n=== 恢复执行（Command(resume={user_val!r})）===")
    final_outputs = agent.invoke(Command(resume=user_val), config)
    for m in final_outputs["messages"][len(outputs["messages"]):]:
        m.pretty_print()

    print("\n=== final_result ===")
    print(final_outputs.get("final_result"))
