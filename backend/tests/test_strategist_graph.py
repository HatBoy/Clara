"""幕僚长子图测试（Track B 批 1 起）：条件边三岔直调。全管道 e2e 分散在各
节点测试文件（classifier 消费矩阵 test_strategist_classifier / context 取数
test_strategist_context / brain 与危机直穿 e2e test_strategist_brain）；
dispatcher+planner 与出口（批 5）、run_preflight 收编回归（批 6）后续批次落。
"""

from langgraph.graph import END

from agents.strategist import _route_after_classify, strategist_app


def test_graph_compiles_to_singleton():
    # 模块级单例即编译成功（build_strategist 拓扑五节点）
    assert strategist_app is not None


def test_route_after_classify_three_way():
    assert _route_after_classify({"final_reply": "已写入"}) == END
    assert _route_after_classify(
        {"question_plan": {"kind": "strategist_clarify"}}) == END
    assert _route_after_classify({"scenario_type": "crisis"}) == "context_fetcher"
    assert _route_after_classify({}) == "context_fetcher"
