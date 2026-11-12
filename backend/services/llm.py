"""LLM 薄封装（2026-09-20 增，2026-10-11 重构为 ChatLiteLLM 引擎）：手写区 agents/ 的统一 LLM 入口。

两个入口（异常口径同源，见下）：
- init_llm()：返回 LangChain BaseChatModel（官方 langchain-litellm 引擎 + Clara 加固），
  手写区工具调用 / 多轮编排用——invoke 接受 LangChain 消息或 OpenAI dict（自动 coercion）、
  bind_tools 直接配合 ToolNode / interrupt / MemorySaver。
- call_llm()：init_llm 的文本便捷壳（dict 进 str 出，§3.1 冻结签名），意图分类 / 归纳链等
  "只要文本"场景用。
- call_llm_stream()：call_llm 的流式姊妹（2026-10-16 增）——逐 token 回调 on_chunk 并聚合
  返回全文，qa / smalltalk 节点 token 级打字机流式用；引擎与加固同源。

契约（docs/ai-agents.md §3.1）：同步阻塞——async 上下文（工作流壳 / SSE 壳）调用方负责
asyncio.to_thread（线程隔离铁律，卡事件循环会拖迟同进程的提醒调度）。

引擎选型（docs/ai-pipeline.md §2）：官方 langchain-litellm 的 ChatLiteLLM 而非自写
BaseChatModel 子类——消息转换、reasoning_content / tool_calls 提取、anthropic 签名
thinking_blocks 回放、流式聚合、tool_choice 语义修正、重试全部官方测试兜底；
Clara 只加固两处：上下文预算前置拦截、reasoning_content 回传剥离。

异常约定：
- NoActiveModelError：库内无激活模型（零激活态）。壳统一翻译为引导卡片口径
  （前端"配置模型后解锁"），不是错误——节点内不要各自 try/except。
- ContextOverflowError：输入估算 token 超出激活模型 context_length 档位
  （前置拦截，不发请求——省一次注定 400 的往返）。
- litellm 各异常不捕获向上抛，由调用方（壳/工作流）按降级矩阵分类处理。
"""

import logging
import os
import time
from collections.abc import Callable

# 必须发生在 import litellm 之前（同 services/model_providers.py 先例）：
# 跳过联网拉取模型价格表（离线/代理环境下阻塞导入）
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402
from langchain_litellm import ChatLiteLLM  # noqa: E402

from database import SessionLocal
from services.model_providers import get_active_model

# —— 估算常量（粗估启发式，宁保守不冒进；不引 tokenizer 保持零依赖） ——
_CHARS_PER_TOKEN = 2  # 中英混合粗估：约 2 字符 = 1 token
_MESSAGE_OVERHEAD_TOKENS = 8  # 每条消息的 role/分隔符开销粗估
_OUTPUT_RESERVE_TOKENS = 1024  # max_tokens 未指定时的默认输出预留
_CONTEXT_SAFETY_RATIO = 0.9  # 档位是标称值，留 10% 余量防边界 400

# 整段生成超时（秒）——比连通探测的 10s 宽：排期推理/归纳链一次调用可能较长
_CALL_TIMEOUT_S = 120.0
# 小调用超时分档（2026-10-26 增）：max_tokens ≤ 该值的意图分类类小调用
# （router 100 / judge 60 / classify 120 / delete 100 / settings 150）超时从
# 120s 收到 45s——非流式分支黑屏上限按调用量级分档，卡死时早失败早降级
_SMALL_CALL_TIMEOUT_S = 45.0
_SMALL_CALL_MAX_TOKENS = 200

logger = logging.getLogger(__name__)


def _log_enter(messages: list[dict], *, json_mode: bool, max_tokens: int | None) -> str:
    """调用侧摘要（用于 [llm] 打点）：system 提示词头 30 字（识别是哪个环节的
    调用）+ 关键参数。全部 LLM 调用（router/各分支/两子图）经 call_llm 与
    call_llm_stream 两口，单点打点即全链覆盖（2026-10-22 真机排障需求）。"""
    head = next(((m.get("content") or "") for m in messages
                 if m.get("role") == "system"), "")
    return f"system[{head[:30].strip()}…] json={int(json_mode)} max={max_tokens}"

