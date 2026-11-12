"""services/llm.py 测试（基建见 conftest.py）。

call_llm / init_llm 内部自开 SessionLocal 读激活模型——测试把 llm.SessionLocal 换成
TestingSessionLocal（内存库），litellm.completion 换成捕获 kwargs 的替身
（不真联网，同 test_model_providers 替身先例）。

替身形状注意（2026-10-11 ChatLiteLLM 引擎起）：官方引擎以 dict-like 消费响应
（response.get("usage") 等），假响应必须是纯 dict；流式替身返回 dict chunk 迭代器，
且 tool_calls 分片须键全在（真实 litellm 把 pydantic delta model_dump 后缺失值为
None，键本身存在——缺键会被官方转换器 KeyError 整批丢弃）。
"""

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from conftest import TestingSessionLocal
from models import ModelProvider
from services import llm


def _patch_completion(monkeypatch, content="你好", tool_calls=None, reasoning=None) -> dict:
    """把 litellm.completion 换成返回固定 dict 响应的替身，返回捕获 dict。"""
    seen: dict = {}

    def fake(**kwargs):
        seen["kwargs"] = kwargs
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm.litellm, "completion", fake)
    return seen


def _patch_completion_stream(monkeypatch, deltas: list[dict]) -> dict:
    """流式替身：deltas 为逐 chunk 的 delta dict 列表，返回 dict chunk 迭代器。"""
    seen: dict = {}

    def fake(**kwargs):
        seen["kwargs"] = kwargs
        return iter([{"choices": [{"delta": d}]} for d in deltas])

    monkeypatch.setattr(llm.litellm, "completion", fake)
    return seen


def _activate(**overrides) -> None:
    """在测试库造一行激活模型（context_length 等可覆盖）。"""
    with TestingSessionLocal() as db:
        db.add(
            ModelProvider(
                name=f"m-{uuid.uuid4().hex[:6]}",
                model_name="deepseek/deepseek-chat",
                is_active=True,
                **overrides,
            )
        )
        db.commit()


def _use_test_db(monkeypatch) -> None:
    """llm 读库会话指向内存测试库。"""
    monkeypatch.setattr(llm, "SessionLocal", TestingSessionLocal)


# —— 正常路径与调用形状（call_llm 薄壳经 init_llm 引擎，两入口共用一条路径） ——


def test_call_llm_ok_returns_content_and_kwargs(monkeypatch):
    _use_test_db(monkeypatch)
    _activate(api_key="sk-test", base_url="https://relay.example.com/v1")
    seen = _patch_completion(monkeypatch, content="早上好")
    out = llm.call_llm([{"role": "user", "content": "打招呼"}])
    assert out == "早上好"
    kw = seen["kwargs"]
    assert kw["model"] == "deepseek/deepseek-chat"
    assert kw["api_key"] == "sk-test"
    # ChatLiteLLM 引擎把构造的 base_url 以 api_base 键落到 litellm.completion
    assert kw["api_base"] == "https://relay.example.com/v1"
    assert kw["messages"] == [{"role": "user", "content": "打招呼"}]
    assert kw["timeout"] == llm._CALL_TIMEOUT_S


def test_call_llm_empty_key_placeholder_and_official_route(monkeypatch):
    # 免鉴权本地端点：api_key 空 → 占位符 "EMPTY"；base_url 空 → api_base None（官方路由）
    _use_test_db(monkeypatch)
    _activate(api_key=None, base_url=None)
    seen = _patch_completion(monkeypatch)
    llm.call_llm([{"role": "user", "content": "hi"}])
    assert seen["kwargs"]["api_key"] == "EMPTY"
    # 官方路由：base_url 空时引擎整体省略 api_base 键（get 兜 None）
    assert seen["kwargs"].get("api_base") is None


def test_call_llm_json_mode(monkeypatch):
    _use_test_db(monkeypatch)
    _activate()
    seen = _patch_completion(monkeypatch)
    llm.call_llm([{"role": "user", "content": "分类"}], json_mode=True)
    assert seen["kwargs"]["response_format"] == {"type": "json_object"}


