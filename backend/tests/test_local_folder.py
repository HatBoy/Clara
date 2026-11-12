"""local_folder 模块单测：工具纯函数（多根）+ MCP 工具壳 + 配置读写（service 层直调）。

HTTP 端点（GET/PUT/POST /api/mcp/local-folder）在 routers/mcp_servers.py，对应
API 用例见本文件底部。markitdown 经 _markitdown_factory 假工厂注入（不依赖真实
Magika 模型加载，保持测试轻快）；真实转换链路在打包冒烟覆盖。
"""

import asyncio

import pytest

from services import local_folder as lf

from conftest import TestingSessionLocal


class _FakeConvert:
    """假 MarkItDown.convert 返回值（只暴露 text_content）。"""

    def __init__(self, text: str):
        self.text_content = text


class _FakeMarkItDown:
    """假工厂实例：记录收到的路径，按需抛异常。"""

    def __init__(self, result: str = "converted", raise_exc: Exception | None = None):
        self.result = result
        self.raise_exc = raise_exc
        self.converted_paths: list[str] = []

    def convert(self, path: str) -> _FakeConvert:
        self.converted_paths.append(path)
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeConvert(self.result)


@pytest.fixture
def fake_md(monkeypatch):
    """替换 _markitdown_factory 为可控假实例（每个用例独立）。"""
    inst = _FakeMarkItDown()
    monkeypatch.setattr(lf, "_markitdown_factory", lambda: inst)
    return inst


def posix(p) -> str:
    """测试辅助：绝对路径 → 工具层统一的 POSIX 形式（两边同 resolve 消 Windows 大小写差）。"""
    from pathlib import Path

    return Path(p).resolve().as_posix()


# ---- list_files_in（纯函数；多根，files[].path = 完整绝对 POSIX 路径）----


def test_list_files_basic(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# hi")

    result = lf.list_files_in([tmp_path])
    assert [f["path"] for f in result["files"]] == [posix(tmp_path / "a.txt"), posix(tmp_path / "sub" / "b.md")]
    assert result["files"][0]["size"] == 5
    assert result["total"] == 2
    assert result["truncated"] is False


def test_list_files_multi_roots(tmp_path):
    """多目录：各根文件以自身绝对路径为前缀，同名根名不冲突、来源可区分。"""
    root_a = tmp_path / "docs"
    root_b = tmp_path / "books"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.txt").write_text("x")
    (root_b / "b.txt").write_text("x")

    result = lf.list_files_in([root_a, root_b])
    assert [f["path"] for f in result["files"]] == [posix(root_a / "a.txt"), posix(root_b / "b.txt")]
    assert result["total"] == 2


def test_list_files_skips_hidden(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.txt").write_text("x")
    (tmp_path / ".hidden.txt").write_text("x")
    (tmp_path / "normal.txt").write_text("x")

    result = lf.list_files_in([tmp_path])
    assert [f["path"] for f in result["files"]] == [posix(tmp_path / "normal.txt")]
    assert result["total"] == 1


def test_list_files_truncation(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x")

    result = lf.list_files_in([tmp_path], limit=2)
    assert len(result["files"]) == 2
    assert result["total"] == 3  # total 仍是全量计数
    assert result["truncated"] is True


def test_list_files_limit_shared_across_roots(tmp_path):
    """limit 为全体根共享预算：第一根吃满后第二根只剩 total 计数。"""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "x.txt").write_text("x")
    (root_b / "y.txt").write_text("x")

    result = lf.list_files_in([root_a, root_b], limit=1)
    assert [f["path"] for f in result["files"]] == [posix(root_a / "x.txt")]
    assert result["total"] == 2
    assert result["truncated"] is True


# ---- _resolve_in_roots / read_file_as_markdown（多根 + 假工厂）----


def test_read_file_roundtrip(tmp_path, fake_md):
    (tmp_path / "a.txt").write_text("raw")
    out = lf.read_file_as_markdown([tmp_path], str(tmp_path / "a.txt"))
    assert out == "converted"
    assert fake_md.converted_paths == [str((tmp_path / "a.txt").resolve())]


def test_read_file_multi_roots_each_readable(tmp_path, fake_md):
    """多根：各根文件按完整绝对路径分别可读（前缀区分来源）。"""
    root_a = tmp_path / "docs"
    root_b = tmp_path / "books"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.txt").write_text("x")
    (root_b / "b.txt").write_text("x")

    assert lf.read_file_as_markdown([root_a, root_b], str(root_a / "a.txt")) == "converted"
    assert lf.read_file_as_markdown([root_a, root_b], str(root_b / "b.txt")) == "converted"


def test_read_file_outside_all_roots_blocked(tmp_path):
    """绝对路径注入：指向任何根之外的路径被钳制（多根版越界）。"""
    (tmp_path / "a.txt").write_text("x")
    outside = tmp_path.parent / "outside_escape.txt"
    outside.write_text("secret")
    try:
        with pytest.raises(ValueError, match="越界"):
            lf.read_file_as_markdown([tmp_path], str(outside))
    finally:
        outside.unlink()


def test_read_file_missing(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ValueError, match="文件不存在"):
        lf.read_file_as_markdown([tmp_path], str(tmp_path / "nope.txt"))


def test_read_file_size_limit(tmp_path, fake_md, monkeypatch):
    monkeypatch.setattr(lf, "_MAX_FILE_BYTES", 4)
    (tmp_path / "big.txt").write_text("1234567890")
    with pytest.raises(ValueError, match="上限"):
        lf.read_file_as_markdown([tmp_path], str(tmp_path / "big.txt"))
    assert fake_md.converted_paths == []  # 未到转换层


def test_read_file_convert_error_translated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lf, "_markitdown_factory", lambda: _FakeMarkItDown(raise_exc=RuntimeError("secret-leak"))
    )
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ValueError) as ei:
        lf.read_file_as_markdown([tmp_path], str(tmp_path / "a.txt"))
    assert "文件解析失败：RuntimeError" in str(ei.value)
    assert "secret-leak" not in str(ei.value)  # 不透传原始报错（防路径/凭据细节泄露）