# thinking_mode=True 映射的 reasoning_effort 档（litellm 统一思考参数）
_REASONING_EFFORT = "medium"


class NoActiveModelError(Exception):
    """零激活态：库内无激活模型（用户删了激活行或从未激活）。"""


class ContextOverflowError(Exception):
    """输入估算 token 超出激活模型 context_length 档位（前置拦截）。"""


def estimate_tokens(messages: list) -> int:
    """输入 token 粗估（纯函数）：每条消息字符数 / 2 + 固定开销。接受 OpenAI dict 或
    LangChain 消息对象两种形态（call_llm 路径传 dict，ClaraChatModel 路径传消息对象）。
    不引 tokenizer，只为挡住明显超限的调用（记忆注入预算失控、上下文塞爆等），精度无关紧要。"""
    return sum(
        len(str(m.get("content", "") if isinstance(m, dict) else m.content)) // _CHARS_PER_TOKEN
        + _MESSAGE_OVERHEAD_TOKENS
        for m in messages
    )


# —— thinking_mode 按 provider 前缀分发（输出直接并入 litellm.completion 请求参数） ——
# litellm model_name 形如 "deepseek/deepseek-chat"，前缀即 provider 协议族

_BUDGET_THINKING_PREFIXES = {"anthropic", "vertex_ai", "bedrock"}  # thinking+budget_tokens 协议族
_ENABLE_THINKING_PREFIXES = {"qwen", "dashscope"}  # OpenAI 兼容 extra_body 族
_THINKING_THINK_PREFIXES = {"ollama"}  # litellm think 参数族

_THINKING_BUDGET_TOKENS = 4096  # anthropic 思考预算（API 下限 1024，P0 固定不分档）
_ANTHROPIC_MIN_MAX_TOKENS = 6144  # thinking 要求 max_tokens > budget，不足时抬升

# 小输出预算调用不开思考（2026-10-21 复查批 C 增）：指定 max_tokens < 该值的调用
# （router 100 / Map 600 / REFLECT 800 等结构化小调用）注入 thinking 参数是自我
# 绞杀——anthropic 思考预算下限 1024 会吃光输出预算、qwen enable_thinking 与
# json_mode 不兼容，这类调用直接退化直答；思考留给未指定上限的大调用
_THINKING_MIN_MAX_TOKENS = 2048


def _is_qwen_model(model: str) -> bool:
    """qwen 模型名启发式（2026-11-01 优化批 1）：前缀直判之外，名字含 qwen
    的 openai/ 等中转站形态也命中——中转站后面的 qwen3 平台默认开思考
    （<think> 内联进 content 污染流式与 JSON），按前缀分发不到
    enable_thinking（tech-debt 已登记欠账）。anthropic/ollama 协议族不吃
    extra_body，先排除。"""
    prefix = model.split("/", 1)[0]
    if prefix in _BUDGET_THINKING_PREFIXES or prefix in _THINKING_THINK_PREFIXES:
        return False
    return prefix in _ENABLE_THINKING_PREFIXES or "qwen" in model.lower()


def _is_glm_model(model: str) -> bool:
    """glm 模型名启发式（2026-11-01 真机批）：bigmodel 端点 glm-4.5+ 系列
    平台默认开思考，且思考与正文共享 max_tokens——qa 流式 2000 预算被思考
    吃光正文零帧（真机「模型没有返回内容」根因）。「不传参数 ≠ 关闭」与
    qwen 同型；名字含 glm 覆盖 zai/ 前缀与 openai/ 中转站两种形态。
    anthropic/ollama 协议族不吃 extra_body，先排除。"""
    prefix = model.split("/", 1)[0]
    if prefix in _BUDGET_THINKING_PREFIXES or prefix in _THINKING_THINK_PREFIXES:
        return False
    return "glm" in model.lower()