def test_call_llm_thinking_mode_maps_reasoning_effort(monkeypatch):
    _use_test_db(monkeypatch)
    _activate(thinking_mode=True)
    seen = _patch_completion(monkeypatch)
    llm.call_llm([{"role": "user", "content": "深思"}])
    assert seen["kwargs"]["reasoning_effort"] == llm._REASONING_EFFORT
    # provider 不支持的可选参数静默丢弃（zai/GLM 无 reasoning_effort 实测踩坑）
    assert seen["kwargs"]["drop_params"] is True


def test_call_llm_default_no_extra_kwargs(monkeypatch):
    # 未开 json_mode / thinking_mode 时不多传任何可选参数；
    # temperature 默认 0（2026-10-11 定策：内置固定消除跨厂商默认值漂移，调用点可覆盖）
    _use_test_db(monkeypatch)
    _activate(thinking_mode=False)
    seen = _patch_completion(monkeypatch)
    llm.call_llm([{"role": "user", "content": "闲聊"}])
    assert seen["kwargs"]["temperature"] == 0
    assert "response_format" not in seen["kwargs"]
    assert "reasoning_effort" not in seen["kwargs"]


# —— 异常路径 ——


def test_call_llm_no_active_model(monkeypatch):
    # 零激活态：不发请求直接抛 NoActiveModelError
    _use_test_db(monkeypatch)
    seen = _patch_completion(monkeypatch)
    try:
        llm.call_llm([{"role": "user", "content": "hi"}])
        raise AssertionError("应抛 NoActiveModelError")
    except llm.NoActiveModelError:
        pass
    assert "kwargs" not in seen  # 替身未被调用


def test_call_llm_context_overflow_blocks_request(monkeypatch):
    # 8K 档 + 超长输入：前置拦截抛 ContextOverflowError，替身未被调用
    _use_test_db(monkeypatch)
    _activate(context_length=8192)
    seen = _patch_completion(monkeypatch)
    long_messages = [{"role": "user", "content": "长" * 40_000}]
    try:
        llm.call_llm(long_messages)
        raise AssertionError("应抛 ContextOverflowError")
    except llm.ContextOverflowError:
        pass
    assert "kwargs" not in seen


def test_call_llm_max_tokens_tightens_budget(monkeypatch):
    # 显式 max_tokens 收紧输入预算：同长度输入默认档可过、大 max_tokens 档被拦
    _use_test_db(monkeypatch)
    _activate(context_length=8192)
    _patch_completion(monkeypatch)
    messages = [{"role": "user", "content": "字" * 5_000}]  # 估算约 2508 token
    assert llm.call_llm(messages) != ""  # 默认预留 1024：2508 < 8192*0.9-1024=6348，放行
    try:
        llm.call_llm(messages, max_tokens=6_000)  # 预算 7372-6000=1372 < 2508，拦截
        raise AssertionError("应抛 ContextOverflowError")
    except llm.ContextOverflowError:
        pass


# —— estimate_tokens 纯函数 ——


def test_estimate_tokens_empty():
    assert llm.estimate_tokens([]) == 0


def test_estimate_tokens_formula():
    messages = [
        {"role": "system", "content": "a" * 10},
        {"role": "user", "content": "你好世界"},  # 4 字符 → 2 token
    ]
    # 10//2 + 8 + 4//2 + 8 = 5+8+2+8 = 23
    assert llm.estimate_tokens(messages) == 23


def test_estimate_tokens_missing_content():
    # 缺 content 键的消息只算固定开销，不炸
    assert llm.estimate_tokens([{"role": "user"}]) == llm._MESSAGE_OVERHEAD_TOKENS


def test_estimate_tokens_langchain_messages():
    # ClaraChatModel 路径传消息对象，同样公式粗估
    assert llm.estimate_tokens([HumanMessage("你好世界")]) == 2 + llm._MESSAGE_OVERHEAD_TOKENS


# —— init_llm 工厂 ——


def test_init_llm_no_active_model(monkeypatch):
    _use_test_db(monkeypatch)
    seen = _patch_completion(monkeypatch)
    try:
        llm.init_llm()
        raise AssertionError("应抛 NoActiveModelError")
    except llm.NoActiveModelError:
        pass
    assert "kwargs" not in seen


