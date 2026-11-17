"""WebDAV 客户端（2026-11-14 同步备份）：httpx 实现的标准化客户端，零 DB 依赖
（httpx.MockTransport 可直测，全项目唯一 HTTP 客户端无需新依赖）。

兼容性要点（各服务实测坑，改客户端前先读）：
- Basic Auth 头手工 base64(utf-8) 构造——httpx 内置 BasicAuth 按 latin-1 编码，
  坚果云/Nextcloud 的非 ASCII 密码会直接 UnicodeEncodeError；
- trust_env=False 铁律（同 services/weather.py）：本机代理会拦掉对外请求；
- 207 多播响应按 local-name 匹配 XML 元素（strip namespace，Nextcloud 常带
  多个命名空间）；href 兼容相对/绝对 URL（Nextcloud 返回绝对地址）；
- MKCOL 405 = 目录已存在（Alist/部分实现），放行；403 = 无写权限；
- 首推 manifest 带 If-None-Match: *（仅当远端不存在才写入）——双端同时首同步
  的竞态下后到者收 412 转并集合并，杜绝互相覆盖丢数据；不支持该头的服务器
  忽略之，退化为无守卫行为无害；
- 表文件 GET/PUT 60s 超时（大表 MB 级 JSON），其余 30s。
"""

import base64
import logging
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

# 单个表文件响应体上限（200MB）：损坏/恶意大文件拉爆内存的防御
MAX_TABLE_BYTES = 200 * 1024 * 1024