def _no_thinking(model: str) -> dict:
    """显式关闭思考（2026-10-26 抽公共）：qwen/dashscope 族（enable_thinking）
    与 glm 族（thinking.type=disabled，2026-11-01 真机批增）的 OpenAI 兼容端点
    平台默认开思考——不传参数 ≠ 关闭；其余协议族默认不思考，返回空 dict 即可。
    供"明确不该思考"的三种调用统一走：json_mode 短路 / 用户关 thinking_mode /
    小 max_tokens 闸。"""
    if _is_qwen_model(model):
        return {"extra_body": {"enable_thinking": False}}
    if _is_glm_model(model):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def _thinking_params(model: str, thinking: bool, max_tokens: int | None,
                     json_mode: bool = False) -> dict:
    """thinking_mode=True 时按 provider 前缀分发 litellm 思考参数；False 恒空 dict。

    - json_mode 短路（2026-10-21 增）：结构化输出调用一律零思考（与 max_tokens
      闸独立——此前靠"小调用不注入"间接保证，大 max_tokens 的 json_mode 调用仍会
      踩 anthropic 思考吃光输出 / qwen enable_thinking×response_format 不兼容）
    - 三个"零思考"分支（json_mode / thinking=False / 小 max_tokens）统一经
      _no_thinking 显式压平台默认（2026-10-26 修订：此前返回空 dict，qwen 族
      端点平台默认开思考时照样思考——不传 ≠ 关；qa/smalltalk 流式首字延迟根因）
    - 小 max_tokens 闸：max_tokens < _THINKING_MIN_MAX_TOKENS 时不注入任何思考
      参数（该调用退化直答，见常量注释）；未指定上限（None）照常注入
    - anthropic 族：max_tokens 必须 > budget_tokens，用户未指定或给太小则抬升
      （思考开着却被 max_tokens 掐死无意义；取舍：抬升优先于尊重小 max_tokens）
    - 默认分支 reasoning_effort 配合 drop_params=True：provider 不支持时静默丢弃，
      降级普通对话（thinking_mode 是增强项，zai/GLM 实测无该参数）
    """
    if json_mode:
        return _no_thinking(model)
    if not thinking:
        return _no_thinking(model)
    if max_tokens is not None and max_tokens < _THINKING_MIN_MAX_TOKENS:
        return _no_thinking(model)
    prefix = model.split("/", 1)[0]
    if prefix in _BUDGET_THINKING_PREFIXES:
        eff_max = max(max_tokens or 0, _ANTHROPIC_MIN_MAX_TOKENS)
        return {
            "max_tokens": eff_max,
            "thinking": {"type": "enabled", "budget_tokens": _THINKING_BUDGET_TOKENS},
        }
    if _is_qwen_model(model):
        return {"extra_body": {"enable_thinking": True}}
    if prefix in _THINKING_THINK_PREFIXES:
        return {"think": {"type": "enabled"}}
    return {"reasoning_effort": _REASONING_EFFORT}


def _strip_reasoning(message: AIMessage) -> AIMessage:
    """深拷贝后剥掉 reasoning_content（不动调用方传入的消息对象）。"""
    stripped = message.model_copy(deep=True)
    stripped.additional_kwargs.pop("reasoning_content", None)
    return stripped


def _precheck_and_strip(
    messages: list[BaseMessage], context_length: int, max_tokens: int | None
) -> list[BaseMessage]:
    """ClaraChatModel 对引擎调用的两处前置加固（四个生成路径共用）：

    1. ContextOverflowError 前置拦截：输入估算 + 输出预留超出档位（留安全余量）即拒发；
    2. reasoning_content 回传剥离：引擎默认会把该字段发回（OpenAI 兼容推理端点会读），
       但 DeepSeek 官方文档明确输入带该字段返回 400——一刀切剥掉。
       anthropic 签名 thinking_blocks 走引擎另一条回放通道，不受影响。
    """
    budget = int(context_length * _CONTEXT_SAFETY_RATIO) - (max_tokens or _OUTPUT_RESERVE_TOKENS)
    estimated = estimate_tokens(messages)
    if estimated > budget:
        raise ContextOverflowError(
            f"输入估算约 {estimated} token，超出激活模型可用预算 {budget}"
            f"（context_length={context_length}）"
        )
    return [
        _strip_reasoning(m)
        if isinstance(m, AIMessage) and m.additional_kwargs.get("reasoning_content")
        else m
        for m in messages
    ]