def test_init_llm_maps_provider_row(monkeypatch):
    # 激活行字段 → 实例字段 / model_kwargs（EMPTY 与 None 归一、drop_params 恒开）
    _use_test_db(monkeypatch)
    _activate(api_key=None, base_url=None, thinking_mode=False, context_length=65536)
    m = llm.init_llm(temperature=0.3, max_tokens=800)
    assert m.model == "deepseek/deepseek-chat"
    assert m.api_key == "EMPTY"
    assert m.api_base is None  # 构造别名 base_url 在引擎内部落为 api_base 字段
    assert m.temperature == 0.3
    assert m.max_tokens == 800
    assert m.context_length == 65536
    assert m.model_kwargs["drop_params"] is True
    assert "reasoning_effort" not in m.model_kwargs


def test_init_llm_json_mode_response_format(monkeypatch):
    _use_test_db(monkeypatch)
    _activate()
    seen = _patch_completion(monkeypatch)
    llm.init_llm(json_mode=True).invoke([HumanMessage("分类")])
    assert seen["kwargs"]["response_format"] == {"type": "json_object"}


# —— ClaraChatModel：消息转换 / 工具 / 思考内容 ——


def _model(**overrides):
    """绕过读库直接构造实例（测试专用；thinking 等在 model_kwargs 显式给）。"""
    base = dict(
        model="deepseek/deepseek-chat",
        api_key="sk-test",
        context_length=32768,
        model_kwargs={"drop_params": True},
    )
    base.update(overrides)
    return llm.ClaraChatModel(**base)


def test_generate_langchain_messages(monkeypatch):
    # SystemMessage/HumanMessage 进 → 引擎转 OpenAI dict 发给 litellm
    seen = _patch_completion(monkeypatch, content="收到")
    r = _model().invoke([SystemMessage("你是日程助手"), HumanMessage("你好")])
    assert seen["kwargs"]["messages"] == [
        {"role": "system", "content": "你是日程助手"},
        {"role": "user", "content": "你好"},
    ]
    assert r.content == "收到"
    assert isinstance(r, AIMessage)


def test_generate_dict_input_roundtrip(monkeypatch):
    # dict 直塞 invoke（基类 coercion）同样落 OpenAI dict——call_llm 路径的往返幂等哨兵，
    # 防 langchain-core 升级静默破坏 coercion 契约
    seen = _patch_completion(monkeypatch)
    _model().invoke([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert seen["kwargs"]["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


def test_generate_tool_calls_to_langchain_format(monkeypatch):
    # litellm 响应 tool_calls（OpenAI 格式，arguments 为 JSON 串）→ AIMessage.tool_calls
    _patch_completion(
        monkeypatch,
        content="",
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": '{"a": 1}'}}],
    )
    r = _model().invoke([HumanMessage("算 1+1")])
    assert r.tool_calls == [{"name": "add", "args": {"a": 1}, "id": "call_1", "type": "tool_call"}]


def test_generate_reasoning_content_extracted(monkeypatch):
    # litellm 统一思考字段 → AIMessage.additional_kwargs["reasoning_content"]
    _patch_completion(monkeypatch, content="答案", reasoning="思考过程")
    r = _model().invoke([HumanMessage("深思")])
    assert r.additional_kwargs["reasoning_content"] == "思考过程"


def test_generate_strips_reasoning_on_resend(monkeypatch):
    # 多轮回传：引擎默认会把 reasoning_content 发回（DeepSeek 拒收 400），
    # 子类加固剥离——本用例是该剥离行为的哨兵（上游行为变化响亮失败）
    seen = _patch_completion(monkeypatch)
    history = [
        SystemMessage("s"),
        HumanMessage("hi"),
        AIMessage(content="ans", additional_kwargs={"reasoning_content": "思考"}),
        ToolMessage(content="2", tool_call_id="call_1"),
        HumanMessage("继续"),
    ]
    _model().invoke(history)
    sent = seen["kwargs"]["messages"][2]
    assert "reasoning_content" not in sent
    assert sent["role"] == "assistant"
    # 原消息对象不被就地修改（调用方历史无副作用）
    assert history[2].additional_kwargs["reasoning_content"] == "思考"


def test_bind_tools_openai_format(monkeypatch):
    # bind_tools 接受 @tool 函数 → litellm 收到 OpenAI tool 格式
    @tool
    def add(a: int, b: int) -> int:
        """加法"""
        return a + b

    seen = _patch_completion(monkeypatch)
    _model().bind_tools([add]).invoke([HumanMessage("算 1+2")])
    tools = seen["kwargs"]["tools"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "add"
    assert "parameters" in tools[0]["function"]


def test_generate_context_overflow_direct_model(monkeypatch):
    # 拦截在模型实例内（不经 init_llm 也生效）：8K 档 + 超长输入，替身未被调
    seen = _patch_completion(monkeypatch)
    try:
        _model(context_length=8192).invoke([HumanMessage("长" * 40_000)])
        raise AssertionError("应抛 ContextOverflowError")
    except llm.ContextOverflowError:
        pass
    assert "kwargs" not in seen


# —— 流式（stream 聚合：content / reasoning_content / tool_calls 三通道） ——


def test_stream_aggregates_content_and_reasoning(monkeypatch):
    _patch_completion_stream(
        monkeypatch,
        [
            {"role": "assistant", "reasoning_content": "思"},
            {"role": "assistant", "reasoning_content": "考"},
            {"role": "assistant", "content": "答"},
            {"role": "assistant", "content": "案"},
        ],
    )
    acc = None
    for chunk in _model().stream([HumanMessage("流式")]):
        acc = chunk if acc is None else acc + chunk
    assert acc.content == "答案"
    assert acc.additional_kwargs["reasoning_content"] == "思考"


def test_stream_aggregates_tool_call_fragments(monkeypatch):
    # 分片 arguments 按 index 拼接成完整 JSON——片段键全在、缺失值为 None（真实 litellm 形状）
    _patch_completion_stream(
        monkeypatch,
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "add", "arguments": '{"a"'}}
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {"index": 0, "id": None, "type": None,
                     "function": {"name": None, "arguments": ": 1}"}}
                ],
            },
        ],
    )
    acc = None
    for chunk in _model().stream([HumanMessage("调工具")]):
        acc = chunk if acc is None else acc + chunk
    assert acc.tool_calls == [{"name": "add", "args": {"a": 1}, "id": "call_1", "type": "tool_call"}]


