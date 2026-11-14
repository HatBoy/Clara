"""关于页检查更新测试（routers/about.py + services/update_check.py）。

备份导出/恢复用例已于 2026-11-14 同步备份迁至 tests/test_sync.py（端点
迁 /api/sync/*；service 层 services/backup.py 不变）。
"""

import pytest

from services import update_check


# —— 检查更新（services/update_check.py；网络替身 _fetch_release，零真实外呼）——


def test_update_check_ok(monkeypatch):
    monkeypatch.setattr(
        update_check, "_fetch_release", lambda: (200, {"tag_name": "v1.0.1", "html_url": "u"})
    )
    out = update_check.latest_release()
    assert out == {"available": True, "tag": "v1.0.1", "html_url": "u"}


def test_update_check_404_is_expected_state(monkeypatch):
    """仓库未上传 / 无 Release——预期态而非错误。"""
    monkeypatch.setattr(update_check, "_fetch_release", lambda: (404, {}))
    out = update_check.latest_release()
    assert out["available"] is False
    assert "暂无发布" in out["reason"]


def test_update_check_network_error(monkeypatch):
    import httpx

    def boom():
        raise httpx.ConnectError("网络不可达（测试注入）")

    monkeypatch.setattr(update_check, "_fetch_release", boom)
    out = update_check.latest_release()
    assert out["available"] is False
    assert "网络" in out["reason"]


def test_update_check_missing_tag(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_release", lambda: (200, {"html_url": "u"}))
    out = update_check.latest_release()
    assert out["available"] is False
    assert "版本号" in out["reason"]
