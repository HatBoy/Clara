"""LLM 输出解析的公共防御件（全 agents 手写区共用）。

为什么单独成模块：graph.py 顶部 import agents.nodes（挂节点函数），若 nodes.py
再从 graph.py import 解析函数会构成 import 环——把无依赖的纯函数下沉到本模块，
graph.py / nodes.py / reflection.py 三处共用同一份实现，杜绝复制漂移。

三层防御纪律（docs/ai-agents.md §五 坑 ②）：LLM 的 JSON 输出永远不可直接信——
剥围栏 → json.loads → 形状校验，任一失败返回 None，由调用方决定兜底口径
（节点兜底 smalltalk / 降级纯文本，编排零卡零落库）。
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# 思考模型内联的前导 <think>…</think>（qwen3 开源部署/聚合平台把思考写进 content，
# enable_thinking=False 关不掉时经此剥掉——2026-10-21 "排期推理失败"根因）；
# 未闭合（思考吃光 max_tokens 正文没开始）不匹配：内容已残缺，不硬救
_THINK_PREFIX_RE = re.compile(r"^<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def loads_obj(text: str) -> dict | None:
    """剥思考前缀与代码围栏（```json ... ```）→ json.loads → 取 dict 形状；任一
    失败返回 None 并 warning 记录残文头部（此前静默 None，排障只能靠猜）。所有
    "期望 LLM 输出一个 JSON 对象"的解析入口共用此函数。

    确定性容错（2026-11-01 优化批 4）：json.loads 失败后先做零成本修复再试一次
    ——尾逗号剔除（`"a": 1,}`）/ 整体单引号替换（全文无双引号时的国产模型
    偶发形态）。截断类残缺不在此硬救（未闭合 = 内容已缺失，交给
    call_llm_json 的修复重试）。"""
    if not isinstance(text, str) or not text:
        return None
    stripped = _THINK_PREFIX_RE.sub("", text.strip(), count=1)
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped[:4].lower() == "json":
            stripped = stripped[4:]
    try:
        data = json.loads(stripped)
    except ValueError:
        repaired = re.sub(r",\s*([}\]])", r"\1", stripped)
        if '"' not in repaired and "'" in repaired:
            repaired = repaired.replace("'", '"')
        data = None
        if repaired != stripped:
            try:
                data = json.loads(repaired)
            except ValueError:
                data = None
        if data is None:
            logger.warning("LLM JSON 解析失败，输出头部：%r", stripped[:200])
            return None
    return data if isinstance(data, dict) else None


def call_llm_json(system: str, user: str, *, max_tokens: int) -> dict | None:
    """json_mode 调用 + 解析 + 窄域修复重试一次（2026-11-01 优化批 4）。

    解析失败把残文头回喂重问一次（带「只输出合法 JSON」的明确约束）——只服务
    schedule 链 profiler / adjudicator 两次大 JSON（qwen 思考污染的真机痛点：
    降级文案不是等价体验）。重试只发生在本会失败的调用上，轮调用数上限不变；
    仍失败返回 None 走调用方既有降级（引擎回落规则槽 / 保持 finder 主荐）。"""
    from services.llm import call_llm

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    out = call_llm(messages, json_mode=True, max_tokens=max_tokens)
    data = loads_obj(out)
    if data is not None:
        return data
    logger.warning("JSON 解析失败，窄域修复重试一次（残文头 120 字：%s）",
                   (out or "")[:120])
    retry_user = (
        user
        + "\n\n【注意】你上一次的输出不是合法 JSON（开头内容："
        + (out or "")[:120].replace("\n", " ")
        + "…）。请重新回答，只输出一个合法的 JSON 对象——不要解释文字、不要"
          " markdown 围栏、不要 <think> 思考过程。")
    return loads_obj(call_llm(
        [{"role": "system", "content": system},
         {"role": "user", "content": retry_user}],
        json_mode=True, max_tokens=max_tokens))


def is_int(v) -> bool:
    """int 判定（排除 bool——bool 是 int 子类，True 会被当 1 混进 duration/idx）。"""
    return isinstance(v, int) and not isinstance(v, bool)
