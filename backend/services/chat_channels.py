"""聊天通讯渠道管理（2026-10-23 增，「设置 → 聊天通讯」）：飞书/钉钉/企微/微信接入。

两部分（docs/chat-comm.md）：
- 渠道配置层（批 4）：四渠道目录（单一事实源；wechat 扫码形态不收粘贴凭据）
  + 凭据 CRUD + 凭据探测（app_id/app_secret 换平台 access token，只验凭据零
  副作用；飞书 tenant_access_token/internal、钉钉 v1.0/getAccessToken、企微
  ws 订阅一次；结果落 conn_* 三列）。
- SDK worker 层（批 5）：每渠道一个 daemon 线程跑官方长连接（飞书 lark-oapi
  ws.Client.start() 阻塞 / 钉钉 dingtalk-stream start_forever 自建 loop / 企微
  aibot WSClient 手动 loop 驱动 + 互踢防线；微信不用 SDK——iLink 协议纯同步
  httpx 长轮询 hold 35s，context_token 逐条刷新缓存，-14 过期即退），回调
  过滤链（单聊 → 防自回环 → 仅文本 → 截断）+ message_id 去重后经
  run_coroutine_threadsafe 投递主 FastAPI 循环跑 handle_incoming（远程对话轮，
  与桌面 SSE 轮共用 chat_turn.TURN_LOCK 与全链管道——agents 逻辑零改动）。

worker 生命周期：lark ws 无可靠 stop——invalidate() 全量重启靠「代际（epoch）
守卫」让旧连接回调直接失活（残活线程 daemon 随进程退出）；_MAIN_LOOP 未设
（测试/纯 CRUD 环境）时重启 no-op，测试注入 FakeFactory 零 SDK 依赖。

死锁三禁（SDK 回调线程 ↔ 主循环，docs/chat-comm.md）：
① 回调内不 .result() 等待投递 future；② 主循环不 join worker 线程；
③ to_thread 工作线程内不 create_task 不碰 asyncio 原语。
"""

import asyncio
import base64
import json
import logging
import os
import re
import struct
import threading
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ChatChannel

logger = logging.getLogger(__name__)

# 探测/收发 HTTP 直连（trust_env=False）：飞书/钉钉是国内服务，环境代理
# （HTTP_PROXY 等）反而拦截直连（mcp_servers 2026-09-20 Obsidian 排障同坑）
_PROBE_TIMEOUT = 10.0

# 三渠道目录（GET /api/chat-channels 的单一事实源；顺序即前端展示序）
CHANNEL_CATALOG: list[dict] = [
    {
        "provider": "feishu",
        "name": "飞书",
        "desc": "开放平台自建应用机器人，长连接收发消息",
        "setup_url": "https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive",
        "placeholder": False,
    },
    {
        "provider": "dingtalk",
        "name": "钉钉",
        "desc": "企业内部应用机器人，Stream 模式收发消息",
        "setup_url": "https://open.dingtalk.com/document/orgapp/the-creation-and-acquisition-of-the-corporate-internal-app",
        "placeholder": False,
    },
    {
        "provider": "wecom",
        "name": "企业微信",
        "desc": "智能机器人 API 模式长连接，收发消息",
        "setup_url": "https://developer.work.weixin.qq.com/document/path/101463",
        "placeholder": False,
    },
    {
        "provider": "wechat",
        "name": "微信",
        "desc": "官方 ClawBot 插件，扫码授权接入个人微信",
        "setup_url": "https://docs.openclaw.ai/zh-CN/channels/wechat",
        "placeholder": False,
    },
]

# 可配置渠道（目录内全部四渠道；wechat 虽正式但凭据是扫码形态，
# upsert_channel 显式挡 422——扫码链见登录端点）
_CONFIGURABLE = {p["provider"] for p in CHANNEL_CATALOG if not p["placeholder"]}


def _post_json(url: str, payload: dict) -> httpx.Response:
    """同步 POST JSON 小壳——独立成函数是测试替身锚点（monkeypatch 本函数注入
    FakeResponse，不碰 httpx 内部）。直连不走环境代理（见 _PROBE_TIMEOUT 注释）。"""
    with httpx.Client(trust_env=False, timeout=_PROBE_TIMEOUT) as client:
        return client.post(url, json=payload)


def _wecom_probe(bot_id: str, secret: str) -> tuple[bool, str | None]:
    """企微智能机器人凭据探测：SDK WSClient 建连 + aibot_subscribe 认证一次。

    长连接模式无独立 HTTP token 端点（官方文档 101463）——探测 = 建连发订阅帧
    看认证响应：errcode==0 触发 SDK "authenticated" 事件；errcode!=0 只 "error"
    不断连不重连（连接空挂，aibot 1.0.2 ws.py 实装核验）→ finally 主动
    disconnect 收尾（_is_manual_close 拦掉重连调度）。打桩锚点（测试替换本函数
    零网络零 SDK）。

    互踢窗口：同一 bot 只允许一个长连接，probe 建连会踢掉在跑 worker——调用点
    （保存/测试）落库后 _notify_config_changed 全量重启，新 worker 顶回；秒级
    窗口内至多丢一条消息（tech-debt 登记）。"""
    import asyncio

    state: dict = {}

    async def _run() -> None:
        from aibot import WSClient, WSClientOptions

        done = asyncio.Event()

        def _on_authenticated() -> None:
            state["ok"] = True
            done.set()

        def _on_error(err: Exception) -> None:
            if "ok" not in state:  # 首个结论为准（心跳等后续错误不覆盖认证结论）
                state["error"] = str(err)
                done.set()

        client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret))
        client.on("authenticated", _on_authenticated)
        client.on("error", _on_error)
        try:
            await asyncio.wait_for(client.connect(), timeout=_PROBE_TIMEOUT)
            await asyncio.wait_for(done.wait(), timeout=_PROBE_TIMEOUT)
        finally:
            try:
                client.disconnect()
                await asyncio.sleep(0.2)  # 给 ensure_future 的异步收尾留时间
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except Exception:  # 网络层异常（含超时）统一归连接失败，不裸抛；固定中文文案供前端归类
        return False, "无法连接企业微信：网络不可达或被代理/防火墙拦截（检查本机网络）"[:200]
    if state.get("ok"):
        return True, None
    return False, f"企业微信凭据校验失败：{state.get('error') or '认证超时'}"[:200]


