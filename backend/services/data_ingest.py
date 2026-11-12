"""数据接入采集管道：agents/tools.py 采集工具与 ingest 工作流背后的四源统一采集层。

四类源的读取入口全部复用既有 services（分层红线：agents 消费外部上下文恒经 services
包装，docs/ai-agents.md §七）：
- 本地文件夹：local_folder 纯函数进程内直调（list_files_in + read_file_as_markdown），
  不走 MCP HTTP 自环——同进程绕回环端点纯浪费；
- Composio 应用：integrations 连接管理 + client.tools.execute 按 toolkit 映射表拉取
  （SDK 0.21.1 实测：execute(tool_slug, arguments, connected_account_id)，响应
  {data, successful, error}）；
- MCP 服务：mcp_servers.get_connections 的连接描述 + mcp_session 会话池优先调用
  （池路径异常回落本模块的一次性桥接 _mcp_bridge——降级铁律：池化是性能优化不是
  正确性依赖）。桥接只能跑在无事件循环的线程（agent 节点被 LangGraph 扔 executor /
  工作流 runner 经 to_thread），在事件循环线程直调会 RuntimeError。
- CLI 工具（2026-10-15 增，第四源 kind="cli"）：cli_tools 适配器注册表（飞书
  lark-cli / 钉钉 dws / Todoist td）+ 通用执行器——一次性子进程无会话无握手，
  argv 数组 + which 解析 .cmd + 超时树杀；窄口自定义行（custom:<uuid>）readonly
  白名单恒空不进补采；采集命令待真机实测，填入 cli_tools.ADAPTERS 即生效。

核心能力（设计编号 M1/M2 对应 docs/ai-ingest.md §八–§十）：
- 增量游标（M1-③）：ingest_sources.last_success_at 为锚，时间窗 clamp 到 [1h, 72h]；
- 指纹与台账（M2-①）：条目级 sha1 指纹 + ingestion_items 跨日去重（拒绝过的指纹
  永静默），采集收尾统一 _filter_and_keepalive；
- 健康度（M2-⑤A）：fail_streak 连败计数随每次尝试回写，≥3 前端徽标标红；
- 并发采集（M2-⑤B）：远程源（Composio + MCP）单次 asyncio.run 内 gather——MCP 池
  优先每源独立超时，Composio 同步 SDK 包 to_thread；本地文件夹主线程直调不参与；
- 每源配置（M2-②）：采集动作三级优先（行配置 > 内置映射表 > note 清单），
  enabled=False / pause_until 未到期整源静默跳过；
- 受控补采（M1-④）：list_source_actions / call_readonly_tool——service 层硬闸
  （LLM 不可绕过），deny-list 只读判定，拒绝/失败一律"失败是数据"不抛异常；
- 信任降频（M2-③）：apply_trust_policy 纯规则夜间挂接（不采 LLM）；
- 预算代理（§十-3）：claim_llm_budget 以调用次数为预算（模块态跨日重置）。

口径与取舍：
- 失败是数据不是异常（同 probe_server）：逐源 try/except → ok=False + 中文摘要，单源
  挂不毁整批；LLM 拿到 error 文本自行在综述里降级表述。
- 预算截断前置（宁保守防 ContextOverflow，拦截兜底在 llm 层但别指望它）：每源条数
  ≤20、单条 body ≤1500 字符、每源正文 ≤12000 字符；任一触发置 truncated=True，LLM
  知道数据不全会在综述里声明。
- 时间窗过滤对本地文件夹精确（mtime 便宜）；结构化 API 不强行拼时间参数（各家过滤
  参数不统一，拼错静默漏数据更糟）——拉最新 N 条 + 条目自带时间，可解析时间的条目
  事后丢弃窗口外旧条（未来时间保留），解析不了保留交给 LLM 判断。全局默认窗 72h
  （2026-10-15 起由 24h 放宽，覆盖周末中断；实际窗口经行配置/游标推导见 _window_for）。
- 本地文件夹格式分级 + 价值信号：静态采集只读文本类扩展名（_LOCAL_TEXT_EXTS），
  富格式默认不采留补采按需转换；文本文件首 512 字符扫价值信号（checkbox/日期/TODO/
  待办）打 sched_hint，用于每源 20 名额的分配（_apply_budget 排序），不做价值过滤。
- 采集动作映射表（Composio 按 toolkit_slug / MCP 按精选模板特征串）：命中才调，未映射
  源不瞎调（LLM 自主调用工具属受控补采），把可用工具清单放 note 供后续配置。
  目标工具名 / 参数名以真机 tools.list 的 inputSchema 为准，实测后填表即生效——
  映射登记与实测进度见 docs/ai-ingest.md。
- db 会话不进协程：SQLAlchemy Session 非线程安全，行配置在构建阶段（主线程）读好
  闭包传入，attempt/台账回写统一在收尾阶段（主线程）做。
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import CliTool, IngestSource, IngestionItem
from services import cli_tools, integrations, local_folder, mcp_servers, mcp_session
from services.settings import get_settings

logger = logging.getLogger(__name__)

# 预算常量（见模块 docstring「口径与取舍」）
_MAX_ITEMS_PER_SOURCE = 20
_MAX_BODY_CHARS = 1500
_MAX_SOURCE_CHARS = 12000

# MCP 单源墙钟预算（秒）：含 stdio 首跑拉起（npx 下载依赖）+ 握手 + 工具调用
_MCP_TIMEOUT_S = 90.0

# 增量窗口钳制（小时）：下限防"刚采过就再采"白跑，上限即首采护栏（冷启动限流）。
# 上限 168h = 一周（2026-10-15 起由 72h 放宽）：周末/短假没开机，恢复后首采按游标
# 补齐中断期；LLM 侧成本不随窗口放大——已见条目由台账指纹滤除，条数/字符由预算钳制
_WINDOW_MIN_H = 1
_WINDOW_MAX_H = 168

# 连败冷却（2026-10-15 起，离线源静默跳过）：冷却窗 = 基数 × 连败数，封顶一天——
# 启用但服务离线的源（如 Obsidian 没开）不再每次采集都白跑握手两条路径；冷却过后
# 自动重试一次，成功即 fail_streak 清零自愈。与信任降频 _paused 语义分离：那个按
# 采纳率（apply_trust_policy），这个按连通性
_FAIL_COOLDOWN_BASE_H = 2.0
_FAIL_COOLDOWN_MAX_H = 24.0

# 认证类错误特征词（2026-10-15 起，源过滤第三层）：子串、大小写不敏感。识别口径 =
# 在线但凭据无效的源（钉钉等 MCP 握手与探测能过，数据调用才暴露）——错误文本
# 或（条目抽干后的）返回体命中即打 [认证] 前缀并计连败（冷却跳过）。误杀代价 =
# 综述多提示一句查 Key（用户一眼辨真伪），漏放代价 = 认证失效被当普通失败、用户
# 不知道去重配——不对称，宁误杀。裸 "401"/"403" 不进表：时间戳/id 常含该子串
# （HTTP 形态已由探测/桥接的结构化"鉴权失败"文案覆盖）。中文标记 "api key 无效"
# （2026-10-16 起）命中 _translate_upstream 的 AuthenticationError 文案——Composio
# Key 失效同样要打 [认证]（此前全英文表不命中中文翻译文案）
_AUTH_ERROR_MARKERS = (
    "鉴权失败", "api key 无效", "unauthorized", "forbidden", "permission denied",
    "invalid token", "token invalid", "token expired", "expired token",
    "invalid access token", "app_access_token", "app access token",
    "tenant_access_token", "tenant access token",
    "invalid sessionkey", "sessionkey invalid",
    "invalid api key", "api key invalid", "invalid_api_key",
    "invalid app secret", "invalid authentication", "authentication failed",
    "failed to authenticate", "not authenticated", "authentication required",
)

# 瞬时失败特征词（2026-10-16 起，_with_retry 白名单）：子串、大小写不敏感，只作用
# 于 outcome 的 error 文本（不碰条目正文）。认证类（[认证] 前缀）/ 二进制缺失 /
# 工具缺失等确定性失败重试必败不进表；白名单而非黑名单——error 文本来源混杂
# （自产中文 + CLI stderr ≤200 字符 + Composio resp.error 英文原文），黑名单不可
# 穷举；漏放的代价只是少重试一次、冷却机制仍在（不对称，宁漏放）。裸状态码
# （"503" 等）不进表：stderr 摘要可能含 id/时间戳子串（同 _AUTH_ERROR_MARKERS
# 排除裸 401/403 的论证）
_TRANSIENT_MARKERS = (
    "超时", "无法连接", "服务不可用", "服务端错误",
    "timeout", "timed out", "connection reset", "connection refused",
    "network error", "temporarily unavailable",
)
_RETRY_MAX_ATTEMPTS = 3  # 单源单轮总尝试次数（首跑 + 2 重试）
_RETRY_BACKOFF_S = 1.0  # 短退避：全部源并发在同一事件循环，sleep 不阻塞他源

# 本地文件夹格式分级（2026-10-15 起）：静态采集只读文本类。markitdown 转换（贵）
# 不该花在低概率素材上——富格式（pdf/docx/xlsx…）默认不采，note 声明数量，留给
# 补采 ReAct 按 LLM 价值判断后经 read_file 单个按需转换
_LOCAL_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv", ".html", ".htm", ".json"}

# Composio 采集映射：toolkit_slug → (tool_slug, arguments)。
# 参数名以云端 inputSchema 为准（GMAIL_FETCH_EMAILS 的 max_results 已按公开 schema 核对）；
# 其余应用按用户实际连接后经 tools.list 核对补填（未填走 note 清单路径，不阻塞）。
_COMPOSIO_FETCH_ACTIONS: dict[str, tuple[str, dict]] = {
    "gmail": ("GMAIL_FETCH_EMAILS", {"max_results": 20}),
}

# MCP 精选模板采集映射：特征串（url/command 子串匹配）→ (工具名, 参数)。
# 工具名留 None = 特征已识别但目标工具未实测确认 → 退化为 note 工具清单路径。
# 滴答（2026-10-15 真机实测）：收件箱 id 恒为 "inbox"，参数名是 snake_case 的
# project_id（工具描述里的 projectId 是误导）；其余清单 id 因账号而异，走补采
# 路径（list_projects 后按 id 取）。
_MCP_FETCH_TOOLS: dict[str, tuple[str | None, dict]] = {
    "dida365": ("get_project_with_undone_tasks", {"project_id": "inbox"}),  # mcp.dida365.com
    "27123": (None, {}),  # Obsidian（Local REST API 插件端点）
    "dingtalk-mcp": (None, {}),  # 钉钉
}

# —— 高可信直通（2026-10-24）—————————————————————————————————————————————
# 两级白名单（用户方案二清单收编为「接入默认值」，方案一的按源开关仍是权威控制）：
# 任务型精选工具接入即预开 trusted（纯任务域、无笔记噪音）；混合域工具（lark =
# 日历 + 任务 + 文档）默认关，用户按需开启。CLI 侧按 adapter_id 判（cli_tools.connect
# 建行时消费），MCP 侧按特征串判（mcp_servers 建行时消费，与 _MCP_FETCH_TOOLS 同键）。
DEFAULT_TRUSTED_CLI = {"todoist", "dingtalk"}
DEFAULT_TRUSTED_MCP_MARKERS = {"dida365"}
# MCP 采集动作的条目域（marker → 域；行配置显式覆盖 fetch_tool 时失域保守不直通）。
_MCP_DOMAINS: dict[str, str] = {"dida365": "todo"}

# 受控补采（M1-④）常量：只读判定 deny-list 特征词（子串、大小写不敏感）。
# 用 deny-list 而非 allow-list：白名单不可穷举（各家命名不统一），误杀代价 =
# LLM 收到拒绝另选工具（下一轮自纠），漏放代价 = 真写操作被执行——不对称，宁误杀。
_TOOL_DENIED_MARKERS = (
    "create", "add", "write", "update", "delete", "remove", "send", "post",
    "patch", "put", "move", "close", "complete", "archive", "trash", "assign",
    "share", "publish", "submit", "reply", "forward", "label", "modify",
    "edit", "upload", "import", "sync",
)
_TOOL_TIMEOUT_S = 60.0  # 单次补采限时（补采针对已握手源，无需 90s 拉起预算）
_DAILY_TOOL_CAP = 2  # 每源每日补采次数上限（当日重置；防 LLM 循环刷调用）
_TOOL_CAP_STATE: dict[str, tuple[date, int]] = {}  # key -> (当日, 已用)；模块态，重启归零

# LLM 预算代理（§十-3）：模块态 (当日, 已用)。跨日重置、重启归零——持久化不值得
# （预算是防失控护栏不是计费，登记 docs/tech-debt.md）。0 = 不限。
_LLM_BUDGET_STATE: tuple[date, int] | None = None

# 条目启发式键（_extract_items 用；不追求精确——LLM 消费原始 JSON 的能力远强于启发式，
# 这里只做形态统一与预算控制）
_TITLE_KEYS = ("subject", "title", "name", "summary", "headline", "snippet", "content", "text")
# 日程/任务强信号时间键：条目命中即带 sched_hint=True，预算截断时优先保留——综合
# 工具（笔记/文档类）默认序里无关纯文本可能占满条数上限，结构化条目不能被挤掉
# （startDate/dueDate 为滴答驼峰形态，2026-10-15 真机实测补入）
_SCHED_TIME_KEYS = (
    "start_time", "end_time", "startDate", "dueDate", "due", "due_at", "due_date", "deadline",
)
_TIME_KEYS = (
    "date", "time", "created_at", "updated_at", "received_at", "sent_at",
    "createdTime", "modifiedTime",
    *_SCHED_TIME_KEYS, "timestamp",
)
# 指纹易变键（_fingerprint 抗漂移剔除集，2026-10-15 起）= 通用时间键（强信号键
# 保留：截止变化是实质变化）——body 是整条压缩 JSON，modifiedTime 每次编辑都变，
# 不剔则同一任务次日指纹漂移、重复进 LLM 再提一次
_FP_VOLATILE_KEYS = frozenset(k for k in _TIME_KEYS if k not in _SCHED_TIME_KEYS)


def fetch_recent(db: Session, *, hours: int = 72) -> dict:
    """从全部已启用数据源拉取最近数据（签名冻结，agents 手写区契约；hours 为
    全局默认窗，实际每源窗口经行配置 / 增量游标推导，见 _window_for）。

    返回 {"sources": [{source, kind, ok, items, error, note, key, fail_streak,
    deduped}], "truncated"}：items 条目 {id, title, body, time, fingerprint}；
    ok=False 时 error 为中文摘要、items 恒空；未映射源 note 携带可用工具清单。
    key 为源稳定键（口径见 IngestSource docstring）、fail_streak 为本次尝试后的
    连败计数、deduped 为台账滤除的已见/已拒条数。未启用的源不产出条目
    （未启用 ≠ 失败）；enabled=False / pause_until 未到期 / 连败冷却中
    （_cooling_down：离线源 2h×连败封顶 24h 静默跳过）的源同样静默跳过——
    sources 恒为列表（全跳过/全失败时可为空），不存在 None 返回路径。
    认证类失败 error 带 [认证] 前缀（源过滤第三层：服务在线但凭据无效，
    错误 JSON 被条目启发式抽干成空结果时由 _auth_in_payload 主动识别）。
    整体未预期异常降级为空信封（error 键说明原因，只取类型名）——直调方
    （run_ingest）与 @tool 壳都不该见到异常。
    """
    try:
        return _fetch_recent_impl(db, hours=hours)
    except Exception as e:
        # catastrophic 外闸：收尾/构建残余异常到此为止（逐源隔离见各阶段内部）
        logger.error("数据采集整体异常（信封降级）", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {"sources": [], "truncated": False,
                "error": f"采集异常：{type(e).__name__}（本次无数据，可稍后重试）"}


def _fetch_recent_impl(db: Session, *, hours: int) -> dict:
    now = datetime.now()
    # (key, kind, 显示名, outcome)——outcome = (ok, items|None, note|None, error|None, clipped)
    raw: list[tuple[str, str, str, tuple]] = []

    lf = _collect_local_folder(db, now, hours)
    if lf is not None:
        raw.append(lf)
    raw.extend(_collect_remote(db, now, hours))

    # 收尾阶段（主线程）：统一记 attempt / 戳指纹 / 台账过滤
    entries = [_finalize_source(db, key, kind, name, outcome)
               for key, kind, name, outcome in raw]
    sources = [e[0] for e in entries]
    truncated = any(e[1] for e in entries)

    # 逐源明细日志（2026-11-04 用户需求：采集了什么、去向可追）——源名/拉到
    # 条数/台账已见滤除数/失败或跳过原因，每源一行
    for s in sources:
        if s.get("ok"):
            logger.info("[ingest] 采集 源=%s：拉到 %d 条（台账已见滤除 %d 条）",
                        s["source"], len(s["items"]), s.get("deduped", 0))
        else:
            logger.info("[ingest] 采集 源=%s：失败——%s", s["source"], s.get("error"))
    logger.info("数据采集：%d 个源，%d 条条目（台账滤除 %d），truncated=%s",
                len(sources), sum(len(s["items"]) for s in sources),
                sum(s.get("deduped", 0) for s in sources), truncated)
    return {"sources": sources, "truncated": truncated}


# —— 本地文件夹（进程内直调纯函数；mtime 时间窗过滤） ————————————————————————


def _collect_local_folder(db: Session, now: datetime,
                          default_hours: int) -> tuple[str, str, str, tuple] | None:
    """启用才采（integration_settings 开关 + ingest_sources 三闸：enabled/pause/
    连败冷却，2026-10-16 起与远程源对齐）。list_files_in 不返回 mtime，此处对命中
    清单补 stat 过滤时间窗；窗口内先按 _LOCAL_TEXT_EXTS 格式分级（富格式跳过并
    计入 note），再扫价值信号排序（含 checkbox/日期/TODO 的文件优先占名额），最新
    在前；单文件读取失败（超限/格式不支持/TOCTOU）降级为 body 内错误文本不弃条目；
    清单读取失败（目录不可访问）= 源级失败条目进冷却。
    返回 (key, kind, 显示名, outcome)；未启用返回 None。
    """
    paths, enabled = local_folder.get_local_folder_state(db)
    if not (enabled and paths):
        return None
    key = "local_folder"
    row = _source_row(db, key)
    if not _source_enabled(row) or _paused(row, now) or _cooling_down(row, now):
        return None
    since = now - timedelta(hours=_window_for(row, default_hours))
    roots = [Path(p) for p in paths]
    try:
        listing = local_folder.list_files_in(roots)
    except Exception:
        # 目录不可访问（网络盘掉线/权限回收）= 源失败进冷却（2026-10-16 起文件夹
        # 引入 ok=False 路径，三闸与远程源对齐），不炸穿整批
        logger.warning("本地文件夹清单读取失败", exc_info=True)
        return key, "local_folder", "本地文件夹", (
            False, None, None,
            "本地文件夹无法读取（目录不可访问、磁盘或网络盘未就绪）：请到数据接入检查路径",
            False)

    recent: list[tuple[datetime, dict]] = []
    for f in listing["files"]:
        try:
            mtime = datetime.fromtimestamp(Path(f["path"]).stat().st_mtime)
        except OSError:
            continue
        if mtime >= since:
            recent.append((mtime, f))
    recent.sort(key=lambda pair: pair[0], reverse=True)

    # 格式分级：文本类才进静态采集；富格式留补采按需转换（note 声明数量）
    text_recent = [pair for pair in recent
                   if Path(pair[1]["path"]).suffix.lower() in _LOCAL_TEXT_EXTS]
    skipped_rich = len(recent) - len(text_recent)
    # 价值信号排序：零成本扫描（前 512 字符）决定谁优先占 20 名额——信号只做预算
    # 分配不做价值过滤（与 _SCHED_TIME_KEYS 的保序定位一致），进窗口后由 Map 判断
    signalled = [(_local_sched_signal(Path(f["path"])), mtime, f)
                 for mtime, f in text_recent]
    signalled.sort(key=lambda t: not t[0])

    items = []
    for signal, mtime, f in signalled[:_MAX_ITEMS_PER_SOURCE]:
        try:
            body = local_folder.read_file_as_markdown(roots, f["path"])
        except ValueError as e:
            body = f"（读取失败：{e}）"
        except OSError:
            # stat() 与打开间的 TOCTOU（文件消失/被占用）——固定文案不透传
            # str(e)，防本地路径泄露（同 _describe_error 口径）
            body = "（读取失败：文件在读取前消失或被占用）"
        items.append({
            "id": f["path"],
            "title": Path(f["path"]).name,
            "body": body,
            "time": mtime.isoformat(timespec="seconds"),
            "sched_hint": signal,
            "domain": "doc",  # 笔记恒文档域——高可信开关对本地文件夹无直通效果
        })
    items, clipped = _apply_budget(items)
    note = None
    if listing["truncated"] or skipped_rich:
        parts = []
        if listing["truncated"]:
            parts.append(f"共 {listing['total']} 个文件，{len(recent)} 个在时间窗内")
        if skipped_rich:
            parts.append(f"{skipped_rich} 个富格式文件未读取（可经补采按需读取）")
        note = "；".join(parts)
    outcome = (True, items, note, None,
               clipped or len(text_recent) > _MAX_ITEMS_PER_SOURCE)
    return key, "local_folder", "本地文件夹", outcome


# 本地文件夹价值信号：checkbox / ISO 或简写日期 / TODO / 待办（零成本正则，粗筛
# 够用；误命中只影响名额排序不丢数据，漏命中由 mtime 序兜底）
_LOCAL_SIGNAL_RE = re.compile(r"-\s\[[ xX]\]|\d{4}-\d{1,2}-\d{1,2}|\bTODO\b|待办",
                              re.IGNORECASE)


def _local_sched_signal(path: Path) -> bool:
    """读文件前 512 字符扫描价值信号（读不了视为无信号，不弃条目）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(512)
    except OSError:
        return False
    return bool(_LOCAL_SIGNAL_RE.search(head))


