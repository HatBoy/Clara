"""内置本地文件夹 MCP 服务端（2026-09-18 增，同日扩多目录）：数据接入页
"本地文件夹"卡的后端。

一个模块 owning 整个特性：IntegrationSettings 读写 + FastMCP 实例 + 工具纯函数 +
挂载路径知识单一来源。技术要点（详见 docs 与 MCP 接入计划）：

- FastMCP 的 streamable_http_app() 返回 Starlette app，session_manager 惰性建于
  首次调用——mcp_http_app() 缓存该次调用，main.py 在模块导入期 mount；
  StreamableHTTPSessionManager.run() 不可重入，故挂载恒存在（不随开关启停），
  启用判定在工具调用层：未启用时工具返回中文错误文本（LLM 可读数据），不抛。
- markitdown 延迟 import + 单例（Magika 识别模型加载不便宜；测试 monkeypatch 点）。
- 安全钳制：read 路径 resolve 后必须落在某个已配置根内（防 ../ 与任意绝对路径
  注入）、单文件 ≤10MB、list 递归 ≤500 文件（超限截断并标记）。
- 多目录（2026-09-18 扩）：配置为根路径列表（≤10 项）。list_files 返回的文件
  path 用根的完整绝对路径（POSIX 形式）作前缀——用根目录名做前缀会在两个根
  同名时冲突致 read_file 歧义，绝对路径天然零歧义；read_file 收完整路径，
  resolve 后按 is_relative_to 判定归属某个根。
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from sqlalchemy.orm import Session

import config
from database import SessionLocal
from models import IntegrationSettings
from schemas import LocalFolderOut, LocalFolderTestOut, LocalFolderUpdate

# 挂载点（main.py app.mount 用）；最终工具端点 = 挂载点 + FastMCP 内部默认路径
# "/mcp" = http://127.0.0.1:{PORT}/mcp/local/mcp（双 /mcp 是 FastMCP 默认内部路径，已知无害）
MOUNT_PATH = "/mcp/local"

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 单文件读取上限 10MB
_MAX_FILES = 500  # list_files 递归上限（超限截断；多根共享同一预算）
_MAX_PATHS = 10  # 目录数上限（契约层同步拦）

# 进程内 MCP 服务端实例（stateless_http：每请求独立会话，无状态最轻量）
mcp = FastMCP("clara-local-folder", stateless_http=True)

_mcp_app = None  # streamable_http_app() 的缓存（惰性 session_manager 建于首次调用）


def mcp_http_app():
    """返回可挂载的 Starlette app，只调一次 streamable_http_app() 并缓存。

    必须先于任何 session_manager 访问调用本函数（session_manager 惰性创建于
    streamable_http_app 内部，main.py 的 mount 在模块导入期先行满足此序）。
    """
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = mcp.streamable_http_app()
    return _mcp_app


def local_folder_mcp_url() -> str:
    """本地文件夹服务的 MCP 端点（阶段二 agent 构造连接用；端口与 uvicorn 绑定同源）。"""
    return f"http://127.0.0.1:{config.PORT}{MOUNT_PATH}/mcp"


_md_instance = None  # MarkItDown 单例（延迟创建）


def _markitdown_factory():
    """延迟 import + 单例工厂（测试 monkeypatch 点；Magika 模型加载不便宜）。"""
    global _md_instance
    if _md_instance is None:
        from markitdown import MarkItDown

        _md_instance = MarkItDown()
    return _md_instance


# ---- 配置读写（IntegrationSettings 加列；出口显式构造防 from_attributes 静默 None）----


def _row(db: Session) -> IntegrationSettings:
    """复用 integrations 的单行 get_or_create（id 恒 1）。"""
    from services.integrations import get_key_settings

    return get_key_settings(db)


def get_local_folder_state(db: Session) -> tuple[list[str], bool]:
    """读当前配置：返回 (paths, enabled)。paths 空列表 = 未配置。"""
    r = _row(db)
    return list(r.local_folder_paths or []), bool(r.local_folder_enabled)


def get_local_folder(db: Session) -> LocalFolderOut:
    """读完整配置（含持久化的上次测试结果）——GET 端点出口（_out 显式构造）。"""
    return _out(_row(db))


def _out(row: IntegrationSettings) -> LocalFolderOut:
    """ORM 行 → 契约。列名 local_folder_* 与字段名 paths/enabled/test_* 不一致，
    from_attributes 按字段名取属性取不到（会静默落 None），必须显式构造。"""
    return LocalFolderOut(
        paths=list(row.local_folder_paths or []),
        enabled=bool(row.local_folder_enabled),
        test_ok=row.local_folder_test_ok,
        test_files=row.local_folder_test_files,
    )


def _validate_paths(paths: list[str]) -> None:
    """多目录校验（ValueError → router 422；错误带具体路径便于多目录定位）。

    恒校验每项绝对路径 + 目录存在（不只在启用时）——否则坏路径落库后，卡上
    "启用"按钮每次都 422（2026-09-18 修复）。绝对路径要求防 PyInstaller 打包
    形态下相对路径解析到不定 CWD。
    """
    if len(paths) > _MAX_PATHS:
        raise ValueError(f"目录数不能超过 {_MAX_PATHS} 个")
    for p in paths:
        if not Path(p).is_absolute():
            raise ValueError(f"路径必须是绝对路径（如 C:\\Users\\me\\Documents）：{p}")
    for p in paths:
        if not Path(p).is_dir():
            raise ValueError(f"文件夹不存在或不可访问，请检查路径：{p}")


def save_local_folder(db: Session, body: LocalFolderUpdate) -> LocalFolderOut:
    """保存配置（PUT 入参全量两键；exclude_unset 合并现值）。

    paths 显式 null / 空列表 = 清除全部；合并态 enabled=True 时 paths 须非空
    （只发 enabled 键时路径来自库中现值）。
    目录列表实际变更时清空上次测试结果（旧计数对新配置是谎言，回"未测"）。
    """
    patch = body.model_dump(exclude_unset=True)
    r = _row(db)
    if "paths" not in patch:
        paths = list(r.local_folder_paths or [])  # 键不出现 = 不动（取库现值）
    else:
        paths = list(patch["paths"]) if patch["paths"] is not None else []  # 显式 null = 清除
    enabled = bool(patch["enabled"]) if "enabled" in patch else bool(r.local_folder_enabled)
    old_paths = list(r.local_folder_paths or [])

    _validate_paths(paths)
    if enabled and not paths:
        raise ValueError("启用本地文件夹服务前须先配置文件夹路径")

    r.local_folder_paths = paths or None
    r.local_folder_enabled = enabled
    if paths != old_paths:  # 目录变更 → 测试结果失效
        r.local_folder_test_ok = None
        r.local_folder_test_files = None
    db.commit()
    db.refresh(r)
    # 启停联动镜像 ingest_sources.enabled（采集配置 UI 已撤，卡片停用 = 退出采集）
    # + 用户介入即重置（目录失联冷却不拦修好的路径）——延迟导入避免循环依赖
    # （data_ingest 顶层导入本模块）
    from services import data_ingest

    if "enabled" in patch:
        data_ingest.mirror_source_enabled(db, "local_folder", enabled)
    data_ingest.reset_source_health(db, "local_folder")
    return _out(r)


def test_local_folder(db: Session, paths: list[str] | None = None) -> LocalFolderTestOut:
    """有效性测试（失败是数据不是异常，同 probe_model 口径）。

    paths 传值 = 草稿探测（测配置面板当前填写列表，不读库、不落库——选完文件夹
    即可测，无需先保存，2026-09-18 修复）；缺省 = 测库中已保存路径并**落库**
    test_ok/test_files（2026-09-20 增，卡上「测试」按钮的结果持久化展示在卡
    标题旁徽标，直到下一次测试或目录变更）。
    只验路径可读 + 文件计数，不做 markitdown 试解析——保持测试轻快无格式依赖；
    解析失败在工具层以错误文本呈现（登记取舍）。
    """
    persisted = paths is None  # 卡上测试（测库值）才落库
    if persisted:
        paths, _enabled = get_local_folder_state(db)
    if not paths:
        # "尚未配置"不是对已保存配置的测试，不落库（徽标保持"未测"）
        return LocalFolderTestOut(ok=False, error="尚未配置本地文件夹")
    bad = [p for p in paths if not Path(p).is_absolute() or not Path(p).is_dir()]
    if bad:
        out = LocalFolderTestOut(ok=False, error="文件夹不存在或不可访问，请检查路径：" + "；".join(bad))
    else:
        total = sum(list_files_in([Path(p)])["total"] for p in paths)
        out = LocalFolderTestOut(ok=True, files_count=total)
    if persisted:
        r = _row(db)
        r.local_folder_test_ok = out.ok
        r.local_folder_test_files = out.files_count if out.ok else None
        db.commit()
        if out.ok:
            from services import data_ingest

            data_ingest.reset_source_health(db, "local_folder")  # 测试通过即重置连败
    return out


# ---- 工具纯函数（不碰 DB，可直测）----


def list_files_in(roots: list[Path], limit: int = _MAX_FILES) -> dict:
    """递归列出各根目录内全部文件：完整绝对 POSIX 路径 + 字节大小（多目录
    2026-09-18：绝对路径前缀天然区分来源，同名根不冲突）。

    跳过 "." 前缀的隐藏目录与文件（.git / .DS_Store 等）；到 limit 截断并标记
    truncated（total 仍为全量计数，调用方知道还有更多；limit 为全体根共享预算，
    按根列表顺序消费）。单文件 stat 失败容忍跳过。
    """
    files: list[dict] = []
    total = 0
    truncated = False
    for root in roots:
        for dirpath, dirnames, filenames in os_walk(root):
            # 原地过滤隐藏目录，阻止 os.walk 下钻
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                total += 1
                if len(files) >= limit:
                    truncated = True
                    continue
                full = Path(dirpath) / fn
                try:
                    size = full.stat().st_size
                except OSError:
                    continue  # 竞态消失/无权限的文件跳过
                files.append({"path": full.resolve().as_posix(), "size": size})
    return {"files": files, "total": total, "truncated": truncated}


def os_walk(root: Path):
    """os.walk 的模块级薄封装（仅为测试可 monkeypatch 打桩隐藏目录过滤；非热点路径）。"""
    import os

    return os.walk(root)


def _resolve_in_roots(roots: list[Path], path: str) -> Path:
    """把完整绝对路径解析为某个根内的文件，越界/不存在即 ValueError。

    resolve() 消解 ..、符号链接与大小写变体后逐根 is_relative_to 判定归属——
    不在任何根内（含指向其他目录的绝对路径注入）即拒绝。
    """
    p = Path(path).resolve()
    for root in roots:
        if p.is_relative_to(root.resolve()):
            if not p.is_file():
                raise ValueError(f"文件不存在: {path}")
            return p
    raise ValueError("路径越界：只允许访问已配置文件夹内的文件")


def read_file_as_markdown(roots: list[Path], path: str) -> str:
    """读单个文件并转为 Markdown（markitdown：docx/pdf/xlsx/pptx/txt/md/html/csv 等）。

    path 为 list_files 返回的完整绝对路径。
    """
    target = _resolve_in_roots(roots, path)
    if target.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB 上限，不支持读取")
    try:
        return _markitdown_factory().convert(str(target)).text_content
    except ValueError:
        raise  # _resolve_in_roots 的中文错误原样上抛
    except Exception as e:  # markitdown 各类解析失败翻译为 LLM 可读中文
        raise ValueError(_translate_convert_error(e)) from e


def _translate_convert_error(e: Exception) -> str:
    """markitdown 异常 → 中文摘要（不透传原始报错，可能含本地路径细节）。"""
    from markitdown import MissingDependencyException, UnsupportedFormatException

    if isinstance(e, MissingDependencyException):
        return "该格式需要额外的解析组件（markitdown 富格式扩展未安装）"
    if isinstance(e, UnsupportedFormatException):
        return "暂不支持该文件格式"
    return f"文件解析失败：{type(e).__name__}"


# ---- MCP 工具壳（薄 DB 适配；同步函数——FastMCP 自动扔线程池，不堵事件循环）----


def _current_roots() -> list[Path]:
    """读库取启用中的根目录列表；未启用/未配置返回空列表（工具层给中文错误文本）。"""
    with SessionLocal() as db:
        paths, enabled = get_local_folder_state(db)
    return [Path(p) for p in paths] if enabled and paths else []


_NOT_ENABLED_FILES = {"error": "本地文件夹服务未启用或未配置，请先在 Clara 设置 → 数据接入中开启"}
_NOT_ENABLED_TEXT = "错误：本地文件夹服务未启用或未配置，请先在 Clara 设置 → 数据接入中开启"


@mcp.tool()
def list_files() -> dict:
    """列出本地文件夹内全部文件（完整绝对路径与字节大小，支持多个根目录）。
    返回 {files, total, truncated}。files 每项 {path: 绝对路径, size: 字节}；
    truncated=True 表示超过 500 个文件被截断。"""
    roots = _current_roots()
    if not roots:
        return _NOT_ENABLED_FILES
    return list_files_in(roots)


@mcp.tool()
def read_file(path: str) -> str:
    """读取单个文件并转为 Markdown（支持 docx/pdf/xlsx/pptx/txt/md/html/csv/json 等）。

    path 用 list_files 返回的完整绝对路径；单文件上限 10MB。"""
    roots = _current_roots()
    if not roots:
        return _NOT_ENABLED_TEXT
    try:
        return read_file_as_markdown(roots, path)
    except ValueError as e:
        return f"错误：{e}"  # 工具错误是给 LLM 的数据，不抛崩会话