# —— call_llm_stream（流式便捷口：on_chunk 只收正文增量，聚合返回全文） ——


def test_call_llm_stream_calls_on_chunk_and_returns_full(monkeypatch):
    # chunks 混 reasoning_content 与 content：on_chunk 只收正文逐段（思考不外漏），返回全文
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch,
        [
            {"role": "assistant", "reasoning_content": "思"},
            {"role": "assistant", "content": "今天"},
            {"role": "assistant", "content": "有 3 件事"},
            {"role": "assistant", "reasoning_content": "考"},
            {"role": "assistant", "content": ""},
        ],
    )
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "今天有什么"}], on_chunk=got.append)
    assert got == ["今天", "有 3 件事"]  # 空段不回调、思考不进回调
    assert out == "今天有 3 件事"


def test_call_llm_stream_empty_content_no_callback(monkeypatch):
    # content 全空（纯思考模型形态）→ 零回调、返回 ""
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch, [{"role": "assistant", "reasoning_content": "思考"}]
    )
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "hi"}], on_chunk=got.append)
    assert got == []
    assert out == ""


def test_call_llm_stream_exception_propagates(monkeypatch):
    # 故障口径同 call_llm：completion 抛异常原样上抛，不吞不译
    _use_test_db(monkeypatch)
    _activate()

    def fake(**kwargs):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(llm.litellm, "completion", fake)
    try:
        llm.call_llm_stream([{"role": "user", "content": "x"}], on_chunk=lambda t: None)
        raise AssertionError("应抛 RuntimeError")
    except RuntimeError:
        pass


# —— _thinking_params 纯函数（per-provider 前缀分发） ——