# —— 瞬时失败重试（2026-10-16 起；文件夹源不重试：条目级锁已被条目降级消化、
# 源级失败多为确定性且 1s 退避救不了，靠冷却跨轮退避） —————————————————————


def _retryable_error(error: str | None) -> bool:
    """失败是否值得本轮内重试（白名单口径宁少勿滥，特征词见 _TRANSIENT_MARKERS）：
    认证类（[认证] 前缀）、二进制缺失等确定性失败重试必败；漏放的代价只是少重试
    一次、冷却机制仍在。"""
    if not error or error.startswith("[认证]"):
        return False
    if "未找到 CLI 可执行文件" in error:
        return False
    low = error.lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


async def _with_retry(factory, key: str) -> tuple:
    """job 通用重试包装（单 choke point，_composio_job/_mcp_job/_cli_job 实现不动）：
    失败且 _retryable_error → 退避后重跑整个 job，至多 _RETRY_MAX_ATTEMPTS 次；
    最终 outcome（无论成败）交收尾阶段——fail_streak 按最终结果每轮只计一次，
    与冷却互不干扰（重试在 job 内完成、不触 db；冷却判据 last_attempt_at 收尾才写）。
    job 协程自身消化异常是契约，此处 except 兜实现 bug（兼作 gather 护栏——异常
    结构上到不了 gather，故不需要 return_exceptions）。重跑 factory 即完整重走：
    MCP 池路径失败已 _drop 会话、重试自然重建（无僵尸）；Composio 复用已建 client
    （无状态）；CLI 重跑 discover_bin（which 级廉价）。npx 冷启动超时第二次常因
    依赖已缓存而成功——超时列入白名单的收益场景。"""
    outcome = (False, None, None, "采集未执行", False)
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            outcome = await factory()
        except Exception as e:
            logger.warning("源 %s 采集协程异常（第 %d 次）", key, attempt, exc_info=True)
            outcome = (False, None, None, f"采集协程异常：{type(e).__name__}", False)
        if outcome[0] or attempt >= _RETRY_MAX_ATTEMPTS or not _retryable_error(outcome[3]):
            break
        logger.info("源 %s 瞬时失败，%.0f 秒后重试（%d/%d）：%s",
                    key, _RETRY_BACKOFF_S, attempt + 1, _RETRY_MAX_ATTEMPTS, outcome[3])
        await asyncio.sleep(_RETRY_BACKOFF_S)
    return outcome


