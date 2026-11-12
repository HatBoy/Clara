"""Agent执行过程中用到的tools集中管理编写

数据接入三工具（fetch_recent_data / list_data_sources / call_data_tool）：
薄壳调 services/data_ingest（四源采集管道 / 会话池 / 补采硬闸见其模块
docstring）。使用口径：确定性编排（graph.run_ingest / nodes 的 ingest 分支）直调
service 函数（可测、不依赖 LLM 工具循环）；@tool 壳供 ReAct 受控补采与工具循环
练习用——两者共用一条采集路径不漂移。docstring 即发给模型的工具描述。三壳各有
catastrophic 兜底（2026-10-16 起）：任何未预期异常降级为 JSON 信封（错误文案只取
类型名），永不向 LangGraph 工具循环抛异常。
"""

import json

from langchain_core.tools import tool

from database import SessionLocal
from services import data_ingest


@tool
def fetch_recent_data(hours: int = 72) -> str:
    """从 Clara 已启用的数据接入源（本地文件夹 / MCP 服务 / Composio 应用 / CLI 工具）
    拉取最近 hours 小时（默认 72，覆盖周末中断）的原始数据。返回 JSON 文本：{"sources": [{"source", "kind", "ok",
    "key", "fail_streak", "items": [{"id", "title", "body", "time", "fingerprint"}],
    "deduped", "error", "note"}], "truncated"}；ok=False 的源 items 为空、error 为
    中文摘要；deduped 为该源已见过而滤除的条数；truncated=True 表示数据被预算截断。
    没有新数据时 sources 为空列表或各源 items 全空（恒返回 JSON 文本，不会是 None），
    已停用或离线冷却中的源直接不出现。用于每日采集汇总、从邮件/文档/待办应用中提炼
    任务与日程建议。"""

    # catastrophic 兜底（2026-10-16 起）：service 层外闸之上的最后防线（覆盖
    # SessionLocal 建连等）——永不向工具循环抛异常；错误文案只取类型名不透传
    # str(e)，防凭据/路径泄露（同 _describe_error 口径）
    try:
        with SessionLocal() as db:
            return json.dumps(data_ingest.fetch_recent(db, hours=hours), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"sources": [], "truncated": False,
                           "error": f"采集异常：{type(e).__name__}（本次无数据，可稍后重试）"},
                          ensure_ascii=False)


@tool
def list_data_sources() -> str:
    """列出 Clara 各数据接入源（Composio 应用 / MCP 服务 / CLI 工具）当前可用的只读采集
    工具清单。返回 JSON 文本：[{"source", "key", "kind", "tools": [{"name", "description"}]}]。
    补采前先调用本工具看清各源能用什么工具与正确工具名，再选目标源的工具调用
    call_data_tool。"""

    # catastrophic 兜底：保持外层 list 形态（信封伪条目带 error 键），LLM 可读
    try:
        with SessionLocal() as db:
            return json.dumps(data_ingest.list_source_actions(db), ensure_ascii=False)
    except Exception as e:
        return json.dumps([{"source": "（数据源清单获取失败）", "key": None,
                            "kind": "unknown", "tools": [],
                            "error": f"清单异常：{type(e).__name__}"}],
                          ensure_ascii=False)


@tool
def call_data_tool(key: str, tool: str, arguments: dict) -> str:
    """对指定数据源（key 来自 list_data_sources）调用单个只读工具补采数据。
    arguments 必须是 JSON 对象（≤2000 字符）；仅允许读取类工具（写入/发送类会被
    拒绝）；每源每日至多 2 次。返回 JSON 文本：{"source", "kind", "ok", "items",
    "error", "note"}——ok=False 时 error 说明被拒或失败的原因（读它换工具或换源，
    不要原样重试）；error 以 [认证] 开头表示该源的 Key / 凭据已失效（服务在线也
    取不到数据），换源并在综述中提示用户重新配置，重试本源同样会失败。"""

    # catastrophic 兜底：单源条目同 service 层 _fail_tool 九键形态
    try:
        with SessionLocal() as db:
            return json.dumps(
                data_ingest.call_readonly_tool(db, key=key, tool=tool, arguments=arguments),
                ensure_ascii=False,
            )
    except Exception as e:
        return json.dumps({"source": key, "kind": "unknown", "ok": False, "items": [],
                           "error": f"补采异常：{type(e).__name__}", "note": None,
                           "key": key, "fail_streak": 0, "deduped": 0},
                          ensure_ascii=False)