def probe_channel(provider: str, app_id: str, app_secret: str) -> tuple[bool, str | None]:
    """凭据探测（纯网络调用不碰库）：成功返回 (True, None)，失败 (False, 中文摘要)。

    摘要只取平台错误文案 + 错误码，截 200 字符（列宽）；网络层异常归「无法连接
    开放平台」（固定中文分类文案，不带 httpx 类型名——设置页徽标靠「无法连接」
    前缀归类为网络故障，与凭据错误区分，2026-11-05 网关 DNS 劫持排障定案）。
    文案给设置页展示与排障用，不进任何提示词。"""
    try:
        if provider == "feishu":
            resp = _post_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 0:
                return True, None
            detail = data.get("msg") or data.get("message") or f"HTTP {resp.status_code}"
            code = data.get("code")
            return False, f"飞书凭据校验失败：{detail}（code={code}）"[:200]
        if provider == "dingtalk":
            resp = _post_json(
                "https://oapi.dingtalk.com/v1.0/getAccessToken",
                {"appKey": app_id, "appSecret": app_secret},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("accessToken"):
                return True, None
            # v1.0 错误体：message 为主文案；部分版本键名不同，全兜
            detail = data.get("message") or data.get("msg") or f"HTTP {resp.status_code}"
            code = data.get("code", "")
            return False, f"钉钉凭据校验失败：{detail}（{code}）"[:200]
        if provider == "wecom":
            return _wecom_probe(app_id, app_secret)  # 文案在探内拼好（含渠道名）
        if provider == "wechat":
            # 行字段语义映射：app_id 列 = bot_token，app_secret 列 = baseurl
            return _wechat_probe(app_id, app_secret)
        return False, f"未知渠道：{provider}"
    except httpx.HTTPError:
        return False, "无法连接开放平台：网络不可达或被代理/防火墙拦截（检查本机网络）"[:200]


def get_channel(db: Session, provider: str) -> ChatChannel | None:
    return db.execute(
        select(ChatChannel).where(ChatChannel.provider == provider)
    ).scalar_one_or_none()


def _row_to_out(row: ChatChannel | None, meta: dict) -> dict:
    """目录项 + 行状态合并。app_secret 原样回显（Composio key 先例——本地
    单用户明文定调，编辑表单回填 + 前端密码态/眼睛查看）。"""
    return {
        "provider": meta["provider"],
        "name": meta["name"],
        "desc": meta["desc"],
        "setup_url": meta["setup_url"],
        "placeholder": meta["placeholder"],
        "configured": row is not None,
        "app_id": row.app_id if row else None,
        "app_secret": row.app_secret if row else None,
        "enabled": row.enabled if row else False,
        "push_enabled": row.push_enabled if row else False,
        "push_ready": bool(row.push_target) if row else False,
        "conn_ok": row.conn_ok if row else None,
        "conn_error": row.conn_error if row else None,
        "conn_at": row.conn_at if row else None,
    }


def _meta_of(provider: str) -> dict:
    """目录元数据查找；provider 不在目录（脏库行）回落最小占位——不 500，
    正常路径 upsert/test 已闸在目录内。"""
    for meta in CHANNEL_CATALOG:
        if meta["provider"] == provider:
            return meta
    return {"provider": provider, "name": provider, "desc": "", "setup_url": None,
            "placeholder": True}


def describe_channel(db: Session, provider: str) -> dict:
    """单渠道目录 + 行状态合并（router 单行端点的统一出口）。"""
    return _row_to_out(get_channel(db, provider), _meta_of(provider))


def list_channels(db: Session) -> list[dict]:
    """三渠道目录合并接入行（固定三行顺序稳定；wechat 恒 configured=False）。"""
    rows = {r.provider: r for r in db.execute(select(ChatChannel)).scalars()}
    return [_row_to_out(rows.get(m["provider"]), m) for m in CHANNEL_CATALOG]


def _write_probe_result(db: Session, row: ChatChannel, ok: bool, error: str | None) -> None:
    row.conn_ok = ok
    row.conn_error = error
    row.conn_at = datetime.now()
    db.commit()


def upsert_channel(db: Session, provider: str, app_id: str, app_secret: str) -> tuple[ChatChannel, bool]:
    """保存/更新凭据并立即探测（保存即验证——错误凭据当场暴露，不留「看起来
    已连接」的假象）。编辑时 app_secret 留空 = 保持旧值（前端已回显旧值，此
    分支仅兜底手改请求）。返回 (行, 探测是否成功)。ValueError → 422 由 router 翻译。"""
    if provider not in _CONFIGURABLE:
        raise ValueError(f"未知渠道：{provider}")
    if provider == "wechat":
        raise ValueError("微信为扫码接入，无需粘贴凭据（点击卡片上的扫码按钮）")
    row = get_channel(db, provider)
    secret = app_secret.strip()
    if row is None:
        if not app_id.strip() or not secret:
            raise ValueError("App ID 与 App Secret 均不能为空")
        row = ChatChannel(provider=provider, app_id=app_id.strip(), app_secret=secret)
        db.add(row)
    else:
        if not app_id.strip():
            raise ValueError("App ID 不能为空")
        row.app_id = app_id.strip()
        if secret:
            row.app_secret = secret  # 留空 = 沿用旧 secret（编辑形态）
    db.commit()
    ok, error = probe_channel(provider, row.app_id, row.app_secret)
    _write_probe_result(db, row, ok, error)
    logger.info("[chat-channel] %s 凭据已保存，探测%s", provider, "通过" if ok else f"失败：{error}")
    _notify_config_changed()
    return row, ok


def test_channel(db: Session, provider: str) -> tuple[ChatChannel, bool]:
    """「测试」按钮：对已存凭据重新探测并回写。未配置由 router 翻 404。"""
    row = get_channel(db, provider)
    if row is None:
        raise LookupError(provider)
    ok, error = probe_channel(provider, row.app_id, row.app_secret)
    _write_probe_result(db, row, ok, error)
    _notify_config_changed()
    return row, ok


def set_enabled(db: Session, provider: str, enabled: bool) -> ChatChannel:
    """启停（False = worker 不监听该渠道；行保留凭据与探测史）。批 5 挂 worker
    invalidate——此处只落库。未配置由 router 翻 404。"""
    row = get_channel(db, provider)
    if row is None:
        raise LookupError(provider)
    row.enabled = enabled
    db.commit()
    logger.info("[chat-channel] %s 已%s", provider, "启用" if enabled else "停用")
    _notify_config_changed()
    return row


def set_push_enabled(db: Session, provider: str, enabled: bool) -> ChatChannel:
    """定时任务推送开关（只改 push_enabled，不动 worker 装载——推送与监听
    正交）。未配置由 router 翻 404（set_enabled 同款约定）。"""
    row = get_channel(db, provider)
    if row is None:
        raise LookupError(provider)
    row.push_enabled = enabled
    db.commit()
    logger.info("[chat-channel] %s 推送已%s", provider, "开启" if enabled else "关闭")
    return row


def delete_channel(db: Session, provider: str) -> None:
    """删除接入（硬删，无业务数据引用）。批 5 挂 worker invalidate。"""
    row = get_channel(db, provider)
    if row is None:
        raise LookupError(provider)
    db.delete(row)
    db.commit()
    logger.info("[chat-channel] %s 接入已删除", provider)
    _notify_config_changed()


# —— 微信 iLink 登录链（官方 ClawBot 插件；纯 httpx 自实现，零第三方 SDK） ————
# 协议（社区 wechatbot SDK protocol.py / auth.py 源码核验，2026-10-23）：
# 基址 https://ilinkai.weixin.qq.com，路径全带 /ilink/bot 前缀。登录链两 GET 只需
# 公共头 iLink-App-Id: bot + iLink-App-ClientVersion（uint32 编码版本串）；批 4 的
# 收发 POST 另带 AuthorizationType / Authorization: Bearer <token> / X-WECHAT-UIN
# （每次随机 u32 的 base64）+ Content-Type，body 带 base_info.channel_version。
_ILINK_BASE = "https://ilinkai.weixin.qq.com"
# 版本编码与社区 SDK 同构：(major<<16)|(minor<<8)|patch 进 ClientVersion 头；
# base_info.channel_version 用官方协议文档示例串 "2.0.0"（131072 = 2<<16）
_ILINK_CHANNEL_VERSION = "2.0.0"
_ILINK_CLIENT_VERSION = str(2 << 16)
# 登录票据（内存态：扫码是分钟级短交互，进程重启重扫即可，不落库）
_wechat_logins: dict[str, dict] = {}
_LOGIN_TTL = 300.0  # 票据有效期（秒）；过期票按 expired 语义应答，前端重新出码


def _ilink_headers(token: str | None = None) -> dict[str, str]:
    """iLink 请求头：登录链 GET 只需公共头；token 提供时为收发 POST 的认证头
    全集（X-WECHAT-UIN 每次随机 u32 base64——服务端只校验存在性，SDK 同构）。"""
    base = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": _ILINK_CLIENT_VERSION}
    if token is None:
        return base
    uin = base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode()).decode()
    return {**base, "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": uin}


def _ilink_check(resp: httpx.Response, label: str) -> dict:
    """iLink 响应统一校验（对齐社区 SDK _parse_response）：HTTP>=400 或
    ret/errcode 非 0 抛 ValueError（中文文案直显扫码弹层）。"""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        raise ValueError(f"{label}失败：{data.get('errmsg') or f'HTTP {resp.status_code}'}")
    ret, errcode = data.get("ret"), data.get("errcode")
    if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
        raise ValueError(f"{label}失败：{data.get('errmsg') or f'ret={ret} errcode={errcode}'}")
    return data


def _ilink_get(base: str, path: str, timeout: float = _PROBE_TIMEOUT) -> dict:
    """登录链 GET 小壳（打桩锚点——测试 monkeypatch 此函数零网络依赖）。
    timeout 可调：get_qrcode_status 是 hold 式长轮询（服务端等状态变化才返回，
    与 getupdates 同模式——2026-10-23 真机实锤），轮询须传 ~40s 覆盖 35s hold。"""
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        return _ilink_check(client.get(f"{base}{path}", headers=_ilink_headers()), "微信登录")