# —— 远程源并发采集（M2-⑤B：构建→IO→收尾三段，db 只在主线程碰） —————————————


def _collect_remote(db: Session, now: datetime,
                    default_hours: int) -> list[tuple[str, str, str, tuple]]:
    """Composio + MCP 各源并发采集：构建阶段（主线程）读行配置筛源定动作，IO 阶段
    单次 asyncio.run 内 gather（MCP 会话池每源独立超时 / Composio 同步 SDK 包
    to_thread——每源独立 execute 无共享可变态，线程安全），失败是数据逐源隔离。"""
    results: list[tuple[str, str, str, tuple]] = []
    jobs: list[tuple[str, str, str, object]] = []  # (key, kind, name, 协程工厂)

    # —— 构建阶段：Composio ——
    try:
        connections = integrations.list_connections(db)
    except integrations.KeyNotConfiguredError:
        connections = []
    except Exception as e:
        logger.warning("Composio 连接列表拉取失败", exc_info=True)
        results.append(("composio", "composio", "Composio",
                        (False, None, None, integrations._translate_upstream(e), False)))
        connections = []
    client = None
    if connections:
        try:
            client = integrations._build_client(
                integrations.get_key_settings(db).composio_api_key)
        except Exception as e:
            results.append(("composio", "composio", "Composio",
                            (False, None, None, integrations._translate_upstream(e), False)))
            connections = []
    for conn in connections:
        row = _source_row(db, conn.id)
        if not _source_enabled(row) or _paused(row, now) or _cooling_down(row, now):
            continue
        name = conn.name or conn.toolkit_slug
        mapped = _resolve_action(row, _COMPOSIO_FETCH_ACTIONS.get(conn.toolkit_slug))
        since = now - timedelta(hours=_window_for(row, default_hours))
        jobs.append((conn.id, "composio", name,
                     _composio_job(client, conn.id, conn.toolkit_slug, name, mapped, since)))

    # —— 构建阶段：MCP（get_connections 已滤 enabled 并剔除 local_folder 虚拟连接） ——
    try:
        conns = mcp_servers.get_connections(db)
    except Exception as e:
        # 族级兜底（镜像上方 Composio 先例）：清单读取失败不炸穿整批
        logger.warning("MCP 连接清单读取失败", exc_info=True)
        results.append(("mcp", "mcp", "MCP 服务",
                        (False, None, None,
                         f"MCP 服务清单读取失败：{type(e).__name__}", False)))
        conns = {}
    conns.pop("local_folder", None)
    for name, conn in conns.items():
        row = _source_row(db, name)
        if not _source_enabled(row) or _paused(row, now) or _cooling_down(row, now):
            continue
        marker = next((m for m in _MCP_FETCH_TOOLS
                       if m in (conn.get("url") or "") or m in (conn.get("command") or "")), None)
        mapped = _resolve_action(row, _MCP_FETCH_TOOLS[marker] if marker else None)
        # 条目域跟 marker 走（滴答=todo）；行配置显式覆盖 fetch_tool 后目标工具
        # 不受控，失域保守不直通（与 Composio 未标域同口径）
        domain = _MCP_DOMAINS.get(marker) if marker and not (row and row.fetch_tool) else None
        since = now - timedelta(hours=_window_for(row, default_hours))
        jobs.append((name, "mcp", name, _mcp_job(conn, name, mapped, since, domain)))

    # —— 构建阶段：CLI 工具（第四源；spec_for 兼自定义行，readonly 恒空不进补采） ——
    try:
        for cli_row in db.scalars(select(CliTool).where(CliTool.enabled.is_(True))):
            spec = cli_tools.spec_for(cli_row)
            if spec is None:
                continue
            key = f"cli:{cli_row.adapter_id}"
            row = _source_row(db, key)
            if not _source_enabled(row) or _paused(row, now) or _cooling_down(row, now):
                continue
            # 行配置覆盖与 MCP 同构：fetch_tool 填命令名即只跑该条；缺省跑全部 fetches
            # （一源多域：日历 + 任务各一条，逐条执行合并）
            override = _resolve_action(row, None)
            mapped = [override] if override is not None else [(c.name, {}) for c in spec.fetches]
            since = now - timedelta(hours=_window_for(row, default_hours))
            jobs.append((key, "cli", spec.name, _cli_job(spec, mapped, since, now)))
    except Exception as e:
        logger.warning("CLI 工具清单读取失败", exc_info=True)
        results.append(("cli", "cli", "CLI 工具",
                        (False, None, None,
                         f"CLI 工具清单读取失败：{type(e).__name__}", False)))

    if not jobs:
        return results

    # —— IO 阶段 ——
    async def _gather():
        # _with_retry 单 choke point：瞬时类失败退避重试（≤_RETRY_MAX_ATTEMPTS 次）
        # 且吞尽协程异常（结构上异常到不了 gather）
        return await asyncio.gather(*(
            _with_retry(factory, key) for key, _, _, factory in jobs))

    try:
        outcomes = asyncio.run(_gather())
    except RuntimeError:
        # 事件循环线程内直调：整批降级为逐源失败条目（线程约束见模块 docstring）
        error = "并发采集需在无事件循环的线程中调用（agent 节点 / 工作流 worker）"
        outcomes = [(False, None, None, error, False)] * len(jobs)
    results.extend((key, kind, name, outcome)
                   for (key, kind, name, _), outcome in zip(jobs, outcomes))
    return results


def _composio_job(client, connected_account_id: str, toolkit_slug: str, name: str,
                  mapped, since: datetime):
    """单连接 IO 协程工厂：同步 SDK 调用包 to_thread 并发，整体 wait_for 限墙钟。
    返回 outcome（失败是数据，异常在协程内消化不外溢打断 gather）。"""
    async def run() -> tuple:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_composio_one_sync, client, connected_account_id,
                                  toolkit_slug, name, mapped, since),
                timeout=_MCP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return (False, None, None, f"云端调用超时（>{int(_MCP_TIMEOUT_S)} 秒）", False)
        except Exception as e:
            logger.warning("Composio 应用 %s 采集失败", name, exc_info=True)
            return (False, None, None, _mark_auth(integrations._translate_upstream(e)), False)
    return run


def _composio_one_sync(client, connected_account_id: str, toolkit_slug: str, name: str,
                       mapped, since: datetime) -> tuple:
    """单连接采集（线程池内跑，不碰 db）：映射命中 → execute 拉取（响应启发式抽条目）；
    未命中 → note 工具清单。返回 outcome。"""
    if mapped is None:
        tools = client.tools.list(toolkit_slug=toolkit_slug, important="true")
        slugs = [t.slug for t in tools.items[:15]]
        note = "未内置该应用的采集动作；可用重要工具：" + "、".join(slugs) \
            if slugs else "未内置采集动作且云端未返回工具清单"
        return (True, [], note, None, False)

    tool_slug, arguments = mapped
    resp = client.tools.execute(tool_slug, arguments=dict(arguments),
                                connected_account_id=connected_account_id)
    if not resp.successful:
        # 连接级凭据过期（resp.error 常为英文特征词）同样过 _mark_auth 打 [认证]
        return (False, None, None,
                _mark_auth(f"云端执行 {tool_slug} 失败：{resp.error or '未返回原因'}"),
                False)
    items = _within_window(_extract_items(resp.data), since)
    items, clipped = _apply_budget(items)
    return (True, items, None, None, clipped)