# ---- MCP 工具壳 ----


def test_tools_registered():
    """两个工具注册冒烟（挂载的服务端工具面）。"""
    tools = asyncio.run(lf.mcp.list_tools())
    assert {t.name for t in tools} == {"list_files", "read_file"}


def _set_state(paths, enabled):
    """直写配置行（复用 service 的 get_or_create 落行）。"""
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        lf.save_local_folder(db, LocalFolderUpdate(paths=paths, enabled=enabled))


def test_tool_not_enabled(monkeypatch):
    monkeypatch.setattr(lf, "SessionLocal", TestingSessionLocal)
    _set_state([], False)
    result = lf.list_files()  # 同步工具函数直接调
    assert result == lf._NOT_ENABLED_FILES
    assert lf.read_file("a.txt") == lf._NOT_ENABLED_TEXT


def test_tool_enabled_lists_and_reads(tmp_path, monkeypatch, fake_md):
    monkeypatch.setattr(lf, "SessionLocal", TestingSessionLocal)
    (tmp_path / "a.txt").write_text("x")
    _set_state([str(tmp_path)], True)

    result = lf.list_files()
    assert result["total"] == 1
    assert result["files"][0]["path"] == posix(tmp_path / "a.txt")
    # 工具层把 ValueError 包成 "错误：…" 文本（给 LLM 的数据，不抛崩会话）
    assert lf.read_file(str(tmp_path.parent / "escape.txt")).startswith("错误：")


# ---- 配置读写（service 层直调；HTTP 语义在底部 API 用例覆盖）----


def test_save_and_get_state(tmp_path):
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        out = lf.save_local_folder(db, LocalFolderUpdate(paths=[str(tmp_path)], enabled=True))
        assert out.paths == [str(tmp_path)] and out.enabled is True
        assert lf.get_local_folder_state(db) == ([str(tmp_path)], True)


def test_save_rejects_relative_path():
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        with pytest.raises(ValueError, match="绝对路径"):
            lf.save_local_folder(db, LocalFolderUpdate(paths=["Documents/notes"]))


def test_save_enabled_requires_paths():
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        with pytest.raises(ValueError, match="配置文件夹路径"):
            lf.save_local_folder(db, LocalFolderUpdate(paths=[], enabled=True))


def test_save_rejects_missing_dir_even_disabled():
    """恒校验（2026-09-18）：目录不存在的路径无论启用与否都拒收——否则坏路径
    落库后，卡上"启用"按钮每次都 422。错误带具体路径便于多目录定位。"""
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        with pytest.raises(ValueError, match="不存在"):
            lf.save_local_folder(db, LocalFolderUpdate(paths=[r"C:\__clara_no_such_dir__"]))


def test_save_merge_enabled_only(tmp_path):
    """只发 enabled 键时路径来自库中现值（exclude_unset 合并态）。"""
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        lf.save_local_folder(db, LocalFolderUpdate(paths=[str(tmp_path)], enabled=False))
        out = lf.save_local_folder(db, LocalFolderUpdate(enabled=True))
        assert out.enabled is True and out.paths == [str(tmp_path)]


def test_folder_test_states(tmp_path):
    from models import IntegrationSettings
    from schemas import LocalFolderUpdate

    with TestingSessionLocal() as db:
        # 未配置
        out = lf.test_local_folder(db)
        assert out.ok is False and "尚未配置" in out.error
        # 草稿探测：传 paths 即测该列表，不读库不落库（选完文件夹即可测）
        out = lf.test_local_folder(db, paths=[r"C:\__clara_no_such_dir__"])
        assert out.ok is False and "不存在" in out.error
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / ".h").write_text("x")
        out = lf.test_local_folder(db, paths=[str(tmp_path)])
        assert out.ok is True and out.files_count == 1  # 隐藏文件不计
        # 库中路径已失效（目录保存后被删）：直写 ORM 构造——save 恒校验后坏路径
        # 无法经 save 落库，但该分支仍是真实场景
        lf.save_local_folder(db, LocalFolderUpdate(paths=[str(tmp_path)]))
        row = db.get(IntegrationSettings, 1)
        row.local_folder_paths = [r"C:\__clara_no_such_dir__"]
        db.commit()
        out = lf.test_local_folder(db)
        assert out.ok is False and "不存在" in out.error


