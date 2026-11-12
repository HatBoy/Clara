"""services/data_ingest.py 测试（基建见 conftest.py）。

替身策略照抄既有先例：Composio 经 monkeypatch integrations._build_client /
integrations.list_connections（连接列表假数据）；MCP 经 monkeypatch
mcp_servers._client_factory（假 client + 假工具，ainvoke 返回 JSON 文本）；
本地文件夹用 tmp_path 真文件（local_folder 纯函数本身可直测）。asyncio.run 在
pytest 主线程直跑（同 test_mcp_servers 先例，未装 pytest-asyncio）。
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from conftest import TestingSessionLocal
from models import IntegrationSettings, McpServer
from schemas import (
    ComposioConnectionOut,
    LocalFolderUpdate,
    McpServerCreate,
    McpServerUpdate,
)
from services import data_ingest, local_folder, mcp_servers


# —— 测试基建小件 ——————————————————————————————————————————————


def _set_local_folder(paths: list[str] | None, enabled: bool) -> None:
    with TestingSessionLocal() as db:
        row = db.get(IntegrationSettings, 1) or IntegrationSettings(id=1)
        row.local_folder_paths = paths
        row.local_folder_enabled = enabled
        db.add(row)
        db.commit()


def _set_composio_key(monkeypatch, connections: list[ComposioConnectionOut], client=None) -> None:
    """连接列表走假数据（绕开云端）；client 缺省 = 无需执行（未映射路径）。
    签名须带 enrich 关键字（默认 True）：_enumerate_sources 传 enrich=False，
    单参 lambda 会 TypeError 且被调用方 except Exception 静默吞掉。"""
    monkeypatch.setattr(
        "services.integrations.list_connections", lambda db, enrich=True: connections
    )
    if client is not None:
        monkeypatch.setattr("services.integrations._build_client", lambda key: client)
        monkeypatch.setattr(
            "services.integrations.get_key_settings",
            lambda db: IntegrationSettings(id=1, composio_api_key="k"),
        )


class _FakeExecuteResp:
    def __init__(self, *, successful=True, data=None, error=None):
        self.successful = successful
        self.data = data or {}
        self.error = error


class _FakeTools:
    def __init__(self, items):
        self.items = items


class _SlugTool:
    def __init__(self, slug, description=""):
        self.slug = slug
        self.description = description


class _FakeComposioClient:
    """tools.execute / tools.list 假客户端（mapped 与 note 两条路径共用）。"""

    def __init__(self, *, resp=None, listed=None):
        self._resp = resp
        self._listed = listed
        self.executed: list[tuple] = []

    @property
    def tools(self):
        return self

    def execute(self, tool_slug, *, arguments=None, connected_account_id=None):
        self.executed.append((tool_slug, arguments, connected_account_id))
        return self._resp

    def list(self, *, toolkit_slug=None, important=None, **kw):
        return _FakeTools(self._listed or [])


class _FakeMcpTool:
    """假 LangChain 工具：name/description/ainvoke（返回 JSON 文本）。"""

    def __init__(self, name, result):
        self.name = name
        self.description = f"{name} 描述"
        self._result = result

    async def ainvoke(self, args):
        return self._result


class _FakeMcpClient:
    def __init__(self, tools):
        self._tools = tools

    async def get_tools(self):
        await asyncio.sleep(0)
        return self._tools


def _set_mcp_rows(rows: list[dict]) -> None:
    with TestingSessionLocal() as db:
        for r in rows:
            db.add(McpServer(transport="streamable_http", **r))
        db.commit()


# —— 本地文件夹 ————————————————————————————————————————————————


def test_local_folder_filters_by_mtime(tmp_path):
    recent = tmp_path / "最近的笔记.md"
    recent.write_text("今天写的周报草稿", encoding="utf-8")
    old = tmp_path / "旧文件.md"
    old.write_text("三天前的旧东西", encoding="utf-8")
    three_days_ago = (datetime.now() - timedelta(days=3)).timestamp()
    os.utime(old, (three_days_ago, three_days_ago))
    _set_local_folder([str(tmp_path)], True)

    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert len(out["sources"]) == 1
    src = out["sources"][0]
    assert src["kind"] == "local_folder" and src["ok"] is True
    assert [it["title"] for it in src["items"]] == ["最近的笔记.md"]
    assert "周报草稿" in src["items"][0]["body"]
    assert out["truncated"] is False


def test_local_folder_disabled_absent(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    _set_local_folder([str(tmp_path)], False)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert out["sources"] == []


def test_local_folder_body_budget_clips(tmp_path, monkeypatch):
    big = tmp_path / "大文件.md"
    big.write_text("长" * 5000, encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert len(src["items"][0]["body"]) <= data_ingest._MAX_BODY_CHARS + 10
    assert out["truncated"] is True


def test_local_folder_read_failure_degrades_to_error_text(tmp_path, monkeypatch):
    f = tmp_path / "读不了的.md"
    f.write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)

    def _boom(roots, path):
        raise ValueError("文件超过 10MB 上限，不支持读取")

    monkeypatch.setattr("services.local_folder.read_file_as_markdown", _boom)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True  # 单文件失败是条目级降级，不是源失败
    assert "读取失败" in src["items"][0]["body"]


def test_local_folder_rich_formats_skipped(tmp_path):
    (tmp_path / "讲义.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "笔记.md").write_text("普通笔记", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    # 格式分级：富格式不进静态采集（留补采按需转换），note 声明数量
    assert [it["title"] for it in src["items"]] == ["笔记.md"]
    assert "富格式" in src["note"]


def test_local_folder_signal_ranks_first(tmp_path, monkeypatch):
    plain = tmp_path / "普通笔记.md"  # 更新时间更新，但无价值信号
    plain.write_text("随便写点什么", encoding="utf-8")
    todo = tmp_path / "待办清单.md"
    todo.write_text("一些上下文\n- [ ] 周五前交报告", encoding="utf-8")
    older = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(todo, (older, older))
    monkeypatch.setattr(data_ingest, "_MAX_ITEMS_PER_SOURCE", 1)  # 名额 1 个
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    # 信号排序：含 checkbox 的旧文件胜过更新的普通文件，且带 sched_hint=True
    assert [it["title"] for it in src["items"]] == ["待办清单.md"]
    assert src["items"][0]["sched_hint"] is True


# —— Composio ———————————————————————————————————————————————————


def _conn(slug="gmail", ca_id="ca1", name=None):
    return ComposioConnectionOut(
        id=ca_id, status="ACTIVE", toolkit_slug=slug, name=name or slug,
        logo=None, description=None, created_at=None, updated_at=None,
    )


def test_composio_key_missing_no_source():
    # 未配置 Key：无源不是错误（list_connections 抛 KeyNotConfiguredError 被吞）
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert out["sources"] == []


def test_composio_mapped_action_extracts_items(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(data={
        "emails": [{"id": "m1", "subject": "周会改期", "date": datetime.now().isoformat(),
                    "body": "周五的评审挪到上午十点"}]
    }))
    _set_composio_key(monkeypatch, [_conn("gmail", "ca-9", "Gmail")], client)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and src["source"] == "Gmail"
    assert client.executed == [("GMAIL_FETCH_EMAILS", {"max_results": 20}, "ca-9")]
    assert src["items"][0]["title"] == "周会改期"
    assert "评审挪到上午十点" in src["items"][0]["body"]


def test_composio_old_items_dropped_by_window(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(data={
        "emails": [
            {"id": "new", "subject": "新邮件", "date": datetime.now().isoformat()},
            {"id": "old", "subject": "旧邮件", "date": (datetime.now() - timedelta(days=3)).isoformat()},
        ]
    }))
    _set_composio_key(monkeypatch, [_conn()], client)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert [it["title"] for it in out["sources"][0]["items"]] == ["新邮件"]


def test_composio_unmapped_lists_tools_in_note(monkeypatch):
    client = _FakeComposioClient(listed=[_SlugTool("SLACK_FETCH_MESSAGES"), _SlugTool("SLACK_SEND_MESSAGE")])
    _set_composio_key(monkeypatch, [_conn("slack", "ca-2", "Slack")], client)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and src["items"] == []
    assert "SLACK_FETCH_MESSAGES" in src["note"]


def test_composio_execute_failure_is_data(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(successful=False, error="quota exceeded"))
    _set_composio_key(monkeypatch, [_conn()], client)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and src["items"] == []
    assert "GMAIL_FETCH_EMAILS" in src["error"]


# —— MCP ————————————————————————————————————————————————————————


def test_mcp_unmapped_lists_tools_in_note(monkeypatch):
    # 中性 url（不命中内置映射特征串）——滴答已实测填入映射，未映射场景换假域名
    _set_mcp_rows([{"name": "我的待办", "url": "https://mcp.example.com/mcp"}])
    monkeypatch.setattr(
        "services.mcp_servers._client_factory",
        lambda: lambda connections: _FakeMcpClient([_FakeMcpTool("get_tasks", "{}")]),
    )
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["kind"] == "mcp" and src["ok"] is True
    assert "get_tasks" in src["note"] and src["items"] == []


def test_mcp_mapped_tool_called_and_parsed(monkeypatch):
    _set_mcp_rows([{"name": "我的待办", "url": "https://mcp.dida365.com/mcp"}])
    monkeypatch.setattr(
        "services.mcp_servers._client_factory",
        lambda: lambda connections: _FakeMcpClient(
            [_FakeMcpTool("get_recent_tasks", json.dumps(
                {"tasks": [{"id": "t1", "title": "买牛奶", "due": None}]}, ensure_ascii=False))]
        ),
    )
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS", {"dida365": ("get_recent_tasks", {"limit": 10})})
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert [it["title"] for it in src["items"]] == ["买牛奶"]


def test_mcp_mapped_tool_missing_from_server(monkeypatch):
    _set_mcp_rows([{"name": "我的待办", "url": "https://mcp.dida365.com/mcp"}])
    monkeypatch.setattr(
        "services.mcp_servers._client_factory",
        lambda: lambda connections: _FakeMcpClient([_FakeMcpTool("别的工具", "{}")]),
    )
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS", {"dida365": ("get_recent_tasks", {})})
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["items"] == [] and "get_recent_tasks" in src["note"]


def test_mcp_failure_is_data(monkeypatch):
    class _Boom:
        async def get_tools(self):
            raise OSError("spawn failed")

    _set_mcp_rows([{"name": "挂掉的服务", "url": "https://example.com/mcp"}])
    monkeypatch.setattr("services.mcp_servers._client_factory", lambda: lambda connections: _Boom())
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and src["items"] == []
    assert src["error"]  # 中文摘要非空


def test_mcp_local_folder_virtual_conn_excluded(tmp_path, monkeypatch):
    # 本地文件夹启用时 get_connections 会并入 local_folder 虚拟连接，采集层应剔除
    # （已直采，不重复出源）——剔除失败则桥接被调、_client_factory 假替身即抛错
    _set_local_folder([str(tmp_path)], True)
    _set_mcp_rows([])

    def _boom():
        raise AssertionError("虚拟连接未被剔除，不应走 MCP 桥接")

    monkeypatch.setattr("services.mcp_servers._client_factory", _boom)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert [s["kind"] for s in out["sources"]] == ["local_folder"]


# —— 纯函数 ————————————————————————————————————————————————————


def test_extract_items_falls_back_to_single_item():
    items = data_ingest._extract_items({"foo": "bar"})
    assert len(items) == 1
    assert "foo" in items[0]["body"]


def test_extract_items_finds_nested_list():
    items = data_ingest._extract_items({"data": {"results": [{"name": "甲"}, {"name": "乙"}]}})
    assert [it["title"] for it in items] == ["甲", "乙"]


def test_apply_budget_limits_count_and_chars():
    items = [{"id": str(i), "title": "t", "body": "x" * 100, "time": None}
             for i in range(data_ingest._MAX_ITEMS_PER_SOURCE + 5)]
    out, clipped = data_ingest._apply_budget(items)
    assert clipped is True
    assert len(out) == data_ingest._MAX_ITEMS_PER_SOURCE
    long = [{"id": "0", "title": "t", "body": "y" * 3000, "time": None}]
    out, clipped = data_ingest._apply_budget(long)
    assert clipped is True and out[0]["body"].endswith("…（截断）")


def test_extract_items_marks_sched_hint():
    # 日程/任务强信号时间键（due）→ True；通用时间戳（created_at）与无时间 → False
    items = data_ingest._extract_items({"results": [
        {"name": "任务甲", "due": "2026-10-16T09:00:00"},
        {"name": "笔记乙", "created_at": "2026-10-15T08:00:00"},
        {"name": "无时间丙"},
    ]})
    assert [it["sched_hint"] for it in items] == [True, False, False]
    assert items[0]["time"] is not None  # due 同时作为时间提取


def test_extract_items_sched_hint_not_shadowed():
    # 遮蔽修复（2026-10-15）：同含通用键与强信号键时，任一强信号键命中即 True——
    # 修复前 hit 取第一个命中键（created_at 在前），sched_hint 被遮蔽成 False
    items = data_ingest._extract_items({"results": [{
        "name": "带截止的任务", "created_at": "2026-10-14T09:00:00",
        "dueDate": "2026-10-16T18:00:00",
    }]})
    assert items[0]["sched_hint"] is True


def test_fingerprint_stable_across_volatile_fields():
    # 抗漂移（2026-10-15）：body 是整条压缩 JSON，modifiedTime 每次编辑都变——
    # 剔易变键后同一实质条目指纹不变；dueDate（强信号键）变化 = 实质变化指纹必变
    def body(modified, due):
        return json.dumps({"id": "t1", "title": "交报告", "modifiedTime": modified,
                           "dueDate": due}, ensure_ascii=False)

    fp_a = data_ingest._fingerprint("s", {"id": "t1", "body": body("2026-10-14T09:00:00", "2026-10-16T18:00:00")})
    fp_b = data_ingest._fingerprint("s", {"id": "t1", "body": body("2026-10-15T20:00:00", "2026-10-16T18:00:00")})
    fp_c = data_ingest._fingerprint("s", {"id": "t1", "body": body("2026-10-15T20:00:00", "2026-10-17T18:00:00")})
    assert fp_a == fp_b  # 仅易变键变化：同一实质条目，不重复进 LLM
    assert fp_a != fp_c  # 截止变化：实质变化，新指纹重新进入
    # 纯文本 body（非 JSON）无键可剔，原样参与
    plain = data_ingest._fingerprint("s", {"id": "x", "body": "纯文本内容"})
    assert plain == data_ingest._fingerprint("s", {"id": "x", "body": "纯文本内容"})
    assert plain != data_ingest._fingerprint("s", {"id": "x", "body": "纯文本内容变了"})


def test_apply_budget_keeps_sched_hint_first():
    # 混排超上限：无信号纯文本在前也会被排后截掉，结构化条目优先保留
    plain = [{"id": f"p{i}", "title": "笔记", "body": "x" * 10,
              "time": None, "sched_hint": False}
             for i in range(data_ingest._MAX_ITEMS_PER_SOURCE)]
    task = {"id": "t0", "title": "任务", "body": "y" * 10,
            "time": "2026-10-16T09:00:00", "sched_hint": True}
    out, clipped = data_ingest._apply_budget(plain + [task])
    assert clipped is True
    assert out[0]["id"] == "t0"  # 稳定排序：信号条目最前，同级（纯文本）保原序


# —— 游标 / 指纹 / 台账 / 配置 / 补采 / 预算 / 信任 ——————————————————————————


import pytest
from models import IngestionItem, Proposal
from schemas import IngestSourceUpdate
from services.settings import get_settings


@pytest.fixture(autouse=True)
def _reset_ingest_module_state():
    """清补采次数与 LLM 预算两处模块态（跨用例不串台；会话池由 conftest 清）。"""
    data_ingest._TOOL_CAP_STATE.clear()
    data_ingest._LLM_BUDGET_STATE = None
    yield


def _ingest_source_row(key):
    from models import IngestSource
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        return db.scalar(select(IngestSource).where(IngestSource.source_key == key))


def _backdate_last_attempt(key, hours=25):
    """把源行 last_attempt_at 回拨到冷却封顶之外（25h > 24h），模拟冷却窗已过。"""
    from models import IngestSource
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        row = db.scalar(select(IngestSource).where(IngestSource.source_key == key))
        row.last_attempt_at = datetime.now() - timedelta(hours=hours)
        db.commit()


def test_first_fetch_creates_cursor_row_with_fingerprint(tmp_path):
    (tmp_path / "a.md").write_text("内容甲", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["key"] == "local_folder" and src["fail_streak"] == 0
    assert len(src["items"][0]["fingerprint"]) == 40  # sha1 hex
    row = _ingest_source_row("local_folder")
    assert row is not None and row.last_success_at is not None


def test_cursor_narrows_window(tmp_path):
    fresh = tmp_path / "新.md"
    fresh.write_text("一小时前", encoding="utf-8")
    stale = tmp_path / "旧.md"
    stale.write_text("三十小时前", encoding="utf-8")
    stale_time = (datetime.now() - timedelta(hours=30)).timestamp()
    os.utime(stale, (stale_time, stale_time))
    _set_local_folder([str(tmp_path)], True)
    # 游标 = 2 小时前成功 → 窗口 clamp 到 2h：30h 旧文件滤除、新文件保留
    from models import IngestSource

    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="local_folder",
                            last_success_at=datetime.now() - timedelta(hours=2)))
        db.commit()
        out = data_ingest.fetch_recent(db, hours=24)
    assert [it["title"] for it in out["sources"][0]["items"]] == ["新.md"]


def test_window_widens_after_long_gap(tmp_path):
    # 长中断补齐（2026-10-15 起上限 168h）：5 天没开机，恢复首采按游标放行 5 天前的
    # 改动（修复前钳制 72h 会丢）——周末/短假场景不再错过日程
    old = tmp_path / "五天前的笔记.md"
    old.write_text("- [ ] 旧待办", encoding="utf-8")
    five_days = (datetime.now() - timedelta(days=5)).timestamp()
    os.utime(old, (five_days, five_days))
    _set_local_folder([str(tmp_path)], True)
    from models import IngestSource

    with TestingSessionLocal() as db:
        # 游标锚点比文件 mtime 再早 1 小时（round 钳制会吃掉秒级余量，留足窗宽）
        db.add(IngestSource(source_key="local_folder",
                            last_success_at=datetime.now() - timedelta(days=5, hours=1)))
        db.commit()
        out = data_ingest.fetch_recent(db, hours=24)
    assert [it["title"] for it in out["sources"][0]["items"]] == ["五天前的笔记.md"]


def test_fail_streak_counts_and_resets(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(successful=False, error="quota"))
    _set_composio_key(monkeypatch, [_conn()], client)
    with TestingSessionLocal() as db:
        data_ingest.fetch_recent(db, hours=24)
    _backdate_last_attempt("ca1")  # 冷却窗已过（模拟时间流逝）：连败继续累计
    with TestingSessionLocal() as db:
        data_ingest.fetch_recent(db, hours=24)
    row = _ingest_source_row("ca1")
    assert row.fail_streak == 2 and "quota" in row.last_error

    ok_client = _FakeComposioClient(resp=_FakeExecuteResp(data={"emails": []}))
    monkeypatch.setattr("services.integrations._build_client", lambda key: ok_client)
    _backdate_last_attempt("ca1")  # 冷却过后恢复重试，成功清零自愈
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert out["sources"][0]["ok"] is True and out["sources"][0]["fail_streak"] == 0
    row = _ingest_source_row("ca1")
    assert row.fail_streak == 0 and row.last_error is None and row.last_ok_at is not None


def test_failed_source_cooldown_skips(monkeypatch):
    """连败冷却（离线源静默跳过）：冷却窗内再采，源不出现、不记尝试、连败不涨。"""
    client = _FakeComposioClient(resp=_FakeExecuteResp(successful=False, error="quota"))
    _set_composio_key(monkeypatch, [_conn()], client)
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
        second = data_ingest.fetch_recent(db, hours=24)  # 距上次失败仅数秒：冷却中
    assert first["sources"][0]["ok"] is False
    assert second["sources"] == []  # 静默跳过：不产出条目、不算一次尝试
    assert _ingest_source_row("ca1").fail_streak == 1


def test_mcp_auth_error_payload_marked_and_cooldowns(monkeypatch):
    """第三层过滤：服务在线但凭据无效——错误 JSON 被启发式抽干成空结果时回看
    返回体，判失败并打 [认证] 标、记连败，冷却窗内再采静默跳过。"""
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS",
                        {"fake-mcp": ("get_events", {})})
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_events", json.dumps(
        {"code": 99991668, "msg": "Invalid app_access_token"}))])
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
        second = data_ingest.fetch_recent(db, hours=24)  # 认证失败进连败 → 冷却跳过
    src = first["sources"][0]
    assert src["ok"] is False and src["error"].startswith("[认证]")
    assert src["fail_streak"] == 1
    assert second["sources"] == []
    row = _ingest_source_row("示例")
    assert row.fail_streak == 1 and row.last_error.startswith("[认证]")


def test_mcp_auth_marker_not_false_positive_on_data(monkeypatch):
    """防误杀：条目非空说明真拿到了数据，正文含认证特征词也不判认证失败。"""
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS",
                        {"fake-mcp": ("get_events", {})})
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_events", json.dumps(
        {"events": [{"id": "e1", "title": "安全审计培训：unauthorized 风险清单",
                     "time": None}]}, ensure_ascii=False))])
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and len(src["items"]) == 1


def test_call_readonly_tool_auth_error_marked(monkeypatch):
    """补采路径同款检测：真实数据调用返回认证错误 JSON → [认证] 标 + 记连败。"""
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_events", json.dumps(
        {"code": 99991661, "msg": "Invalid tenant_access_token"}))])
    with TestingSessionLocal() as db:
        out = data_ingest.call_readonly_tool(db, key="示例", tool="get_events",
                                             arguments={})
    assert out["ok"] is False and out["error"].startswith("[认证]")
    assert out["fail_streak"] == 1


def test_seen_fingerprint_keepalive_and_dedup(tmp_path):
    (tmp_path / "a.md").write_text("不变的内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
        second = data_ingest.fetch_recent(db, hours=24)
    assert first["sources"][0]["deduped"] == 0
    assert second["sources"][0]["deduped"] == 1  # 已见保活滤除，不重复进 LLM
    with TestingSessionLocal() as db:
        from sqlalchemy import select

        rows = list(db.scalars(select(IngestionItem)))
    assert len(rows) == 1 and rows[0].seen_count == 2


def test_rejected_fingerprint_silent_forever(tmp_path):
    (tmp_path / "被拒绝.md").write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
        fp = first["sources"][0]["items"][0]["fingerprint"]
        data_ingest.on_proposal_settled(
            db, "schedule",
            {"evidence": [{"source_key": "local_folder", "fingerprint": fp}]},
            "rejected",
        )
        second = data_ingest.fetch_recent(db, hours=24)
    assert second["sources"][0]["items"] == []
    assert second["sources"][0]["deduped"] == 1


def test_on_proposal_settled_ignores_accepted_and_other_kinds():
    with TestingSessionLocal() as db:
        data_ingest.on_proposal_settled(db, "schedule", {"evidence": []}, "accepted")  # 不动
        data_ingest.on_proposal_settled(db, "preflight", {"x": 1}, "rejected")  # 非采集类不动
    # 无异常即通过（空 payload 早退分支）


def test_payload_refs_both_kinds():
    refs = data_ingest.payload_refs("schedule", {
        "evidence": [{"source_key": "s1", "fingerprint": "f1"},
                     {"source_key": "s1", "fingerprint": "f1"},  # 去重
                     {"source_key": None, "fingerprint": "f2"}],  # 缺键不计
    })
    assert refs == {("s1", "f1")}
    refs = data_ingest.payload_refs("inbox", {
        "items": [{"source_ref": {"source_key": "s2", "fingerprint": "f2"}}],
    })
    assert refs == {("s2", "f2")}


def test_mark_items_proposed(tmp_path):
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
        fp = out["sources"][0]["items"][0]["fingerprint"]
        data_ingest.mark_items_proposed(db, [("local_folder", fp), ("local_folder", "查无指纹")])
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        row = db.scalar(select(IngestionItem).where(IngestionItem.fingerprint == fp))
    assert row.proposed_at is not None


def test_purge_stale_items():
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        db.add_all([
            IngestionItem(source_key="s", fingerprint="old", item_title="旧",
                          last_seen_at=datetime.now() - timedelta(days=31)),
            IngestionItem(source_key="s", fingerprint="new", item_title="新",
                          last_seen_at=datetime.now() - timedelta(days=29)),
        ])
        db.commit()
        n = data_ingest.purge_stale_items(db)
        left = list(db.scalars(select(IngestionItem)))
    assert n == 1 and [r.fingerprint for r in left] == ["new"]


def test_update_ingest_source_overrides_mapping(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(
        data={"events": [{"id": "e1", "title": "日历事件"}]}))
    _set_composio_key(monkeypatch, [_conn("calendar", "ca-7", "日历")], client)
    with TestingSessionLocal() as db:
        row = data_ingest.update_ingest_source(
            db, "ca-7", IngestSourceUpdate(fetch_tool="CAL_LIST_EVENTS", fetch_args={"limit": 5}))
        assert row.fetch_tool == "CAL_LIST_EVENTS"
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert client.executed == [("CAL_LIST_EVENTS", {"limit": 5}, "ca-7")]
    assert src["items"][0]["title"] == "日历事件"


def test_update_ingest_source_rejects_unknown_key_and_long_args(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)  # 源存在，走参数校验分支
    with TestingSessionLocal() as db:
        assert data_ingest.update_ingest_source(db, "不存在", IngestSourceUpdate(enabled=True)) is None
        with pytest.raises(ValueError):
            data_ingest.update_ingest_source(
                db, "local_folder", IngestSourceUpdate(fetch_args={"k": "v" * 3000}))


def test_enabled_false_and_pause_skip_source(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    from models import IngestSource

    with TestingSessionLocal() as db:
        data_ingest.update_ingest_source(db, "local_folder", IngestSourceUpdate(enabled=False))
        assert data_ingest.fetch_recent(db, hours=24)["sources"] == []
        data_ingest.update_ingest_source(db, "local_folder", IngestSourceUpdate(enabled=True))
        # pause_until 不开放 PUT（信任降频由 apply_trust_policy 专用），直接改行验证跳过
        row = data_ingest._source_row(db, "local_folder")
        row.pause_until = datetime.now() + timedelta(hours=1)
        db.commit()
        assert data_ingest.fetch_recent(db, hours=24)["sources"] == []  # 降频中静默


# —— 卡片启停联动（配置面板已撤：停用/启用是源的退出手段，镜像写采集行）—————


def test_mcp_toggle_mirrors_ingest_source():
    from schemas import McpServerUpdate
    from services import mcp_servers

    _set_mcp_rows([{"name": "联动服务", "url": "https://x.example/mcp"}])
    with TestingSessionLocal() as db:
        row = db.query(McpServer).filter_by(name="联动服务").one()
        mcp_servers.update_server(db, row.id, McpServerUpdate(enabled=False))
    r = _ingest_source_row("联动服务")
    assert r is not None and r.enabled is False
    with TestingSessionLocal() as db:
        mcp_servers.update_server(db, row.id, McpServerUpdate(enabled=True))
    assert _ingest_source_row("联动服务").enabled is True


def test_local_folder_toggle_mirrors_ingest_source(tmp_path):
    from schemas import LocalFolderUpdate
    from services import local_folder

    with TestingSessionLocal() as db:
        local_folder.save_local_folder(db, LocalFolderUpdate(paths=[str(tmp_path)], enabled=True))
    assert _ingest_source_row("local_folder").enabled is True
    with TestingSessionLocal() as db:
        local_folder.save_local_folder(db, LocalFolderUpdate(enabled=False))
    assert _ingest_source_row("local_folder").enabled is False


def test_window_hours_override_widens(tmp_path):
    old = tmp_path / "两天前.md"
    old.write_text("旧内容", encoding="utf-8")
    os.utime(old, ((datetime.now() - timedelta(hours=48)).timestamp(),) * 2)
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        data_ingest.update_ingest_source(db, "local_folder", IngestSourceUpdate(window_hours=72))
        out = data_ingest.fetch_recent(db, hours=1)  # 全局默认 1h，行覆盖 72h 放行旧文件
    assert [it["title"] for it in out["sources"][0]["items"]] == ["两天前.md"]


def test_list_ingest_sources_shapes(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    _set_composio_key(monkeypatch, [_conn("gmail", "ca-3", "Gmail")])
    with TestingSessionLocal() as db:
        rows = data_ingest.list_ingest_sources(db)
    by_key = {r["key"]: r for r in rows}
    assert set(by_key) == {"local_folder", "ca-3"}
    assert by_key["local_folder"]["kind"] == "local_folder" and by_key["local_folder"]["enabled"]
    assert by_key["ca-3"]["source"] == "Gmail" and by_key["ca-3"]["available_tools"] == []


def test_enumerate_sources_passes_enrich_false(monkeypatch):
    """源枚举走 enrich=False（2026-10-15 数据接入页打开慢排障：省每 slug 一次
    元数据云端往返）——记录 kwargs 的假体锁定接线，防将来回退成默认 enrich。"""
    calls: list[dict] = []

    def fake_list(db, enrich=True):
        calls.append({"enrich": enrich})
        return [_conn("gmail", "ca-3", "Gmail")]

    monkeypatch.setattr("services.integrations.list_connections", fake_list)
    with TestingSessionLocal() as db:
        rows = data_ingest.list_ingest_sources(db)
    assert calls == [{"enrich": False}]
    assert [r["key"] for r in rows if r["kind"] == "composio"] == ["ca-3"]


def _set_mcp_factory(monkeypatch, tools):
    monkeypatch.setattr(
        "services.mcp_servers._client_factory",
        lambda: lambda connections: _FakeMcpClient(tools),
    )


def test_call_readonly_tool_hard_gates(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)  # local_folder 成为真实源，走专属拒绝分支
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_tasks", "{}")])
    with TestingSessionLocal() as db:
        # 写操作特征词（大小写不敏感）
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="CREATE_TASK", arguments={})
        assert r["ok"] is False and "只读" in r["error"]
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="send_message", arguments={})
        assert r["ok"] is False
        # 源不存在 / 本地文件夹拒绝
        assert data_ingest.call_readonly_tool(db, key="查无", tool="get", arguments={})["ok"] is False
        r = data_ingest.call_readonly_tool(db, key="local_folder", tool="get", arguments={})
        assert r["ok"] is False and "不支持补采" in r["error"]
        # 参数形态
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments="不是对象")
        assert r["ok"] is False and "JSON 对象" in r["error"]
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks",
                                           arguments={"q": "v" * 3000})
        assert r["ok"] is False and "2000" in r["error"]


def test_call_readonly_tool_daily_cap(monkeypatch):
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    _set_mcp_factory(monkeypatch, [_FakeMcpTool(
        "get_tasks", json.dumps({"tasks": [{"id": "t1", "title": "买牛奶"}]}, ensure_ascii=False))])
    with TestingSessionLocal() as db:
        first = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments={})
        second = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments={})
        third = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments={})
    assert first["ok"] is True and first["items"][0]["title"] == "买牛奶"
    assert second["ok"] is True
    assert third["ok"] is False and "上限" in third["error"]


def test_call_readonly_tool_skips_rejected(monkeypatch):
    # rejected 滤除（跨路径对账）：台账里被拒过的指纹，补采结果同样滤除不再进候选
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    payload = json.dumps({"tasks": [
        {"id": "t1", "title": "被拒过的任务", "created_at": datetime.now().isoformat()},
        {"id": "t2", "title": "新任务", "created_at": datetime.now().isoformat()},
    ]}, ensure_ascii=False)
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_tasks", payload)])
    with TestingSessionLocal() as db:
        r1 = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments={})
        assert [it["title"] for it in r1["items"]] == ["被拒过的任务", "新任务"]
        db.add(IngestionItem(source_key=r1["key"], fingerprint=r1["items"][0]["fingerprint"],
                             item_title="被拒过的任务", rejected_at=datetime.now()))
        db.commit()
        r2 = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks", arguments={})
    assert r2["ok"] is True
    assert [it["title"] for it in r2["items"]] == ["新任务"]


def test_call_readonly_tool_composio_path(monkeypatch):
    client = _FakeComposioClient(resp=_FakeExecuteResp(
        data={"emails": [{"id": "m1", "subject": "补采邮件", "date": datetime.now().isoformat()}]}))
    _set_composio_key(monkeypatch, [_conn("gmail", "ca-5", "Gmail")], client)
    with TestingSessionLocal() as db:
        r = data_ingest.call_readonly_tool(db, key="ca-5", tool="GMAIL_SEARCH_THREADS",
                                           arguments={"query": "invoice"})
    assert r["ok"] is True and r["items"][0]["title"] == "补采邮件"
    assert "补采工具" in r["note"]


def test_list_source_actions_shapes(monkeypatch):
    _set_composio_key(monkeypatch, [_conn("slack", "ca-8", "Slack")],
                      _FakeComposioClient(listed=[_SlugTool("SLACK_FETCH_MESSAGES")]))
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_tasks", "{}")])
    with TestingSessionLocal() as db:
        rows = data_ingest.list_source_actions(db)
    by_key = {r["key"]: r for r in rows}
    assert by_key["ca-8"]["tools"][0]["name"] == "SLACK_FETCH_MESSAGES"
    assert by_key["待办服务"]["tools"][0]["name"] == "get_tasks"


def test_claim_llm_budget_counts_and_resets():
    with TestingSessionLocal() as db:
        s = get_settings(db)
        s.ingest_llm_budget = 2
        db.commit()
        assert data_ingest.claim_llm_budget(db) is True
        assert data_ingest.claim_llm_budget(db) is True
        assert data_ingest.claim_llm_budget(db) is False  # 当日额度尽
        # 跨日重置：直接改模块态日期模拟次日
        from datetime import date as _date

        data_ingest._LLM_BUDGET_STATE = (_date.today() - timedelta(days=1), 2)
        assert data_ingest.claim_llm_budget(db) is True
        # 0 = 不限
        s.ingest_llm_budget = 0
        db.commit()
        data_ingest._LLM_BUDGET_STATE = None
        assert all(data_ingest.claim_llm_budget(db) for _ in range(50))


def _settle(db, key, outcome, n=1, kind="schedule"):
    """造 n 条带源键溯源的已决建议（信任统计测试基建）。"""
    from services.ai_data import create_proposal, record_proposal_outcome

    for _ in range(n):
        p = create_proposal(db, kind=kind, payload={
            "draft_plan": [],
            "evidence": [{"source_key": key, "fingerprint": f"fp-{_}-{id(p) if False else key}"}],
        } if kind == "schedule" else {"items": [{"title": "t", "source_ref": {
            "source_key": key, "fingerprint": f"fp-{key}"}}]})
        record_proposal_outcome(db, p.id, outcome)


def test_apply_trust_policy_pauses_low_rate():
    from models import IngestSource
    from sqlalchemy import select

    with TestingSessionLocal() as db:
        _settle(db, "good-src", "accepted", n=6)
        _settle(db, "bad-src", "rejected", n=6)
        result = data_ingest.apply_trust_policy(db)
        assert result["paused"] == ["bad-src"]
        row = db.scalar(select(IngestSource).where(IngestSource.source_key == "bad-src"))
        assert row.pause_until is not None and row.pause_until.hour == 9
        good = db.scalar(select(IngestSource).where(IngestSource.source_key == "good-src"))
        assert good is None or good.pause_until is None


def test_adoption_stats_by_source_aggregates():
    from services.ai_data import adoption_stats_by_source

    with TestingSessionLocal() as db:
        _settle(db, "s1", "accepted", n=7)
        _settle(db, "s1", "rejected", n=3)
        _settle(db, "s2", "modified", n=2)
        # preflight 类与 expired 不进采集口径
        from services.ai_data import create_proposal, record_proposal_outcome

        p = create_proposal(db, kind="preflight", payload={"headline": "x"})
        record_proposal_outcome(db, p.id, "rejected")
        stats = adoption_stats_by_source(db)
    assert stats["total"] == 12 and stats["accepted_modified"] == 9
    assert stats["rate"] == 9 / 12
    by = {s["source_key"]: s for s in stats["by_source"]}
    assert by["s1"]["total"] == 10 and by["s1"]["rate"] == 0.7
    assert by["s2"]["rate"] == 1.0


# —— CLI 工具（第四源 kind="cli"）—————————————————————————————————————
# 替身同 test_cli_tools.py：fake 适配器注入 ADAPTERS（launchers 指向
# tests/fixtures/fake_cli.py，sys.executable 启动），命令名与假件子命令对应。
# 管道行为全走替身（指纹/游标/台账/预算/信任对 CLI 源自动兼容是设计目标）。


import sys as _sys
from pathlib import Path as _Path

from models import CliTool as _CliTool
from services import cli_tools as _cli_tools
from services.cli_tools import CliAdapter as _CliAdapter, CliCommand as _CliCommand

_FAKE_CLI = _Path(__file__).parent / "fixtures" / "fake_cli.py"


def _fake_cli_spec(**overrides) -> _CliAdapter:
    """全量声明 fake 适配器（镜像 lark 能力面）；overrides 覆盖单项便于变体用例。"""
    defaults = dict(
        adapter_id="fake", name="假 CLI", description="测试替身",
        launchers=((_sys.executable, (str(_FAKE_CLI),)),),
        version_argv=("version",),
        auth_check=_CliCommand("auth status", "检查授权状态", ("auth-status",), 10.0),
        auth_start=_CliCommand("auth login", "发起授权", ("auth-start",), 10.0),
        auth_url_re=r"https?://\S+",
        fetches=(_CliCommand("fetch", "静态采集", ("fetch",), 15.0),),
        readonly=(_CliCommand("lookup", "补采查询", ("fetch", "{arg_keyword}"), 15.0),),
    )
    return _CliAdapter(**{**defaults, **overrides})


@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec())


def _set_cli_row(adapter_id: str = "fake", enabled: bool = True, **kw) -> None:
    with TestingSessionLocal() as db:
        db.add(_CliTool(adapter_id=adapter_id, enabled=enabled, **kw))
        db.commit()


def test_cli_source_enumeration_and_toggle(fake_cli):
    _set_cli_row()
    with TestingSessionLocal() as db:
        rows = [s for s in data_ingest._enumerate_sources(db) if s["key"] == "cli:fake"]
    assert rows and rows[0]["kind"] == "cli" and rows[0]["source"] == "假 CLI"
    # 停用行直接跳过（enabled=False 不出现，同 Composio/MCP 口径）
    with TestingSessionLocal() as db:
        row = db.get(_CliTool, "fake")
        row.enabled = False
        db.commit()
        assert all(s["key"] != "cli:fake" for s in data_ingest._enumerate_sources(db))


def test_cli_fetch_recent_full_chain(fake_cli, monkeypatch):
    # FAKE_NOW 固定基准：两次子进程输出时间戳/指纹一致（dedup 断言前提；30/45 分钟
    # 偏移避开二次采集增量游标窗 1h 下限的边界抖动）
    monkeypatch.setenv("FAKE_NOW", datetime.now().isoformat())
    _set_cli_row()
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
    src = first["sources"][0]
    assert src["key"] == "cli:fake" and src["ok"] is True and src["fail_streak"] == 0
    assert [it["id"] for it in src["items"]] == ["f1", "f2"]  # 窗内留、窗外 old 丢
    assert src["items"][0]["sched_hint"] is True  # dueDate 强信号
    # 二次采集：指纹台账已见全滤（keepalive），不重复进 LLM
    with TestingSessionLocal() as db:
        second = data_ingest.fetch_recent(db, hours=24)
    assert second["sources"][0]["items"] == [] and second["sources"][0]["deduped"] == 2


def test_cli_multi_fetch_merges_domains(fake_cli, monkeypatch):
    # 一源多域（fetches 多条，lark 实测后回归）：日历与任务各一条命令，条目拼接
    # 出全量；单条失败是数据走 note 不毁整源（第二条指 fail 子命令验证）
    monkeypatch.setenv("FAKE_NOW", datetime.now().isoformat())
    spec = _fake_cli_spec(fetches=(
        _CliCommand("fetch", "静态采集一", ("fetch",), 15.0),
        _CliCommand("fetch2", "静态采集二", ("fetch2",), 15.0),
    ))
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", spec)
    _set_cli_row()
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and [it["id"] for it in src["items"]] == ["f1", "f2", "g1"]
    assert src["fail_streak"] == 0

    fail_spec = _fake_cli_spec(adapter_id="fake2", fetches=(
        _CliCommand("fetch", "静态采集一", ("fetch",), 15.0),
        _CliCommand("bad", "会失败的域", ("fail",), 15.0),
    ))
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake2", fail_spec)
    _set_cli_row(adapter_id="fake2")  # 全新源 key：f1/f2 不受首轮台账影响
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = next(s for s in out["sources"] if s["key"] == "cli:fake2")
    assert src["ok"] is True and [it["id"] for it in src["items"]] == ["f1", "f2"]
    assert src["note"] and "bad" in src["note"] and "boom" in src["note"]


def test_cli_fetch_none_notes_declared_commands(monkeypatch):
    # fetch 未实测填入 → ok=True 空条目 + note 声明可用命令清单（不阻塞采集）
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(fetches=()))
    _set_cli_row()
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and src["items"] == []
    assert "待真机实测" in src["note"] and "lookup" in src["note"]


def test_cli_call_readonly_gates_and_cap(fake_cli):
    _set_cli_row()
    with TestingSessionLocal() as db:
        # 白名单外命令拒
        r = data_ingest.call_readonly_tool(db, key="cli:fake", tool="deploy", arguments={})
        assert r["ok"] is False and "白名单" in r["error"]
        # 白名单命中：lookup 渲染 {arg_keyword} → 假件 fetch 输出，窗内条目回收
        r = data_ingest.call_readonly_tool(db, key="cli:fake", tool="lookup",
                                           arguments={"keyword": "x"})
        assert r["ok"] is True and [it["id"] for it in r["items"]] == ["f1", "f2"]
        assert "补采工具" in r["note"]
        # 每日 2 次帽（共用闸，CLI 分支计帽不漂移；白名单拒不计次）
        data_ingest.call_readonly_tool(db, key="cli:fake", tool="lookup",
                                       arguments={"keyword": "x"})
        r3 = data_ingest.call_readonly_tool(db, key="cli:fake", tool="lookup",
                                            arguments={"keyword": "x"})
        assert r3["ok"] is False and "上限" in r3["error"]


def test_cli_call_readonly_denies_write_marker_declaration(monkeypatch):
    # 声明静态 token 含写操作特征词（适配器笔误纵深）：白名单命中也拒
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(
        fetches=(),
        readonly=(_CliCommand("purge", "危险声明", ("fetch", "--delete", "{arg_q}"), 15.0),),
    ))
    _set_cli_row()
    with TestingSessionLocal() as db:
        r = data_ingest.call_readonly_tool(db, key="cli:fake", tool="purge",
                                           arguments={"q": "x"})
    assert r["ok"] is False and "写操作特征词" in r["error"]


def test_cli_auth_error_marked(monkeypatch):
    # fetch 指到 auth-status（FAKE_AUTHED 未设 → stderr not authenticated 退 1）
    # → [认证] 前缀（LLM 换源不重试）+ 连败记数
    monkeypatch.setenv("FAKE_AUTHED", "")  # delenv 的空值形态，auth-status 判非 "1"
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(
        fetches=(_CliCommand("fetch", "静态采集", ("auth-status",), 15.0),)))
    _set_cli_row()
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and src["error"].startswith("[认证]")
    assert src["fail_streak"] == 1


def test_cli_timeout_marks_failure(monkeypatch):
    monkeypatch.setattr(data_ingest, "_RETRY_BACKOFF_S", 0)  # 超时属瞬时类会重试：退避置零保速度
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(
        fetches=(_CliCommand("fetch", "静态采集", ("slow",), 1.0),)))
    _set_cli_row()
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and "超时" in src["error"]


def test_custom_cli_collects_and_rejects_replenish():
    """窄口自定义：伪 spec 静态采集走通（{since} 占位渲染）；readonly 恒空不进补采。"""
    _set_cli_row(adapter_id="custom:abc", name="我的清单", custom_bin=_sys.executable,
                 custom_argv=[str(_FAKE_CLI), "fetch", "{since}"])
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["key"] == "cli:custom:abc" and src["ok"] is True
    assert [it["id"] for it in src["items"]] == ["f1", "f2"]
    with TestingSessionLocal() as db:
        r = data_ingest.call_readonly_tool(db, key="cli:custom:abc", tool="fetch",
                                           arguments={})
    assert r["ok"] is False and "不支持补采" in r["error"]


# —— 采集防御加固（2026-10-16）：六层过滤闭环 / 瞬时重试 / catastrophic 信封 ——————


import time  # noqa: E402

import httpx  # noqa: E402

from agents import tools as agent_tools  # noqa: E402


class _SlowComposioClient(_FakeComposioClient):
    """execute 先记次再睡 0.3s（>0.05s 假超时帽）：记次在调用起点定格，
    不受 wait_for 取消的线程尾巴影响。"""

    def execute(self, tool_slug, *, arguments=None, connected_account_id=None):
        self.executed.append((tool_slug, arguments, connected_account_id))
        time.sleep(0.3)
        return self._resp


class _RaisingComposioClient(_FakeComposioClient):
    def execute(self, tool_slug, *, arguments=None, connected_account_id=None):
        raise RuntimeError("云端炸了")


class _CountingMcpTool(_FakeMcpTool):
    """ainvoke 计数版（断言「恰一次尝试、未重试」）。"""

    def __init__(self, name, result):
        super().__init__(name, result)
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        return self._result


def test_retryable_error_classification():
    f = data_ingest._retryable_error
    assert f("连接超时：服务未在 90 秒内完成握手与调用") is True
    assert f("无法连接服务：检查网络与 URL 是否可达") is True
    assert f("CLI 执行失败：命令超时（>1 秒），已终止") is True
    assert f("connection refused by peer") is True
    assert f("[认证] 服务在线但凭据无效（msg）") is False
    assert f("未找到 CLI 可执行文件（被卸载或移动？请重新「检测」）") is False
    assert f("云端执行 X 失败：quota exceeded") is False
    assert f(None) is False
    assert f("") is False


def test_local_folder_listing_failure_is_data_and_cooldowns(tmp_path, monkeypatch):
    """缺口 A：目录不可访问 = 源失败条目（不炸穿整批）+ 新冷却闸第二轮静默跳过。"""
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)

    def _boom(roots):
        raise PermissionError("denied")

    monkeypatch.setattr("services.local_folder.list_files_in", _boom)
    with TestingSessionLocal() as db:
        first = data_ingest.fetch_recent(db, hours=24)
    src = first["sources"][0]
    assert src["key"] == "local_folder" and src["ok"] is False and src["items"] == []
    assert "无法读取" in src["error"] and src["fail_streak"] == 1

    # 补丁仍生效：若冷却闸缺失，第二轮会再产出失败条目——「不出现」即证明静默跳过
    with TestingSessionLocal() as db:
        second = data_ingest.fetch_recent(db, hours=24)
    assert all(s["key"] != "local_folder" for s in second["sources"])
    assert _ingest_source_row("local_folder").fail_streak == 1  # 未再记尝试


def test_local_folder_read_oserror_degrades_to_error_text(tmp_path, monkeypatch):
    """缺口 B：read_file_as_markdown 抛 OSError（TOCTOU）→ 条目级降级不弃条目。"""
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)

    def _boom(roots, rel):
        raise OSError("winerror 32 共享冲突")

    monkeypatch.setattr("services.local_folder.read_file_as_markdown", _boom)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and len(src["items"]) == 1
    assert "读取失败" in src["items"][0]["body"]
    assert "winerror" not in src["items"][0]["body"]  # 固定文案不透传 str(e)


def test_fetch_retry_recovers_transient_mcp_failure(monkeypatch):
    """瞬时失败重试：首轮握手连败（池+桥接两路都炸）→ 退避后重试 → 第二轮成功。"""
    monkeypatch.setattr(data_ingest, "_RETRY_BACKOFF_S", 0)
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS", {"fake-mcp": ("get_events", {})})
    state = {"handshakes": 0}
    payload = json.dumps({"events": [{"id": "e1", "title": "重试后拿到的日程",
                                      "time": None}]}, ensure_ascii=False)

    class _FlakyClient:
        async def get_tools(self):
            state["handshakes"] += 1
            if state["handshakes"] <= 2:  # 首轮：池路径 + 桥接回落都失败
                raise httpx.ConnectError("connection refused")
            return [_FakeMcpTool("get_events", payload)]

    monkeypatch.setattr(
        "services.mcp_servers._client_factory",
        lambda: lambda connections: _FlakyClient(),
    )
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is True and [it["title"] for it in src["items"]] == ["重试后拿到的日程"]
    assert state["handshakes"] == 3  # 首轮 2 次（池+桥接）+ 重试轮 1 次


def test_fetch_retry_auth_not_retried(monkeypatch):
    """认证类确定性失败不重试：单次尝试即定性，重试只会白等。"""
    monkeypatch.setattr(data_ingest, "_RETRY_BACKOFF_S", 0)
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS", {"fake-mcp": ("get_events", {})})
    tool = _CountingMcpTool("get_events", json.dumps(
        {"code": 99991668, "msg": "Invalid app_access_token"}))
    _set_mcp_factory(monkeypatch, [tool])
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and src["error"].startswith("[认证]")
    assert tool.calls == 1


def test_fetch_retry_timeout_exhausted_three_attempts(monkeypatch):
    """瞬时类恒失败：至多 3 次尝试后 ok=False，连败按最终结果每轮只计一次。"""
    monkeypatch.setattr(data_ingest, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(data_ingest, "_MCP_TIMEOUT_S", 0.05)
    client = _SlowComposioClient(resp=_FakeExecuteResp(data={"emails": []}))
    _set_composio_key(monkeypatch, [_conn()], client)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and "超时" in src["error"]
    assert len(client.executed) == 3
    assert src["fail_streak"] == 1


def test_composio_auth_chinese_marker_flagged(monkeypatch):
    """缺口 E：Composio Key 失效的中文翻译文案命中认证标记 → 打 [认证] 前缀。"""
    assert data_ingest._auth_failure("API Key 无效或已失效，请重新配置") is True
    monkeypatch.setattr("services.integrations._translate_upstream",
                        lambda e: "API Key 无效或已失效，请重新配置")
    _set_composio_key(monkeypatch, [_conn()], _RaisingComposioClient())
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["ok"] is False and src["error"].startswith("[认证]")


def test_finalize_note_attempt_failure_degrades_not_fatal(monkeypatch):
    """缺口 C：收尾逐源隔离——单源回写炸（db 故障）不毁整批，数据照常返回。"""
    _set_composio_key(monkeypatch, [_conn()], _FakeComposioClient(
        resp=_FakeExecuteResp(data={"emails": []})))
    _set_mcp_rows([{"name": "示例", "url": "https://fake-mcp.example.com/mcp"}])
    monkeypatch.setattr(data_ingest, "_MCP_FETCH_TOOLS", {"fake-mcp": ("get_events", {})})
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_events", json.dumps(
        {"events": [{"id": "e1", "title": "日程", "time": None}]}, ensure_ascii=False))])
    real = data_ingest._note_attempt

    def flaky(db, key, ok, error=None):
        if key == "ca1":
            raise RuntimeError("db commit 炸了")
        return real(db, key, ok, error)

    monkeypatch.setattr(data_ingest, "_note_attempt", flaky)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    by_key = {s["key"]: s for s in out["sources"]}
    assert set(by_key) == {"ca1", "示例"}  # 双源都在，未毁整批
    ca1 = by_key["ca1"]
    assert ca1["ok"] is True and ca1["fail_streak"] == 0  # streak 降级 0
    assert ca1["items"] and len(ca1["items"][0]["fingerprint"]) == 40
    assert by_key["示例"]["fail_streak"] == 0


def test_call_readonly_tool_missing_conn_chinese_error(monkeypatch):
    """缺口 D-①：枚举后连接被并发停用/删除 → 中文指引而非 KeyError 退化文案。"""
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    states = [{"待办服务": {"url": "https://mcp.dida365.com/mcp"}}, {}]  # 枚举见 → 执行时消失
    monkeypatch.setattr("services.mcp_servers.get_connections",
                        lambda db: states.pop(0) if states else {})
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_tasks", "{}")])
    with TestingSessionLocal() as db:
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks",
                                           arguments={})
    assert r["ok"] is False and "连接信息已变更" in r["error"]
    assert "KeyError" not in r["error"]


def test_call_readonly_tool_tail_failure_returns_envelope(monkeypatch):
    """缺口 D-②：补采执行成功但尾段回写炸 → 降级失败条目，不抛异常。"""
    _set_mcp_rows([{"name": "待办服务", "url": "https://mcp.dida365.com/mcp"}])
    _set_mcp_factory(monkeypatch, [_FakeMcpTool("get_tasks", json.dumps(
        {"tasks": [{"id": "t1", "title": "买牛奶"}]}, ensure_ascii=False))])

    def boom(*a, **kw):
        raise RuntimeError("db 炸")

    monkeypatch.setattr(data_ingest, "_note_attempt", boom)
    with TestingSessionLocal() as db:
        r = data_ingest.call_readonly_tool(db, key="待办服务", tool="get_tasks",
                                           arguments={})
    assert r["ok"] is False and "结果处理失败" in r["error"] and r["items"] == []


def test_fetch_recent_catastrophic_envelope(monkeypatch):
    """service 层 catastrophic 外闸：内部全炸 → 空信封 + error 键（直调方不见异常）。"""

    def boom(*a, **kw):
        raise RuntimeError("全面崩溃")

    monkeypatch.setattr(data_ingest, "_collect_local_folder", boom)
    monkeypatch.setattr(data_ingest, "_collect_remote", boom)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert out["sources"] == [] and out["truncated"] is False
    assert "采集异常" in out["error"] and "RuntimeError" in out["error"]


def test_tool_shells_never_raise(monkeypatch):
    """@tool 三壳 catastrophic 信封：service 层炸 → 合法 JSON，永不向工具循环抛异常。"""

    def boom(*a, **kw):
        raise RuntimeError("service 崩")

    monkeypatch.setattr(data_ingest, "fetch_recent", boom)
    monkeypatch.setattr(data_ingest, "list_source_actions", boom)
    monkeypatch.setattr(data_ingest, "call_readonly_tool", boom)
    out = json.loads(agent_tools.fetch_recent_data.invoke({"hours": 24}))
    assert out["sources"] == [] and out["truncated"] is False and "采集异常" in out["error"]
    rows = json.loads(agent_tools.list_data_sources.invoke({}))
    assert isinstance(rows, list) and "清单异常" in rows[0]["error"]
    r = json.loads(agent_tools.call_data_tool.invoke(
        {"key": "k", "tool": "t", "arguments": {}}))
    assert r["ok"] is False and "补采异常" in r["error"]


def test_mcp_build_stage_failure_family_entry(monkeypatch):
    """构建阶段族级兜底：MCP 清单 db 读炸 → 族级失败条目（镜像 Composio 先例）。"""
    _set_composio_key(monkeypatch, [])  # 无 Composio 源，聚焦 MCP 族

    def boom(db):
        raise RuntimeError("db 读炸")

    monkeypatch.setattr("services.mcp_servers.get_connections", boom)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = out["sources"][0]
    assert src["kind"] == "mcp" and src["ok"] is False
    assert "清单读取失败" in src["error"]


# —— 配置变更及时生效（2026-10-16：用户介入即重置连败冷却）————————————————————


def test_reset_source_health_clears_evidence_keeps_cursor():
    """reset 语义：连败证据清零、错误清空；增量游标保留（窗口不被放大）；无行不建行。"""
    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "x", True)              # 先成功（游标落库）
        data_ingest._note_attempt(db, "x", False, "连接失败")   # 再失败（连败=1）
    with TestingSessionLocal() as db:
        data_ingest.reset_source_health(db, "x")
        row = data_ingest._source_row(db, "x")
        assert row.fail_streak == 0 and row.last_error is None
        assert row.last_success_at is not None
    with TestingSessionLocal() as db:
        data_ingest.reset_source_health(db, "y")  # 无行不建行（首访才落口径不变）
        assert data_ingest._source_row(db, "y") is None


def _flaky_mcp_factory(state: dict):
    """状态化假 factory：state["ok"]=False 时握手抛连接错误；True 时返回正常假工具。"""

    def _factory():
        def _make(connections):
            if not state["ok"]:
                async def _boom():
                    raise ConnectionError("坏 token")

                return SimpleNamespace(get_tools=_boom)
            due = datetime.now().isoformat(timespec="seconds")
            return _FakeMcpClient([
                _FakeMcpTool(
                    "get_project_with_undone_tasks",
                    f'{{"tasks": [{{"id": "t1", "title": "买牛奶", "due": "{due}"}}]}}',
                )
            ])

        return _make

    return _factory


def test_mcp_token_fix_collects_again(monkeypatch):
    """用户排障场景（滴答清单换 token）端到端：坏凭据失败进冷却 → 冷却期内静默
    跳过（bug 形态）→ update_server 写入即重置 + 池会话失效 → 下轮立即恢复采集。"""
    invalidated: list[str] = []
    monkeypatch.setattr("services.mcp_session.invalidate",
                        lambda name: invalidated.append(name))
    state = {"ok": False}
    monkeypatch.setattr("services.mcp_servers._client_factory", _flaky_mcp_factory(state))
    _set_mcp_rows([{"name": "滴答清单", "url": "https://mcp.dida365.com/mcp"}])

    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = next(s for s in out["sources"] if s["key"] == "滴答清单")
    assert src["ok"] is False and src["fail_streak"] == 1

    # 用户尚未介入：即使凭据已好也会被冷却静默跳过（本次排障的直接根因）
    state["ok"] = True
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert all(s["key"] != "滴答清单" for s in out["sources"])

    # 修好 token（任意字段写入）→ 连败证据作废 + 池会话同步失效
    with TestingSessionLocal() as db:
        server_id = db.scalar(select(McpServer.id).where(McpServer.name == "滴答清单"))
        mcp_servers.update_server(db, server_id,
                                  McpServerUpdate(headers={"token": "good"}))
    assert invalidated == ["滴答清单"]

    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    src = next(s for s in out["sources"] if s["key"] == "滴答清单")
    assert src["ok"] is True and src["items"]


def test_mcp_test_success_lifts_cooldown(monkeypatch):
    """测试通过即放行：test_server 探测成功重置连败冷却——探测结果此前只写
    last_test_* 四列不碰健康列，「测试通过 ≠ 下轮会重试」是隐蔽缺口。"""
    state = {"ok": False}
    monkeypatch.setattr("services.mcp_servers._client_factory", _flaky_mcp_factory(state))
    _set_mcp_rows([{"name": "滴答清单", "url": "https://mcp.dida365.com/mcp"}])
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert next(s for s in out["sources"] if s["key"] == "滴答清单")["ok"] is False

    state["ok"] = True  # 服务恢复 + 用户点「测试」通过
    with TestingSessionLocal() as db:
        server_id = db.scalar(select(McpServer.id).where(McpServer.name == "滴答清单"))
        result = asyncio.run(mcp_servers.test_server(db, server_id))
    assert result.ok is True

    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert any(s["key"] == "滴答清单" and s["ok"] for s in out["sources"])


def test_mcp_recreate_same_name_not_blocked(monkeypatch):
    """同名重建：残留 ingest_sources 行（enabled=False + 连败）不拦新服务——
    create_server 镜像 enabled=True 并重置（对齐 cli_tools.connect 口径）。"""
    _set_mcp_factory(monkeypatch,
                     [_FakeMcpTool("get_project_with_undone_tasks", '{"tasks": []}')])
    with TestingSessionLocal() as db:
        row = mcp_servers.create_server(db, McpServerCreate(
            transport="streamable_http", name="待办", url="https://mcp.dida365.com/mcp"))
        data_ingest.mirror_source_enabled(db, "待办", False)     # 旧服务：停用态
        data_ingest._note_attempt(db, "待办", False, "连接失败")  # 且带连败
        assert mcp_servers.delete_server(db, row.id) is True

    with TestingSessionLocal() as db:
        mcp_servers.create_server(db, McpServerCreate(
            transport="streamable_http", name="待办", url="https://mcp.dida365.com/mcp"))
        out = data_ingest.fetch_recent(db, hours=24)
    assert any(s["key"] == "待办" and s["ok"] for s in out["sources"])


def test_local_folder_save_and_test_reset_cooldown(tmp_path):
    """目录失联进冷却 → 保存（路径修复/启停）或卡上测试通过 → 下轮立即重采。"""
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "local_folder", False, "目录不可访问")

    with TestingSessionLocal() as db:
        local_folder.save_local_folder(
            db, LocalFolderUpdate(paths=[str(tmp_path)], enabled=True))
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert any(s["key"] == "local_folder" and s["ok"] for s in out["sources"])

    with TestingSessionLocal() as db:
        data_ingest._note_attempt(db, "local_folder", False, "再次失联")
        result = local_folder.test_local_folder(db)  # persisted：测库中路径并落库
    assert result.ok is True
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    assert any(s["key"] == "local_folder" and s["ok"] for s in out["sources"])


# —— 教练溯源单跳探测（2026-10-23 批 3）——————————————————————————————————


def test_probe_source_cli_picks_task_domain_and_textualizes(monkeypatch):
    """fake 适配器 readonly 换成 calendar/task 两域搜索（镜像 lark）：解析取
    task 域（教练问的多是项目任务），参数键 arg_query 即占位符名，结果压成
    「【标题】正文」文本行。"""
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(readonly=(
        _CliCommand("calendar +search-event", "日程搜索",
                    ("cal-search", "{arg_query}"), 15.0),
        _CliCommand("task +search", "任务搜索", ("task-search", "{arg_query}"), 15.0),
    )))
    _set_cli_row()
    seen = {}

    def _fake_call(db, *, key, tool, arguments):
        seen.update(tool=tool, arguments=arguments)
        return {"ok": True, "items": [
            {"title": "发布会物料 v2", "body": '{"note": "Q3 物料清单：主视觉、邀请函"}'},
            {"title": "供应商报价", "body": '{"amount": 12000}'},
        ]}

    monkeypatch.setattr(data_ingest, "call_readonly_tool", _fake_call)
    with TestingSessionLocal() as db:
        text = data_ingest.probe_source(db, "cli:fake", "发布会物料")
    assert seen == {"tool": "task +search", "arguments": {"arg_query": "发布会物料"}}
    assert "【发布会物料 v2】" in text and "主视觉" in text


def test_probe_source_silent_downgrades(monkeypatch):
    """三重降级全静默 None：① 源不存在 ② 该源无搜索类工具（readonly=lookup）③
    工具调用失败（ok=False，含超帽/deny-list 同形态）。静态 evidence 兜底由
    调用方负责，此处只验不抛不响。"""
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec())
    _set_cli_row()
    monkeypatch.setattr(
        data_ingest, "call_readonly_tool",
        lambda db, *, key, tool, arguments: {"ok": False, "items": []})
    with TestingSessionLocal() as db:
        assert data_ingest.probe_source(db, "cli:not-exist", "查询") is None  # 源不存
        assert data_ingest.probe_source(db, "cli:fake", "查询") is None       # 无搜索工具
    monkeypatch.setitem(_cli_tools.ADAPTERS, "fake", _fake_cli_spec(readonly=(
        _CliCommand("task +search", "任务搜索", ("task-search", "{arg_query}"), 15.0),)))
    with TestingSessionLocal() as db:
        assert data_ingest.probe_source(db, "cli:fake", "查询") is None       # 调用失败


# —— 高可信直通（2026-10-24）：域标注 / 台账落列 / 拆分判定 / 默认值 ———————————


def test_local_folder_items_labeled_doc_domain(tmp_path):
    # 本地文件夹恒文档域——笔记提炼条目永不直通（用户定案：提取的不准确须确认）
    (tmp_path / "a.md").write_text("内容甲", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        out = data_ingest.fetch_recent(db, hours=24)
    item = out["sources"][0]["items"][0]
    assert item["domain"] == "doc"
    with TestingSessionLocal() as db:
        row = db.scalar(select(IngestionItem).where(
            IngestionItem.fingerprint == item["fingerprint"]))
    assert row is not None and row.domain == "doc"  # 台账行随新条目落列


def test_cli_fetch_commands_declare_domain():
    # lark 一源两域按命令声明（calendar +agenda / task +get-my-tasks）；默认值
    # 白名单只收纯任务型精选工具（lark 混合文档域不预开）
    from services.cli_tools import ADAPTERS

    domains = {c.name: c.domain for c in ADAPTERS["lark"].fetches}
    assert domains["calendar +agenda"] == "calendar"
    assert domains["task +get-my-tasks"] == "todo"
    assert data_ingest.DEFAULT_TRUSTED_CLI == {"todoist", "dingtalk"}
    assert data_ingest.DEFAULT_TRUSTED_MCP_MARKERS == {"dida365"}
    assert "lark" not in data_ingest.DEFAULT_TRUSTED_CLI


def test_split_trusted_direct_partitions():
    # 判定矩阵：trusted ∧ 台账域 ∈ {todo, calendar} 才直落；doc / 无证据 / 无
    # source_ref / 非 trusted 源 / 台账查不到一律保守建卡；rest 证据 idx 重排
    from models import IngestSource

    with TestingSessionLocal() as db:
        db.add(IngestSource(source_key="cli:lark", trusted=True))
        for fp, dom in (("a" * 40, "todo"), ("b" * 40, "calendar"), ("c" * 40, "doc")):
            db.add(IngestionItem(source_key="cli:lark", fingerprint=fp,
                                 item_title="t", domain=dom))
        db.commit()

        draft = [
            {"task_id": None, "title": "日历直落", "start": "2026-10-25T10:00:00", "duration": 60},
            {"task_id": None, "title": "无证据草案", "start": "2026-10-25T11:00:00", "duration": 30},
            {"task_id": None, "title": "笔记草案", "start": "2026-10-25T15:00:00", "duration": 45},
        ]
        inbox = [
            {"title": "待办直落", "duration": 30, "notes": None,
             "source_ref": {"source_key": "cli:lark", "fingerprint": "a" * 40}},
            {"title": "文档提炼待办", "duration": None, "notes": None,
             "source_ref": {"source_key": "cli:lark", "fingerprint": "c" * 40}},
            {"title": "无溯源待办", "duration": None, "notes": None, "source_ref": None},
            {"title": "未登记源待办", "duration": None, "notes": None,
             "source_ref": {"source_key": "cli:todoist", "fingerprint": "d" * 40}},
        ]
        evidence = [
            {"idx": 0, "source": "飞书", "source_key": "cli:lark", "item_id": "i1",
             "title": "日历直落", "time": None, "fingerprint": "b" * 40},
            {"idx": 2, "source": "飞书", "source_key": "cli:lark", "item_id": "i3",
             "title": "笔记草案", "time": None, "fingerprint": "c" * 40},
        ]
        split = data_ingest.split_trusted_direct(db, draft, inbox, evidence)

        assert [d["title"] for d in split["draft_direct"]] == ["日历直落"]
        assert [d["title"] for d in split["draft_rest"]] == ["无证据草案", "笔记草案"]
        # rest 证据 idx 重排对齐收缩后草案序（原 idx2 → 1）
        assert len(split["evidence_rest"]) == 1 and split["evidence_rest"][0]["idx"] == 1
        assert len(split["inbox_direct"]) == 1 and split["inbox_direct"][0]["title"] == "待办直落"
        assert [it["title"] for it in split["inbox_rest"]] == [
            "文档提炼待办", "无溯源待办", "未登记源待办"]
        # 直落条目附 source_ref（apply_plan_entries 溯源透传），原条目不动
        entry = split["draft_direct_entries"][0]
        assert entry["source_ref"] == {"source_key": "cli:lark", "fingerprint": "b" * 40}
        assert "source_ref" not in split["draft_direct"][0]
        # 降级合并预计算：direct(1) + rest(2)，direct 证据 idx→0、rest 证据偏移 1
        assert [d["title"] for d in split["draft_fallback"]] == [
            "日历直落", "无证据草案", "笔记草案"]
        assert [e["idx"] for e in split["evidence_fallback"]] == [0, 2]


def test_split_trusted_direct_all_pass_through_when_untrusted():
    # 无 trusted 源时全量回传（调用方零分支）；evidence 原样（无重排发生）
    with TestingSessionLocal() as db:
        draft = [{"task_id": None, "title": "草案", "start": "2026-10-25T10:00:00",
                  "duration": 60}]
        evidence = [{"idx": 0, "source": "飞书", "source_key": "cli:lark", "item_id": "i1",
                     "title": "草案", "time": None, "fingerprint": "a" * 40}]
        inbox = [{"title": "待办", "duration": None, "notes": None,
                  "source_ref": {"source_key": "cli:lark", "fingerprint": "a" * 40}}]
        split = data_ingest.split_trusted_direct(db, draft, inbox, evidence)
        assert split["draft_rest"] == draft and split["inbox_rest"] == inbox
        assert split["evidence_rest"] == evidence
        assert not split["draft_direct"] and not split["inbox_direct"]
        assert split["draft_direct_entries"] == [] and split["evidence_direct"] == []


def test_mirror_source_enabled_trusted_on_create_only():
    # trusted 仅建行时生效（接入默认值落点）；存量行再镜像（启停/重接入）不
    # 重置用户开关——用户关掉的直通不会因重新接入复活
    with TestingSessionLocal() as db:
        data_ingest.mirror_source_enabled(db, "cli:todoist", True, trusted=True)
    assert _ingest_source_row("cli:todoist").trusted is True
    with TestingSessionLocal() as db:
        # 未带 trusted 的镜像（启停路径）也不动
        data_ingest.mirror_source_enabled(db, "cli:todoist", False)
    assert _ingest_source_row("cli:todoist").trusted is True
    from models import IngestSource

    with TestingSessionLocal() as db:  # 用户显式关
        row = db.scalar(select(IngestSource).where(IngestSource.source_key == "cli:todoist"))
        row.trusted = False
        db.commit()
    with TestingSessionLocal() as db:  # 重接入镜像 trusted=True 不复活
        data_ingest.mirror_source_enabled(db, "cli:todoist", True, trusted=True)
    assert _ingest_source_row("cli:todoist").trusted is False


def test_update_ingest_source_trusted_toggle(tmp_path):
    # PUT 翻转直通开关 + list 带出（前端数据接入卡消费同一端点）
    (tmp_path / "a.md").write_text("内容甲", encoding="utf-8")
    _set_local_folder([str(tmp_path)], True)
    with TestingSessionLocal() as db:
        row = data_ingest.update_ingest_source(db, "local_folder", IngestSourceUpdate(trusted=True))
    assert row is not None and row.trusted is True
    rows = data_ingest.list_ingest_sources(TestingSessionLocal())
    src = next(r for r in rows if r["key"] == "local_folder")
    assert src["trusted"] is True


def test_iso_strips_timezone_to_local_naive():
    """_iso 出口统一本地 naive（2026-11-05 真机日期丢失批）：滴答 MCP 返回
    +08:00 标准带时区形态，旧版原样透传——直映射判定 tzinfo is None 不成立、
    带时间日程全部降级无时间待办。所有源进系统即转本地 naive（_within_window
    同款口径），datetime 对象与 Z 后缀同覆盖。"""
    from datetime import datetime as _dt, timezone as _tz

    local = _dt.now().astimezone().strftime("%z")  # 本地偏移（如 +0800）
    off = f"{local[:3]}:{local[3:]}" if local else "+00:00"
    out = data_ingest._iso(f"2026-11-05T10:01:35{off}")
    assert out == "2026-11-05T10:01:35"           # 本地时区同偏移 → 数值不变去后缀
    assert data_ingest._iso("2026-11-05T02:01:35Z").startswith("2026-11-05T")
    assert "+" not in data_ingest._iso("2026-11-05T02:01:35Z")
    assert data_ingest._iso(_dt(2026, 11, 5, 10, 1, 35,
                                tzinfo=_tz.utc)).count("+") == 0
    assert data_ingest._iso("2026-11-05T10:01:35") == "2026-11-05T10:01:35"  # naive 原样
    assert data_ingest._iso("不是时间") is None


def test_structured_seen_unproposed_replays_but_doc_and_proposed_filter(tmp_path):
    """台账分域重放（2026-11-05 存量卡死修复）：todo/calendar 域「已见且从未
    proposed」重新放行（直映射零 LLM，装配查重幂等）——时区修复前被吞的旧条目
    拿得到重新处理；曾进卡（proposed）与 doc 域已见照旧滤除。"""
    with TestingSessionLocal() as db:
        # 三条同源已见台账行：todo 未提 / todo 已提 / doc 未提
        db.add(IngestionItem(source_key="local_folder", fingerprint="a" * 40,
                             item_title="待办旧条", domain="todo"))
        db.add(IngestionItem(source_key="local_folder", fingerprint="b" * 40,
                             item_title="已提条", domain="todo",
                             proposed_at=datetime.now()))
        db.add(IngestionItem(source_key="local_folder", fingerprint="c" * 40,
                             item_title="笔记旧条", domain="doc"))
        db.commit()
        items = [
            {"id": "1", "title": "待办旧条", "body": "{}", "time": None,
             "fingerprint": "a" * 40, "domain": "todo"},
            {"id": "2", "title": "已提条", "body": "{}", "time": None,
             "fingerprint": "b" * 40, "domain": "todo"},
            {"id": "3", "title": "笔记旧条", "body": "{}", "time": None,
             "fingerprint": "c" * 40, "domain": "doc"},
        ]
        kept, deduped = data_ingest._filter_and_keepalive(db, "local_folder", items)
    assert [it["title"] for it in kept] == ["待办旧条"]   # 结构化未提重放
    assert deduped == 2                                     # 已提 + doc 照滤


def test_extract_items_prefers_sched_time_over_metadata():
    """time 取值强信号优先（2026-11-05 真机数据篡改批）：旧键序 createdTime
    排在 dueDate 前——15:00 的日程被顶替成创建时刻、无日期待办被凭空排期
    （真机 AAA/BBB 项目双病例）。元数据键永不作为 time。"""
    payload = {"tasks": [
        {"id": "t1", "title": "BBB", "dueDate": "2026-11-05T15:00:00+08:00",
         "createdTime": "2026-11-05T11:09:22+08:00"},   # 日程：due 优先
        {"id": "t2", "title": "AAA", "createdTime": "2026-11-05T11:09:19+08:00"},
                                                          # 纯待办：无日程时间
        {"id": "t3", "title": "笔记", "date": "2026-11-05"},  # date 语义键可回落
    ]}
    items = data_ingest._extract_items(payload)
    by_title = {it["title"]: it for it in items}
    assert by_title["BBB"]["time"] == "2026-11-05T15:00:00"   # dueDate 胜出
    assert by_title["AAA"]["time"] is None                     # createdTime 不算
    assert by_title["笔记"]["time"] is not None