def _mcp_job(conn: dict, name: str, mapped, since: datetime, domain: str | None = None):
    """单服务 IO 协程工厂：会话池优先，池路径任何异常回落一次性桥接（降级铁律）。
    返回 outcome。"""
    async def run() -> tuple:
        tool_name, arguments = mapped if mapped else (None, {})
        try:
            try:
                payload = await mcp_session.acall(conn, tool_name, arguments, name,
                                                  _MCP_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise
            except Exception:
                logger.info("MCP 会话池路径异常，回落一次性桥接：%s", name)
                payload = await asyncio.wait_for(
                    _mcp_once(conn, tool_name, arguments), timeout=_MCP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return (False, None, None,
                    f"连接超时：服务未在 {int(_MCP_TIMEOUT_S)} 秒内完成握手与调用", False)
        except Exception as e:
            # 失败摘要一行（与返回给 LLM 的中文口径同源）；全堆栈降 debug——
            # 连接类失败的 ExceptionGroup 展开动辄几十行，日常日志只关心哪个源、什么原因
            logger.warning("MCP 服务 %s 采集失败：%s", name, mcp_servers._describe_error(e))
            logger.debug("MCP 服务 %s 采集失败堆栈", name, exc_info=True)
            return (False, None, None, _mark_auth(mcp_servers._describe_error(e)), False)
        outcome = _payload_to_outcome(payload, since)
        if domain and outcome[1]:  # 高可信直通配套：条目随台账落域
            for it in outcome[1]:
                it["domain"] = domain
        return outcome
    return run


def _cli_job(spec: "cli_tools.CliAdapter", mapped: list, since: datetime, now: datetime):
    """单 CLI 源 IO 协程工厂（镜像 _mcp_job）：一次性子进程无会话——渲染 argv →
    arun_cli → stdout 复用 _payload_to_outcome 整形（抽条目/时间窗/预算/认证识别）。
    mapped 是 (命令名, 参数) 列表——一源多域逐条执行合并（lark：日历 +agenda 与
    任务 +get-my-tasks 各一条），单条失败是数据不毁整源（note 携带摘要）、执行过
    且全败才算源失败；纯配置问题（命令名不在清单/渲染失败）ok=True 走 note 提示，
    不进连败冷却（冷却救不了配置错误）。命令未声明 → note 清单路径（同未映射
    MCP，待实测填入即生效）。异常在协程内消化不外溢打断 gather。"""
    commands = {c.name: c for c in spec.fetches + spec.readonly}

    async def run() -> tuple:
        if not mapped:
            names = "、".join(commands) or "无"
            return (True, [], f"未内置该 CLI 的采集命令（待真机实测）；"
                    f"可用只读命令：{names}", None, False)
        found = cli_tools.discover_bin(spec)
        if found is None:
            return (False, None, None, "未找到 CLI 可执行文件（被卸载或移动？请重新「检测」）", False)
        items, notes, error, ok_any, attempted, truncated = [], [], None, False, False, False
        for name, args in mapped:
            cmd = commands.get(name)
            if cmd is None:
                notes.append(f"配置的采集命令 {name} 不在该 CLI 的命令清单中")
                continue
            argv = cli_tools.render_argv(cmd.argv, since=since, until=now,
                                         arguments=args or None)
            if argv is None:
                notes.append(f"采集命令 {cmd.name} 参数渲染失败（占位缺值）")
                continue
            try:
                ok, out, err = await cli_tools.arun_cli(
                    spec, [found[0], *found[1], *argv], cmd.timeout_s)
            except Exception as e:  # to_thread 极端异常兜底
                logger.warning("CLI 源 %s 采集异常", spec.adapter_id, exc_info=True)
                notes.append(f"采集命令 {cmd.name} 执行异常：{e}")
                continue
            attempted = True
            if not ok:
                notes.append(f"{cmd.name}：{_mark_auth(err)[:120]}")
                error = _mark_auth(f"CLI 执行失败：{err}")
                continue
            ok_i, items_i, note_i, _err_i, trunc_i = _payload_to_outcome(
                {"result": _json_or_text(out)}, since)
            ok_any = ok_any or ok_i
            # 高可信直通配套：条目随台账落域（lark 一源两域——日历命令条目 calendar、
            # 任务命令条目 todo；自定义行命令 domain=None 恒不直通）
            for it in items_i:
                it["domain"] = cmd.domain
            items.extend(items_i)
            if note_i:
                notes.append(f"{cmd.name}：{note_i}")
            truncated = truncated or trunc_i
        if attempted and not ok_any:
            return (False, None, None, error or "CLI 采集命令全部失败", False)
        return (True, items, "；".join(notes) or None, None, truncated)
    return run


async def _mcp_once(conn: dict, tool_name: str | None, arguments: dict) -> dict:
    """一次性桥接（async 版，会话池的回落路径；同步壳 _mcp_bridge 的协程体）：
    一次握手内完成定位与调用，不复用会话。"""
    client = mcp_servers._client_factory()(connections={"target": conn})
    tools = await client.get_tools()
    if tool_name is None:
        return {"tools": [{"name": t.name, "description": (t.description or "")[:120]}
                          for t in tools[:20]]}
    for t in tools:
        if t.name == tool_name:
            return {"result": await t.ainvoke(arguments)}
    return {"missing": tool_name}


def _mcp_bridge(conn: dict, tool_name: str | None, arguments: dict, name: str) -> dict:
    """同步壳 → 一次 asyncio.run 内完成握手 + 定位 + 调用（补采回落路径，保留不删）。

    返回 {"result": 工具返回} / {"missing": 工具名} / {"tools": [{name, description}]}。
    只能跑在无事件循环的线程（见模块 docstring）；整体 wait_for 限墙钟。
    """
    async def _run():
        return await _mcp_once(conn, tool_name, arguments)

    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=_MCP_TIMEOUT_S))
    except RuntimeError:
        raise RuntimeError("MCP 桥接需在无事件循环的线程中调用（agent 节点 / 工作流 worker）") from None


def _payload_to_outcome(payload: dict, since: datetime) -> tuple:
    """桥接/池结果 → outcome：result 启发式抽条目 + 时间窗 + 预算；tools/missing 进 note。
    条目抽干且返回体命中认证特征 → 判失败（ok=False，[认证] 错误）——错误 JSON 被
    启发式吞成"在线+空数据"是最隐蔽的失效形态，见 _auth_in_payload。"""
    if "result" in payload:
        raw = _json_or_text(payload["result"])
        if _first_dict_list(raw) is None:
            # 非业务包装（无条目数组）——错误响应或纯文本兜底形态，条目抽取前先看
            # 认证特征：错误 dict 会被 _extract_items 兜底成伪条目（ok=True 假象）
            auth_error = _auth_in_payload(raw)
            if auth_error:
                return (False, None, None, auth_error, False)
        items = _within_window(_extract_items(raw), since)
        items, clipped = _apply_budget(items)
        return (True, items, None, None, clipped)
    if "missing" in payload:
        return (True, [], f"映射的工具 {payload['missing']} 不在该服务工具清单中，请核对映射表",
                None, False)
    tools = payload.get("tools") or []
    note = ("未配置采集动作；可用工具：" + "、".join(t["name"] for t in tools)) if tools \
        else "未配置采集动作且服务未返回工具清单"
    return (True, [], note, None, False)


def _finalize_source(db: Session, key: str, kind: str, name: str,
                     outcome: tuple) -> tuple[dict, bool]:
    """收尾阶段（主线程）：记 attempt（健康度）→ 戳指纹 → 台账过滤，组装最终源条目。
    ok=False 时 items 恒空、不进台账（失败无条目可去重）。逐源隔离（2026-10-16 起）：
    回写/台账单源失败 rollback 降级不毁整批——指纹在两 db 步之间戳，回放条目自带
    fingerprint（模板 meta 消费不缺键）。"""
    ok, items, note, error, clipped = outcome
    try:
        streak = _note_attempt(db, key, ok, error)
    except Exception:
        logger.warning("源 %s 尝试回写失败（数据照常返回）", key, exc_info=True)
        db.rollback()
        streak = 0
    entry = {"source": name, "kind": kind, "ok": ok, "error": error, "note": note,
             "key": key, "fail_streak": streak, "deduped": 0}
    if not ok:
        entry["items"] = []
        return entry, False
    items = items or []
    for it in items:
        it["fingerprint"] = _fingerprint(key, it)
    try:
        kept, deduped = _filter_and_keepalive(db, key, items)
    except Exception:
        logger.warning("源 %s 台账过滤失败（本轮回放全部条目）", key, exc_info=True)
        db.rollback()
        kept, deduped = items, 0
    entry["items"] = kept
    entry["deduped"] = deduped
    return entry, clipped


# —— 源行状态：游标 / 窗口 / 健康度 / 配置解析 ———————————————————————————


def _source_row(db: Session, key: str) -> IngestSource | None:
    """取源行（无行 = 全默认：启用、无游标、无覆盖——不预建行，首访才落）。"""
    return db.scalar(select(IngestSource).where(IngestSource.source_key == key))


def _source_enabled(row: IngestSource | None) -> bool:
    """行缺省视为启用（ingest_sources.enabled 只做减法：显式 False 才停）。"""
    return row is None or row.enabled


def _paused(row: IngestSource | None, now: datetime) -> bool:
    """信任降频中（pause_until 未到期）整源静默跳过——静默不产出条目，综述由
    final_reply 的源价值提示兜底（docs/ai-ingest.md §九-9.3）。"""
    return row is not None and row.pause_until is not None and now < row.pause_until


def _cooling_down(row: IngestSource | None, now: datetime) -> bool:
    """连败冷却中（离线源静默跳过）：fail_streak≥1 且距上次尝试未满冷却窗
    （_FAIL_COOLDOWN_BASE_H × 连败数，封顶 _FAIL_COOLDOWN_MAX_H）。冷却过后照常
    重试，成功即清零——恢复自愈不需要任何清理任务。"""
    if row is None or not row.fail_streak or row.last_attempt_at is None:
        return False
    cooldown = timedelta(hours=min(_FAIL_COOLDOWN_BASE_H * row.fail_streak,
                                   _FAIL_COOLDOWN_MAX_H))
    return now - row.last_attempt_at < cooldown


def _auth_failure(error: str) -> bool:
    """错误文本是否认证类（特征词子串、大小写不敏感）。"""
    low = (error or "").lower()
    return any(m in low for m in _AUTH_ERROR_MARKERS)


def _mark_auth(error: str) -> str:
    """认证类错误统一打 [认证] 前缀（幂等）：落库 last_error 与返回 LLM 的 error
    同源可辨——综述据此提示用户重配 Key，补采模型据此换源不再重试本源。"""
    if error and not error.startswith("[认证]") and _auth_failure(error):
        return f"[认证] {error}"
    return error


def _window_for(row: IngestSource | None, default_hours: int) -> int:
    """推导该源时间窗（小时）：行覆盖窗 window_hours > 增量游标（距上次成功）> 全局
    默认；统一钳制 [1h, 168h]（下限防白跑、上限即首采护栏=一周中断补齐上限）。"""
    hours = float(default_hours)
    if row is not None:
        if row.window_hours:
            hours = float(row.window_hours)
        elif row.last_success_at is not None:
            hours = (datetime.now() - row.last_success_at).total_seconds() / 3600
    return int(max(_WINDOW_MIN_H, min(_WINDOW_MAX_H, round(hours))))


def _resolve_action(row: IngestSource | None, default) -> tuple | None:
    """采集动作三级优先（M2-②）：行配置 fetch_tool/fetch_args > 内置映射表 > None
    （None = note 清单路径）。行配置显式覆盖即使内置表未映射该源也生效。"""
    if row is not None and row.fetch_tool:
        return (row.fetch_tool, dict(row.fetch_args or {}))
    return default