class ClaraChatModel(ChatLiteLLM):
    """官方 ChatLiteLLM 引擎 + Clara 加固（经 init_llm 构造，不直接 new）。

    继承所得（官方测试兜底，不自写）：LangChain 消息 → OpenAI dict 转换、
    reasoning_content / tool_calls 响应提取、anthropic 签名 thinking_blocks 回放、
    _stream 流式聚合（tool_call 分片按 index 拼接）、bind_tools 的 tool_choice 语义
    修正、tenacity 重试。四个生成路径（同步/异步 × 整段/流式）统一过
    _precheck_and_strip 加固后再交 super()。
    """

    context_length: int = 32768  # ModelProvider 档位，预算拦截用

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        messages = _precheck_and_strip(messages, self.context_length, self.max_tokens)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        messages = _precheck_and_strip(messages, self.context_length, self.max_tokens)
        return super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        # 异步路径同样过加固（P0 约定同步调用，但守卫须覆盖引擎全部生成路径）
        messages = _precheck_and_strip(messages, self.context_length, self.max_tokens)
        return super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        messages = _precheck_and_strip(messages, self.context_length, self.max_tokens)
        return super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)


def _timeout_for(max_tokens: int | None) -> float:
    """按输出预算分档超时（init_llm 单点消费）：小调用 45s，其余 120s。"""
    if max_tokens is not None and max_tokens <= _SMALL_CALL_MAX_TOKENS:
        return _SMALL_CALL_TIMEOUT_S
    return _CALL_TIMEOUT_S


# init_llm 实例微缓存（2026-10-26 提速批 3）：同键 5s 内复用构造结果——
# 同轮多节点 / Map 并发线程连续 init 不再逐次查库构造。5s 后重读：激活切换
# 的生效延迟上限即 5s（原"每跳重读"语义小步放宽，取舍登记 tech-debt 当日）。
# 无激活（抛 NoActiveModelError）不写缓存——补配即生效不等人。GIL 下 tuple
# 赋值原子，并发最坏重复构造互相覆盖，无害。
_INIT_CACHE_TTL_S = 5.0
_init_cache: tuple[float, tuple, "ClaraChatModel"] | None = None


def init_llm(
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
    json_mode: bool = False,
    role: str = "main",  # main=激活模型 / aux=轻量助手槽（未配置回落 main，批 9）
) -> ClaraChatModel:
    """读激活 ModelProvider 构造聊天模型实例（LangChain BaseChatModel）。

    手写区认可入口：model.invoke([SystemMessage(...), HumanMessage(...)])、
    model.bind_tools([@tool ...]) 直接可用；消息也接受 OpenAI dict（自动 coercion）。
    零激活抛 NoActiveModelError（壳翻译引导卡）；同参数 5s TTL 内命中微缓存
    （_INIT_CACHE_TTL_S，2026-10-26 增——原"每次调用读一次库"改为短窗复用，
    激活切换最迟 5s 生效）。thinking 开关只认库里的
    thinking_mode（单一事实源，要覆盖属测试需求，测试直接 new ClaraChatModel）。

    参数策略（2026-10-11 定：内置固定为主，调用点覆盖为辅，设置页不暴露——
    这些是 agents/ 开发者的旋钮，不是终端用户的配置项）：
    - temperature 默认 0：各家默认值不一（GLM≈1.0 / qwen≈0.7），意图分类 / JSON 产出 /
      归纳链要稳定可复现；闲聊等节点可显式调高。drop_params 兜底个别只认默认值的推理模型
    - max_tokens 维持 None（厂商默认即各自安全值）：各家输出上限差异大（4K~64K），
      钉固定值可能在低上限模型上 400，比偶尔输出偏长更糟
    - timeout 按 max_tokens 自动分档（2026-10-26 增）：≤200 的小调用 45s，其余
      120s（_timeout_for 单点），不暴露
    """
    global _init_cache
    key = (role, temperature, max_tokens, json_mode)
    now = time.perf_counter()
    if (_init_cache is not None and _init_cache[1] == key
            and now - _init_cache[0] < _INIT_CACHE_TTL_S):
        return _init_cache[2]
    with SessionLocal() as db:
        model = get_active_model(db)
        if role == "aux":
            # 轻量助手槽（优化批 9，解禁 7——全方案最大速度杠杆）：小调用白名单
            # 走本地小模型可亚秒级；未配置回落激活模型（行为不变），零激活照抛
            from services.model_providers import get_aux_model

            model = get_aux_model(db) or model
    if model is None:
        raise NoActiveModelError("未激活任何模型，请到设置 → 模型接入中激活")

    thinking = _thinking_params(model.model_name, model.thinking_mode, max_tokens,
                                json_mode=json_mode)
    # anthropic 族思考要求 max_tokens > budget：抬升后的值以字段形式落（避免与
    # model_kwargs 同键两处），预算拦截随之收紧，语义自洽
    eff_max = thinking.pop("max_tokens", None) or max_tokens
    llm = ClaraChatModel(
        model=model.model_name,
        api_key=model.api_key or "EMPTY",  # 空 key 占位符（免鉴权本地端点，同 probe_model 口径）
        base_url=model.base_url or None,  # None = LiteLLM 按 provider/ 前缀官方路由
        temperature=temperature,
        max_tokens=eff_max,
        request_timeout=_timeout_for(max_tokens),
        context_length=model.context_length,
        model_kwargs={
            "drop_params": True,  # provider 不支持的可选参数静默丢弃（如 zai/GLM 无 reasoning_effort）
            **thinking,
            **({"response_format": {"type": "json_object"}} if json_mode else {}),
        },
    )
    _init_cache = (now, key, llm)
    return llm