def _ilink_post(base: str, token: str, path: str, body: dict,
                timeout: float = 45.0) -> dict:
    """收发 POST 小壳（批 4 worker 消费；打桩锚点同上）。base_info 自动注入；
    错误判定交调用方（worker 需按 errcode -14 识别会话过期，不能在此统一抛）。"""
    payload = {**body, "base_info": {"channel_version": _ILINK_CHANNEL_VERSION}}
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        resp = client.post(f"{base}{path}", json=payload, headers=_ilink_headers(token))
    try:
        return resp.json()
    except ValueError:
        return {"errcode": -1, "errmsg": f"响应非 JSON（HTTP {resp.status_code}）"}


def start_wechat_login() -> dict:
    """发起扫码登录：get_bot_qrcode（bot_type=3 个人微信）出码，票据进内存态。
    qrcode_img_content 是 URL 字符串（协议核验），前端渲染成二维码供手机微信扫。
    每次都是全新会话（不带 local_token_list——该 bot 已绑定其他客户端时服务端
    答 binded_redirect，轮询时透传提示解绑）。"""
    qr = _ilink_get(_ILINK_BASE, "/ilink/bot/get_bot_qrcode?bot_type=3")
    code, img = qr.get("qrcode") or "", qr.get("qrcode_img_content") or ""
    if not code:
        raise ValueError(f"微信二维码响应异常：{json.dumps(qr, ensure_ascii=False)[:200]}")
    ticket = uuid4().hex
    _wechat_logins[ticket] = {"qrcode": code, "qr_url": img, "poll_base": _ILINK_BASE,
                              "created": time.time()}
    return {"ticket": ticket, "qr_url": img}


def poll_wechat_login(db: Session, ticket: str,
                      verify_code: str | None = None) -> dict:
    """轮询扫码状态（前端 2s 一拍）。状态机（SDK auth.py 核验）：
    wait/scaned 透传；confirmed → bot_token/baseurl 落库（行字段语义映射零迁移：
    app_id 列 = bot_token，app_secret 列 = baseurl）+ conn_ok=True + 启用 +
    _notify_config_changed 拉起 worker，票据消费即清；expired / need_verifycode
    （配对码挑战：前端输入后经 verify_code 参数带回重轮）/ verify_code_blocked
    （配对码错太多次，重新出码）/ binded_redirect（已绑定其他客户端）透传前端
    文案映射；scaned_but_redirect 为 IDC 重定向——跟随 redirect_host 换轮询基址，
    票据内消化，前端按 scaned 显示无感。"""
    entry = _wechat_logins.get(ticket)
    if entry is None or time.time() - entry["created"] > _LOGIN_TTL:
        _wechat_logins.pop(ticket, None)
        return {"status": "expired"}
    path = f"/ilink/bot/get_qrcode_status?qrcode={quote(entry['qrcode'], safe='')}"
    if verify_code:
        path += f"&verify_code={quote(verify_code, safe='')}"
    try:
        # hold 式长轮询：40s 覆盖服务端 35s hold，状态变化即返回（推送形态）；
        # hold 满仍未变化（理论不达）归 wait，下一轮继续——绝不能 500 打断前端轮询
        data = _ilink_get(entry["poll_base"], path, timeout=40.0)
    except httpx.ReadTimeout:
        return {"status": "wait"}
    status = data.get("status") or "wait"
    if status == "scaned_but_redirect":
        host = data.get("redirect_host")
        if host:
            entry["poll_base"] = f"https://{host}"
        return {"status": "scaned"}
    if status == "confirmed":
        token, baseurl = data.get("bot_token") or "", data.get("baseurl") or _ILINK_BASE
        if not token:
            raise ValueError("微信登录确认但未返回 token（请重新扫码）")
        _wechat_logins.pop(ticket, None)
        row = get_channel(db, "wechat")
        if row is None:
            row = ChatChannel(provider="wechat", app_id=token, app_secret=baseurl)
            db.add(row)
        else:
            row.app_id, row.app_secret = token, baseurl
        row.enabled = True  # 扫码登录即接入意图，恒启用（停用是后来的显式决策）
        row.conn_ok = True
        row.conn_error = None
        row.conn_at = datetime.now()
        db.commit()
        logger.info("[chat-channel] 微信扫码登录成功，token 已落库")
        _notify_config_changed()
        return {"status": "confirmed"}
    return {"status": status}


# —— SDK worker 层（批 5）———————————————————————————————————————————————

# 主 FastAPI 事件循环（start_all 捕获；None = 未运行/测试环境，投递与重启 no-op）
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None
# 关闭标志（main lifespan 退出段置 True：不再启动新 worker，投递丢弃）
_closing = False
# 代际守卫：invalidate 全量重启时 +1，旧 worker 回调闭包捕获旧代即失活
# （lark ws 无可靠 stop，残活连接最多多发一轮旧凭据调用，直接丢弃安全——用户重发即可）
_epoch = 0
# 消息去重 LRU（SDK 可能重投同 message_id；cap 512 防无界增长），线程安全——
# worker 线程与主循环都可能触达
_seen: OrderedDict[str, None] = OrderedDict()
_seen_lock = threading.Lock()
_SEEN_CAP = 512
# 渠道消息与回复的长度闸（平台消息体上限约万级，markdown 渲染后更肥；4000 与
# 提示词侧输入截断同档）
_MAX_TEXT = 4000
# 微信 context_token 会话缓存（user_id → 最新令牌）：worker 逐条刷新维护，
# 回帖闭包与定时推送共用（24h 无对话即过期——推送尽力而为，跳过不重试）
_wechat_ctx_tokens: dict[str, str] = {}
# 推送目标缓存（provider → JSON 串）：worker 落库前比对，目标不变跳过写库
_push_targets: dict[str, str] = {}

# 渠道 → worker 线程体（_restart_workers 经此派发；测试 monkeypatch 注入 Fake）
_WORKER_FACTORIES: dict = {}


def _notify_config_changed() -> None:
    """配置写入（保存/重测/启停/删除）后的 worker 重启挂点：epoch+1 全量重启。
    _MAIN_LOOP 未设（测试 / start_all 未跑）时 no-op——纯 CRUD 环境零线程。"""
    if _MAIN_LOOP is not None and not _closing:
        _restart_workers()


def _dedup(message_id: str) -> bool:
    """首见放行 True，重投 False；LRU cap 512（最旧的被逐出后允许再次出现——
    超长窗口的重复只在攻击级重放下可能漏，可接受）。"""
    with _seen_lock:
        if message_id in _seen:
            _seen.move_to_end(message_id)
            return False
        _seen[message_id] = None
        while len(_seen) > _SEEN_CAP:
            _seen.popitem(last=False)
        return True


def render_channel_markdown(text: str, has_cards: bool = False) -> str:
    """回复文本 → 渠道 markdown（纯函数，测试直测）：4000 字截断 + 尾注；本轮
    产出建议卡时附脚注（草案卡只在桌面端有采纳按钮，渠道内引导继续对话调整；
    脚注不落库——桌面/渠道消息内容一致，见 docs/chat-comm.md）。"""
    body = text.strip() or "（助手本轮没有文字回复，请到桌面端查看建议卡片）"
    if len(body) > _MAX_TEXT:
        body = body[:_MAX_TEXT] + "…（已截断，完整内容见桌面端）"
    if has_cards:
        body += "\n\n---\n（草案/建议卡可在桌面端查看采纳，或继续对话调整）"
    return body