def _note_attempt(db: Session, key: str, ok: bool, error: str | None = None) -> int:
    """回写一次尝试（get_or_create 源行）：成功清零连败 + 记 success/ok_at + 清 error；
    失败连败 +1 + 记中文摘要（≤200）。返回本次后的连败计数。"""
    row = _source_row(db, key)
    if row is None:
        row = IngestSource(source_key=key)
        db.add(row)
    row.last_attempt_at = datetime.now()
    if ok:
        row.last_success_at = row.last_attempt_at
        row.last_ok_at = row.last_attempt_at
        row.fail_streak = 0
        row.last_error = None
    else:
        row.fail_streak = (row.fail_streak or 0) + 1
        if error:
            row.last_error = error[:200]
    db.commit()
    return row.fail_streak or 0


def _fingerprint(key: str, item: dict) -> str:
    """条目指纹（冻结定义，docs/ai-ingest.md §九-9.1）：sha1(source_key|item.id or
    title|body 前 200 字符)——body 前缀保证无稳定 id 的启发式条目跨日对得上，
    内容实质变化 → 新指纹重新进入。

    抗漂移（2026-10-15 起）：body 可解析为 JSON dict 时先剔 _FP_VOLATILE_KEYS 再
    sort_keys 序列化——同一条目仅 modifiedTime 等触碰变化不再漂移成新指纹（跨天
    重复进 LLM 的主因）；dueDate 等强信号键保留参与（截止变化 = 实质变化）。解析
    失败（纯文本）原样。算法变更致存量台账一次性失配重放一遍（上线前无生产流量）。"""
    basis = "|".join([
        key,
        str(item.get("id") or item.get("title") or ""),
        _stable_body(item.get("body") or "")[:200],
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _stable_body(body: str) -> str:
    """body 抗漂移稳定化：JSON dict 剔易变时间键 + sort_keys 重排（与键序无关）；
    非 JSON 原样返回（纯文本条目无键可剔）。"""
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(d, dict):
        return body
    d = {k: v for k, v in d.items() if k not in _FP_VOLATILE_KEYS}
    return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


# —— 台账（M2-①）：内容级跨日去重 / 拒绝记忆 / 留存清理 ————————————————————


def _filter_and_keepalive(db: Session, key: str, items: list[dict]) -> tuple[list[dict], int]:
    """采集收尾台账过滤：rejected 滤除（永静默）/ 已见保活滤除（last_seen +
    seen_count，不重复进 LLM）/ 新指纹插入放行。返回 (放行条目, 滤除计数)。

    结构化域例外（2026-11-05 存量卡死修复）：todo/calendar 条目「已见且从未
    proposed（没进过任何建议卡/直落）」重新放行——直映射零 LLM 成本，重放幂等
    靠装配层查重。背景：时区修复前降级落库/被丢弃的旧条目被「已见即滤」永久
    吞掉，下游任何形态修复（时区/直映射/装配）后旧条目都拿不到重新处理，真机
    表现为「拉到 0 条像源坏了」。doc 域维持滤除（已见不进 LLM 的成本语义不变）。
    曾产出（proposed_at 非空）与被拒（rejected_at）条目照滤——不重复建卡/直落。"""
    now = datetime.now()
    rows = {r.fingerprint: r for r in db.scalars(
        select(IngestionItem).where(IngestionItem.source_key == key))}
    kept: list[dict] = []
    deduped = 0
    for it in items:
        fp = it.get("fingerprint") or ""
        row = rows.get(fp)
        if row is None:
            db.add(IngestionItem(source_key=key, fingerprint=fp,
                                 item_title=(it.get("title") or "")[:80],
                                 domain=it.get("domain")))
            kept.append(it)
        elif row.rejected_at is not None:
            deduped += 1
        elif (it.get("domain") in STRUCTURED_DOMAINS
              and row.proposed_at is None):
            row.last_seen_at = now
            row.seen_count = (row.seen_count or 1) + 1
            kept.append(it)  # 结构化已见未提：重放（直映射零成本，装配查重幂等）
        else:
            row.last_seen_at = now
            row.seen_count = (row.seen_count or 1) + 1
            deduped += 1
    db.commit()
    return kept, deduped


def mark_items_proposed(db: Session, refs: list[tuple[str, str]]) -> None:
    """建议卡落库后回写台账（proposed_at 首次进卡时刻）。refs = [(source_key,
    fingerprint)]；查无行（如补采条目未入台账）静默跳过。"""
    now = datetime.now()
    for key, fp in set(refs):
        row = db.scalar(select(IngestionItem).where(
            IngestionItem.source_key == key, IngestionItem.fingerprint == fp))
        if row is not None and row.proposed_at is None:
            row.proposed_at = now
    db.commit()


STRUCTURED_DOMAINS = ("todo", "calendar")

# 源优先级键（2026-11-05 优先级同步批，用户需求：滴答高中低随源显示）：滴答系
# 0=无 1=高 3=中 5=低（openapi 惯例）；字符串形态（high/medium/low 与中文）宽容
_PRIORITY_KEYS = ("priority", "priorityLevel", "importance")


def mapped_priority(raw) -> int | None:
    """源原始优先级 → Clara 口径（1高 2中 3低）；无/不认识返回 None（调用方
    默认低——用户定案「无优先级默认低」）。滴答数值域单源实测为准；其他数值
    域源（如 0-3 式）遇到再适配（tech-debt 登记），当前 1/3/5 映射高/中/低、
    2 兜底中、0 与负数视为无。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        low = raw.strip().lower()
        return {"high": 1, "p1": 1, "高": 1,
                "medium": 2, "mid": 2, "p2": 2, "中": 2,
                "low": 3, "p3": 3, "p4": 3, "低": 3}.get(low)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {1: 1, 2: 2, 3: 2, 5: 3}.get(int(raw)) if raw else None
    return None


def split_structured_items(src: dict) -> tuple[list[dict], list[dict]]:
    """单源条目按台账 domain 拆 (结构化条目, 其余条目)（2026-11-05 采集零 LLM 批）。

    todo/calendar 域 = fetch 层结构性数据（滴答/飞书日历待办/钉钉待办等——本来就是
    日程/待办，标题时间即权威数据）；doc/未标域 = 综合工具素材（本地文件夹/
    Obsidian/Notion/飞书文档），才需要 LLM 从数据中提炼。返回原条目引用零拷贝，
    调用方（agents/graph._map_phase）据此分流：结构化条目直映射零 LLM（真机铁证：
    滴答「时间待定」待办被 Reduce LLM 编造成次日 09:30 的日历事件直通落库——
    结构化数据过 LLM 只会失真 + 白耗 7s），其余进 SOURCE_DIGEST 提炼。"""
    structured, rest = [], []
    for it in src.get("items") or []:
        (structured if it.get("domain") in STRUCTURED_DOMAINS else rest).append(it)
    return structured, rest


# —— 高可信直通拆分（2026-10-24）———————————————————————————————————————

def split_trusted_direct(db: Session, draft_plan: list[dict], inbox_items: list[dict],
                         evidence: list[dict]) -> dict:
    """Reduce 产物按「源 trusted ∧ 台账 domain ∈ {todo, calendar}」拆直落组与建卡组
    （用户定案：结构性数据——待办/日历域——免确认直落收件箱/日历；笔记文档提炼
    的建议不准确，恒须进数据源页确认）。

    判定纯靠台账回查（指纹对账）——零 LLM 依赖、零提示词改动：inbox 条目读自带
    source_ref，draft 条目按 evidence 溯源行（无证据 = 无法对账，保守建卡）；台账
    查不到（补采条目 / Reduce 白名单后滤残留）同样保守建卡。调用方：workflows.
    _run_ingest 与 chat_turn.finalize_turn（直落落库在壳层，同 auto_apply 先例）。

    返回八键：draft_rest / inbox_rest / evidence_rest（建卡组——原条目形状不变，
    rest 证据行 idx 已重排对齐收缩后的草案序，前端溯源契约不破）；draft_direct /
    inbox_direct（直落组原条目，建 accepted 卡作落库快照与台账闭环）；draft_direct_
    entries（直落草案 + source_ref——apply_plan_entries 溯源三列透传形态）；evidence_
    direct（直落草案证据行，原 idx 保留，payload_refs / mark_items_proposed 消费）；
    draft_fallback / evidence_fallback（降级合并预计算——直落落库失败整批并回建卡
    时的 idx 重排形态）。无任何直落时退化为全量回传，调用方零分支。"""
    trusted_keys = set(db.scalars(
        select(IngestSource.source_key).where(IngestSource.trusted.is_(True))))

    # 批量预取（2026-11-01 优化批 3）：候选指纹 → domain 一次查齐——旧版逐条
    # scalar 回查 N+1（条目多时采集轮收尾拖慢），判定逻辑与口径零变化
    _pairs = set()
    for it in inbox_items:
        ref = it.get("source_ref") or {}
        if ref.get("source_key") in trusted_keys and ref.get("fingerprint"):
            _pairs.add((ref["source_key"], ref["fingerprint"]))
    for e in evidence:
        if e.get("source_key") in trusted_keys and e.get("fingerprint"):
            _pairs.add((e["source_key"], e["fingerprint"]))
    domain_map: dict[tuple[str, str], str | None] = {}
    if _pairs:
        _fps = {fp for _sk, fp in _pairs}
        for sk, fp, dom in db.execute(
            select(IngestionItem.source_key, IngestionItem.fingerprint,
                   IngestionItem.domain).where(
                IngestionItem.source_key.in_(trusted_keys),
                IngestionItem.fingerprint.in_(_fps))):
            domain_map[(sk, fp)] = dom

    def _direct(sk, fp) -> bool:
        if not sk or not fp or sk not in trusted_keys:
            return False
        return domain_map.get((sk, fp)) in ("todo", "calendar")

    inbox_direct: list[dict] = []
    inbox_rest: list[dict] = []
    for it in inbox_items:
        ref = it.get("source_ref") or {}
        hit = _direct(ref.get("source_key"), ref.get("fingerprint"))
        (inbox_direct if hit else inbox_rest).append(it)
        if hit:
            # 直通明细（2026-11-04 用户需求）：哪条待办从哪个源直落收件箱
            logger.info("[ingest] 高可信直通：源=%s 待办「%s」→ 直落收件箱",
                        ref.get("source_key"), str(it.get("title") or "?")[:20])

    ev_by_idx: dict[int, dict] = {}
    for e in evidence:
        i = e.get("idx")
        if isinstance(i, int) and i not in ev_by_idx:
            ev_by_idx[i] = e  # 同 idx 多证据取首条（对账判定一条即足）
    draft_direct: list[dict] = []
    draft_rest: list[dict] = []
    direct_idx: set[int] = set()
    for i, d in enumerate(draft_plan):
        e = ev_by_idx.get(i)
        if e is not None and _direct(e.get("source_key"), e.get("fingerprint")):
            draft_direct.append(d)
            direct_idx.add(i)
            # 直通明细（2026-11-04 用户需求）：哪条日程从哪个源直落日历（含时间）
            logger.info("[ingest] 高可信直通：源=%s 日程「%s」→ 直落日历%s",
                        e.get("source_key"), str(d.get("title") or "?")[:20],
                        f"（{str(d.get('start'))[:16]}）" if d.get("start") else "")
        else:
            draft_rest.append(d)

    evidence_direct = [e for e in evidence if e.get("idx") in direct_idx]
    remap = {old: new for new, old in enumerate(
        i for i in range(len(draft_plan)) if i not in direct_idx)}
    evidence_rest = [{**e, "idx": remap[e["idx"]]} for e in evidence
                     if e.get("idx") not in direct_idx]
    # 直落条目附 source_ref（溯源三列随 apply_plan_entries 落 Task；原条目不动——
    # payload 快照保持 Reduce 契约形状），按草案原序枚举保持稳定
    draft_direct_entries = [
        {**d, "source_ref": {"source_key": ev_by_idx[i]["source_key"],
                             "fingerprint": ev_by_idx[i]["fingerprint"]}}
        for i, d in enumerate(draft_plan) if i in direct_idx]
    # 降级合并预计算（直落落库 ValueError 时调用方整批并回建卡）：direct 在前，
    # direct 证据 idx 映射到 0..len(direct)-1、rest 证据 idx 偏移 len(direct)——
    # idx 几何集中在拆分处一次算清，调用方降级路径零重排代码
    shift = {old: new for new, old in enumerate(sorted(direct_idx))}
    evidence_fallback = (
        [{**e, "idx": shift[e["idx"]]} for e in evidence if e.get("idx") in direct_idx]
        + [{**e, "idx": e["idx"] + len(draft_direct)} for e in evidence_rest])
    return {
        "draft_rest": draft_rest, "inbox_rest": inbox_rest, "evidence_rest": evidence_rest,
        "draft_direct": draft_direct, "inbox_direct": inbox_direct,
        "draft_direct_entries": draft_direct_entries, "evidence_direct": evidence_direct,
        "draft_fallback": draft_direct + draft_rest, "evidence_fallback": evidence_fallback,
    }


def payload_refs(kind: str, payload: dict) -> set[tuple[str, str]]:
    """解析建议卡 payload 覆盖的 (source_key, fingerprint) 集合（mark/settle 共用，
    冻结契约：schedule 卡读 evidence 数组、inbox 卡逐条读 source_ref）。旧卡无
    这些键返回空集。"""
    fps: set[tuple[str, str]] = set()
    if kind == "schedule":
        for e in payload.get("evidence") or []:
            if e.get("source_key") and e.get("fingerprint"):
                fps.add((e["source_key"], e["fingerprint"]))
    elif kind == "inbox":
        for it in payload.get("items") or []:
            ref = it.get("source_ref") or {}
            if ref.get("source_key") and ref.get("fingerprint"):
                fps.add((ref["source_key"], ref["fingerprint"]))
    return fps


def on_proposal_settled(db: Session, kind: str, payload: dict, outcome: str) -> None:
    """建议结算钩子（routers 结算端点后一行调用，转发不是业务——同 settings 先例）：
    outcome=rejected 且 kind∈{schedule, inbox} → 卡内全部指纹置 rejected_at 永静默。
    accepted/expired 不动台账（已见条目本就 keepalive 滤除，不二次打扰）。"""
    if outcome != "rejected" or kind not in ("schedule", "inbox"):
        return
    fps = payload_refs(kind, payload)
    if not fps:
        return
    now = datetime.now()
    for key, fp in fps:
        row = db.scalar(select(IngestionItem).where(
            IngestionItem.source_key == key, IngestionItem.fingerprint == fp))
        if row is not None and row.rejected_at is None:
            row.rejected_at = now
    db.commit()


def purge_stale_items(db: Session, *, days: int = 30) -> int:
    """留存清理（夜间挂接）：物理删除 days 天未见条目（台账无软删价值）。返回删除数。"""
    cutoff = datetime.now() - timedelta(days=days)
    n = db.execute(delete(IngestionItem).where(IngestionItem.last_seen_at < cutoff)).rowcount
    db.commit()
    return n or 0


# —— 每源配置（M2-②）：枚举 / 读取 / 更新 —————————————————————————————————


def _enumerate_sources(db: Session) -> list[dict]:
    """枚举当前真实启用的源（key/kind/显示名；list_ingest_sources 与补采源校验共用）。
    Composio 连接清单本身是云端调用（现状 DataSettings 同款成本，绕不开），但走
    enrich=False 跳过每 slug 的元数据 N+1（本函数消费方只用 id/slug/kind，name 落
    slug 兜底——前端摘要行不显示该字段，2026-10-15 数据接入页打开慢排障）；MCP 与
    本地文件夹读本地库零网络。"""
    out: list[dict] = []
    paths, enabled = local_folder.get_local_folder_state(db)
    if enabled and paths:
        out.append({"key": "local_folder", "source": "本地文件夹", "kind": "local_folder"})
    try:
        connections = integrations.list_connections(db, enrich=False)
    except Exception:
        connections = []  # Key 未配置 / 拉取失败 = 无 Composio 源（徽标另有展示）
    for conn in connections:
        out.append({"key": conn.id, "source": conn.name or conn.toolkit_slug,
                    "kind": "composio"})
    for name in mcp_servers.get_connections(db):
        if name != "local_folder":
            out.append({"key": name, "source": name, "kind": "mcp"})
    for cli_row in db.scalars(select(CliTool).where(CliTool.enabled.is_(True))):
        spec = cli_tools.spec_for(cli_row)  # 精选脏行（注册表查无且非自定义）跳过
        if spec is not None:
            out.append({"key": f"cli:{cli_row.adapter_id}", "source": spec.name, "kind": "cli"})
    return out


def list_ingest_sources(db: Session) -> list[dict]:
    """GET /api/ingest/sources 数据源：枚举 + 行配置/健康度 + available_tools。
    零额外网络——available_tools 仅 MCP 会话池命中时零成本填充（未命中空列表，
    前端引导「先点卡上『测试』刷新工具清单」，不做独立探测）。"""
    out: list[dict] = []
    for s in _enumerate_sources(db):
        row = _source_row(db, s["key"])
        out.append({
            **s,
            "enabled": _source_enabled(row),
            "trusted": bool(row.trusted) if row else False,
            "fetch_tool": row.fetch_tool if row else None,
            "fetch_args": row.fetch_args if row else None,
            "window_hours": row.window_hours if row else None,
            "pause_until": row.pause_until if row else None,
            "fail_streak": row.fail_streak if row else 0,
            "last_error": row.last_error if row else None,
            "last_ok_at": row.last_ok_at if row else None,
            "last_success_at": row.last_success_at if row else None,
            "available_tools": mcp_session.peek_tools(s["key"]) if s["kind"] == "mcp"
                else cli_tools.declared_actions(s["key"]) if s["kind"] == "cli" else [],
        })
    return out


def update_ingest_source(db: Session, key: str, patch) -> IngestSource | None:
    """PUT /api/ingest/sources/{key}：key 必须是当前真实源（防脏键落行），行
    get_or_create 后逐列覆盖。patch 为 schemas.IngestSourceUpdate（exclude_unset）；
    fetch_args 已在 schema 层校验 dict，此处补序列化长度闸（≤2000 字符）。返回
    更新后的行；key 未登记返回 None（路由转 404）。"""
    if key not in {s["key"] for s in _enumerate_sources(db)}:
        return None
    row = _source_row(db, key)
    if row is None:
        row = IngestSource(source_key=key)
        db.add(row)
    data = patch.model_dump(exclude_unset=True)
    if data.get("fetch_args") is not None:
        if len(json.dumps(data["fetch_args"], ensure_ascii=False, default=str)) > 2000:
            raise ValueError("采集参数序列化后超过 2000 字符")
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


def mirror_source_enabled(db: Session, key: str, enabled: bool,
                          *, trusted: bool | None = None) -> None:
    """卡片启停联动镜像（get_or_create 行）：MCP / 本地文件夹的停用即不参与采集。
    采集配置 UI 已撤（2026-10-15），卡片「停用/启用」是源的退出手段——同步镜像
    保证摘要行如实显示停用态，也防存量 enabled=False 行恢复连接后被采集闸卡死。
    trusted（2026-10-24）仅在**建行**时生效——接入默认值策略的落点；存量行不动
    （重接入不重置用户关掉的直通开关）。"""
    row = _source_row(db, key)
    if row is None:
        db.add(IngestSource(source_key=key, enabled=enabled,
                            trusted=bool(trusted)))
    else:
        row.enabled = enabled
    db.commit()


def reset_source_health(db: Session, key: str) -> None:
    """用户介入即重置连败证据（2026-10-16 起，滴答清单换 token 后仍被冷却跳过的
    排障结论）：配置写入（新建/编辑/接入/启用）或验证成功（测试通过）时由各接入
    service 调用——旧失败是对旧配置的记录，凭据修好后冷却闸（_cooling_down）不该
    继续拦好源。fail_streak 清零 + last_error 清空即放行下轮采集；增量游标
    last_success_at 与信任降频 pause_until 不动（前者是增量语义，重置会放大窗口
    重复采；后者是采纳率判断，与连通性无关）。行不存在则无证据可清、不建行
    （首访才落的口径不变）。"""
    row = _source_row(db, key)
    if row is not None and (row.fail_streak or row.last_error):
        row.fail_streak = 0
        row.last_error = None
        db.commit()


# —— 受控补采（M1-④）：清单 + 只读工具硬闸 ——————————————————————————————


def list_source_actions(db) -> list[dict]:
    """各源可用工具清单（LLM 补采前先看清单再选工具；也供配置面板参考）。实时探测：
    Composio tools.list 前 15 / MCP 会话池拉取前 20，失败源给空清单（零中断）。
    只能在无事件循环的线程调用（mcp_session.call 线程约束）。"""
    out: list[dict] = []
    try:
        connections = integrations.list_connections(db)
    except Exception:
        connections = []
    if connections:
        try:
            client = integrations._build_client(
                integrations.get_key_settings(db).composio_api_key)
            for conn in connections:
                try:
                    tools = client.tools.list(toolkit_slug=conn.toolkit_slug, important="true")
                    tool_list = [{"name": t.slug, "description": (t.description or "")[:120]}
                                 for t in tools.items[:15]]
                except Exception:
                    tool_list = []
                out.append({"source": conn.name or conn.toolkit_slug, "key": conn.id,
                            "kind": "composio", "tools": tool_list})
        except Exception:
            pass  # client 构建失败 = Composio 整体不可用，源清单留空
    conns = mcp_servers.get_connections(db)
    conns.pop("local_folder", None)
    for name, conn in conns.items():
        try:
            payload = mcp_session.call(conn, None, {}, name, _MCP_TIMEOUT_S)
            tool_list = payload.get("tools") or []
        except Exception:
            logger.warning("MCP 服务 %s 工具清单拉取失败", name, exc_info=True)
            tool_list = []
        out.append({"source": name, "key": name, "kind": "mcp", "tools": tool_list})
    for s in _enumerate_sources(db):
        if s["kind"] == "cli":  # 声明即清单，零子进程零网络
            out.append({"source": s["source"], "key": s["key"], "kind": "cli",
                        "tools": cli_tools.declared_actions(s["key"])})
    return out


def call_readonly_tool(db: Session, *, key: str, tool: str, arguments) -> dict:
    """受控单源单工具补采（LLM 不可绕过的 service 层硬闸，提示词只是软约束）。

    硬闸五条：① key 解析到启用源（local_folder 拒绝——纯函数已全量直采，补采无意义）
    ② 只读判定 = deny-list 特征词子串 ③ arguments JSON 对象序列化 ≤2000 字符
    ④ 单调用限时 60s、每源每日 ≤2 次 ⑤ 结果过 _extract_items + 时间窗 + 预算并记
    attempt。拒绝/失败一律返回"失败是数据"的单源条目（fetch_recent 同款形态）不抛
    异常——调用方是 @tool 壳，抛异常会打断 ReAct 循环，错误文本交给模型自选下一步。

    取舍：补采条目不进台账指纹链（补采是即时消费，若走 keepalive 会导致同工具只有
    第一次补采有数据；登记 docs/tech-debt.md）。"""
    def _fail_tool(error: str) -> dict:
        error = _mark_auth(error)  # 认证类打 [认证] 前缀：LLM 读到换源不重试、综述提示重配 Key
        try:
            streak = _note_attempt(db, key, False, error)
        except Exception:
            # 公共出口自身不能再抛（docstring 承诺不抛异常）：回写失败降级 streak=0
            logger.warning("补采 %s 失败回写异常", key, exc_info=True)
            db.rollback()
            streak = 0
        return {"source": display, "kind": kind, "ok": False, "items": [], "error": error,
                "note": None, "key": key, "fail_streak": streak,
                "deduped": 0}

    # ① 源解析
    src = next((s for s in _enumerate_sources(db) if s["key"] == key), None)
    if src is None:
        return {"source": key, "kind": "unknown", "ok": False, "items": [],
                "error": "该数据源不存在或未启用", "note": None, "key": key,
                "fail_streak": 0, "deduped": 0}
    display, kind = src["source"], src["kind"]
    if kind == "local_folder":
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": "本地文件夹不支持补采（每日采集已覆盖全部文件）", "note": None,
                "key": key, "fail_streak": 0, "deduped": 0}
    # ② 只读判定
    low = (tool or "").lower()
    if any(m in low for m in _TOOL_DENIED_MARKERS):
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": f"工具 {tool} 不是只读工具，已拒绝（补采仅允许读取类动作）",
                "note": None, "key": key, "fail_streak": 0, "deduped": 0}
    # ③ 参数形态
    if not isinstance(arguments, dict):
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": "arguments 必须是 JSON 对象", "note": None, "key": key,
                "fail_streak": 0, "deduped": 0}
    if len(json.dumps(arguments, ensure_ascii=False, default=str)) > 2000:
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": "arguments 序列化后超过 2000 字符，已拒绝", "note": None,
                "key": key, "fail_streak": 0, "deduped": 0}
    # ④ 每日次数上限（执行即计——云端拒绝也算一次调用，防刷）
    today = date.today()
    used_day, used = _TOOL_CAP_STATE.get(key, (today, 0))
    if used_day != today:
        used = 0
    if used >= _DAILY_TOOL_CAP:
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": f"该源今日补采已达上限（{_DAILY_TOOL_CAP} 次）", "note": None,
                "key": key, "fail_streak": 0, "deduped": 0}

    # ⑤ 执行 + 结果整形
    try:
        if kind == "composio":
            client = integrations._build_client(
                integrations.get_key_settings(db).composio_api_key)
            resp = client.tools.execute(tool, arguments=dict(arguments), connected_account_id=key)
            _TOOL_CAP_STATE[key] = (today, used + 1)
            if not resp.successful:
                return _fail_tool(f"云端执行 {tool} 失败：{resp.error or '未返回原因'}")
            raw = resp.data
        elif kind == "cli":
            cli_row = db.get(CliTool, key.removeprefix(cli_tools.SOURCE_PREFIX))
            if cli_row is not None and cli_row.custom_bin:
                # 窄口红线：自定义行仅参与静态采集，不进 AI 补采（固定 argv 无白名单
                # 可言——不能因 fetch 命令名恰好命中而放行任意用户命令）
                return _fail_tool("自定义 CLI 源不支持补采（仅参与静态采集）")
            # 白名单（含内置 fetches）+ 声明静态 token 二次 deny-list
            spec = cli_tools.spec_for(cli_row) if cli_row is not None else None
            commands = {c.name: c for c in
                        (spec.fetches + spec.readonly if spec is not None else ())}
            cmd = commands.get(tool)
            if spec is None or cmd is None:
                return _fail_tool(f"命令 {tool} 不在该 CLI 的只读白名单中（自定义 CLI 源不支持补采）")
            if cli_tools.static_tokens_denied(cmd.argv):
                return _fail_tool(f"命令 {tool} 的声明含写操作特征词，已拒绝")
            found = cli_tools.discover_bin(spec)
            if found is None:
                return _fail_tool("未找到 CLI 可执行文件")
            argv = cli_tools.render_argv(cmd.argv, arguments=arguments)
            if argv is None:
                return _fail_tool(f"命令 {tool} 参数渲染失败（占位缺值）")
            _TOOL_CAP_STATE[key] = (today, used + 1)
            ok, out, err = cli_tools.run_cli(spec, [found[0], *found[1], *argv], cmd.timeout_s)
            if not ok:
                return _fail_tool(f"CLI 执行失败：{err}")
            raw = _json_or_text(out)
        else:
            conn = mcp_servers.get_connections(db).get(key)
            if conn is None:
                # 枚举后连接被并发停用/删除：给 LLM 可行动的中文指引而非 KeyError 文案
                return _fail_tool("该 MCP 服务的连接信息已变更（可能被停用或删除），"
                                  "请重新 list_data_sources")
            _TOOL_CAP_STATE[key] = (today, used + 1)
            try:
                try:
                    payload = mcp_session.call(conn, tool, arguments, display, _TOOL_TIMEOUT_S)
                except Exception:
                    logger.info("MCP 会话池路径异常，回落一次性桥接：%s", display)
                    payload = _mcp_bridge(conn, tool, arguments, display)
            except asyncio.TimeoutError:
                return _fail_tool(f"工具调用超时（>{int(_TOOL_TIMEOUT_S)} 秒）")
            if "missing" in payload:
                return _fail_tool(f"工具 {payload['missing']} 不在该服务工具清单中")
            raw = _json_or_text(payload["result"])
    except Exception as e:
        logger.warning("补采 %s/%s 失败", key, tool, exc_info=True)
        msg = integrations._translate_upstream(e) if kind == "composio" else mcp_servers._describe_error(e)
        return _fail_tool(msg)
    # 尾段（整形/台账/回写）兜底（2026-10-16 起）：此前在大 try 之外，DB 异常会
    # 冒泡违背「不抛异常」承诺——降级为静态失败条目（不再碰 db）
    try:
        # 非业务包装（无条目数组）时条目抽取前先看认证特征（同静态采集
        # _payload_to_outcome）：错误 dict 会被 _extract_items 兜底成伪条目
        if _first_dict_list(raw) is None:
            auth_error = _auth_in_payload(raw)
            if auth_error:
                return _fail_tool(auth_error)
        items = _within_window(_extract_items(raw), datetime.now() - timedelta(hours=24))
        items, clipped = _apply_budget(items)
        for it in items:
            it["fingerprint"] = _fingerprint(key, it)
        # rejected 滤除（跨路径对账，2026-10-15 起）：用户明确拒绝过的内容补采也不该
        # 再提——按该源台账 rejected 指纹集滤除（keepalive 不适用于补采，见上方取舍）
        rejected = {fp for (fp,) in db.execute(
            select(IngestionItem.fingerprint).where(
                IngestionItem.source_key == key,
                IngestionItem.rejected_at.isnot(None),
            )
        )}
        items = [it for it in items if it["fingerprint"] not in rejected]
        streak = _note_attempt(db, key, True)
    except Exception as e:
        logger.warning("补采 %s/%s 结果处理异常", key, tool, exc_info=True)
        db.rollback()
        return {"source": display, "kind": kind, "ok": False, "items": [],
                "error": f"补采结果处理失败：{type(e).__name__}", "note": None,
                "key": key, "fail_streak": 0, "deduped": 0}
    return {"source": display, "kind": kind, "ok": True, "items": items, "error": None,
            "note": f"补采工具 {tool}", "key": key, "fail_streak": streak, "deduped": 0}


# —— 教练溯源单跳探测（幕僚长 context_fetcher 消费，2026-10-23） ————————————
# 口径：探测与补采共享每源每日帽（_DAILY_TOOL_CAP=2）——探测是有预算的外部调用，
# 不因换个入口绕过限流；探测失败记源健康连败（与采集同账）——工具解析错配（如
# MCP 搜索工具参数不叫 query）会真实拉低源健康，各源参数键待真机核验回填
# docs/ai-ingest.md 映射表后此副作用自然收敛。


def _probe_tool_for(db: Session, key: str, src: dict) -> tuple[str, str] | None:
    """探测工具解析（纯解析不执行）：返回 (工具名, 查询参数键) 或 None。

    CLI：白名单（fetches+readonly，call_readonly_tool 同口径）挑名含 search/
    query 的命令，task/todo 域优先（教练问的多是项目任务——lark 实测 calendar/
    task 两域搜索取任务域；参数键 arg_query 即 render_argv 占位符名）。MCP：
    会话池拉清单挑同口径首个，参数键 query（主流默认，Obsidian 待真机核验）。
    Composio 不进探测首批——现有工具无任务域搜索价值，Notion 连接后随映射表
    一并核验。解析失败/零候选 → None，探测静默降级。"""
    def _pick(names: list[str]) -> str | None:
        hits = [n for n in names if any(t in n.lower() for t in ("search", "query"))]
        hits.sort(key=lambda n: not any(w in n.lower() for w in ("task", "todo")))
        return hits[0] if hits else None

    if src["kind"] == "cli":
        cli_row = db.get(CliTool, key.removeprefix(cli_tools.SOURCE_PREFIX))
        spec = cli_tools.spec_for(cli_row) if cli_row is not None else None
        if spec is None:
            return None
        name = _pick([c.name for c in spec.fetches + spec.readonly])
        return (name, "arg_query") if name else None
    if src["kind"] == "mcp":
        conn = mcp_servers.get_connections(db).get(key)
        if conn is None:
            return None
        try:
            payload = mcp_session.call(conn, None, {}, key, _MCP_TIMEOUT_S)
        except Exception:
            logger.info("溯源探测：MCP %s 工具清单拉取失败，探测降级", key)
            return None
        name = _pick([t.get("name") or "" for t in payload.get("tools") or []])
        return (name, "query") if name else None
    return None


def probe_source(db: Session, source_key: str, query: str) -> str | None:
    """教练溯源单跳探测（公开入口，幕僚长 task_breakdown/execution 场景经
    context_fetcher 调用）：按相关任务标题回源搜索更多素材。

    确定性触发零 LLM 决策（防失控——场景闸与每轮 1 次在调用方），复用
    call_readonly_tool 全部硬闸（源解析/deny-list/参数限长/60s 限时/每日帽，
    见其 docstring）。结果条目压成「【标题】正文」文本行截 2000 字符；无
    工具/超帽/失败/零命中一律 None 静默降级——调用方不区分原因（静态
    evidence 摘录兜底，教练链路永不因探测失败降级）。"""
    query = (query or "").strip()[:40]
    if not query:
        return None
    src = next((s for s in _enumerate_sources(db) if s["key"] == source_key), None)
    if src is None:
        return None
    tool = _probe_tool_for(db, source_key, src)
    if tool is None:
        return None
    result = call_readonly_tool(db, key=source_key, tool=tool[0],
                                arguments={tool[1]: query})
    if not result.get("ok"):
        return None
    parts = [f"【{(it.get('title') or '条目').strip()[:60]}】"
             f"{(it.get('body') or '').strip()[:360]}"
             for it in (result.get("items") or [])[:5] if isinstance(it, dict)]
    return "\n".join(parts)[:2000] or None


# —— 信任与预算（M2-③ / §十-3） ———————————————————————————————————————


def apply_trust_policy(db: Session) -> dict:
    """信任降频纯规则（夜间挂接，零 LLM）：按源结算表 total≥5 且 rate<0.3 →
    pause_until=明日 09:00；其余清空（到期恢复 = 只比较时刻，无清理任务）。"""
    from services import ai_data  # 局部导入防循环（ai_data 顶层 import settings）

    stats = ai_data.adoption_stats_by_source(db)
    tomorrow_9 = (datetime.now() + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    paused: list[str] = []
    for s in stats["by_source"]:
        row = _source_row(db, s["source_key"])
        if s["total"] >= 5 and s["rate"] < 0.3:
            if row is None:
                row = IngestSource(source_key=s["source_key"])
                db.add(row)
            row.pause_until = tomorrow_9
            paused.append(s["source_key"])
        elif row is not None and row.pause_until is not None:
            row.pause_until = None
    db.commit()
    return {"paused": paused, "by_source": stats["by_source"]}


def claim_llm_budget(db: Session, n: int = 1) -> bool:
    """采集 LLM 预算代理（§十-3）：以调用次数计（call_llm 只返回 str，token 数拿不到
    ——改签名是冻结红线，登记取舍）。cap=ingest_llm_budget（0=不限）；模块态跨日
    重置、重启归零。返回 False = 本轮预算已尽，调用方（手写区 Map/Reduce 模板）应
    跳过 LLM 走纯综述降级。"""
    global _LLM_BUDGET_STATE
    cap = get_settings(db).ingest_llm_budget or 0
    if cap <= 0:
        return True
    today = date.today()
    if _LLM_BUDGET_STATE is None or _LLM_BUDGET_STATE[0] != today:
        _LLM_BUDGET_STATE = (today, 0)
    if _LLM_BUDGET_STATE[1] + n > cap:
        return False
    _LLM_BUDGET_STATE = (_LLM_BUDGET_STATE[0], _LLM_BUDGET_STATE[1] + n)
    return True


def refund_llm_budget(n: int = 1) -> None:
    """预扣返还（2026-10-26 提速批 3）：Map 并发化后预算在主线程一次性预扣，
    源 LLM 调用失败的额度还回——预算按实际消耗计，失败重试不白扣（cap=0 不
    限时计数恒零，返还 no-op）。"""
    global _LLM_BUDGET_STATE
    if _LLM_BUDGET_STATE is not None and n > 0:
        _LLM_BUDGET_STATE = (_LLM_BUDGET_STATE[0],
                             max(0, _LLM_BUDGET_STATE[1] - n))


# —— 共用小件（条目启发式 / 预算 / 时间窗） ——————————————————————————————


def _apply_budget(items: list[dict]) -> tuple[list[dict], bool]:
    """预算截断（纯函数）：条数上限 → 单条 body 上限 → 每源正文字符上限，逐级置标记。
    截断前稳定排序：sched_hint 条目在前（防无关笔记挤占上限），同级保持原序——
    排序不改变条目内容，指纹与台账不受影响。"""
    clipped = len(items) > _MAX_ITEMS_PER_SOURCE
    out: list[dict] = []
    total = 0
    for it in sorted(items, key=lambda x: not x.get("sched_hint"))[:_MAX_ITEMS_PER_SOURCE]:
        body = it.get("body") or ""
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "…（截断）"
            clipped = True
        total += len(body)
        if total > _MAX_SOURCE_CHARS:
            clipped = True
            break
        out.append({**it, "body": body})
    return out, clipped


def _extract_items(payload) -> list[dict]:
    """通用启发式抽条目（纯函数）：在工具返回 JSON 里 DFS 找第一个"元素为 dict 的非空
    列表"逐条映射（title/time 按常见键取，body 为整条压缩 JSON）；找不到列表则整体
    作为单条。LLM 拿到的是原始数据本身，启发式只负责形态统一。"""
    raw = _first_dict_list(payload)
    if raw is None:
        raw = [payload] if isinstance(payload, dict) else [{"body": str(payload)}]
    items = []
    for i, d in enumerate(raw):
        if not isinstance(d, dict):
            items.append({"id": str(i), "title": str(d)[:80],
                          "body": str(d), "time": None})
            continue
        title = next((str(d[k]) for k in _TITLE_KEYS if d.get(k)), f"条目 {i + 1}")
        # time 取值：强信号键优先（2026-11-05 真机数据篡改批）：旧序 _TIME_KEYS
        # 把元数据键（createdTime 等）排在 dueDate 之前——同含两键的条目日程时间
        # 被创建时刻顶替（真机「15:00 的日程直落成采集时刻」），只有 createdTime
        # 的纯待办被凭空排期（「无日期待办直落当前时刻」）。元数据键永不作为
        # time；date/time 两通用首键语义明确可作回落（doc 域材料的参考时间同步
        # 收紧——createdTime 满屏都是，当参考时间只是噪声）
        hit = next((k for k in _SCHED_TIME_KEYS if d.get(k) is not None), None) \
            or next((k for k in ("date", "time") if d.get(k) is not None), None)
        prio_raw = next((d[k] for k in _PRIORITY_KEYS
                         if d.get(k) is not None and d.get(k) != ""), None)
        items.append({
            "id": str(d.get("id") or i),
            "title": title[:80],
            "body": json.dumps(d, ensure_ascii=False, default=str),
            "time": _iso(d[hit]) if hit else None,
            "priority": prio_raw,
            # 信号与取值解耦（2026-10-15 修复）：任一强信号键非空即 True——原先只看
            # 第一个命中键，通用键（created_at/modifiedTime…）排在 _SCHED_TIME_KEYS
            # 之前，同含 modifiedTime 与 dueDate 的条目会被遮蔽成 False
            "sched_hint": any(d.get(k) is not None for k in _SCHED_TIME_KEYS),
        })
    return items


def _first_dict_list(payload):
    """DFS 找第一个非空且元素为 dict 的列表（直接命中优先于深层嵌套）。"""
    if isinstance(payload, list):
        return payload if payload and isinstance(payload[0], dict) else None
    if isinstance(payload, dict):
        for value in payload.values():
            found = _first_dict_list(value)
            if found is not None:
                return found
    return None


def _iso(value) -> str | None:
    """常见时间形态 → **本地 naive** ISO 字符串；解析不了返回 None（交给 LLM
    判断，不硬猜）。

    时区统一在出口剥掉（2026-11-05 真机日期丢失批）：滴答 MCP 返回
    `2026-11-05T10:01:35+08:00` 标准带时区形态，旧版原样透传——直映射判定
    `tzinfo is None` 不成立、带时间日程全部降级成无时间待办（日期丢失），
    LLM 路径的 draft 校验同样丢带时区条目。系统契约是 naive 本地时间
    （_within_window 窗口比较早已同款转换），所有源进系统即转本地 naive，
    下游（直映射 / Map 提示词输入 / 窗口过滤）全部一致。桌面单机形态，
    数据源时区与本地一致的合理假设，跨时区源属罕见边缘（登记 tech-debt）。"""
    def _to_local_naive(dt: datetime) -> datetime:
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt

    if isinstance(value, datetime):
        return _to_local_naive(value).isoformat(timespec="seconds")
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return _to_local_naive(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        ).isoformat(timespec="seconds")
    except ValueError:
        return None


def _within_window(items: list[dict], since: datetime) -> list[dict]:
    """可解析时间的条目丢弃窗口外旧条；解析不了的保留（见模块 docstring 时间窗取舍）。"""
    out = []
    for it in items:
        t = it.get("time")
        if t is None:
            out.append(it)
            continue
        try:
            parsed = datetime.fromisoformat(t)
        except ValueError:
            out.append(it)
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        if parsed >= since:
            out.append(it)
    return out


def _json_or_text(result) -> object:
    """工具返回 → JSON 对象（MCP 工具多返回 JSON 文本；LangChain content_and_artifact
    形态取 content 部分）；非 JSON 原样字符串，由 _extract_items 兜底单条目。"""
    if isinstance(result, tuple):  # StructuredTool(response_format="content_and_artifact")
        result = result[0]
    if isinstance(result, list):  # 内容块列表：逐块取文本（TextContent dict/对象）
        blocks = []
        for b in result:
            text = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            blocks.append(str(text) if text is not None else str(b))
        # 每块常为独立 JSON 对象（滴答官方 MCP 实测：一项目/任务一块）——逐块解析，
        # 全部成功则恢复结构（多块 → 列表逐条命中；单块 → 对象本身，让
        # _first_dict_list 继续下钻其内层条目数组，如 {"tasks": [...]} 包装）；
        # 有失败块则拼纯文本走兜底
        parsed = []
        for blk in blocks:
            try:
                parsed.append(json.loads(blk))
            except (ValueError, TypeError):
                parsed = None
                break
        if parsed:
            return parsed[0] if len(parsed) == 1 else parsed
        result = "\n".join(blocks)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def _auth_in_payload(raw) -> str | None:
    """返回体是否认证错误（第三层过滤的主动检测）：服务在线、凭据无效时，错误
    JSON（如 {"code":99991668,"msg":"Invalid app_access_token"}）不抛异常，
    _extract_items 会把无条目数组的 dict 兜底成伪条目（ok=True 假象）——握手/
    探测发现不了，必须在条目抽取前识别。只认 dict 错误结构（顶层含 code/error/
    msg/message 之一）：纯文本与单对象数据宁漏不误杀；业务包装（有条目数组）由
    调用方 _first_dict_list 前置排除。命中的是错误码/描述词，不含凭据本体（与
    _describe_error 不读 str(e) 的红线口径一致）。返回打标错误文案或 None。"""
    if not isinstance(raw, dict) or not ({"code", "error", "msg", "message"} & raw.keys()):
        return None
    text = json.dumps(raw, ensure_ascii=False, default=str)
    hit = next((m for m in _AUTH_ERROR_MARKERS if m in text.lower()), None)
    if hit is None:
        return None
    return f"[认证] 服务在线但凭据无效（{hit}）：请到数据接入重新配置该源的 Key / 授权"
