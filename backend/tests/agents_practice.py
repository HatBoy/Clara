"""手写区练习自测：实现一步跑一步（docs/ai-agents.md §四练习路径的验收工具）。

本文件不以 test_ 前缀命名，pytest 全库扫描（默认 python_files=test_*.py）不会收集，
基线保持 310 passed 不受练习代码影响；手动指定路径运行（pytest 对显式路径跳过
文件名模式匹配，两全）：

    cd backend && python -m pytest tests/agents_practice.py -q       # 全部
    python -m pytest tests/agents_practice.py -q -k call_llm         # 单选

test_practice_call_llm 需真联网与激活模型（设置 → 模型接入）；未激活时报
NoActiveModelError，观察其形态也是第 1 步的收获之一。

替身先例索引（写不联网的断言时照抄这些模式）：
- LLM 替身：tests/test_llm.py:24（monkeypatch llm.litellm.completion 为假 completion）
  + tests/test_llm.py:42（_use_test_db：替换 llm.SessionLocal 为测试库）
- 图替身：tests/test_ai_coach.py:18 / :31（FakeGraph + _patch_graph，锚点 ai_coach._load_graph）
- 工作流库替身：tests/test_workflows.py:17（_use_test_db）

⚠ 库隔离陷阱：call_llm 内部自开真实 SessionLocal 读激活模型——测试里只隔离你传入
的 db 不够，必须按 test_llm.py:44 那样替换 llm.SessionLocal 才真正隔离。
"""

from services.llm import call_llm


def test_practice_call_llm():
    """第 1 步冒烟：链路通 = 激活模型配置正确、litellm 协议无误。"""
    reply = call_llm([{"role": "user", "content": "回复：pong"}])
    assert reply


# —— 第 2 步起解开使用（build_graph 实现后）——
#
# def test_practice_graph_invoke():
#     """第 2 步验收：以壳同款初态 invoke，断言契约键。"""
#     from agents.graph import build_graph
#
#     final = build_graph().invoke({"user_input": "你好", "memory_rules": []})
#     assert isinstance(final.get("final_reply", ""), str)