@pytest.mark.parametrize(
    ("model_name", "expected_fragment"),
    [
        # anthropic 族：thinking + budget，且 max_tokens 抬升到 > budget
        ("anthropic/claude-sonnet-5", {"thinking": {"type": "enabled", "budget_tokens": llm._THINKING_BUDGET_TOKENS}}),
        ("vertex_ai/claude-sonnet-5", {"thinking": {"type": "enabled", "budget_tokens": llm._THINKING_BUDGET_TOKENS}}),
        ("bedrock/us.anthropic.claude", {"thinking": {"type": "enabled", "budget_tokens": llm._THINKING_BUDGET_TOKENS}}),
        # qwen 族：OpenAI 兼容 extra_body
        ("qwen/qwen3-max", {"extra_body": {"enable_thinking": True}}),
        ("dashscope/qwen3-max", {"extra_body": {"enable_thinking": True}}),
        # ollama：litellm think 参数
        ("ollama/qwen3", {"think": {"type": "enabled"}}),
        # 默认分支：reasoning_effort + drop_params 兜底（openai/deepseek/zai/...）
        ("deepseek/deepseek-reasoner", {"reasoning_effort": llm._REASONING_EFFORT}),
        ("zai/glm-5", {"reasoning_effort": llm._REASONING_EFFORT}),
    ],
)
def test_thinking_params_by_prefix(model_name, expected_fragment):
    params = llm._thinking_params(model_name, True, None)
    for key, value in expected_fragment.items():
        assert params[key] == value


def test_thinking_params_anthropic_raises_max_tokens():
    # 未指定上限（None）→ 抬到 > budget 的下限；给大值则尊重原值
    params = llm._thinking_params("anthropic/claude-sonnet-5", True, None)
    assert params["max_tokens"] == llm._ANTHROPIC_MIN_MAX_TOKENS  # 抬升下限
    assert params["max_tokens"] > params["thinking"]["budget_tokens"]
    big = llm._thinking_params("anthropic/claude-sonnet-5", True, 20_000)
    assert big["max_tokens"] == 20_000  # 用户给的更大则尊重


def test_thinking_params_small_max_tokens_skipped():
    # 小输出预算调用（router 100 / Map 600 / REFLECT 800 等结构化小调用）不注入
    # 任何思考参数（2026-10-21 复查批 C 增）：anthropic 思考预算会吃光输出、
    # qwen enable_thinking 与 json_mode 不兼容——该调用退化直答。
    # 2026-10-26 修订：qwen 族显式 enable_thinking=False 压平台默认（不传≠关，
    # 平台默认开思考时照样思考）；其余协议族默认不思考维持空 dict
    assert llm._thinking_params("qwen/qwen3-max", True, 800) == {
        "extra_body": {"enable_thinking": False}}
    for model in ("anthropic/claude-sonnet-5", "deepseek/deepseek-reasoner"):
        assert llm._thinking_params(model, True, 800) == {}


def test_call_llm_stream_filters_leading_think_inline(monkeypatch):
    # 前导 <think> 过滤（2026-10-26 提速批 1）：qwen3 开源部署把思考内联进 content
    # 且头尾标签拆在多个 chunk 间——on_chunk 只收闭合后的正文，思考段零外漏
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch,
        [
            {"role": "assistant", "content": "<thi"},
            {"role": "assistant", "content": "nk>先想一下</th"},
            {"role": "assistant", "content": "ink>你好"},
            {"role": "assistant", "content": "，世界"},
        ],
    )
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "嗨"}], on_chunk=got.append)
    assert got == ["你好", "，世界"]
    assert out == "你好，世界"


def test_call_llm_stream_unclosed_think_drops_all(monkeypatch):
    # 流终未闭合 </think> = 全思考无正文：返回空串零回调（对齐 loads_obj
    # "未闭合=截断不硬救"口径），由调用方空兜底话术
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch, [{"role": "assistant", "content": "<think>一直思考没闭合"}])
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "hi"}], on_chunk=got.append)
    assert got == []
    assert out == ""


def test_call_llm_stream_plain_text_zero_buffer(monkeypatch):
    # 正文开头即非 "<" 前缀：零缓冲逐块转发——打字机粒度不受过滤影响
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch,
        [{"role": "assistant", "content": "今"},
         {"role": "assistant", "content": "天有 3 件事"}],
    )
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "今天"}], on_chunk=got.append)
    assert got == ["今", "天有 3 件事"]
    assert out == "今天有 3 件事"