def _channel_cards_text(events: list[dict]) -> str:
    """远程轮建议卡 → 渠道文本化（2026-10-23 批 2）：draft/delete 卡在渠道没有
    按钮，把条目内容和对话确认指引拼进回复（图头 confirm_shortcut 消费——
    回「确认/算了」即采纳/撤下，与桌面点卡片等价）。applied 已落库无需引导；
    inbox/preflight 采纳语义在桌面端，走泛化尾注不在此处理。纯函数测试直测。"""
    parts: list[str] = []
    for e in events:
        if e.get("type") == "draft":
            lines = []
            for i, d in enumerate(e.get("draft_plan") or [], 1):
                title = str(d.get("title") or "").strip()
                if not title:
                    continue
                when = ""
                start = d.get("start")
                if isinstance(start, str) and len(start) >= 16:
                    try:
                        when = datetime.fromisoformat(start).strftime("%m-%d %H:%M")
                    except ValueError:
                        pass
                dur = d.get("duration")
                if isinstance(dur, int) and dur:
                    when += f"（{dur}分钟）"
                lines.append(f"{i}. {title} {when}".rstrip())
            if lines:
                parts.append("📋 待确认草案：\n" + "\n".join(lines)
                             + "\n\n回复「确认」写入日历；要调整直接说（如「体检改到下午3点」）；回复「算了」取消。")
        elif e.get("type") == "proposal" and e.get("kind") == "delete":
            titles = "、".join(
                str(t.get("title") or "").strip()
                for t in (e.get("payload") or {}).get("tasks") or [] if t.get("title"))
            if titles:
                parts.append(f"🗑 待删除：{titles}\n确认要删请回复「确认删除」，回复「算了」保留。")
    return "\n\n".join(parts)[:_MAX_TEXT]


def _append_user_sync(provider: str, text: str) -> None:
    """远程轮段1：用户消息即时落库（to_thread）。handle_incoming 落库后立即
    broadcast chat_sync——桌面端在轮次开始就显示远程用户消息，而不是等整轮
    跑完才一次性出现（2026-10-23 真机反馈的时序契约：飞书发消息 → 桌面显示
    → 处理 → 双端同时显示回复）。"""
    from database import SessionLocal
    from services import ai_data

    with SessionLocal() as db:
        ai_data.append_message(db, role="user", content=text, source=provider)
        logger.info("[chat-channel] %s 新对话轮：%s", provider, text[:60])


def _run_turn_sync(provider: str, text: str, graph) -> tuple[list[dict], str]:
    """远程轮段2主体（to_thread 工作线程跑；死锁三禁③：不 create_task 不碰
    asyncio 原语）：user 消息已由 _append_user_sync 落库——与桌面 SSE 轮同构
    （桌面壳也是 invoke 前 append，节点读历史口径一致零漂移）。非流式
    invoke——远程渠道无打字机，半截 JSON 本就无法消费。source 透传 user 与
    assistant 两侧（finalize 内部写 assistant 消息）。"""
    from database import SessionLocal
    from services import chat_turn

    with SessionLocal() as db:
        state = chat_turn.build_initial_state(db, text)
        final = graph().invoke(state)
        events = chat_turn.finalize_turn(db, final, source=provider)
        reply_text = next(
            (e.get("content") for e in events if e.get("type") == "reply"), "")
        return events, reply_text


# 渠道侧无助手可用时的引导文案（快查三连的渠道镜像，语义同桌面 error 帧但口语化）
_DISABLED_HINT = "AI 功能当前已关闭，请到桌面端「设置 → 智能体」开启后再聊～"
_NO_MODEL_HINT = "还没有激活任何模型，请先到桌面端「设置 → 模型接入」激活模型～"
_NOT_READY_HINT = "日程助手尚未就绪（智能体管道未装配完成），请稍后再试～"
_TURN_ERROR_HINT = "助手这轮开小差了（运行异常），请稍后重试或到桌面端查看详情。"


async def handle_incoming(provider: str, text: str, reply, epoch: int | None = None) -> None:
    """远程渠道消息 → 对话轮（主 FastAPI 循环协程；_deliver 投递，测试直调）。

    reply: Callable[[str], None] 同步发送（渠道实现：飞书 im API / 钉钉
    session_webhook），统一在 to_thread 里调用避免阻塞主循环。快查三连失败/
    轮次异常都回渠道友好文案（不静默丢——用户在手机上等回复）。轮末
    broadcast chat_sync 通知桌面端刷新会话（前端早退，无提示音/通知）。
    """
    from database import SessionLocal
    from services import chat_turn, workflows
    from services.model_providers import get_active_model
    from services.settings import get_settings

    if epoch is not None and epoch != _epoch:
        return  # 旧代 worker 残活消息（双保险，_deliver 已查）

    # 快查三连（同桌面端 /chat 前置闸，毫秒级不进锁）
    with SessionLocal() as db:
        if not get_settings(db).ai_enabled:
            await asyncio.to_thread(reply, _DISABLED_HINT)
            return
        if get_active_model(db) is None:
            await asyncio.to_thread(reply, _NO_MODEL_HINT)
            return
    try:
        graph = chat_turn.load_graph()
    except (ImportError, AttributeError):
        await asyncio.to_thread(reply, _NOT_READY_HINT)
        return

    async with chat_turn.get_turn_lock():
        try:
            # 段1：用户消息即时落库并广播——桌面端立刻显示远程消息（不等整轮）
            await asyncio.to_thread(_append_user_sync, provider, text)
            workflows.broadcast({"type": "chat_sync", "provider": provider})
            # 段2：轮次主体 + finalize（与桌面轮同管道）
            events, reply_text = await asyncio.to_thread(
                _run_turn_sync, provider, text, graph)
        except Exception:
            logger.exception("[chat-channel] %s 轮次异常（输入：%s）", provider, text[:50])
            await asyncio.to_thread(reply, _TURN_ERROR_HINT)
            return

    # 卡片文本化（批 2）：draft/delete 卡拼条目+对话确认指引；其余卡（inbox/
    # preflight）保留泛化桌面引导尾注——两者互斥防重复（render_channel_markdown
    # 的 has_cards 仅在无具体卡片文本时生效）
    cards_text = _channel_cards_text(events)
    has_cards = any(e.get("type") in ("draft", "proposal", "applied") for e in events)
    body = render_channel_markdown(reply_text, has_cards and not cards_text)
    if cards_text:
        body = f"{body}\n\n{cards_text}" if reply_text.strip() else cards_text
    await asyncio.to_thread(reply, body)
    # 桌面端同步：chat_sync 前端早退只刷会话，不打提示音（用户正在手机聊）
    workflows.broadcast({"type": "chat_sync", "provider": provider})
    chat_turn.schedule_compaction()


def _deliver(provider: str, text: str, reply, epoch: int) -> None:
    """SDK 回调线程 → 主循环投递（死锁三禁①：run_coroutine_threadsafe 后不等待
    future，立即返回不阻塞 SDK 线程）。旧代失活 / 主循环未设 / 关闭中均直接
    丢弃。异常吞为日志——投递层故障不应炸穿 SDK 回调线程。"""
    if _closing or epoch != _epoch or _MAIN_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            handle_incoming(provider, text, reply), _MAIN_LOOP)
    except RuntimeError:
        logger.warning("[chat-channel] 主循环不可用，%s 消息丢弃", provider)


# —— 飞书 worker（lark-oapi ws 长连接；SDK 延迟 import） ——————————————————


def _feishu_extract(event) -> tuple[str, str, str] | tuple[str, None, None] | None:
    """事件对象 → (动作, 文本, open_id)。返回 None = 静默忽略（群聊/自回环/
    重投/字段缺失）；("hint", None, None) = 非文本消息（回引导文案）；("ok",
    text, open_id) = 进入对话轮。纯属性访问（测试用 SimpleNamespace 造对象，
    不依赖 SDK 类型）。"""
    msg = getattr(getattr(event, "event", None), "message", None)
    sender = getattr(getattr(event, "event", None), "sender", None)
    if msg is None or sender is None:
        return None
    if getattr(msg, "chat_type", "") != "p2p":
        return None  # 仅单聊（用户定案），群聊不响应
    if getattr(sender, "sender_type", "") == "app":
        return None  # 防自回环：自己发的消息不再进轮
    if not _dedup(getattr(msg, "message_id", "") or ""):
        return None
    if getattr(msg, "message_type", "") != "text":
        return ("hint", None, None)  # 图片/富文本等回引导
    try:
        text = (json.loads(getattr(msg, "content", "") or "{}").get("text") or "")
    except (json.JSONDecodeError, AttributeError):
        return None
    open_id = getattr(getattr(sender, "sender_id", None), "open_id", "")
    if not text.strip() or not open_id:
        return None
    return ("ok", text[:_MAX_TEXT], open_id)


