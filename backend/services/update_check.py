"""版本更新检查服务（关于页「检查更新」按钮，2026-11-06 批 A）。

GitHub Releases API 探测最新发布版。出网走 Python 边车而非前端 fetch——与
weather.py 的 trust_env=False 铁律同源（本机代理会拦外网请求），且可测试
（monkeypatch _fetch_release 替身，test_weather 模式）。

永不抛出：仓库尚未上传 GitHub 是当前预期态（404 归「暂无发布版本」），
网络失败 / 限流 / 响应异常一律归 {available: False, reason}，不向前端抛 5xx。
版本比较在前端做（前端持 getVersion() 真源；后端不知道应用版本号）。
"""

import logging

import httpx

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/HatBoy/Clara/releases/latest"
_TIMEOUT_S = 8.0


def _fetch_release() -> tuple[int, dict]:
    """单层 GET → (状态码, JSON 体)。网络层错误以异常上抛，由调用方统一归口。"""
    with httpx.Client(timeout=_TIMEOUT_S, trust_env=False) as client:
        r = client.get(RELEASES_API)
        return r.status_code, r.json()


def latest_release() -> dict:
    """探测最新 Release：成功 {available, tag, html_url}；任何失败
    {available: False, reason: 中文文案}。永不抛出。"""
    try:
        status, body = _fetch_release()
    except (httpx.HTTPError, ValueError):
        logger.warning("更新检查外呼失败", exc_info=True)
        return {"available": False, "reason": "网络连接失败，请稍后再试"}
    if status == 404:
        # 仓库未上传 / 无任何 Release——当前阶段的预期态，不算错误
        return {"available": False, "reason": "暂无发布版本（仓库无 Release 记录）"}
    if status != 200:
        return {"available": False, "reason": f"检查失败（HTTP {status}）"}
    tag = body.get("tag_name") or ""
    if not tag:
        return {"available": False, "reason": "响应缺少版本号"}
    return {"available": True, "tag": tag, "html_url": body.get("html_url") or ""}