def call_llm_aux(
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """轻量助手模型调用（2026-11-01 优化批 9，解禁 7）：小调用白名单的
    role 分流入口——配置了 aux 槽走本地小模型（亚秒级）；**未配置时直接委托
    call_llm**（字面回落：行为与旧版逐字节一致，测试替身打在 call_llm 上
    照常拦截）。白名单：路由 / 场景分类与问询裁决 / 删除定位 / 设置抽取 /
    反思归纳与消解 / 采集源摘要——全是 ≤1000 token 的结构化小调用；人格
    大调用（qa / coach / 剖析 / 裁决 / 合并）恒走主模型保一致性。"""
    with SessionLocal() as db:
        from services.model_providers import get_aux_model

        has_aux = get_aux_model(db) is not None
    if not has_aux:
        return call_llm(messages, json_mode=json_mode, max_tokens=max_tokens)
    tag = _log_enter(messages, json_mode=json_mode, max_tokens=max_tokens)
    t0 = time.perf_counter()
    out = init_llm(role="aux", json_mode=json_mode,
                   max_tokens=max_tokens).invoke(messages).content or ""
    logger.info("[llm] aux 调用 %s → %d 字符（%.1fs）", tag, len(out),
                time.perf_counter() - t0)
    return out


def call_llm(
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """调激活模型补全一段对话，返回 content 文本（§3.1 冻结签名不变）。

    messages 形如 [{"role": "system", "content": ...}, {"role": "user", ...}]；
    json_mode=True 时要求 JSON 输出（litellm response_format）；异常向上抛
    （NoActiveModelError / ContextOverflowError / litellm 原生异常）。
    实现为 init_llm 薄壳：dict 消息经基类 coercion 转 LangChain 消息，行为与
    直接调 init_llm().invoke(...) 完全一致（单一调用路径，不漂移）。
    """
    tag = _log_enter(messages, json_mode=json_mode, max_tokens=max_tokens)
    t0 = time.perf_counter()
    out = init_llm(json_mode=json_mode, max_tokens=max_tokens).invoke(messages).content or ""
    logger.info("[llm] 调用 %s → %d 字符（%.1fs）", tag, len(out),
                time.perf_counter() - t0)
    return out


def stream_model(model, messages: list[dict], *, on_chunk: Callable[[str], None]) -> str:
    """在给定模型实例上流式驱动 + 前导 <think> 三态过滤（2026-11-01 优化批 6
    从 call_llm_stream 抽出的内核）——smalltalk 经 init_llm(temperature=0.7)
    直用（闲聊自然度：参数策略注释预留的「闲聊可显式调高」首次落地），过滤
    状态机与 call_llm_stream 单源不复制。逐 token 回调 on_chunk，返回全文。"""
    parts: list[str] = []
    state = "pending"  # pending=未决（判 think 头）/ thinking=思考内 / plain=正常
    buf = ""

    def _emit(text: str) -> None:
        if text:
            parts.append(text)
            on_chunk(text)

    for chunk in model.stream(messages):
        delta = chunk.content
        if not (isinstance(delta, str) and delta):
            continue
        buf += delta
        if state == "pending":
            if buf.startswith("<think>"):
                state = "thinking"
                buf = buf[len("<think>"):]  # 思考正文（可能已含闭合标签，落入下方判定）
            elif not "<think>".startswith(buf):
                state = "plain"
                _emit(buf)  # 开头即正文（零缓冲逐块转发，打字机粒度不受影响）
                buf = ""
            # buf 仍是 "<think>" 真前缀（如 "<" "<th"）：继续缓冲到能判定
        if state == "thinking":
            if "</think>" in buf:
                tail = buf.split("</think>", 1)[1]
                state = "plain"
                buf = ""
                _emit(tail)
            elif len(buf) > len("</think>"):
                # 闭合标签可能拆在 chunk 间：滚动保留尾部 7 字符防跨块，其余可安全丢
                buf = buf[-(len("</think>") - 1):]
        elif state == "plain":
            _emit(buf)
            buf = ""  # 放行后清缓冲——2026-11-01 真机批修复：此行曾在批 6
            # 重构中丢失，稳态帧 buf 未清导致每帧发「旧缓冲+新增量」的累积
            # 快照（真机打字机/正文逐级重复的根因，多帧序列回归测试补防）
    if state == "pending" and buf:
        # 流在未决态结束（总输出恰为 "<think>" 真前缀，如 "<th"）：按正文收尾
        # （打字机已随流终结束，不再补回调，返回值完整性由 parts 保证）
        parts.append(buf)
    return "".join(parts)


def call_llm_stream(
    messages: list[dict],
    *,
    on_chunk: Callable[[str], None],
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """流式版 call_llm（2026-10-16 增，姊妹函数——call_llm 冻结签名不动）：
    逐 token 回调 on_chunk(增量文本)，同时聚合返回全文 str。

    供 qa / smalltalk 等自然语言节点做 token 级打字机流式；json_mode 调用方
    没有流式意义（半截 JSON 无法消费）仍走 call_llm。引擎与加固同源：
    init_llm().stream()（官方 ChatLiteLLM _stream，_precheck_and_strip 前置拦截
    照样生效）；reasoning_content 在 additional_kwargs 不进 content 通道，
    on_chunk 只会收到正文增量。chunk.content 非 str（如工具调用分片的 list）
    或为空时跳过——空段回调没有呈现价值。异常口径同 call_llm：裸奔上抛。

    前导 <think> 过滤（2026-10-26 增）：qwen3 开源部署走 OpenAI 兼容端点时
    无法用参数关思考（enable_thinking 非 dashscope 专属参数不生效）且 <think>
    内联进 content——不过滤则流式闲聊先打一大段思考文本再出正文（首字慢 +
    脏文本双重痛点）。三态状态机：None=未决（缓冲首 7 字符判断是否 think 头，
    正常回复零缓冲零延迟）、False=思考内（丢弃直到 </think>）、True=正常转发。
    流终仍在思考内 = 全思考无正文，返回空串由调用方空兜底（qa/smalltalk 均有
    固定话术），口径对齐 parsing.loads_obj 的"未闭合=截断不硬救"。
    """
    tag = _log_enter(messages, json_mode=json_mode, max_tokens=max_tokens)
    t0 = time.perf_counter()
    # 内核抽 stream_model（2026-11-01 批 6：smalltalk 温度直用同款过滤）
    out = stream_model(init_llm(json_mode=json_mode, max_tokens=max_tokens),
                       messages, on_chunk=on_chunk)
    logger.info("[llm] 流式调用 %s → %d 字符（%.1fs）", tag, len(out),
                time.perf_counter() - t0)
    return out