def test_call_llm_stream_plain_steady_frames_stay_incremental(monkeypatch):
    # 稳态多帧必须逐帧增量（2026-11-01 真机批回归防）：批 6 重构曾丢
    # plain 分支的 buf 清空行，第 3 帧起每帧发「旧缓冲+新增量」的累积快照
    # ——真机打字机/正文逐级重复（"今天是 ** **11 **11月…"形态）；两帧
    # 用例恰好测不出（帧 1 走 pending 转换已清、帧 2 才进稳态），须 ≥3 帧
    _use_test_db(monkeypatch)
    _activate()
    _patch_completion_stream(
        monkeypatch,
        [{"role": "assistant", "content": "今天是"},
         {"role": "assistant", "content": " **"},
         {"role": "assistant", "content": "11"},
         {"role": "assistant", "content": "月"},
         {"role": "assistant", "content": "1 日"}],
    )
    got: list[str] = []
    out = llm.call_llm_stream([{"role": "user", "content": "今天"}], on_chunk=got.append)
    assert got == ["今天是", " **", "11", "月", "1 日"]
    assert out == "今天是 **11月1 日"


def test_thinking_params_zero_thinking_branches_press_qwen_default():
    # thinking_mode=False / 小 max_tokens 两分支都显式压 qwen 族平台默认思考
    # （2026-10-26：不传≠关——平台默认开思考时照样思考，qa/smalltalk 流式首字
    # 延迟根因）；其余协议族默认不思考维持空 dict
    for kw in ({"thinking": False, "max_tokens": None},
               {"thinking": True, "max_tokens": 2000}):
        assert llm._thinking_params("qwen/qwen3-max", **kw) == {
            "extra_body": {"enable_thinking": False}}
        assert llm._thinking_params("deepseek/deepseek-chat", **kw) == {}


def test_timeout_tiers_by_max_tokens():
    # 小调用超时分档（2026-10-26 提速批 1）：max_tokens ≤ 200 的意图分类类调用
    # 45s，其余 120s（非流式分支黑屏上限按调用量级分档）
    assert llm._timeout_for(60) == llm._SMALL_CALL_TIMEOUT_S
    assert llm._timeout_for(200) == llm._SMALL_CALL_TIMEOUT_S
    assert llm._timeout_for(600) == llm._CALL_TIMEOUT_S
    assert llm._timeout_for(None) == llm._CALL_TIMEOUT_S


def test_thinking_params_json_mode_short_circuit():
    # json_mode 短路（2026-10-21 增）：结构化输出调用一律零思考，与 max_tokens /
    # thinking_mode 无关——此前靠"小调用不注入"间接保证，大 max_tokens 的 json_mode
    # 调用仍会踩思考吃光输出 / enable_thinking×response_format 不兼容；qwen 族
    # 额外显式关平台默认思考（qwen3 开源部署默认开且 <think> 内联进 content）
    assert llm._thinking_params(
        "anthropic/claude-sonnet-5", True, 20_000, json_mode=True) == {}
    assert llm._thinking_params(
        "qwen/qwen3-max", True, None, json_mode=True) == {"extra_body": {"enable_thinking": False}}
    assert llm._thinking_params(
        "deepseek/deepseek-reasoner", True, 800, json_mode=True) == {}


def test_thinking_params_disabled_empty():
    assert llm._thinking_params("anthropic/claude-sonnet-5", False, None) == {}


# —— init_llm 微缓存（2026-10-26 提速批 3）——————————————————————————————


def test_init_llm_cache_hits_same_key(monkeypatch):
    # 同参数 5s 内复用实例（is 同一对象）、零激活不写缓存（补配即生效）
    _use_test_db(monkeypatch)
    _patch_completion(monkeypatch)
    try:
        llm.init_llm()
    except llm.NoActiveModelError:
        pass
    assert llm._init_cache is None  # 抛错路径不缓存
    _activate()
    a = llm.init_llm(json_mode=True)
    b = llm.init_llm(json_mode=True)
    assert a is b  # 命中缓存
    c = llm.init_llm()  # 参数不同不命中（另一键）
    assert c is not a


def test_init_llm_cache_expires_and_picks_up_model_switch(monkeypatch):
    # TTL 过期后重读库——激活切换最迟 5s 生效（缓存时戳手动拨旧模拟）
    _use_test_db(monkeypatch)
    _patch_completion(monkeypatch)
    _activate()
    a = llm.init_llm()
    llm._init_cache = (llm._init_cache[0] - 10.0, llm._init_cache[1], a)
    with TestingSessionLocal() as db:
        row = llm.get_active_model(db)
        row.model_name = "openai/gpt-4o-mini"
        db.commit()
    b = llm.init_llm()
    assert b is not a and b.model == "openai/gpt-4o-mini"