# ---- HTTP 端点（routers/mcp_servers.py 的 local-folder 三条）----


def test_api_get_creates_default_row(client):
    r = client.get("/api/mcp/local-folder")
    assert r.status_code == 200
    assert r.json() == {"paths": [], "enabled": False, "test_ok": None, "test_files": None}


def test_api_put_roundtrip_multi(client, tmp_path):
    other = tmp_path / "sub"
    other.mkdir()
    assert client.put(
        "/api/mcp/local-folder", json={"paths": [str(tmp_path), str(other)], "enabled": True}
    ).status_code == 200
    out = client.get("/api/mcp/local-folder").json()
    assert out == {
        "paths": [str(tmp_path), str(other)],
        "enabled": True,
        "test_ok": None,
        "test_files": None,
    }
    # 显式 null = 清除全部路径
    assert client.put("/api/mcp/local-folder", json={"paths": None, "enabled": False}).status_code == 200
    assert client.get("/api/mcp/local-folder").json() == {
        "paths": [],
        "enabled": False,
        "test_ok": None,
        "test_files": None,
    }


def test_api_put_validations(client):
    assert client.put("/api/mcp/local-folder", json={"paths": ["relative/x"]}).status_code == 422
    assert client.put("/api/mcp/local-folder", json={"enabled": True}).status_code == 422
    assert client.put(
        "/api/mcp/local-folder",
        json={"paths": [r"C:\__clara_no_such_dir__"], "enabled": True},
    ).status_code == 422
    # 恒校验：坏路径即使不启用也拒收（防坏路径落库后启用永远 422）
    assert client.put(
        "/api/mcp/local-folder", json={"paths": [r"C:\__clara_no_such_dir__"]}
    ).status_code == 422
    # 项数上限 / 空串 / 超长（Pydantic 契约层拦）
    assert client.put(
        "/api/mcp/local-folder", json={"paths": ["C:\\"] * 11}
    ).status_code == 422
    assert client.put("/api/mcp/local-folder", json={"paths": [""]}).status_code == 422
    assert client.put("/api/mcp/local-folder", json={"paths": ["x" * 501]}).status_code == 422


def test_api_test_endpoint(client, tmp_path):
    # 未配置 → 失败是数据（200），且不落库（"尚未配置"不是对已保存配置的测试，
    # 徽标保持"未测"）
    r = client.post("/api/mcp/local-folder/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.get("/api/mcp/local-folder").json()["test_ok"] is None
    # 配置真目录 → ok + 计数，结果落库（卡上测试口径，持久化展示到下次测试）
    (tmp_path / "a.txt").write_text("x")
    client.put("/api/mcp/local-folder", json={"paths": [str(tmp_path)]})
    r = client.post("/api/mcp/local-folder/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "files_count": 1, "error": None}
    assert client.get("/api/mcp/local-folder").json() == {
        "paths": [str(tmp_path)],
        "enabled": False,
        "test_ok": True,
        "test_files": 1,
    }
    # body 带 paths = 草稿探测（不读库、无需先保存；多目录合计；失败也是数据）；
    # 草稿结果不落库——库中徽标仍是上次卡上测试的真值
    other = tmp_path / "sub"
    other.mkdir()
    (other / "b.txt").write_text("x")
    (other / "c.txt").write_text("x")
    r = client.post("/api/mcp/local-folder/test", json={"paths": [str(other)]})
    assert r.status_code == 200 and r.json()["files_count"] == 2
    r = client.post(
        "/api/mcp/local-folder/test", json={"paths": [r"C:\__clara_no_such_dir__"]}
    )
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.get("/api/mcp/local-folder").json() == {
        "paths": [str(tmp_path)],
        "enabled": False,
        "test_ok": True,
        "test_files": 1,
    }


def test_api_put_paths_change_clears_test_result(client, tmp_path):
    """目录变更清空测试结果（旧计数对新配置是谎言，回"未测"）；只切开关不清。"""
    (tmp_path / "a.txt").write_text("x")
    client.put("/api/mcp/local-folder", json={"paths": [str(tmp_path)], "enabled": True})
    client.post("/api/mcp/local-folder/test")
    assert client.get("/api/mcp/local-folder").json()["test_ok"] is True

    # 只发 enabled 键（paths 不出现 = 不动）→ 测试结果保留
    client.put("/api/mcp/local-folder", json={"enabled": False})
    out = client.get("/api/mcp/local-folder").json()
    assert out["test_ok"] is True and out["test_files"] == 1

    # 目录实际变更（即使内容相同的另一路径）→ 清空回"未测"
    other = tmp_path / "sub"
    other.mkdir()
    (other / "b.txt").write_text("x")
    client.put("/api/mcp/local-folder", json={"paths": [str(other)]})
    out = client.get("/api/mcp/local-folder").json()
    assert out["paths"] == [str(other)]
    assert out["test_ok"] is None and out["test_files"] is None