def _feishu_send(app_id: str, app_secret: str, open_id: str, text: str) -> None:
    """im API 主动回复（同步，任意线程可调——to_thread 内执行）：interactive
    卡片单 markdown element（渠道端 md 渲染）；失败降级纯 text。
    发送失败只记日志（渠道侧无重试兜底，下一轮用户消息自然恢复）。

    SDK 形态（1.x 实装核验）：body 必须经 CreateMessageRequestBuilder.request_body()
    挂在 request 上、create(request) 单参调用——create 的第二参是 RequestOption，
    误传 body 会炸 option.headers 的 AttributeError（2026-10-23 真机修复）。"""
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequestBuilder,
            CreateMessageRequestBodyBuilder,
        )

        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        def _build(msg_type: str, content: dict):
            body = (CreateMessageRequestBodyBuilder()
                    .receive_id(open_id)
                    .msg_type(msg_type)
                    .content(json.dumps(content, ensure_ascii=False))
                    .build())
            return (CreateMessageRequestBuilder()
                    .receive_id_type("open_id")
                    .request_body(body)
                    .build())

        resp = client.im.v1.message.create(_build(
            "interactive", {"elements": [{"tag": "markdown", "content": text}]}))
        if not resp.success():
            logger.warning("[chat-channel] 飞书卡片发送失败（%s），尝试降级纯文本",
                           getattr(resp, "msg", ""))
            resp = client.im.v1.message.create(_build("text", {"text": text}))
            if not resp.success():
                logger.warning("[chat-channel] 飞书发送降级仍失败：%s",
                               getattr(resp, "msg", ""))
    except Exception:
        logger.exception("[chat-channel] 飞书发送异常")


def _feishu_worker(app_id: str, app_secret: str, epoch: int) -> None:
    """飞书长连接线程体：EventDispatcherHandler 注册消息事件，ws.Client.start()
    阻塞跑（SDK 内部自动重连）。回调同步短路径：提取过滤后立即 _deliver 返回。

    SDK 坑（2026-10-23 真机修复）：lark_oapi.ws.client 在**模块级**持有唯一
    asyncio loop（import 时创建），start() 直接 run_until_complete 该 loop——
    第一个 worker 跑起来后 loop 永久 running，之后任何重启（保存/测试/启停挂
    点）的新 worker 线程拿到同一个 running loop 必炸 "This event loop is
    already running"，飞书通道第二次起全灭。对策：每次 worker 启动前把模块级
    loop 覆写为本线程全新私有 loop（SDK 协程运行时解析模块名，覆写生效），
    同时 stop 旧 loop 让旧线程的 run_until_complete 即时抛错退出（不残活）。"""
    prev_loop = None
    my_loop = None
    try:
        import lark_oapi as lark
        import lark_oapi.ws.client as _ws_mod
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        prev_loop = getattr(_ws_mod, "loop", None)
        my_loop = asyncio.new_event_loop()
        _ws_mod.loop = my_loop
        asyncio.set_event_loop(my_loop)
        if prev_loop is not None and prev_loop.is_running():
            # 旧 worker 残活（卡在旧 loop 的 run_until_complete）：stop 让其
            # 抛 "Event loop stopped before Future completed" 退出线程（下方
            # 静默识别，随后收尾排空旧 loop）
            prev_loop.call_soon_threadsafe(prev_loop.stop)

        def _on_message(data: P2ImMessageReceiveV1) -> None:
            try:
                extracted = _feishu_extract(data)
                if extracted is None:
                    return
                action, text, open_id = extracted
                if action == "hint":
                    _feishu_send(app_id, app_secret, open_id,
                                 "暂只支持文字消息～请用文字和我说。")
                    return
                _save_push_target("feishu", {"open_id": open_id})
                _deliver("feishu", text,
                         lambda t: _feishu_send(app_id, app_secret, open_id, t),
                         epoch)
            except Exception:
                logger.exception("[chat-channel] 飞书回调异常")

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(_on_message)
                   # 已读回执（用户读了 bot 消息，平台默认推送）：不消费但必须
                   # 注册空处理器——否则 SDK 每条已读都打 ERROR
                   # "processor not found" 刷日志（2026-10-23 真机反馈）
                   .register_p2_im_message_message_read_v1(lambda _data: None)
                   # 进入单聊事件（用户点开与 bot 的聊天窗口时平台推送）：同上
                   # 不消费，仅注册空处理器止住 ERROR 噪音（2026-10-23 真机反馈）
                   .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(lambda _data: None)
                   .build())
        client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                                log_level=lark.LogLevel.WARNING)
        logger.info("[chat-channel] 飞书 worker 启动（epoch=%d）", epoch)
        client.start()  # 阻塞直到进程退出（残活经 stop 旧 loop 即时退出，见 docstring）
    except Exception as e:
        if "Event loop stopped before Future completed" in str(e):
            logger.info("[chat-channel] 飞书旧 worker 已停止（epoch=%d 被新代取代）", epoch)
            _drain_stopped_loop(my_loop)
            return
        logger.exception("[chat-channel] 飞书 worker 异常退出")


def _drain_stopped_loop(loop) -> None:
    """已 stop 的 loop 标准收尾（asyncio.run 同款序列）：cancel 全部 task →
    跑一遍迭代让各 task 的 finally 落地（ws 连接关闭 / SDK 缓存 cron 取消）→
    close。不做的话悬空 task 与 GC 期 __del__（ExpiringCache._cron.cancel 等）
    会在 loop 死后连环打 "Event loop is closed" / "Task was destroyed" 噪音
    （2026-10-23 真机反馈）。收尾尽力而为：任何异常不得盖过主流程。"""
    if loop is None:
        return
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5))
    except Exception:
        pass
    finally:
        loop.close()


# —— 钉钉 worker（dingtalk-stream；SDK 延迟 import） ——————————————————————


def _dingtalk_extract(msg) -> tuple[str, str, str] | tuple[str, None, None] | None:
    """ChatbotMessage → (动作, 文本, session_webhook)。语义同 _feishu_extract：
    None=忽略（群聊/重投/空文本）；("hint",…) = 非文本。钉钉机器人回复不回流
    回调（Stream 模式单向），无自回环过滤需求。字段名以 SDK 1.x 实测为准
    （ChatbotMessage.from_dict 把平台 camelCase 映射为 snake_case：msgtype →
    message_type、msgId → message_id；text 消息时 text 才被填充）。"""
    if getattr(msg, "conversation_type", "") != "1":
        return None  # "1" = 单聊（用户定案），群聊不响应
    if not _dedup(getattr(msg, "message_id", "") or ""):
        return None
    mtype = (getattr(msg, "message_type", "") or "").lower()
    if mtype and mtype != "text":
        return ("hint", None, None)
    text = (getattr(getattr(msg, "text", None), "content", "") or "").strip()
    webhook = getattr(msg, "session_webhook", "") or ""
    if not text or not webhook:
        return None
    return ("ok", text[:_MAX_TEXT], webhook)


def _dingtalk_send(session_webhook: str, text: str) -> None:
    """session_webhook 回帖（httpx 直连，90 分钟有效期内够用——单轮立即回复）：
    markdown 消息体；errcode != 0 记日志。"""
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            resp = client.post(session_webhook, json={
                "msgtype": "markdown",
                "markdown": {"title": "日程助手", "text": text},
            })
            data = resp.json()
            if data.get("errcode") not in (0, None):
                logger.warning("[chat-channel] 钉钉回帖失败：%s", data)
    except Exception:
        logger.exception("[chat-channel] 钉钉回帖异常")