class WebDavError(RuntimeError):
    """WebDAV 操作失败（中文 message 直显前端 hint，对齐 update_check 风格）。
    status 携带 HTTP 状态码（网络层异常为 None）——调用方据此识别 429/503
    限频做长退避（坚果云限频窗口是分钟级，短重试只会反复撞窗）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _local_name(tag: str) -> str:
    """XML 元素 local-name（strip 命名空间前缀，如 '{DAV:}href' → 'href'）。"""
    return tag.rsplit("}", 1)[-1]


class WebDavClient:
    """单次同步生命周期内使用的 WebDAV 客户端（close 随轮调用，连接复用）。"""

    def __init__(self, url: str, username: str | None, password: str | None,
                 timeout: httpx.Timeout | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._base = url.rstrip("/")
        # Authorization 头手工构造：utf-8 编码 + 手工 Basic 头（见模块头兼容性注）
        self._headers: dict[str, str] = {}
        if username:
            raw = f"{username}:{password or ''}".encode("utf-8")
            self._headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        self._timeout = timeout or httpx.Timeout(30.0, connect=8.0)
        self._file_timeout = httpx.Timeout(60.0, connect=8.0)
        # 连接复用（2026-11-17 二轮批 1）：旧版每请求新建 Client——大库一轮
        # 22+ 次 TCP+TLS 握手（各 100-300ms），叠加坚果云限频治理目标。
        # 惰性持有随轮关闭（sync_engine._sync_once finally 调 close）
        self._client: httpx.Client | None = None
        self._transport = transport  # 测试注入（httpx.MockTransport）；生产恒 None

    def _client_instance(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(trust_env=False, transport=self._transport)
        return self._client

    def close(self) -> None:
        """随同步轮关闭复用连接（未初始化时零动作）。"""
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _request(self, method: str, path: str, *, headers: dict | None = None,
                 content: bytes | None = None, timeout: httpx.Timeout | None = None) -> httpx.Response:
        """单请求 + 限频重试：503/429（坚果云等免费服务的频率限制，2026-11-16
        真机实测：一轮 22 表全量 PUT 即可触发）短退避重试至多 2 次——限频是
        暂态，立刻失败会让整轮同步作废且下一轮（退避后）又撞同一窗口。"""
        import time

        url = f"{self._base}{path}"
        merged = {**self._headers, **(headers or {})}
        last: httpx.Response | None = None
        for attempt in range(3):  # 首发 + 2 次重试
            if attempt:
                time.sleep(1.5 * attempt)
            try:
                resp = self._client_instance().request(
                    method, url, headers=merged, content=content,
                    timeout=timeout or self._timeout)
            except httpx.HTTPError as e:
                raise WebDavError(f"WebDAV 请求失败（{method} {path}）：{e}") from e
            if resp.status_code not in (429, 503):
                return resp
            last = resp
        return last  # 重试仍限频：按原语义上抛给调用方判状态码

    def _propfind(self, path: str, depth: str) -> httpx.Response:
        return self._request("PROPFIND", path, headers={"Depth": depth})

    # —— 对外方法 ——————————————————————————————————————————————

    def test_connection(self) -> tuple[bool, int, str]:
        """连通性 + 可写性探测：PROPFIND 根（可连）→ MKCOL 同步目录（可写；
        405=已存在放行，403=无写权限）。返回 (ok, ms, message)。"""
        import time

        start = time.perf_counter()
        try:
            resp = self._propfind("/", "0")
            ms = int((time.perf_counter() - start) * 1000)
            if resp.status_code == 401:
                return False, ms, "认证失败：用户名或密码错误"
            if resp.status_code == 403:
                return False, ms, "无访问权限（403）"
            if resp.status_code >= 400:
                return False, ms, f"服务器响应异常（HTTP {resp.status_code}）"
            # 根可连，再探同步目录可写性
            mk = self._request("MKCOL", "/clara-sync")
            if mk.status_code == 403:
                return False, ms, "可连接但无写入权限（403），请检查账号权限"
            if mk.status_code in (429, 503):
                # 限频不是目录问题（二轮批 1 文案修正：旧版「无法创建同步目录
                # （HTTP 429）」误导排障方向）
                return False, ms, f"触发服务器限频（HTTP {mk.status_code}），请稍后再试"
            if mk.status_code >= 400 and mk.status_code not in (405, 201, 200):
                return False, ms, f"无法创建同步目录（HTTP {mk.status_code}）"
            return True, ms, "连接成功，可读写"
        except WebDavError as e:
            ms = int((time.perf_counter() - start) * 1000)
            return False, ms, str(e)

    def ensure_dirs(self) -> None:
        """确保 /clara-sync 与 /clara-sync/tables 存在（不存在则 MKCOL；
        405/200 = 已存在放行，其余抛 WebDavError）。"""
        for path in ("/clara-sync", "/clara-sync/tables"):
            resp = self._request("MKCOL", path)
            if resp.status_code in (200, 201, 405):
                continue
            if resp.status_code == 401:
                raise WebDavError("WebDAV 认证失败：用户名或密码错误", status=401)
            raise WebDavError(f"创建同步目录失败（{path}，HTTP {resp.status_code}）", status=resp.status_code)

    def fetch_manifest(self) -> dict | None:
        """拉取 manifest；404（从未同步过）→ None；解析失败抛 WebDavError
        （调用方守卫 a 依赖此异常中止同步——防损坏 manifest 被当空库处理）。"""
        resp = self._request("GET", "/clara-sync/manifest.json")
        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise WebDavError("WebDAV 认证失败：用户名或密码错误", status=401)
        if resp.status_code >= 400:
            raise WebDavError(f"拉取 manifest 失败（HTTP {resp.status_code}）", status=resp.status_code)
        import json

        try:
            data = json.loads(resp.content)
        except ValueError as e:
            raise WebDavError(f"manifest 损坏无法解析：{e}") from e
        if not isinstance(data, dict):
            raise WebDavError("manifest 格式异常（非对象）")
        return data

    def fetch_table(self, name: str) -> dict | None:
        """拉取单表数据文件；404（远端从未写过该文件）→ None，由调用方区分
        「远端空表」与「manifest 有记录但文件被删」的表级守卫语义；解析失败抛
        WebDavError（整轮中止，防半损坏数据进合并）。"""
        import json

        resp = self._request("GET", f"/clara-sync/tables/{name}.json",
                             timeout=self._file_timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise WebDavError("WebDAV 认证失败：用户名或密码错误", status=401)
        if resp.status_code >= 400:
            raise WebDavError(f"拉取表文件失败（{name}，HTTP {resp.status_code}）", status=resp.status_code)
        if len(resp.content) > MAX_TABLE_BYTES:
            raise WebDavError(
                f"远端表文件超限（{name}，{len(resp.content) // 1048576}MB > 200MB）",
                status=resp.status_code)
        try:
            data = json.loads(resp.content)
        except ValueError as e:
            raise WebDavError(f"远端表文件损坏（{name}）：{e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise WebDavError(f"远端表文件格式异常（{name}）")
        return data

    def push_table(self, name: str, payload: dict) -> None:
        """推送单表数据文件（PUT，全量快照语义）。"""
        import json

        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = self._request("PUT", f"/clara-sync/tables/{name}.json",
                             headers={"Content-Type": "application/json"},
                             content=content, timeout=self._file_timeout)
        if resp.status_code >= 400:
            raise WebDavError(f"推送表文件失败（{name}，HTTP {resp.status_code}）", status=resp.status_code)

    def put_readme(self, content: str) -> None:
        """推送「使用说明.txt」到同步根（2026-11-14 真机批：用户打开 WebDAV
        目录就能知道这些 json 是什么、如何手动恢复——回答「数据在哪里」）。
        仅首同步时调用一次（manifest 404 = 从未同步过），不参与每轮同步。"""
        resp = self._request("PUT", "/clara-sync/使用说明.txt",
                             headers={"Content-Type": "text/plain; charset=utf-8"},
                             content=content.encode("utf-8"))
        if resp.status_code >= 400:
            # 非致命：说明文件缺失不影响同步语义（部分服务器对中文文件名兼容差）
            logger.warning("使用说明推送失败（HTTP %s）", resp.status_code)

    def push_manifest(self, manifest: dict, *, if_none_match: bool = False) -> None:
        """推送 manifest（提交点：推序铁律保证它滞后于全部表文件）。首次推送
        带 If-None-Match: * —— 竞态下远端已被对端先写则收 412，调用方转并集
        合并重走（不支持该头的服务器忽略，退化为无守卫覆盖）。"""
        import json

        headers = {"Content-Type": "application/json"}
        if if_none_match:
            headers["If-None-Match"] = "*"
        content = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        resp = self._request("PUT", "/clara-sync/manifest.json", headers=headers,
                             content=content, timeout=self._file_timeout)
        if resp.status_code == 412:
            raise WebDavError("__PRECONDITION_412__")  # 内部信号：调用方捕获转 Case C
        if resp.status_code >= 400:
            raise WebDavError(f"推送 manifest 失败（HTTP {resp.status_code}）", status=resp.status_code)


def parse_propfind_hrefs(xml_bytes: bytes) -> list[str]:
    """PROPFIND Depth:1 响应解析：抽全部 d:href（按 local-name 匹配，兼容多
    命名空间）；文本内容原样（相对/绝对路径由调用方归一）。列目录能力备而
    未用（manifest.hash 已覆盖增量判定），保留供排查工具使用。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise WebDavError(f"PROPFIND 响应解析失败：{e}") from e
    hrefs: list[str] = []
    for el in root.iter():
        if _local_name(el.tag) == "href" and el.text:
            hrefs.append(el.text.strip())
    return hrefs
