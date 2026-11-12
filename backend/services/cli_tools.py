"""CLI 工具接入（第四源 kind="cli"，2026-10-15 增）：官方 CLI 适配器注册表 + 通用安全执行器。

背景：飞书 lark-cli / 钉钉 dws / Todoist td 等官方 CLI 能力强于对应 MCP，业界正
从 MCP 迁往 CLI。与 MCP stdio 同为子进程形态，但无会话协议——每命令一次性进程，
stdout JSON + 退出码即契约，无握手无池化。凭据恒存 CLI 自己的 OS 钥匙串（由 CLI
的 auth 命令管理），本模块零凭据——与 Composio/MCP 的本质差异。

精选知识全部集中在模块级 ADAPTERS 声明式注册表（与 MCP 精选模板放前端的差异是
有意的：命令声明必须与执行器同源，放前端会漂移）。新增一个精选 CLI = 加一条
spec；采集命令具体 flags 按项目铁律待真机实测后填入（同 data_ingest.
_MCP_FETCH_TOOLS 钉钉 None 先例），未填走 note 清单路径不阻塞。

执行器红线（run_cli 是所有 CLI 子进程的唯一出口）：
- argv 数组 + 先 discover_bin 解析绝对路径（Windows subprocess 不解析 .cmd，
  直接 spawn "lark" 会 WinError 2）；绝无 shell=True、绝无命令字符串拼接
- 超时树杀：Popen + communicate(timeout)，超时后 Windows 走 taskkill /F /T /PID
  ——npm .cmd shim 下真实的 node 孙进程不随 shim 被杀，会常驻泄漏
- 输出按 spec 编码解码（个别工具 GBK）+ 2MB 钳制；错误摘要 ≤200 字符不全量回显
  stderr（CLI 报错可能含 token 片段，同 mcp_servers._describe_error 防泄露口径）

窄口自定义行（adapter_id "custom:<uuid>"）：仅一条固定只读 argv（custom_bin +
custom_argv），占位仅 {since}/{until}；保存与执行双重过 data_ingest.
_TOOL_DENIED_MARKERS（自定义行全静态 token 无用户值，逐项全查）；readonly 白名单
恒空 = 不进补采。_TOOL_DENIED_MARKERS 惰性导入（data_ingest 顶层 import 本模块，
反向顶层导入会成环）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CliTool

logger = logging.getLogger(__name__)

# source_key 前缀（data_ingest 枚举与 declared_actions 共用口径）
SOURCE_PREFIX = "cli:"

_VERSION_TIMEOUT_S = 10.0   # 版本探测（本地命令，快）
_AUTH_TIMEOUT_S = 15.0      # 授权状态探测（本地命令，但个别 CLI 会打网络）
_AUTH_START_TIMEOUT_S = 30.0  # 授权启动（--no-wait 形态打印 URL 即退，留足冷启动）
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # stdout 钳制（超限截断置标记，防失控输出撑爆内存）

# {arg_<名>} 占位（补采参数灌入；自定义行禁用——无白名单无值可灌）
_ARG_PLACEHOLDER_RE = re.compile(r"\{arg_\w+\}")


@dataclass(frozen=True)
class CliCommand:
    """一条 CLI 命令声明。argv 是模板：{since}/{until} 时间窗占位（采集灌）、
    {arg_<名>} 补采参数占位（call_readonly_tool 灌）。timeout_s 按命令可调——
    npx 类冷启动命令可声明 90s。ok_contains：退出码不足以判定成功时的 stdout
    锚点子串（实测 lark-cli 参数错误也 rc=0、错误体在 body——纯退出码会把
    「unknown flag」误判成已授权）；可声明多个锚点——任一命中即通过（lark
    tokenStatus valid/needs_refresh 都算已授权：待刷新 = refresh token 仍在、
    CLI 真实调用时自动刷；None = 只看退出码）。"""

    name: str
    description: str
    argv: tuple[str, ...]
    timeout_s: float = 60.0
    ok_contains: str | tuple[str, ...] | None = None
    # 条目域（2026-10-24 高可信直通）：本命令拉取的数据域 todo/calendar——采集
    # 时随条目写进 ingestion_items.domain，高可信源（ingest_sources.trusted）的
    # todo/calendar 域条目免确认直落。None = 未声明（自定义行）恒不直通
    domain: str | None = None


@dataclass(frozen=True)
class CliAdapter:
    """精选 CLI 适配器声明。launchers 是 (bin 名, 启动器前缀参数) 候选链——同一
    CLI 可能有全局安装（lark → 无前缀）与 npx 兜底（npx -y @larksuite/cli）两条
    启动路径。auth_start=None 且 manual_auth_command/auth_check 也无 → auth_mode
    "none"（授权完全由 CLI 侧自理，直接接入）。"""

    adapter_id: str
    name: str
    description: str
    launchers: tuple[tuple[str, tuple[str, ...]], ...]
    version_argv: tuple[str, ...] = ("--version",)
    auth_check: CliCommand | None = None
    auth_start: CliCommand | None = None
    auth_url_re: str | None = None          # 从 auth_start 的 stdout 提取授权 URL
    manual_auth_command: str | None = None  # manual 档给用户复制的终端命令
    # 静态采集主命令（一源可多域各一条——lark 实测：日历 +agenda 与任务
    # +get-my-tasks 互不覆盖，逐条执行合并）；空 → note 清单路径
    fetches: tuple[CliCommand, ...] = ()
    readonly: tuple[CliCommand, ...] = ()   # 补采白名单（call_readonly_tool 硬闸之一）
    encoding: str = "utf-8"                 # 个别 CLI 输出 GBK，在此声明
    install_url: str | None = None          # 官方文档外链（安装命令不内嵌——可能
    # 变化，2026-10-16 用户定案以官方文档为准，install_command 字段全链路移除）


# —— 精选注册表 ————————————————————————————————————————————————————————————
# lark 2026-10-16 真机实测回填（v1.0.98）：npm 全局 bin 名是 lark-cli（无 lark，
# PATH 只认 lark-cli）；auth 子命令 list/status 输出即 JSON、无 --format flag
# （有 --json 简写），且参数错误也 rc=0 错误体在 body → ok_contains body 锚点；
# 官方安装器 npx @larksuite/cli@latest install（落入 %APPDATA%\npm；安装命令
# 2026-10-16 起不再内嵌展示——命令可能变化，以官方文档 install_url 为准）。fetch 两域
# 各一条：日历 +agenda --start/--end（ISO 8601）+ 任务 +get-my-tasks（状态式
# 全量未完成，靠台账指纹跨天去重，不按创建时间滤——拖欠待办正是价值）。
# dingtalk / todoist 待真机实测（docs/ai-ingest.md §三登记），骨架就位填入即生效。

ADAPTERS: dict[str, CliAdapter] = {
    "lark": CliAdapter(
        adapter_id="lark",
        name="飞书 (lark-cli)",
        description="飞书官方 CLI：日历 / 任务 / 消息 / 文档全量直连",
        launchers=(("lark-cli", ()), ("npx", ("-y", "@larksuite/cli"))),
        auth_check=CliCommand(
            "auth list", "检查授权状态", ("auth", "list"), 30.0,
            # 真机实测 tokenStatus 两值都算已授权：valid 直接可用；needs_refresh
            # = access token 过期待刷（refresh token 仍在，CLI 真实调用时自动刷）
            ok_contains=('"tokenStatus": "valid"', '"tokenStatus": "needs_refresh"')),
        auth_start=CliCommand(
            "auth login", "发起设备流授权（打印授权链接后退出）",
            ("auth", "login", "--no-wait", "--json"), _AUTH_START_TIMEOUT_S),
        auth_url_re=r"https?://\S+",
        fetches=(
            CliCommand("calendar +agenda", "拉时间窗内日历日程",
                       ("calendar", "+agenda", "--start", "{since}", "--end", "{until}"), 90.0,
                       domain="calendar"),
            CliCommand("task +get-my-tasks", "拉当前未完成待办",
                       ("task", "+get-my-tasks", "--complete=false", "--page-limit", "20"), 90.0,
                       domain="todo"),
        ),
        readonly=(
            CliCommand("calendar +search-event", "按关键词搜日历日程",
                       ("calendar", "+search-event", "--query", "{arg_query}", "--page-size", "20")),
            CliCommand("task +search", "按关键词搜待办任务",
                       ("task", "+search", "--query", "{arg_query}", "--page-limit", "20")),
        ),
        install_url="https://github.com/larksuite/cli",
    ),
    "dingtalk": CliAdapter(
        adapter_id="dingtalk",
        name="钉钉 (dws CLI)",
        description="钉钉官方 CLI：日历 / 待办 / DING 消息等（需企业管理员授权）",
        launchers=(("dws", ()),),
        # 授权方式待实测：dws 是否支持 headless 授权未确认，暂 none 档（终端自理）
        install_url="https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli",
    ),
    "todoist": CliAdapter(
        adapter_id="todoist",
        name="Todoist (td CLI)",
        description="Todoist 官方 CLI：待办 / 项目",
        launchers=(("td", ()),),
        install_url="https://www.todoist.com/cli",
    ),
}


# —— 通用执行器 ————————————————————————————————————————————————————————————


def _env_override(adapter_id: str) -> str | None:
    """CLARA_CLI_BIN_<ID 大写> 环境变量：非标准安装位/测试替身的终极指路口。"""
    return os.environ.get(f"CLARA_CLI_BIN_{adapter_id.upper()}") or None


def discover_bin(spec: CliAdapter) -> tuple[str, tuple[str, ...]] | None:
    """发现启动器，返回 (bin 绝对路径, 前缀参数)；找不到返回 None，永不抛。

    顺序：env 覆盖（绝对路径直接采信，配首条 launcher 的前缀）→ 逐 launcher
    shutil.which（Windows 上 which 含 PATHEXT 的 .cmd/.exe 解析——subprocess 自己
    不解析 .cmd，这里不解析 spawn 就 WinError 2）→ %APPDATA%\\npm 显式兜底
    （PyInstaller sidecar 从 GUI 继承的 PATH 可能不含 npm 全局 bin）。执行时每次
    重探（which 级廉价操作，环境会变）。"""
    override = _env_override(spec.adapter_id)
    if override and Path(override).is_file():
        return (override, spec.launchers[0][1] if spec.launchers else ())
    for bin_name, prefix in spec.launchers:
        found = shutil.which(bin_name)
        if found:
            return (found, prefix)
    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_dir = Path(appdata) / "npm"
        for bin_name, prefix in spec.launchers:
            for ext in (".cmd", ".exe", ".ps1", ""):
                cand = npm_dir / f"{bin_name}{ext}"
                if cand.is_file():
                    return (str(cand), prefix)
    return None


def render_argv(
    argv_tpl: tuple[str, ...] | list[str],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    arguments: dict | None = None,
) -> list[str] | None:
    """占位渲染：{since}/{until} 填 ISO 本地时间，{arg_<名>} 从 arguments 取值
    （str() 落 argv 元素位，无 shell 通道）。token 内联形态（--from={since}）同样
    支持。残留未知占位或花括号 → 返回 None——拒绝半渲染命令跑出去。"""
    out: list[str] = []
    for tok in argv_tpl:
        rendered = tok
        if since is not None:
            rendered = rendered.replace("{since}", since.isoformat())
        if until is not None:
            rendered = rendered.replace("{until}", until.isoformat())
        m = _ARG_PLACEHOLDER_RE.search(rendered)
        if m:
            key = m.group(0)[1:-1][len("arg_"):]  # "{arg_k}" → "k"
            if not isinstance(arguments, dict) or key not in arguments:
                return None
            rendered = rendered.replace(m.group(0), str(arguments[key]))
        if "{" in rendered or "}" in rendered:
            return None
        out.append(rendered)
    return out


def _kill_tree(proc: subprocess.Popen) -> None:
    """树杀：Windows 先 taskkill /F /T 整树终止（npm .cmd shim 的 node 孙进程不随
    shim 死），POSIX 退化为 kill()；随后 communicate 收尸（最多再等 5s）。"""
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
    except OSError:
        pass  # 收尸失败登记不了什么——进程已注定终止或已死
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def run_cli(spec: CliAdapter, argv: list[str], timeout_s: float) -> tuple[bool, str, str]:
    """执行单条 CLI 命令（同步、永不抛、失败是数据）：返回 (ok, stdout, 错误摘要)。

    cwd 固定临时目录——打包形态下 sidecar 的 cwd 可能是只读的安装目录，个别 CLI
    会往 cwd 落缓存文件。错误摘要取 stderr 前 200 字符（防凭据片段泄露），退出码
    非零即失败；stdout 超 2MB 截断置标记。"""
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creation, cwd=tempfile.gettempdir(),
        )
    except OSError as e:
        return (False, "", f"无法启动 CLI 进程：{e.strerror or e}")
    try:
        out_b, err_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return (False, "", f"命令超时（>{int(timeout_s)} 秒），已终止")
    stdout = out_b[:_MAX_OUTPUT_BYTES].decode(spec.encoding, errors="replace")
    if len(out_b) > _MAX_OUTPUT_BYTES:
        stdout += "\n…（输出超 2MB 已截断）"
    if proc.returncode != 0:
        err = err_b[:200].decode(spec.encoding, errors="replace").strip()
        return (False, stdout, err or f"退出码 {proc.returncode}")
    return (True, stdout, "")


async def arun_cli(spec: CliAdapter, argv: list[str], timeout_s: float) -> tuple[bool, str, str]:
    """run_cli 的异步壳（采集 gather 协程用；同 _composio_job 包 to_thread 先例）。"""
    import asyncio

    return await asyncio.to_thread(run_cli, spec, argv, timeout_s)


def static_tokens_denied(argv_tpl: tuple[str, ...] | list[str]) -> bool:
    """静态 token 含 deny-list 特征词 → True（只读判定的纵深防御）。

    只查声明侧静态 token（{since}/{until}/{arg_*} 占位跳过），不查占位替换进来的
    用户值——用户数据（邮件标题含 "delete"）是内容不是动作。精选适配器声明笔误
    与自定义窄口命令共用此闸。"""
    from services.data_ingest import _TOOL_DENIED_MARKERS  # 惰性导入防环

    for tok in argv_tpl:
        if tok in ("{since}", "{until}") or _ARG_PLACEHOLDER_RE.search(tok):
            continue
        low = tok.lower()
        if any(marker in low for marker in _TOOL_DENIED_MARKERS):
            return True
    return False


def _anchor_hit(anchor: str | tuple[str, ...] | None, body: str) -> bool:
    """ok_contains 锚点判定（probe / auth_status 共用口径）：str 单锚点 /
    tuple 任一命中 / None 只看退出码。"""
    if anchor is None:
        return True
    anchors = anchor if isinstance(anchor, tuple) else (anchor,)
    return any(a in body for a in anchors)


# —— 行 → spec 解析与声明清单 ———————————————————————————————————————————


def spec_for(row: CliTool) -> CliAdapter | None:
    """行的执行 spec：精选行查注册表；自定义行运行时构造伪 spec（readonly=()
    恒拒补采、无授权概念）。查无且非自定义行 = 脏行，返回 None。"""
    spec = ADAPTERS.get(row.adapter_id)
    if spec is not None:
        return spec
    if row.adapter_id.startswith("custom:") and row.custom_bin and row.custom_argv:
        return CliAdapter(
            adapter_id=row.adapter_id,
            name=row.name or "自定义只读命令",
            description="用户登记的固定只读采集命令",
            launchers=((row.custom_bin, ()),),
            fetches=(CliCommand("fetch", "固定只读采集命令", tuple(row.custom_argv)),),
            readonly=(),  # 白名单恒空：自定义行不进补采
        )
    return None


def declared_actions(source_key: str) -> list[dict]:
    """源的只读命令清单（[{name, description}]，零子进程零网络）——
    list_ingest_sources 的 available_tools 与补采 list_source_actions 共用。
    自定义行恒空清单（无白名单）。"""
    if not source_key.startswith(SOURCE_PREFIX):
        return []
    adapter_id = source_key[len(SOURCE_PREFIX):]
    spec = ADAPTERS.get(adapter_id)
    if spec is None:
        return []
    cmds = spec.fetches + spec.readonly
    return [{"name": c.name, "description": c.description} for c in cmds]


# —— 探测 / 授权 / 接入管理 —————————————————————————————————————————————


def _get_row(db: Session, adapter_id: str) -> CliTool | None:
    return db.get(CliTool, adapter_id)


def _spec_or_raise(adapter_id: str) -> CliAdapter:
    spec = ADAPTERS.get(adapter_id)
    if spec is None:
        raise ValueError(f"未知适配器：{adapter_id}")
    return spec


def probe(db: Session, adapter_id: str) -> dict:
    """安装 + 授权两段探测（失败是数据不抛；已接入行结果落库，未接入行纯返回——
    精选卡「检测」不建行，防探测即接入混进采集）。返回 CliProbeOut 形状。"""
    spec = _spec_or_raise(adapter_id)
    found = discover_bin(spec)
    bin_ok, version, err = False, None, None
    if found is None:
        err = ("未找到可执行文件（安装后重试；非标准安装位可用环境变量 "
               f"CLARA_CLI_BIN_{adapter_id.upper()} 指路）")
    else:
        ok, out, e = run_cli(spec, [found[0], *found[1], *spec.version_argv], _VERSION_TIMEOUT_S)
        bin_ok, err = ok, None if ok else f"版本探测失败：{e}"
        if ok:
            version = out.strip().splitlines()[0][:50] if out.strip() else None
    auth_ok: bool | None = None
    if bin_ok and spec.auth_check is not None:
        ok, out, e = run_cli(spec, [found[0], *found[1], *spec.auth_check.argv], spec.auth_check.timeout_s)
        # 退出码 + ok_contains body 锚点双重判定（lark-cli 实测：参数错误也 rc=0、
        # 错误体在 body，纯退出码会把「unknown flag」误判成已授权）
        auth_ok = ok and _anchor_hit(spec.auth_check.ok_contains, out)
        if not auth_ok:
            err = err or f"授权探测失败：{e or '输出缺少有效凭据标记'}"
    row = _get_row(db, adapter_id)
    if row is not None:
        row.bin_ok, row.bin_version, row.bin_path = bin_ok, version, found[0] if found else None
        row.bin_at = datetime.now()
        row.auth_ok = auth_ok
        if auth_ok is not None:
            row.auth_at = datetime.now()
        row.last_error = err
        db.commit()
        db.refresh(row)
    if bin_ok and auth_ok is not False:
        _reset_health(db, adapter_id)  # 检测通过即重置：与返回值 ok 同口径
    logger.info("CLI 探测 %s：bin=%s auth=%s", adapter_id, bin_ok, auth_ok)
    return {
        "ok": bin_ok and auth_ok is not False, "bin_ok": bin_ok, "bin_version": version,
        "bin_path": found[0] if found else None, "auth_ok": auth_ok, "error": err,
    }


def start_auth(db: Session, adapter_id: str) -> dict:
    """发起授权（CliAuthStartOut 形状）：auto 档 spawn auth_start 捕获 stdout 中的
    授权 URL（前端开系统浏览器 + 轮询 auth/status）；manual 档返回可复制终端命令。"""
    spec = _spec_or_raise(adapter_id)
    if spec.auth_start is None:
        if spec.manual_auth_command:
            return {"ok": False, "url": None, "manual_command": spec.manual_auth_command, "error": None}
        return {"ok": False, "url": None, "manual_command": None,
                "error": "该 CLI 暂不支持程序化授权（授权命令待实测），请在终端自行登录后点「检测」"}
    found = discover_bin(spec)
    if found is None:
        return {"ok": False, "url": None, "manual_command": None, "error": "未找到可执行文件，请先安装并检测"}
    ok, out, err = run_cli(spec, [found[0], *found[1], *spec.auth_start.argv], spec.auth_start.timeout_s)
    if not ok:
        return {"ok": False, "url": None, "manual_command": None, "error": f"授权启动失败：{err}"}
    m = re.search(spec.auth_url_re or r"https?://\S+", out)
    if m is None:
        return {"ok": False, "url": None, "manual_command": None, "error": "未能从输出中识别授权链接（命令形态待实测）"}
    return {"ok": True, "url": m.group(0), "manual_command": None, "error": None}


def auth_status(db: Session, adapter_id: str) -> dict:
    """授权状态轮询（CliAuthStatusOut 形状）；已接入行落库 auth_ok/auth_at。"""
    spec = _spec_or_raise(adapter_id)
    if spec.auth_check is None:
        return {"authenticated": True, "error": None}  # 无程序化探测 = 不设闸，
        # 接入与采集时 CLI 自身的报错会经认证标记路径暴露
    found = discover_bin(spec)
    if found is None:
        return {"authenticated": False, "error": "未找到可执行文件"}
    ok, out, err = run_cli(spec, [found[0], *found[1], *spec.auth_check.argv], spec.auth_check.timeout_s)
    # 与 probe 同口径：body 锚点（多锚点任一命中）双重判定
    ok = ok and _anchor_hit(spec.auth_check.ok_contains, out)
    row = _get_row(db, adapter_id)
    if row is not None:
        row.auth_ok, row.auth_at, row.last_error = ok, datetime.now(), None if ok else f"授权探测失败：{err}"
        db.commit()
    if ok:
        _reset_health(db, adapter_id)  # 重新授权完成即重置（授权过期曾记连败）
    return {"authenticated": ok, "error": None if ok else err}


def connect(db: Session, adapter_id: str) -> CliTool:
    """显式接入（授权完成后的「接入」按钮）：可执行文件不存在 → ValueError（路由
    转 422 引导先安装检测）。行 get_or_create 后 enabled=True，并镜像
    ingest_sources——接入即参与采集。建行即落库探测快照（复用 probe 的落库段）：
    接入前提是检测通过，行上不记这个事实的话卡徽标会落到「未检测」态。"""
    spec = _spec_or_raise(adapter_id)
    if discover_bin(spec) is None:
        raise ValueError("未检测到可执行文件，请先安装并点「检测」")
    row = _get_row(db, adapter_id)
    if row is None:
        row = CliTool(adapter_id=adapter_id)
        db.add(row)
    row.enabled = True
    db.commit()
    probe(db, adapter_id)  # 探测列快照（行已存在，probe 内部落库 + commit）
    db.refresh(row)
    # 高可信默认值策略（2026-10-24，用户方案二白名单收编）：任务型精选工具接入
    # 即默认开直通（纯任务域、无笔记噪音风险）；混合域工具（lark=日历+任务+文档）
    # 默认关，用户按需在数据接入卡开启
    from services.data_ingest import DEFAULT_TRUSTED_CLI  # 惰性导入防环

    _mirror(db, adapter_id, True, trusted=adapter_id in DEFAULT_TRUSTED_CLI)
    _reset_health(db, adapter_id)  # 接入即重置：残留行的旧连败不拦新接入
    logger.info("CLI 接入：%s", adapter_id)
    return row


def update_tool(db: Session, adapter_id: str, body) -> CliTool | None:
    """行更新（卡片「停用/启用」+ 自定义行「配置」编辑）：行不存在返回 None（路由
    转 404）。编辑三闸复刻 create_custom（argv 非空 / 禁 {arg_*} / deny-list）；编辑
    须三字段全量同给（表单编辑态发全量），精选行拒绝编辑（显示名与命令以注册表
    声明为准）。编辑后清测试结果——旧结果对旧命令成立，是谎言（同本地文件夹
    目录变更清 test_ok 先例）。启停成功联动镜像 ingest_sources。"""
    row = _get_row(db, adapter_id)
    if row is None:
        return None
    if body.name is not None or body.bin_path is not None or body.argv is not None:
        if not adapter_id.startswith("custom:"):
            raise ValueError("精选行不支持编辑（显示名与命令以注册表声明为准）")
        if body.name is None or body.bin_path is None or body.argv is None:
            raise ValueError("编辑自定义命令须同时提供显示名称、可执行文件路径与命令参数")
        argv = [a for a in body.argv if a]
        if not argv:
            raise ValueError("命令参数不能为空")
        if any(_ARG_PLACEHOLDER_RE.search(tok) for tok in argv):
            raise ValueError("自定义命令不支持 {arg_*} 参数占位（仅 {since}/{until} 时间窗占位）")
        if static_tokens_denied(argv):
            raise ValueError("命令参数含写操作特征词（create/send/delete 等），只接受只读命令")
        row.name, row.custom_bin, row.custom_argv = body.name, body.bin_path, argv
        row.test_ok = row.test_items = row.test_at = None
        row.last_error = None
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    db.refresh(row)
    if body.enabled is not None:
        _mirror(db, adapter_id, body.enabled)
    _reset_health(db, adapter_id)  # 任何写入（编辑/启停）即重置连败证据
    logger.info("CLI 行更新：%s enabled=%s edited=%s",
                adapter_id, body.enabled, body.name is not None)
    return row


def _run_custom_test(bin_path: str, argv: list[str]) -> dict:
    """自定义命令测试的执行核心（行态与表单态共用）：构造伪 spec → 发现可执行
    文件 → 渲染近 24h 时间窗 → run_cli 计时 → stdout 过 _extract_items 数条目
    （复用采集整形管道，零 CLI 专用解析；不过时间窗——报原始返回数防"有数据"
    被砍成"0 条"误判）。失败是数据不抛。"""
    from datetime import timedelta  # 函数内引用，保持头部导入只留常用品
    from services.data_ingest import _extract_items, _json_or_text  # 惰性导入防环

    def _fail(error: str, latency_ms: int | None = None) -> dict:
        return {"ok": False, "latency_ms": latency_ms, "items_count": None, "error": error}

    now = datetime.now()
    spec = CliAdapter(
        adapter_id="custom:test", name="自定义只读命令", description="",
        launchers=((bin_path, ()),),
        fetches=(CliCommand("fetch", "固定只读采集命令", tuple(argv)),),
        readonly=(),
    )
    found = discover_bin(spec)
    if found is None:
        return _fail("未找到可执行文件（路径无效或文件不存在）")
    rendered = render_argv(argv, since=now - timedelta(hours=24), until=now)
    if rendered is None:
        return _fail("命令参数渲染失败（占位符笔误，仅支持 {since}/{until}）")
    t0 = time.monotonic()
    ok, out, err = run_cli(spec, [found[0], *found[1], *rendered], spec.fetches[0].timeout_s)
    latency_ms = int((time.monotonic() - t0) * 1000)
    if not ok:
        return _fail(f"命令执行失败：{err}" if err else "命令执行失败", latency_ms)
    return {"ok": True, "latency_ms": latency_ms,
            "items_count": len(_extract_items(_json_or_text(out))), "error": None}


def test_custom(db: Session, adapter_id: str) -> dict | None:
    """自定义行「测试」按钮（CliCustomTestOut 形状）：跑 _run_custom_test 后结果
    落库 test_ok/test_items/test_at + last_error（失败摘要），供 GET /tools 徽标
    展示；不进采集台账（测试是探测不是采集）。行不存在返回 None（404）；精选行
    ValueError（路由转 422——精选卡「测试」走 probe 端点）。"""
    row = _get_row(db, adapter_id)
    if row is None:
        return None
    if not adapter_id.startswith("custom:") or not row.custom_bin or not row.custom_argv:
        raise ValueError("精选行请走安装与授权探测（probe）")
    result = _run_custom_test(row.custom_bin, list(row.custom_argv))
    row.test_ok = result["ok"]
    row.test_items = result["items_count"]
    row.test_at = datetime.now()
    row.last_error = result["error"]
    db.commit()
    if result["ok"]:
        _reset_health(db, adapter_id)  # 测试通过即重置连败冷却
    logger.info("CLI 自定义测试 %s：ok=%s items=%s", adapter_id, result["ok"], result["items_count"])
    return result


def test_custom_body(body) -> dict:
    """表单态测试（POST /tools/custom/test，「保存前先试」）：不建行不落库，用
    表单当前值直接跑——校验三闸与 create_custom 同源（argv 非空 / 禁 {arg_*} /
    deny-list），422 与保存口径一致（能保存的命令必能测试，能测试的未必过保存
    闸——name/bin_path 长度仍由 schema 层拦）。"""
    argv = [a for a in body.argv if a]
    if not argv:
        raise ValueError("命令参数不能为空")
    if any(_ARG_PLACEHOLDER_RE.search(tok) for tok in argv):
        raise ValueError("自定义命令不支持 {arg_*} 参数占位（仅 {since}/{until} 时间窗占位）")
    if static_tokens_denied(argv):
        raise ValueError("命令参数含写操作特征词（create/send/delete 等），只接受只读命令")
    return _run_custom_test(body.bin_path, argv)


def delete(db: Session, adapter_id: str) -> CliTool | None:
    """移除接入（204）：只删本表行，不清 CLI 自己的 keychain 凭据——本机重新接入
    免再授权（特性非缺陷）。ingest_sources 残留行无害（同 McpServer 口径）。"""
    row = _get_row(db, adapter_id)
    if row is None:
        return None
    db.delete(row)
    db.commit()
    logger.info("CLI 移除：%s（CLI 侧凭据保留）", adapter_id)
    return row


def create_custom(db: Session, body) -> CliTool:
    """窄口自定义只读命令（ValueError → 路由 422）：argv 全静态 token，占位仅
    {since}/{until}；保存闸 = deny-list 特征词逐项全查（自定义行无用户值，全查
    不误杀）+ 禁 {arg_*} 占位。执行时 static_tokens_denied 二次过闸（纵深防御）。"""
    argv = [a for a in body.argv if a]
    if not argv:
        raise ValueError("命令参数不能为空")
    if any(_ARG_PLACEHOLDER_RE.search(tok) for tok in argv):
        raise ValueError("自定义命令不支持 {arg_*} 参数占位（仅 {since}/{until} 时间窗占位）")
    if static_tokens_denied(argv):
        raise ValueError("命令参数含写操作特征词（create/send/delete 等），只接受只读命令")
    adapter_id = f"custom:{uuid.uuid4()}"
    row = CliTool(
        adapter_id=adapter_id, name=body.name,
        custom_bin=body.bin_path, custom_argv=argv, enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _mirror(db, adapter_id, True)
    logger.info("CLI 自定义接入：%s → %s %s", body.name, body.bin_path, argv)
    return row


def _mirror(db: Session, adapter_id: str, enabled: bool, *, trusted: bool | None = None) -> None:
    """启停联动镜像 ingest_sources（同 mcp_servers.update_server 调 mirror 口径，
    防存量 enabled=False 行在重接入后把采集闸卡死）。trusted 仅建行时生效
    （高可信默认值策略，见 DEFAULT_TRUSTED_CLI 消费点——存量行不动：重接入
    不重置用户关掉的开关）。"""
    from services.data_ingest import mirror_source_enabled  # 惰性导入防环

    mirror_source_enabled(db, f"{SOURCE_PREFIX}{adapter_id}", enabled, trusted=trusted)


def _reset_health(db: Session, adapter_id: str) -> None:
    """用户介入即重置连败冷却（配置写入/验证成功时调用，2026-10-16 起——旧失败
    是对旧配置的记录；同 _mirror 惰性导入防环）。"""
    from services.data_ingest import reset_source_health

    reset_source_health(db, f"{SOURCE_PREFIX}{adapter_id}")


def list_tools(db: Session) -> list[dict]:
    """GET /api/cli/tools 数据源：注册表全量（含未接入，供精选卡）+ DB 行合并 +
    自定义行。零子进程零网络——探测态只读上次落库结果，刷新靠显式 probe。"""
    out: list[dict] = []
    rows = {r.adapter_id: r for r in db.scalars(select(CliTool)).all()}
    for adapter_id, spec in ADAPTERS.items():
        row = rows.get(adapter_id)
        if spec.auth_start is not None:
            auth_mode = "auto"
        elif spec.manual_auth_command or spec.auth_check is not None:
            auth_mode = "manual"
        else:
            auth_mode = "none"
        out.append({
            "adapter_id": adapter_id, "name": spec.name, "description": spec.description,
            "custom": False,
            "install_url": spec.install_url,
            "auth_mode": auth_mode,
            "connected": row is not None,
            "enabled": row.enabled if row else False,
            "bin_ok": row.bin_ok if row else None,
            "bin_version": row.bin_version if row else None,
            "bin_path": row.bin_path if row else None,
            "bin_at": row.bin_at if row else None,
            "auth_ok": row.auth_ok if row else None,
            "auth_at": row.auth_at if row else None,
            "test_ok": None, "test_items": None, "custom_argv": None,  # 精选行无「测试」概念（走 probe）
            "last_error": row.last_error if row else None,
            "fetch_ready": bool(spec.fetches),
            "actions": declared_actions(f"{SOURCE_PREFIX}{adapter_id}"),
        })
    for adapter_id, row in rows.items():
        if adapter_id in ADAPTERS or not adapter_id.startswith("custom:"):
            continue  # 精选已输出；注册表查无的非自定义行 = 脏行不展示
        out.append({
            "adapter_id": adapter_id, "name": row.name or "自定义只读命令",
            "description": "用户登记的固定只读采集命令",
            "custom": True, "install_url": None,
            "auth_mode": "none",
            "connected": True, "enabled": row.enabled,
            "bin_ok": row.bin_ok, "bin_version": row.bin_version,
            "bin_path": row.custom_bin, "bin_at": row.bin_at,
            "auth_ok": None, "auth_at": None,
            "test_ok": row.test_ok, "test_items": row.test_items,
            "custom_argv": row.custom_argv, "last_error": row.last_error,
            "fetch_ready": bool(row.custom_argv),
            "actions": [],
        })
    return out