# —— qwen 名字启发式（2026-11-01 优化批 1）：openai/ 前缀中转站形态 ——————————


def test_is_qwen_model_relay_and_prefix_forms():
    # 前缀直判之外，名字含 qwen 的 openai/ 中转形态也命中（tech-debt 已登记：
    # 按前缀分发不到 enable_thinking，中转站平台默认开思考污染流式与 JSON）；
    # anthropic/ollama 协议族不吃 extra_body，名字撞 qwen 也不算
    assert llm._is_qwen_model("openai/qwen3-235b-a22b")
    assert llm._is_qwen_model("qwen/qwen3-max")
    assert llm._is_qwen_model("dashscope/qwen-plus")
    assert not llm._is_qwen_model("openai/gpt-4o-mini")
    assert not llm._is_qwen_model("deepseek/deepseek-chat")
    assert not llm._is_qwen_model("ollama/qwen3")     # 走 think 参数族
    assert not llm._is_qwen_model("anthropic/qwen-x")  # 走 budget 族


def test_no_thinking_presses_qwen_relay_default():
    # 三个"零思考"分支对 openai/qwen 中转形态同样显式压平台默认——
    # json_mode / thinking 关 / 小 max_tokens，不传 ≠ 关
    for kw in ({"thinking": False, "max_tokens": None},
               {"thinking": True, "max_tokens": 2000},
               {"thinking": True, "max_tokens": None, "json_mode": True}):
        assert llm._thinking_params("openai/qwen3-235b", **kw) == {
            "extra_body": {"enable_thinking": False}}
    # thinking_mode=True 的大调用经名字启发式走 enable_thinking=True
    #（旧版落默认 reasoning_effort 分支，中转站后面的 qwen 收不到）
    assert llm._thinking_params("openai/qwen3-235b", True, None) == {
        "extra_body": {"enable_thinking": True}}


def test_no_thinking_presses_glm_default():
    # glm 族（zai 前缀 / openai 中转名字含 glm）平台默认开思考且思考与正文
    # 共享 max_tokens（真机 qa 流式 2000 预算被思考吃光正文零帧的根因，
    # 2026-11-01 真机批）——三个零思考分支统一压 thinking.type=disabled
    for name in ("zai/glm-5.3-flash", "openai/glm-4.5-air"):
        for kw in ({"thinking": False, "max_tokens": None},
                   {"thinking": True, "max_tokens": 2000},
                   {"thinking": True, "max_tokens": None, "json_mode": True}):
            assert llm._thinking_params(name, **kw) == {
                "extra_body": {"thinking": {"type": "disabled"}}}, name
        # 非 glm 名字不误伤；thinking_mode=True 大调用维持默认分支（端点默认
        # 开思考即用户所愿，reasoning_effort 静默丢弃——不开 enabled 显式干预）
        assert llm._no_thinking("openai/gpt-4o-mini") == {}
        assert llm._thinking_params("zai/glm-5.3-flash", True, None) == {
            "reasoning_effort": llm._REASONING_EFFORT}


# —— 轻量助手槽（优化批 9，解禁 7）———————————————————————————————————————


def test_call_llm_aux_delegates_when_unset(monkeypatch):
    # 未配置 aux：字面委托 call_llm（行为与旧版逐字节一致，替身打 call_llm 照拦）
    _use_test_db(monkeypatch)
    _activate()
    calls: list[str] = []
    monkeypatch.setattr(llm, "call_llm",
                        lambda msgs, **kw: calls.append(msgs[0]["content"]) or "ok")
    out = llm.call_llm_aux([{"role": "system", "content": "s"},
                            {"role": "user", "content": "u"}], json_mode=True)
    assert out == "ok" and calls == ["s"]


def test_call_llm_aux_routes_to_aux_row(monkeypatch):
    # 配置了 aux：路由/分类等小调用走 aux 行（本地小模型亚秒级），主调用不受影响
    _use_test_db(monkeypatch)
    _patch_completion(monkeypatch, content="aux-ok")
    with TestingSessionLocal() as db:
        db.add(ModelProvider(name="aux小模型", model_name="ollama/qwen3:0.6b",
                             is_active=False, is_aux=True))
        db.commit()
    out = llm.call_llm_aux([{"role": "user", "content": "hi"}], max_tokens=100)
    assert out == "aux-ok"