def _dingtalk_worker(app_id: str, app_secret: str, epoch: int) -> None:
    """钉钉 Stream 线程体：自建事件循环跑 start_forever（SDK 自带断线重连）。
    handler 是 async（跑在 worker 自己的 loop 里）——只做提取过滤 + _deliver
    同步投递（run_coroutine_threadsafe 直达主循环，不在 worker loop 里等待）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from dingtalk_stream import ChatbotMessage, Credential, DingTalkStreamClient

        async def _on_message(_session, message: ChatbotMessage) -> None:
            try:
                extracted = _dingtalk_extract(message)
                if extracted is None:
                    return
                action, text, webhook = extracted
                if action == "hint":
                    _dingtalk_send(webhook, "暂只支持文字消息～请用文字和我说。")
                    return
                staff_id = getattr(message, "sender_staff_id", "") or ""
                if staff_id:
                    _save_push_target("dingtalk", {"staff_id": staff_id})
                _deliver("dingtalk", text,
                         lambda t: _dingtalk_send(webhook, t), epoch)
            except Exception:
                logger.exception("[chat-channel] 钉钉回调异常")

        client = DingTalkStreamClient(Credential(app_id, app_secret))
        client.register_callback_handler(ChatbotMessage.TOPIC, _on_message)
        logger.info("[chat-channel] 钉钉 worker 启动（epoch=%d）", epoch)
        loop.run_until_complete(client.start_forever())
    except Exception:
        logger.exception("[chat-channel] 钉钉 worker 异常退出")
    finally:
        loop.close()


# —— 企业微信 worker（aibot SDK ws 长连接；SDK 延迟 import） ——————————————————

# 上一代企微 worker 的 (client, loop)：新 worker 启动前主动断开旧连接——同一
# bot 只允许一个长连接，且被服务端踢断时 SDK 会自动重连反过来踢新 worker
# （死循环）；disconnect 置 _is_manual_close=True 可拦掉重连调度（ws.py 核验）
_wecom_prev: tuple | None = None


def _wecom_extract(frame: dict) -> tuple[str, str, dict] | tuple[str, None, None] | None:
    """企微智能机器人回调帧 → (动作, 文本, 原帧)。语义同 _feishu_extract：
    None=忽略（群聊/compat_info 智能助理消息/重投/空文本）；("hint",…)=非文本。
    字段照官方文档 101463 + aibot 1.0.2 实装核验：body.chattype=="single"、
    body.msgid（排重）、body.msgtype=="text"、body.text.content、body.compat_info
    （存在即非普通用户消息）。回帖凭据 = 原帧本身（reply 透传 headers.req_id，
    24h 内可回、限频 30 条/分钟/会话——单轮一次回复不受限）。"""
    body = frame.get("body") or {}
    if body.get("chattype") != "single":
        return None  # 仅单聊（用户定案），群聊不响应
    if body.get("compat_info") is not None:
        return None  # 智能助理等非普通用户消息
    if not _dedup(body.get("msgid", "") or ""):
        return None
    if body.get("msgtype") != "text":
        return ("hint", None, None)
    text = ((body.get("text") or {}).get("content") or "").strip()
    if not text:
        return None
    return ("ok", text[:_MAX_TEXT], frame)


def _mark_channel_conn(provider: str, reason: str) -> None:
    """worker 侧回写连接状态（凭据失效等终态错误，企微/微信共用）：设置页徽标
    即时可见；短事务直写，任何异常只记日志（运维性写库，失败无碍主流程）。"""
    try:
        from database import SessionLocal

        with SessionLocal() as db:
            row = get_channel(db, provider)
            if row is not None:
                row.conn_ok = False
                row.conn_error = reason[:200]
                row.conn_at = datetime.now()
                db.commit()
    except Exception:
        logger.exception("[chat-channel] %s 连接状态回写失败", provider)


def _make_wecom_reply(client, loop, frame) -> "callable":
    """构造同步 reply 闭包（handle_incoming 在 to_thread 工作线程调用）：
    run_coroutine_threadsafe 投递 SDK loop 后 .result() 等回执。这不违反
    死锁三禁——SDK loop 由 worker 线程独立推进，等待方是 to_thread 工作线程，
    无循环等待环（三禁禁的是「等待方自己卡住推进方」的形态）。reply 串行队列
    内置 5s 回执超时，外层 15s 上限兜底。markdown content 官方上限 20480
    字节，render_channel_markdown 已截 4000 字，此处再钳一道。"""
    def reply(text: str) -> None:
        async def _send():
            await client.reply(frame, {"msgtype": "markdown",
                                       "markdown": {"content": text[:20000]}})
        try:
            asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=15)
        except Exception:
            logger.exception("[chat-channel] 企微回帖异常")
    return reply


def _wecom_worker(bot_id: str, secret: str, epoch: int) -> None:
    """企微智能机器人线程体：手动新建 loop 驱动 SDK（不用 client.run()——拿不到
    loop 引用，回帖闭包与互踢防线都需要它）。互踢防线：启动前 threadsafe
    disconnect 上一代 worker 的 client（disconnect 内部 ensure_future 须在旧
    client 自己的 loop 线程内排队，RuntimeError=旧 loop 已死属正常）+ 1s 退避
    让断开握手完成，再连新连接。

    SDK 事件形态（1.0.2 实装核验）：回调经 pyee 在 SDK loop 线程内同步分发；
    认证失败/重连耗尽只 emit("error") 不断连（连接空挂）→ error 事件回写
    conn_* 后 disconnect + 停 loop 退出线程，等下一次配置变更重启再试。
    hint 分支在 SDK loop 线程内直接 ensure_future 发送（不 .result()——
    在 loop 线程等待自己 loop 的 future 必死锁，死锁三禁①同源）。"""
    global _wecom_prev
    prev = _wecom_prev
    _wecom_prev = None
    if prev is not None:
        prev_client, prev_loop = prev
        try:
            prev_loop.call_soon_threadsafe(prev_client.disconnect)
        except RuntimeError:
            pass  # 旧 loop 已随旧线程收尾，无需断
        time.sleep(1.0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    my_client = None
    try:
        from aibot import WSClient, WSClientOptions

        def _on_message(frame: dict) -> None:
            try:
                extracted = _wecom_extract(frame)
                if extracted is None:
                    return
                action, text, cb = extracted
                if action == "hint":
                    async def _hint():
                        try:
                            await my_client.reply(cb, {"msgtype": "markdown", "markdown": {
                                "content": "暂只支持文字消息～请用文字和我说。"}})
                        except Exception:
                            logger.warning("[chat-channel] 企微 hint 回复失败", exc_info=True)
                    asyncio.ensure_future(_hint())
                    return
                userid = ((cb.get("body") or {}).get("from") or {}).get("userid") or ""
                if userid:
                    _save_push_target("wecom", {"userid": userid})
                _deliver("wecom", text, _make_wecom_reply(my_client, loop, cb), epoch)
            except Exception:
                logger.exception("[chat-channel] 企微回调异常")

        def _on_error(err: Exception) -> None:
            msg = str(err)
            logger.warning("[chat-channel] 企微连接错误（epoch=%d）：%s", epoch, msg)
            # 终态错误（凭据失效 / 重连耗尽，SDK 文案匹配——on_error 无错误类型）：
            # 回写状态 + 断开退出；瞬态错误交给 SDK 自带重连
            if "Authentication failed" in msg or "Max reconnect" in msg:
                _mark_channel_conn("wecom", f"企业微信连接失效：{msg}")
                try:
                    my_client.disconnect()
                    loop.call_later(0.5, loop.stop)  # 留时间给异步收尾再停
                except Exception:
                    loop.stop()

        client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret))
        my_client = client
        _wecom_prev = (client, loop)
        client.on("message", _on_message)
        client.on("error", _on_error)
        logger.info("[chat-channel] 企微 worker 启动（epoch=%d）", epoch)
        loop.run_until_complete(client.connect())
        loop.run_forever()
    except Exception:
        logger.exception("[chat-channel] 企微 worker 异常退出")
    finally:
        if _wecom_prev is not None and _wecom_prev[0] is my_client:
            _wecom_prev = None
        try:
            if my_client is not None:
                my_client.disconnect()
        except Exception:
            pass
        _drain_stopped_loop(loop)


_WORKER_FACTORIES.update({"feishu": _feishu_worker, "dingtalk": _dingtalk_worker,
                          "wecom": _wecom_worker})


# —— 微信 iLink 收发（官方 ClawBot 插件；纯同步 httpx，无 asyncio） ———————

# 回复轻清洗表（微信不渲染 markdown——符号级去标记保文本，不做渲染；顺序敏感：
# 围栏先于行内代码、标记成对先于剥管道）
_MD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```[a-zA-Z0-9_-]*\n?"), ""),      # 代码围栏（语言标记同删）
    (re.compile(r"`([^`\n]+)`"), r"\1"),             # 行内代码
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),        # 加粗
    (re.compile(r"(\*|_)([^*\n_]+)\1"), r"\2"),      # 斜体
    (re.compile(r"~~(.+?)~~"), r"\1"),               # 删除线
    (re.compile(r"^#{1,6}\s+", re.M), ""),           # 标题井号
    (re.compile(r"^\s*[-*+]\s+", re.M), "· "),       # 无序列表 → 间隔号
    (re.compile(r"^\s*\d+\.\s+", re.M), ""),         # 有序列表号
    (re.compile(r"^\s*\|?[:\-\s|]+\|?\s*$", re.M), ""),  # 表格分隔线整行
    (re.compile(r"\s*\|\s*"), " "),                  # 表格内容剥管道符
]


def _strip_markdown(text: str) -> str:
    """渠道 markdown → 微信纯文本（render_channel_markdown 的反向伴生）。"""
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _wechat_extract(msg: dict) -> tuple[str, str, str] | tuple[str, None, None] | None:
    """iLink getupdates 消息 → (动作, 文本, 发送者 user_id)。None=忽略（BOT
    回显 / 空发送者 / 重投）；("hint",…)=非文本（图片/语音等，回提示）。字段照
    社区 SDK protocol.py 核验：message_type 1=用户 2=机器人；文本在 item_list
    内 type==1 的 text_item.text；client_id 作排重键（游标回退重投防护）。"""
    if msg.get("message_type") != 1:
        return None  # BOT 回显（我们自己发的），不进远程轮
    user_id = msg.get("from_user_id") or ""
    if not user_id:
        return None
    if not _dedup(msg.get("client_id", "") or ""):
        return None
    text = ""
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            text = ((item.get("text_item") or {}).get("text") or "").strip()
            break
    if not text:
        return ("hint", None, None)
    return ("ok", text[:_MAX_TEXT], user_id)


def _wechat_send(token: str, baseurl: str, user_id: str, context_token: str,
                 text: str) -> None:
    """sendmessage（协议核验形态：message_type=2 BOT / message_state=2 终稿 /
    item_list type=1 文本；client_id 每次新 uuid）。context_token 须为发给该
    用户的最新值（协议要求逐条刷新，worker 缓存取）。微信不渲染 markdown——
    清洗后发送；失败只记日志不抛（reply 侧兜底，飞书/钉钉同款）。"""
    body = {"msg": {
        "from_user_id": "", "to_user_id": user_id, "client_id": str(uuid4()),
        "message_type": 2, "message_state": 2, "context_token": context_token,
        "item_list": [{"type": 1, "text_item": {"text": _strip_markdown(text)}}],
    }}
    data = _ilink_post(baseurl, token, "/ilink/bot/sendmessage", body)
    ret, errcode = data.get("ret"), data.get("errcode")
    if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
        logger.warning("[chat-channel] 微信回帖失败：ret=%s errcode=%s %s",
                       ret, errcode, data.get("errmsg"))


def _make_wechat_reply(token: str, baseurl: str, user_id: str,
                       tokens: dict[str, str]):
    """构造同步 reply 闭包：发送时从 tokens 缓存取该用户最新 context_token
    （worker 循环逐条刷新维护，含 BOT 回显——协议要点，闭包取值时点最晚化）。"""
    def reply(text: str) -> None:
        ct = tokens.get(user_id)
        if not ct:
            logger.warning("[chat-channel] 微信回帖跳过：%s 无会话令牌", user_id)
            return
        _wechat_send(token, baseurl, user_id, ct, text)
    return reply


def _wechat_worker(token: str, baseurl: str, epoch: int) -> None:
    """微信线程体：纯同步 httpx 长轮询（hold 35s 的形态下同步循环最简单可靠，
    无 asyncio 无死锁面）。errcode -14 = 会话过期（协议规定，token 失效）：
    回写 conn_ok=False + 退出——重新扫码是唯一恢复路径（poll_wechat_login 落新
    token 后 _notify_config_changed 自动拉起新 worker）。网络/未知异常 sleep 3s
    重试；游标 get_updates_buf 不透明回传，只在 ret==0 时推进。"""
    cursor = ""
    logger.info("[chat-channel] 微信 worker 启动（epoch=%d）", epoch)
    while not _closing and epoch == _epoch:
        try:
            data = _ilink_post(baseurl, token, "/ilink/bot/getupdates",
                               {"get_updates_buf": cursor}, timeout=45.0)
        except Exception as e:
            if _closing or epoch != _epoch:
                break
            logger.warning("[chat-channel] 微信长轮询异常，3s 后重试：%s",
                           type(e).__name__)
            time.sleep(3)
            continue
        ret, errcode = data.get("ret"), data.get("errcode")
        if isinstance(errcode, int) and errcode == -14:
            _mark_channel_conn("wechat", "登录已过期，请重新扫码")
            logger.warning("[chat-channel] 微信会话过期（errcode=-14），worker 退出")
            break
        if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
            logger.warning("[chat-channel] 微信 getupdates 错误：ret=%s errcode=%s，3s 后重试",
                           ret, errcode)
            time.sleep(3)
            continue
        cursor = data.get("get_updates_buf") or cursor
        for msg in data.get("msgs") or []:
            # context_token 逐条刷新（USER 消息键 from_user_id，BOT 回显键
            # to_user_id——都是「该用户会话」的最新令牌，回帖永远取缓存值）
            key = msg.get("from_user_id") if msg.get("message_type") == 1 \
                else msg.get("to_user_id")
            ct = msg.get("context_token") or ""
            if key and ct:
                _wechat_ctx_tokens[key] = ct
            try:
                extracted = _wechat_extract(msg)
                if extracted is None:
                    continue
                action, text, user_id = extracted
                if action == "hint":
                    _wechat_send(token, baseurl, user_id,
                                 _wechat_ctx_tokens.get(user_id, ""),
                                 "暂只支持文字消息～请用文字和我说。")
                    continue
                _save_push_target("wechat", {"user_id": user_id})
                _deliver("wechat", text,
                         _make_wechat_reply(token, baseurl, user_id,
                                            _wechat_ctx_tokens), epoch)
            except Exception:
                logger.exception("[chat-channel] 微信消息处理异常")


def _wechat_probe(token: str, baseurl: str) -> tuple[bool, str | None]:
    """「测试」按钮：getupdates 短超时探测（4s）。ReadTimeout = 服务端已接受
    token 正在 hold（长轮询正常形态）→ 有效；-14 = 过期需重扫；其余异常 =
    网络不可达/代理拦截。文案在探内拼好（含渠道名）。"""
    try:
        data = _ilink_post(baseurl or _ILINK_BASE, token, "/ilink/bot/getupdates",
                           {"get_updates_buf": ""}, timeout=4.0)
    except httpx.ReadTimeout:
        return True, None  # 正在 hold：token 有效的证据（ConnectTimeout 不属此类）
    except Exception:
        return False, "无法连接微信服务：网络不可达或被代理/防火墙拦截（检查本机网络）"[:200]
    ret, errcode = data.get("ret"), data.get("errcode")
    if isinstance(errcode, int) and errcode == -14:
        return False, "微信登录已过期，请重新扫码"
    if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
        return False, f"微信凭据校验失败：{data.get('errmsg') or f'ret={ret} errcode={errcode}'}"[:200]
    return True, None


_WORKER_FACTORIES.update({"wechat": _wechat_worker})


# —— worker 管理器（启动/重启/关闭；mcp_session 线程先例 + epoch 守卫） —————


def _restart_workers() -> None:
    """全量重启：epoch+1（旧 worker 失活）→ 读 enabled 行逐渠道拉新 daemon 线程。
    旧线程不 join（死锁三禁② + lark 无 stop），daemon 随进程退出。"""
    global _epoch
    _epoch += 1
    from database import SessionLocal

    with SessionLocal() as db:
        rows = db.execute(select(ChatChannel).where(ChatChannel.enabled)).scalars().all()
    for row in rows:
        factory = _WORKER_FACTORIES.get(row.provider)
        if factory is None:
            continue
        threading.Thread(target=factory, name=f"chat-channel-{row.provider}",
                         args=(row.app_id, row.app_secret, _epoch), daemon=True).start()
    if rows:
        logger.info("[chat-channel] worker 全量重启：%s（epoch=%d）",
                    "、".join(r.provider for r in rows), _epoch)


async def start_all() -> None:
    """主 lifespan 启动段调用（须在主循环协程内）：捕获 running loop 供
    _deliver 投递，随后按库内 enabled 行启动 worker。恢复的快照凭据已在
    maybe_restore_and_snapshot 落库，此处读到的即最终态。"""
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    _restart_workers()


def shutdown() -> None:
    """进程退出收尾（main lifespan yield 后）：关闭标志置位——不再启动新
    worker、在途投递丢弃；残活 SDK 线程 daemon 随进程退出（不 join）。"""
    global _closing
    _closing = True


def invalidate() -> None:
    """外部配置变更（config_backup 恢复路径等）后的重启挂点——service 内写
    操作走 _notify_config_changed（同一语义）。"""
    _notify_config_changed()


# —— 定时任务推送（2026-10-23 二批：晨报/会前准备等主动推送到已开启渠道） ——————


def _save_push_target(provider: str, target: dict) -> None:
    """worker 收到用户消息时缓存推送目标（首条即记；目标不变不重复写库——
    模块级 _push_targets 比对短路）。短事务直写，异常只记日志（运维性写库，
    _mark_channel_conn 同款——失败无碍对话主流程）。"""
    if not target:
        return
    encoded = json.dumps(target, ensure_ascii=False)
    if _push_targets.get(provider) == encoded:
        return
    try:
        from database import SessionLocal

        with SessionLocal() as db:
            row = get_channel(db, provider)
            if row is not None:
                row.push_target = encoded
                db.commit()
        _push_targets[provider] = encoded
    except Exception:
        logger.warning("[chat-channel] %s 推送目标缓存失败", provider, exc_info=True)


def _dingtalk_push(app_id: str, app_secret: str, staff_id: str, text: str) -> None:
    """钉钉主动推送：accessToken（appKey/appSecret，7200s 有效但推送低频不缓存）
    → oToMessages/batchSend（企业内部机器人单聊批量；2026-10-23 官方文档核验：
    userIds 即 staffId，无 unionId 环节；msgKey sampleMarkdown）。前提：用户与
    机器人有过单聊（worker 回调已保证）。"""
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            resp = client.post(
                "https://api.dingtalk.com/v1.0-0.0/oauth2/accessToken",
                json={"appKey": app_id, "appSecret": app_secret})
            access_token = resp.json().get("accessToken") or ""
            if not access_token:
                logger.warning("[chat-channel] 钉钉 accessToken 获取失败：%s",
                               resp.text[:200])
                return
            resp = client.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": access_token},
                json={"robotCode": app_id, "userIds": [staff_id],
                      "msgKey": "sampleMarkdown",
                      "msgParam": json.dumps({"title": "日程助手", "text": text},
                                             ensure_ascii=False)})
            if not resp.is_success:
                logger.warning("[chat-channel] 钉钉推送失败：HTTP %s %s",
                               resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("[chat-channel] 钉钉推送异常")


def _wecom_push(bot_id: str, secret: str, userid: str, text: str) -> None:
    """企微主动推送：复用在线 worker 的长连接（同 bot 只允许一个连接，另建必
    互踢——_wecom_prev 即当前在线 client+loop，跨线程投协程）；worker 不在线
    （监听关闭但推送仍开）则独立短连发完即断（会踢残活连接，worker SDK 自动
    重连自愈，可接受）。send_message 官方语义：chatid 单聊填 userid。"""
    prev = _wecom_prev
    if prev is not None:
        client, loop = prev

        async def _send_on(c):
            await c.send_message(userid, {"msgtype": "markdown",
                                          "markdown": {"content": text}})
        try:
            asyncio.run_coroutine_threadsafe(_send_on(client), loop).result(timeout=30)
            return
        except Exception:
            logger.warning("[chat-channel] 企微推送走在线连接失败，改独立短连",
                           exc_info=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from aibot import WSClient, WSClientOptions

        async def _run():
            client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret))
            await client.connect()
            await client.send_message(userid, {"msgtype": "markdown",
                                               "markdown": {"content": text}})
            await client.disconnect()

        loop.run_until_complete(_run())
    except Exception:
        logger.exception("[chat-channel] 企微推送失败")
    finally:
        _drain_stopped_loop(loop)


def _push_feishu(app_id: str, app_secret: str, target: dict, text: str) -> None:
    open_id = target.get("open_id") or ""
    if not open_id:
        logger.info("[chat-channel] 飞书推送跳过：目标未缓存")
        return
    _feishu_send(app_id, app_secret, open_id, text)


def _push_dingtalk(app_id: str, app_secret: str, target: dict, text: str) -> None:
    staff_id = target.get("staff_id") or ""
    if not staff_id:
        logger.info("[chat-channel] 钉钉推送跳过：目标未缓存")
        return
    _dingtalk_push(app_id, app_secret, staff_id, text)


def _push_wecom(app_id: str, app_secret: str, target: dict, text: str) -> None:
    userid = target.get("userid") or ""
    if not userid:
        logger.info("[chat-channel] 企微推送跳过：目标未缓存")
        return
    _wecom_push(app_id, app_secret, userid, text)


def _push_wechat(app_id: str, app_secret: str, target: dict, text: str) -> None:
    user_id = target.get("user_id") or ""
    ct = _wechat_ctx_tokens.get(user_id, "")
    if not user_id or not ct:
        # 微信限制：仅能回复、context_token 24h 内有效——推送尽力而为
        logger.info("[chat-channel] 微信推送跳过：%s", "目标未缓存" if not user_id
                    else "无有效会话令牌（需近期与机器人对话过）")
        return
    _wechat_send(app_id, app_secret, user_id, ct, text)


_PUSH_SENDERS = {
    "feishu": _push_feishu,
    "dingtalk": _push_dingtalk,
    "wecom": _push_wecom,
    "wechat": _push_wechat,
}


def push_to_channels(text: str) -> None:
    """定时任务主动推送入口（workflows 挂点，批 3）：遍历 enabled ∧ push_enabled
    行，push_target 逐渠道分发。同步阻塞（网络发送，分钟级低频可接受）——调用方
    须在后台线程调（chat_turn._bg_compact_tasks 弱引用池 / to_thread 先例）。推送
    不落库（桌面端已有同文本消息；渠道侧 BOT 回显/自回环过滤零副作用）。逐渠道
    吞日志——推送是旁路，绝不影响工作流主流程。"""
    text = text[:_MAX_TEXT]  # 晨间简报全文可能超长，与回复侧同档截断
    from database import SessionLocal

    rows: list[tuple[str, str, str, str | None]] = []
    try:
        with SessionLocal() as db:
            for row in db.execute(select(ChatChannel).where(
                    ChatChannel.enabled, ChatChannel.push_enabled)).scalars():
                rows.append((row.provider, row.app_id, row.app_secret,
                             row.push_target))
    except Exception:
        logger.warning("[chat-channel] 推送行读取失败", exc_info=True)
        return
    for provider, app_id, app_secret, target_raw in rows:
        sender = _PUSH_SENDERS.get(provider)
        if sender is None:
            continue
        try:
            sender(app_id, app_secret, json.loads(target_raw or "{}"), text)
        except Exception:
            logger.warning("[chat-channel] %s 推送失败", provider, exc_info=True)


def _reset_for_tests() -> None:
    """测试间清模块态（conftest autouse 调用）：epoch/去重/关闭位复位，
    _MAIN_LOOP 置 None（防上个用例注入的 fake loop 串台）。"""
    global _epoch, _closing, _MAIN_LOOP, _wecom_prev
    _epoch = 0
    _closing = False
    _MAIN_LOOP = None
    _wecom_prev = None
    _wechat_logins.clear()
    _wechat_ctx_tokens.clear()
    _push_targets.clear()
    with _seen_lock:
        _seen.clear()
