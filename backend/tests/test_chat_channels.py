"""聊天通讯渠道批 4+5 测试：目录 / CRUD / 凭据探测（_post_json 替身零网络）/
快照纳管 / SDK worker 层（过滤链、去重、epoch 守卫、远程轮全链——FakeGraph +
Fake reply 零 SDK 依赖）。"""

import asyncio
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from models import ModelProvider
from services import chat_channels
from services.config_backup import dump_config, restore_config
from conftest import TestingSessionLocal


class _FakeResp:
    """_post_json 替身返回体（够 .status_code / .json() 即可）。"""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _patch_probe(monkeypatch, status_code: int, payload: dict):
    monkeypatch.setattr(
        chat_channels, "_post_json",
        lambda url, body: _FakeResp(status_code, payload))


# —— 目录 ——

def test_catalog_four_providers(client):
    """四渠道目录固定四行；wechat 转正式（扫码形态，placeholder=False）。"""
    res = client.get("/api/chat-channels")
    assert res.status_code == 200
    items = res.json()
    assert [it["provider"] for it in items] == [
        "feishu", "dingtalk", "wecom", "wechat"]
    assert all(it["placeholder"] is False for it in items)
    assert all(it["configured"] is False for it in items)  # 空库零接入行
    wecom, wechat = items[2], items[3]
    assert wecom["setup_url"].startswith("https://developer.work.weixin.qq.com")
    assert wechat["setup_url"]  # ClawBot 指引外链（扫码形态）


# —— 凭据保存（保存即探测） ——

def test_upsert_feishu_probe_ok_persists(client, monkeypatch):
    _patch_probe(monkeypatch, 200, {"code": 0, "tenant_access_token": "t-x"})
    res = client.put("/api/chat-channels/feishu",
                     json={"app_id": "cli_a1", "app_secret": "sk-1"})
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True and body["app_id"] == "cli_a1"
    assert body["enabled"] is True  # 新接入默认启用
    assert body["conn_ok"] is True and body["conn_error"] is None
    assert body["conn_at"] is not None


def test_upsert_probe_failure_still_saves_with_error(client, monkeypatch):
    """凭据错也保存（失败是数据），conn_* 如实回显——用户改凭据再测即可。"""
    _patch_probe(monkeypatch, 400, {"message": "invalid appSecret"})
    res = client.put("/api/chat-channels/dingtalk",
                     json={"app_id": "dk1", "app_secret": "bad"})
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True and body["conn_ok"] is False
    # 错误摘要带渠道名 + 平台文案，用户能看懂是哪家的凭据错
    assert "钉钉凭据校验失败" in body["conn_error"]
    assert "invalid appSecret" in body["conn_error"]


def test_upsert_network_error_returns_summary(client, monkeypatch):
    """网络层异常归「无法连接开放平台」摘要，不裸抛堆栈。"""
    def _boom(url, body):
        import httpx
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(chat_channels, "_post_json", _boom)
    res = client.put("/api/chat-channels/feishu",
                     json={"app_id": "cli_a1", "app_secret": "sk-1"})
    assert res.status_code == 200
    assert res.json()["conn_ok"] is False
    assert "无法连接" in res.json()["conn_error"]


def test_upsert_rejects_wechat_and_unknown(client):
    """wechat 在目录内但凭据是扫码形态——422 引导扫码；目录外渠道未知 422。"""
    res = client.put("/api/chat-channels/wechat",
                     json={"app_id": "x", "app_secret": "y"})
    assert res.status_code == 422 and "扫码" in res.json()["detail"]
    assert client.put("/api/chat-channels/qqmail",
                      json={"app_id": "x", "app_secret": "y"}).status_code == 422


def test_upsert_wecom_probe_ok_persists(client, monkeypatch):
    """wecom 保存即探测（ws 建连订阅一次）；探测锚点是 _wecom_probe（打桩零网络）。"""
    monkeypatch.setattr(chat_channels, "_wecom_probe",
                        lambda bid, sec: (True, None))
    res = client.put("/api/chat-channels/wecom",
                     json={"app_id": "bot-1", "app_secret": "sk-1"})
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True and body["app_id"] == "bot-1"
    assert body["enabled"] is True and body["conn_ok"] is True


def test_upsert_wecom_probe_failure_still_saves(client, monkeypatch):
    """凭据错也保存（失败是数据）；conn_error 带企微渠道名与平台文案。"""
    monkeypatch.setattr(
        chat_channels, "_wecom_probe",
        lambda bid, sec: (False, "企业微信凭据校验失败：Authentication failed (code: 40001)"))
    res = client.put("/api/chat-channels/wecom",
                     json={"app_id": "bot-1", "app_secret": "bad"})
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True and body["conn_ok"] is False
    assert "企业微信凭据校验失败" in body["conn_error"]


def test_upsert_empty_credentials_422(client):
    res = client.put("/api/chat-channels/feishu",
                     json={"app_id": "  ", "app_secret": "s"})
    assert res.status_code == 422


def test_upsert_overwrites_existing_row(client, monkeypatch):
    _patch_probe(monkeypatch, 200, {"code": 0})
    client.put("/api/chat-channels/feishu",
               json={"app_id": "old", "app_secret": "s1"})
    res = client.put("/api/chat-channels/feishu",
                     json={"app_id": "new", "app_secret": "s2"})
    assert res.status_code == 200
    assert res.json()["app_id"] == "new"
    # 唯一键：仍是三行目录、feishu 一行
    assert len([it for it in client.get("/api/chat-channels").json()
                if it["configured"]]) == 1


def test_upsert_edit_empty_secret_keeps_old(client, monkeypatch):
    """编辑形态：app_secret 留空 = 沿用旧值（secret 不回显）；新建时空 secret 422。"""
    sent_payloads: list = []
    def _capture(url, body):
        sent_payloads.append(body)
        return _FakeResp(200, {"code": 0})
    monkeypatch.setattr(chat_channels, "_post_json", _capture)
    client.put("/api/chat-channels/feishu",
               json={"app_id": "cli", "app_secret": "s1"})
    res = client.put("/api/chat-channels/feishu", json={"app_id": "cli", "app_secret": ""})
    assert res.status_code == 200
    # 探测仍带旧 secret（留空未把凭据清掉）
    assert sent_payloads[-1] == {"app_id": "cli", "app_secret": "s1"}


def test_secret_roundtrip_for_edit_form(client, monkeypatch):
    """app_secret 原样回显（Composio key 先例）——编辑模态回填 + 前端密码态/眼睛
    查看的契约；未配置渠道恒 None。"""
    _patch_probe(monkeypatch, 200, {"code": 0})
    client.put("/api/chat-channels/feishu",
               json={"app_id": "cli", "app_secret": "sk-1"})
    items = client.get("/api/chat-channels").json()
    feishu = [it for it in items if it["provider"] == "feishu"][0]
    assert feishu["app_secret"] == "sk-1"
    assert [it["app_secret"] for it in items if it["provider"] != "feishu"] == [
        None, None, None]  # 其余三渠道未配置恒 None


# —— 测试 / 启停 / 删除 ——

def test_endpoint_not_configured_404(client):
    assert client.post("/api/chat-channels/feishu/test").status_code == 404
    assert client.put("/api/chat-channels/feishu/enabled",
                      json={"enabled": False}).status_code == 404
    assert client.delete("/api/chat-channels/feishu").status_code == 404


def test_probe_refreshes_on_demand(client, monkeypatch):
    """保存时凭据错、改对后「测试」通过——conn_* 刷新。"""
    _patch_probe(monkeypatch, 200, {"code": 20027, "msg": "app secret invalid"})
    client.put("/api/chat-channels/feishu",
               json={"app_id": "cli", "app_secret": "bad"})
    _patch_probe(monkeypatch, 200, {"code": 0, "tenant_access_token": "t"})
    res = client.post("/api/chat-channels/feishu/test")
    assert res.status_code == 200
    assert res.json()["conn_ok"] is True and res.json()["conn_error"] is None


def test_disable_and_delete(client, monkeypatch):
    _patch_probe(monkeypatch, 200, {"code": 0})
    client.put("/api/chat-channels/feishu",
               json={"app_id": "cli", "app_secret": "s"})
    res = client.put("/api/chat-channels/feishu/enabled", json={"enabled": False})
    assert res.status_code == 200 and res.json()["enabled"] is False
    assert client.delete("/api/chat-channels/feishu").status_code == 204
    assert client.delete("/api/chat-channels/feishu").status_code == 404
    item = [it for it in client.get("/api/chat-channels").json()
            if it["provider"] == "feishu"][0]
    assert item["configured"] is False and item["app_id"] is None


# —— 配置快照纳管 ——

def test_snapshot_roundtrip_includes_chat_channels(monkeypatch):
    _patch_probe(monkeypatch, 200, {"expireIn": 7200, "accessToken": "at-x"})
    with TestingSessionLocal() as db:
        chat_channels.upsert_channel(db, "dingtalk", "dk1", "sk-1")
        snap = dump_config(db)
    assert any(r["provider"] == "dingtalk" for r in snap["chat_channels"])

    with TestingSessionLocal() as db:  # 新库（模拟删库重建）
        restore_config(db, snap)
        row = chat_channels.get_channel(db, "dingtalk")
        assert row is not None
        assert (row.app_id, row.app_secret, row.conn_ok) == ("dk1", "sk-1", True)
        assert row.conn_at is not None  # datetime 族经 isoformat 往返还原


def test_snapshot_restore_null_bool_backfills_false(monkeypatch):
    """历史踩坑快照（2026-10-23 真机）：push_enabled 补列前 dump 的行带
    None——恢复 insert 撞 NOT NULL 炸 lifespan；_revive 非空布尔 None 兜底
    归 False，删库自愈路径不再被存量快照卡死。"""
    with TestingSessionLocal() as db:
        snap = dump_config(db)  # 借全量快照骨架
    # 手工植入踩坑形态行（push_enabled=None / push_target=None）
    snap["chat_channels"] = [{
        "id": "c1", "provider": "feishu", "app_id": "f1", "app_secret": "fs",
        "enabled": True, "push_enabled": None, "push_target": None,
        "conn_ok": None, "conn_error": None, "conn_at": None,
        "created_at": "2026-10-23T00:00:00", "updated_at": "2026-10-23T00:00:00",
    }]
    with TestingSessionLocal() as db:
        restore_config(db, snap)
        row = chat_channels.get_channel(db, "feishu")
        assert row.push_enabled is False and row.push_target is None


def test_drain_stopped_loop():
    """旧 worker 退出时的 loop 标准收尾：cancel 传播让 task finally 落地 +
    loop close——不留给 GC 打 "Event loop is closed" 噪音（2026-10-23 真机）。"""
    import asyncio

    loop = asyncio.new_event_loop()
    landed: list = []

    async def _hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            landed.append("cancelled")
            raise

    task = loop.create_task(_hang())
    timer = threading.Timer(0.05, lambda: loop.call_soon_threadsafe(loop.stop))
    timer.start()
    try:
        loop.run_until_complete(asyncio.shield(task))
    except RuntimeError:
        pass  # stop 打断 run_until_complete 的预期形态（旧 worker 同款）
    timer.join()
    chat_channels._drain_stopped_loop(loop)
    assert loop.is_closed()
    assert landed == ["cancelled"]  # drain 的 gather 等到了 finally 落地


# —— SDK worker 层（批 5；FakeGraph + Fake reply 零 SDK 依赖） ————————————

from models import ChatChannel, Message  # noqa: E402
from sqlalchemy import select  # noqa: E402


class _FakeGraph:
    """chat_turn.load_graph 替身：可调用返回自身（镜像 build_graph → compiled
    graph 两跳形态），invoke 返回定值或抛。"""

    def __init__(self, final=None, raiser=None):
        self._final = final or {}
        self._raiser = raiser

    def __call__(self):
        return self

    def invoke(self, state):
        if self._raiser:
            raise self._raiser
        return self._final


def _activate_model() -> None:
    with TestingSessionLocal() as db:
        db.add(ModelProvider(name=f"m-{uuid4().hex[:6]}",
                             model_name="deepseek/deepseek-chat", is_active=True))
        db.commit()


def test_handle_incoming_full_round(monkeypatch):
    """远程轮全链：快查过 → 轮次跑完 → 渠道收到渲染回复 → 消息双侧 source 落库
    → broadcast chat_sync → 压缩调度。带 draft_plan 验证卡片脚注。"""
    from services import chat_turn, workflows

    _activate_model()
    monkeypatch.setattr(chat_turn, "load_graph", lambda: _FakeGraph({
        "final_reply": "已出草案", "draft_plan": [{"title": "体检"}]}))
    compacted: list = []
    monkeypatch.setattr(chat_turn, "schedule_compaction", lambda: compacted.append(1))
    casts: list = []
    monkeypatch.setattr(workflows, "broadcast", lambda ev: casts.append(ev))
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("feishu", "明天上午10点体检", sent.append))

    assert len(sent) == 1 and sent[0].startswith("已出草案")
    # 批 2：draft 卡文本化——草案条目 + 对话确认指引（泛化尾注让位）
    assert "待确认草案" in sent[0] and "1. 体检" in sent[0]
    assert "回复「确认」" in sent[0] and "桌面端查看采纳" not in sent[0]
    # 两次广播：段1（user 落库即刷——桌面实时显示远程消息）+ 轮末（回复）
    assert casts == [{"type": "chat_sync", "provider": "feishu"}] * 2
    assert compacted == [1]
    with TestingSessionLocal() as db:
        rows = db.execute(select(Message).order_by(Message.created_at)).scalars().all()
        assert [(r.role, r.source, r.content) for r in rows] == [
            ("user", "feishu", "明天上午10点体检"),
            ("assistant", "feishu", "已出草案"),
        ]


def test_handle_incoming_disabled_hint(monkeypatch):
    from services.settings import get_settings

    with TestingSessionLocal() as db:
        get_settings(db).ai_enabled = False
        db.commit()
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("dingtalk", "在吗", sent.append))
    assert sent == [chat_channels._DISABLED_HINT]


def test_handle_incoming_no_model_hint(monkeypatch):
    """默认零激活模型（不造 ModelProvider 行）→ 渠道收模型引导。"""
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("feishu", "hi", sent.append))
    assert sent == [chat_channels._NO_MODEL_HINT]


def test_handle_incoming_not_ready_hint(monkeypatch):
    from services import chat_turn

    _activate_model()
    def _boom():
        raise ImportError("agents.graph missing")
    monkeypatch.setattr(chat_turn, "load_graph", _boom)
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("feishu", "hi", sent.append))
    assert sent == [chat_channels._NOT_READY_HINT]


def test_handle_incoming_turn_error_hint(monkeypatch):
    from services import chat_turn

    _activate_model()
    monkeypatch.setattr(chat_turn, "load_graph",
                        lambda: _FakeGraph(raiser=RuntimeError("boom")))
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("dingtalk", "hi", sent.append))
    assert sent == [chat_channels._TURN_ERROR_HINT]


def test_handle_incoming_stale_epoch_dropped(monkeypatch):
    """旧代 worker 残活消息（epoch 已变）直接丢弃——不回也不落库。"""
    _activate_model()
    sent: list = []
    asyncio.run(chat_channels.handle_incoming(
        "feishu", "hi", sent.append, epoch=chat_channels._epoch - 1))
    assert sent == []
    with TestingSessionLocal() as db:
        assert db.execute(select(Message)).scalars().first() is None


def test_render_channel_markdown():
    render = chat_channels.render_channel_markdown
    assert "没有文字回复" in render("")
    long = "字" * 4100
    assert len(render(long)) < 4100 and "已截断" in render(long)
    assert "桌面端" in render("短回复", has_cards=True)


def test_dedup_lru_cap():
    chat_channels._reset_for_tests()
    assert chat_channels._dedup("m1") is True
    assert chat_channels._dedup("m1") is False  # 重投
    for i in range(chat_channels._SEEN_CAP):
        chat_channels._dedup(f"x{i}")
    assert chat_channels._dedup("m1") is True  # 早已被逐出 LRU


def _feishu_event(chat_type="p2p", sender_type="user", msg_type="text",
                  content=None, message_id="m-1", open_id="ou_1"):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            chat_type=chat_type, message_type=msg_type,
            content=content if content is not None else json.dumps({"text": "你好"}),
            message_id=message_id),
        sender=SimpleNamespace(
            sender_type=sender_type,
            sender_id=SimpleNamespace(open_id=open_id))))


def test_feishu_extract_filters():
    ext = chat_channels._feishu_extract
    chat_channels._reset_for_tests()
    assert ext(_feishu_event(chat_type="group")) is None       # 群聊不响应
    assert ext(_feishu_event(sender_type="app")) is None       # bot 自回环
    assert ext(_feishu_event(message_id="m-2")) == ("ok", "你好", "ou_1")
    assert ext(_feishu_event(message_id="m-2")) is None        # 同消息重投
    assert ext(_feishu_event(msg_type="image", message_id="m-3")) == ("hint", None, None)
    long = _feishu_event(content=json.dumps({"text": "长" * 5000}), message_id="m-4")
    assert ext(long)[1] == "长" * chat_channels._MAX_TEXT      # 输入截断
    assert ext(_feishu_event(open_id="", message_id="m-5")) is None  # 缺 open_id


def _dingtalk_msg(conv="1", mtype="text", text="你好", mid="d-1",
                  webhook="https://oapi.dingtalk.com/wh"):
    # 字段名照 SDK 实测（snake_case；msgtype → message_type）——见
    # _dingtalk_extract docstring
    return SimpleNamespace(conversation_type=conv, message_type=mtype,
                           text=SimpleNamespace(content=text),
                           message_id=mid, session_webhook=webhook)


def test_dingtalk_extract_filters():
    ext = chat_channels._dingtalk_extract
    chat_channels._reset_for_tests()
    assert ext(_dingtalk_msg(conv="2")) is None                # 群聊不响应
    assert ext(_dingtalk_msg(mtype="picture", mid="d-2")) == ("hint", None, None)
    assert ext(_dingtalk_msg(mid="d-3")) == ("ok", "你好",
                                             "https://oapi.dingtalk.com/wh")
    assert ext(_dingtalk_msg(mid="d-3")) is None               # 同消息重投
    assert ext(_dingtalk_msg(text="  ", mid="d-4")) is None    # 空文本


def _wecom_frame(chattype="single", msgtype="text", content="你好",
                 msgid="w-1", compat=None):
    # 字段照官方文档 101463 + aibot 1.0.2 实装核验（dict 帧，零 SDK 依赖）
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": "r-1"},
            "body": {"chattype": chattype, "msgtype": msgtype, "msgid": msgid,
                     "compat_info": compat, "text": {"content": content},
                     "from": {"userid": "u1"}}}


def test_wecom_extract_filters():
    ext = chat_channels._wecom_extract
    chat_channels._reset_for_tests()
    assert ext(_wecom_frame(chattype="group", msgid="w-2")) is None  # 群聊不响应
    assert ext(_wecom_frame(compat={"src": 1}, msgid="w-3")) is None  # 智能助理消息
    ok = ext(_wecom_frame(msgid="w-4"))
    assert ok == ("ok", "你好", _wecom_frame(msgid="w-4"))  # 原帧回传作回帖凭据
    assert ext(_wecom_frame(msgid="w-4")) is None             # 同消息重投
    assert ext(_wecom_frame(msgtype="voice", msgid="w-5")) == ("hint", None, None)
    assert ext(_wecom_frame(content="  ", msgid="w-6")) is None
    long = ext(_wecom_frame(content="长" * 5000, msgid="w-7"))
    assert long[1] == "长" * chat_channels._MAX_TEXT           # 输入截断


def test_deliver_epoch_guard(monkeypatch):
    """epoch 不匹配 / 主循环未设：静默丢弃不炸（run_coroutine_threadsafe 不触达）。"""
    chat_channels._reset_for_tests()
    monkeypatch.setattr(chat_channels, "_MAIN_LOOP", None)
    chat_channels._deliver("feishu", "hi", lambda t: None, epoch=0)      # loop 未设
    chat_channels._deliver("feishu", "hi", lambda t: None, epoch=-1)     # 旧代


def test_restart_workers_launches_enabled_rows(monkeypatch):
    """重启只拉 enabled 行；epoch 单调 +1；fake factory 经线程收到凭据。"""
    chat_channels._reset_for_tests()
    started: list = []
    done = threading.Event()

    def fake_factory(app_id, app_secret, epoch):
        started.append((app_id, app_secret, epoch))
        done.set()

    monkeypatch.setattr(chat_channels, "_WORKER_FACTORIES", {"feishu": fake_factory})
    monkeypatch.setattr(chat_channels, "_MAIN_LOOP", "loop-sentinel")
    with TestingSessionLocal() as db:
        db.add(ChatChannel(provider="feishu", app_id="f1", app_secret="fs"))
        db.add(ChatChannel(provider="dingtalk", app_id="d1", app_secret="ds",
                           enabled=False))
        db.commit()
    chat_channels._restart_workers()
    assert done.wait(2)
    assert started == [("f1", "fs", 1)]  # dingtalk 停用不拉
    assert chat_channels._epoch == 1
    chat_channels._reset_for_tests()


def test_notify_config_changed_noop_without_loop():
    """_MAIN_LOOP 未设（测试/纯 CRUD 环境）：写操作挂点 no-op——epoch 不动零线程。"""
    chat_channels._reset_for_tests()
    chat_channels._notify_config_changed()
    assert chat_channels._epoch == 0


def test_shutdown_blocks_restart(monkeypatch):
    chat_channels._reset_for_tests()
    chat_channels.shutdown()
    monkeypatch.setattr(chat_channels, "_MAIN_LOOP", "loop-sentinel")
    chat_channels._notify_config_changed()
    assert chat_channels._epoch == 0  # 关闭后不再启动
    chat_channels._reset_for_tests()


# —— 微信 iLink 登录链（批 3；_ilink_get 替身零网络） ————————————————————

def test_wechat_login_roundtrip_and_persist(client, monkeypatch):
    """出码 → wait/scaned 透传 → confirmed 落库（app_id=bot_token /
    app_secret=baseurl 行字段语义映射）+ conn_ok + 恒启用 + 票据消费即清。"""
    monkeypatch.setattr(chat_channels, "_ilink_get",
                        lambda base, path: {"ret": 0, "qrcode": "qr-1",
                                            "qrcode_img_content": "https://wx/qr"})
    res = client.post("/api/chat-channels/wechat/login")
    assert res.status_code == 200
    body = res.json()
    assert body["ticket"] and body["qr_url"] == "https://wx/qr"
    ticket = body["ticket"]
    seq = iter(["wait", "scaned"])  # 第三次轮询落 confirmed
    calls: list = []

    def fake_get(base, path, timeout=10.0):
        calls.append((base, path, timeout))
        try:
            return {"ret": 0, "status": next(seq)}
        except StopIteration:
            return {"ret": 0, "status": "confirmed", "bot_token": "tk-1",
                    "baseurl": "https://ilink2.weixin.qq.com"}

    monkeypatch.setattr(chat_channels, "_ilink_get", fake_get)
    url = "/api/chat-channels/wechat/login/status"
    assert client.get(url, params={"ticket": ticket}).json()["status"] == "wait"
    assert client.get(url, params={"ticket": ticket}).json()["status"] == "scaned"
    assert client.get(url, params={"ticket": ticket}).json()["status"] == "confirmed"
    assert calls[0][0] == chat_channels._ILINK_BASE
    assert "qrcode=qr-1" in calls[0][1]  # qrcode 进 query（quote 形态）
    assert calls[0][2] == 40.0  # hold 式长轮询须 40s 覆盖服务端 35s hold（真机实锤）
    with TestingSessionLocal() as db:
        row = db.execute(select(ChatChannel).where(
            ChatChannel.provider == "wechat")).scalar_one()
    assert row.app_id == "tk-1"                          # 列语义映射：token
    assert row.app_secret == "https://ilink2.weixin.qq.com"  # 列语义映射：baseurl
    assert row.conn_ok is True and row.enabled is True and row.conn_error is None
    assert client.get(url, params={"ticket": ticket}).json()["status"] == "expired"


def test_wechat_login_states(client, monkeypatch):
    """未知票据 expired / IDC 重定向票据内消化（前端按 scaned 显示）/
    verify_code 随 query 透传（need_verifycode 配对码挑战）。"""
    assert client.get("/api/chat-channels/wechat/login/status",
                      params={"ticket": "none"}).json()["status"] == "expired"
    monkeypatch.setattr(chat_channels, "_ilink_get",
                        lambda base, path: {"ret": 0, "qrcode": "q9",
                                            "qrcode_img_content": "u9"})
    ticket = client.post("/api/chat-channels/wechat/login").json()["ticket"]
    paths: list = []

    def fake_get(base, path, timeout=10.0):
        paths.append((base, path))
        if "verify_code=123" in path:
            return {"ret": 0, "status": "need_verifycode"}
        return {"ret": 0, "status": "scaned_but_redirect",
                "redirect_host": "ilink-sh.example.com"}

    monkeypatch.setattr(chat_channels, "_ilink_get", fake_get)
    url = "/api/chat-channels/wechat/login/status"
    assert client.get(url, params={"ticket": ticket}).json()["status"] == "scaned"
    assert chat_channels._wechat_logins[ticket]["poll_base"] == \
        "https://ilink-sh.example.com"
    assert client.get(url, params={"ticket": ticket, "verify_code": "123"}
                      ).json()["status"] == "need_verifycode"
    assert "verify_code=123" in paths[-1][1]


def test_wechat_login_error_422(client, monkeypatch):
    """出码失败（ret≠0 / qrcode 缺失）→ 422 文案直显扫码弹层。"""
    monkeypatch.setattr(chat_channels, "_ilink_get",
                        lambda base, path, timeout=10.0: {"ret": 1, "errmsg": "稍后再试"})
    res = client.post("/api/chat-channels/wechat/login")
    assert res.status_code == 422 and "稍后再试" in res.json()["detail"]
    monkeypatch.setattr(chat_channels, "_ilink_get",
                        lambda base, path, timeout=10.0: {"ret": 0, "qrcode": ""})
    assert client.post("/api/chat-channels/wechat/login").status_code == 422


def test_wechat_login_hold_timeout_is_wait(client, monkeypatch):
    """status 是 hold 式长轮询：ReadTimeout（hold 满未变化）归 wait 不 500——
    2026-10-23 真机实锤（曾 500 打断前端轮询致 confirmed 永远回不到后端）。"""
    monkeypatch.setattr(chat_channels, "_ilink_get",
                        lambda base, path: {"ret": 0, "qrcode": "qh",
                                            "qrcode_img_content": "uh"})
    ticket = client.post("/api/chat-channels/wechat/login").json()["ticket"]

    def hold(base, path, timeout=10.0):
        raise httpx.ReadTimeout("hold")

    monkeypatch.setattr(chat_channels, "_ilink_get", hold)
    res = client.get("/api/chat-channels/wechat/login/status", params={"ticket": ticket})
    assert res.status_code == 200 and res.json()["status"] == "wait"


# —— 微信 iLink 收发 worker（批 4） ————————————————————————————————

def _wechat_msg(mtype=1, uid="u9", ct="ct-1", cid="c-1", text="你好"):
    # 字段照社区 SDK protocol.py 核验（dict 消息，零网络依赖）
    items = [{"type": 1, "text_item": {"text": text}}] if text is not None else \
        [{"type": 2, "image_item": {"url": "http://img"}}]
    return {"message_type": mtype, "from_user_id": uid, "to_user_id": "bot-1",
            "context_token": ct, "client_id": cid, "item_list": items}


def test_wechat_extract_filters():
    ext = chat_channels._wechat_extract
    chat_channels._reset_for_tests()
    assert ext(_wechat_msg(mtype=2, cid="c-2")) is None            # BOT 回显
    assert ext(_wechat_msg(uid="", cid="c-3")) is None             # 空发送者
    ok = ext(_wechat_msg(cid="c-4"))
    assert ok == ("ok", "你好", "u9")                             # 第三元=回帖键
    assert ext(_wechat_msg(cid="c-4")) is None                    # 同 client_id 重投
    assert ext(_wechat_msg(text=None, cid="c-5")) == ("hint", None, None)
    long = ext(_wechat_msg(text="长" * 5000, cid="c-6"))
    assert long[1] == "长" * chat_channels._MAX_TEXT              # 输入截断


def test_strip_markdown_samples():
    strip = chat_channels._strip_markdown
    assert strip("**加粗**与*斜体*") == "加粗与斜体"
    assert strip("# 标题\n正文") == "标题\n正文"
    assert strip("- 事项一\n- 事项二") == "· 事项一\n· 事项二"
    assert strip("1. 甲\n2. 乙") == "甲\n乙"
    assert strip("`code`") == "code"
    assert strip("```python\nprint(1)\n```") == "print(1)\n"
    assert strip("~~误~~") == "误"
    out = strip("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "|" not in out and "---" not in out  # 管道与分隔线全清
    assert "A" in out and "2" in out           # 表内容保留


def test_wechat_worker_loop_and_expired(monkeypatch):
    """长轮询循环：游标回传推进 → USER 消息经 _deliver 投递（reply 闭包携带
    tokens 缓存最新 context_token）→ -14 会话过期落库 conn_ok=False + 退出。
    _ilink_post 响应序列驱动，monkeypatch time.sleep 防真实退避。"""
    chat_channels._reset_for_tests()
    seq = [
        {"ret": 0, "msgs": [], "get_updates_buf": "cur-1"},
        {"ret": 0, "msgs": [_wechat_msg(cid="w-1", ct="ct-9")], "get_updates_buf": "cur-2"},
        {"ret": 0, "errcode": -14, "errmsg": "expired"},
    ]
    posts: list = []

    def fake_post(base, token, path, body, timeout=45.0):
        posts.append((path, dict(body)))
        return seq.pop(0)

    monkeypatch.setattr(chat_channels, "_ilink_post", fake_post)
    delivered: list = []
    monkeypatch.setattr(chat_channels, "_deliver",
                        lambda provider, text, reply, epoch: delivered.append(
                            (provider, text, reply)))
    with TestingSessionLocal() as db:
        db.add(ChatChannel(provider="wechat", app_id="tk-9",
                           app_secret="https://b.example"))
        db.commit()
    chat_channels._wechat_worker("tk-9", "https://b.example", 0)
    # 游标推进：第二轮请求携带第一轮返回的 buf
    assert posts[0][1]["get_updates_buf"] == ""
    assert posts[1][1]["get_updates_buf"] == "cur-1"
    assert len(delivered) == 1 and delivered[0][0] == "wechat"
    assert delivered[0][1] == "你好"
    # reply 闭包用缓存 token（ct-9 而非消息外旧值）
    sent: list = []

    def fake_send(token, baseurl, user_id, context_token, text):
        sent.append((user_id, context_token, text))

    monkeypatch.setattr(chat_channels, "_wechat_send", fake_send)
    delivered[0][2]("回复")
    assert sent == [("u9", "ct-9", "回复")]
    # -14 落库：conn_ok=False + 过期文案（会话过期语义，设置页徽标可见）
    with TestingSessionLocal() as db:
        row = db.execute(select(ChatChannel).where(
            ChatChannel.provider == "wechat")).scalar_one()
    assert row.conn_ok is False and "重新扫码" in row.conn_error
    # worker 收到用户消息时缓存推送目标（二批批 3 插桩）
    assert row.push_target == '{"user_id": "u9"}'


def test_wechat_probe_three_forms(monkeypatch):
    """探测三形态：ReadTimeout（正在 hold）= 有效；-14 = 过期；ret≠0 = 凭据
    失败；秒回 ret==0 = 有效。"""
    chat_channels._reset_for_tests()

    def raise_read_timeout(base, token, path, body, timeout=45.0):
        raise httpx.ReadTimeout("hold")

    monkeypatch.setattr(chat_channels, "_ilink_post", raise_read_timeout)
    assert chat_channels._wechat_probe("tk", "https://b") == (True, None)

    monkeypatch.setattr(chat_channels, "_ilink_post",
                        lambda *a, **k: {"ret": 0, "errcode": -14})
    ok, err = chat_channels._wechat_probe("tk", "https://b")
    assert ok is False and "重新扫码" in err

    monkeypatch.setattr(chat_channels, "_ilink_post",
                        lambda *a, **k: {"ret": -2, "errcode": 0, "errmsg": "参数错误"})
    ok, err = chat_channels._wechat_probe("tk", "https://b")
    assert ok is False and "参数错误" in err

    monkeypatch.setattr(chat_channels, "_ilink_post",
                        lambda *a, **k: {"ret": 0, "msgs": []})
    assert chat_channels._wechat_probe("tk", "https://b") == (True, None)


# —— 批 2：建议卡渠道文本化（对话式确认配套）———————————————————————————


def test_channel_cards_text_forms():
    """纯函数：draft 卡条目（时刻/时长格式化）+ delete 卡清单；applied 与
    inbox/preflight 卡不产文本（走泛化尾注）。"""
    draft_ev = {"type": "draft", "proposal_id": "p1", "draft_plan": [
        {"title": "体检", "start": "2026-10-24T10:00:00", "duration": 30},
        {"title": "取快递"},
        {"title": ""},  # 空标题防御
    ]}
    out = chat_channels._channel_cards_text([draft_ev])
    assert "📋 待确认草案：" in out
    assert "1. 体检 10-24 10:00（30分钟）" in out
    assert "2. 取快递" in out and "3." not in out
    assert "回复「确认」" in out and "「算了」取消" in out

    del_ev = {"type": "proposal", "proposal_id": "p2", "kind": "delete",
              "payload": {"tasks": [{"task_id": "t1", "title": "旧复盘"}]}}
    out2 = chat_channels._channel_cards_text([del_ev])
    assert "🗑 待删除：旧复盘" in out2 and "「确认删除」" in out2

    # applied / inbox 卡不文本化
    assert chat_channels._channel_cards_text([
        {"type": "applied", "proposal_id": "p3", "tasks": []},
        {"type": "proposal", "kind": "inbox", "payload": {"items": [{"title": "礼物"}]}},
    ]) == ""


def test_handle_incoming_delete_card_guide(monkeypatch):
    """delete 卡全链：渠道回复带删除清单与确认指引（confirm_shortcut 消费）。"""
    from services import chat_turn

    _activate_model()
    monkeypatch.setattr(chat_turn, "load_graph", lambda: _FakeGraph({
        "final_reply": "找到 1 个任务", "delete_plan": [
            {"task_id": "t-1", "title": "旧复盘"}]}))
    sent: list = []
    asyncio.run(chat_channels.handle_incoming("feishu", "删掉旧复盘", sent.append))
    assert len(sent) == 1
    assert "🗑 待删除：旧复盘" in sent[0] and "「确认删除」" in sent[0]
    assert "待确认草案" not in sent[0]


# —— 定时任务推送链（2026-10-23 二批批 3：开关列 / 目标缓存 / 四渠道分发） ————


def test_push_columns_and_toggle():
    """新列默认关；set_push_enabled 翻转（未配置 LookupError）；_row_to_out 双键。"""
    with TestingSessionLocal() as db:
        db.add(ChatChannel(provider="feishu", app_id="f1", app_secret="fs"))
        db.commit()
        row = chat_channels.get_channel(db, "feishu")
        assert row.push_enabled is False and row.push_target is None
        out = chat_channels._row_to_out(row, chat_channels._meta_of("feishu"))
        assert out["push_enabled"] is False and out["push_ready"] is False
        chat_channels.set_push_enabled(db, "feishu", True)
        assert row.push_enabled is True
    with pytest.raises(LookupError):
        with TestingSessionLocal() as db:
            chat_channels.set_push_enabled(db, "dingtalk", True)


def test_save_push_target_cache_and_skip(monkeypatch):
    chat_channels._reset_for_tests()
    with TestingSessionLocal() as db:
        db.add(ChatChannel(provider="feishu", app_id="f1", app_secret="fs"))
        db.commit()

    chat_channels._save_push_target("feishu", {"open_id": "ou-1"})
    with TestingSessionLocal() as db:
        assert chat_channels.get_channel(db, "feishu").push_target == '{"open_id": "ou-1"}'

    # 同目标命中缓存短路——不再触库（get_channel 打桩探测）
    real = chat_channels.get_channel
    probed: list = []
    monkeypatch.setattr(chat_channels, "get_channel",
                        lambda db, p: probed.append(p) or real(db, p))
    chat_channels._save_push_target("feishu", {"open_id": "ou-1"})
    assert probed == []

    # 目标变化 → 更新；空目标不写
    chat_channels._save_push_target("feishu", {"open_id": "ou-2"})
    chat_channels._save_push_target("feishu", {})
    with TestingSessionLocal() as db:
        assert chat_channels.get_channel(db, "feishu").push_target == '{"open_id": "ou-2"}'


def test_push_to_channels_dispatch(monkeypatch):
    """只发 enabled ∧ push_enabled ∧ 有目标的行；微信无会话令牌跳过、注入后可达；
    enabled=False（停用监听）即使推送开着也整体跳过——监听与推送正交但停用优先。"""
    chat_channels._reset_for_tests()
    with TestingSessionLocal() as db:
        db.add(ChatChannel(provider="feishu", app_id="f-id", app_secret="f-sec",
                           push_enabled=True, push_target='{"open_id": "ou-1"}'))
        db.add(ChatChannel(provider="dingtalk", app_id="d-id", app_secret="d-sec",
                           push_enabled=True, push_target='{"staff_id": "s-1"}'))
        db.add(ChatChannel(provider="wechat", app_id="w-tk", app_secret="w-base",
                           push_enabled=True, push_target='{"user_id": "u-9"}'))
        db.add(ChatChannel(provider="wecom", app_id="wc-1", app_secret="wc-sec",
                           enabled=False, push_enabled=True,
                           push_target='{"userid": "wcu"}'))  # 停用 → 不推
        db.commit()

    sent: list = []
    monkeypatch.setattr(chat_channels, "_feishu_send",
                        lambda *a: sent.append(("feishu", a)))
    monkeypatch.setattr(chat_channels, "_dingtalk_push",
                        lambda *a: sent.append(("dingtalk", a)))
    monkeypatch.setattr(chat_channels, "_wecom_push",
                        lambda *a: sent.append(("wecom", a)))
    monkeypatch.setattr(chat_channels, "_wechat_send",
                        lambda *a: sent.append(("wechat", a)))

    chat_channels.push_to_channels("早报内容")
    assert {s[0] for s in sent} == {"feishu", "dingtalk"}  # wecom 停用 / wechat 无令牌
    f = [s for s in sent if s[0] == "feishu"][0][1]
    assert f == ("f-id", "f-sec", "ou-1", "早报内容")

    # 微信令牌注入后可达（provider 行 app_id=token / app_secret=baseurl 语义映射）
    chat_channels._wechat_ctx_tokens["u-9"] = "ct-1"
    sent.clear()
    chat_channels.push_to_channels("早报内容")
    assert {s[0] for s in sent} == {"feishu", "dingtalk", "wechat"}
    w = [s for s in sent if s[0] == "wechat"][0][1]
    assert w == ("w-tk", "w-base", "u-9", "ct-1", "早报内容")

    # 推送关（push_enabled=False）即使目标令牌齐全也不发
    with TestingSessionLocal() as db:
        chat_channels.set_push_enabled(db, "wechat", False)
    sent.clear()
    chat_channels.push_to_channels("早报内容")
    assert {s[0] for s in sent} == {"feishu", "dingtalk"}


def test_dingtalk_push_request_shape(monkeypatch):
    """两跳请求形态：accessToken（appKey/appSecret）→ batchSend（token header +
    robotCode/userIds/msgKey sampleMarkdown/msgParam JSON 串）。"""
    posts: list = []

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posts.append((url, json, headers))
            if "accessToken" in url:
                return SimpleNamespace(status_code=200, is_success=True,
                                       json=lambda: {"accessToken": "tk-1"}, text="")
            return SimpleNamespace(status_code=200, is_success=True,
                                   json=lambda: {"processQueryKey": "q"}, text="")

    monkeypatch.setattr(chat_channels.httpx, "Client", _FakeClient)
    chat_channels._dingtalk_push("app-1", "sec", "staff-9", "早报")
    assert posts[0][0].endswith("/v1.0-0.0/oauth2/accessToken")
    assert posts[0][1] == {"appKey": "app-1", "appSecret": "sec"}
    url, body, headers = posts[1]
    assert url.endswith("/robot/oToMessages/batchSend")
    assert headers == {"x-acs-dingtalk-access-token": "tk-1"}
    assert body["robotCode"] == "app-1"
    assert body["userIds"] == ["staff-9"]
    assert body["msgKey"] == "sampleMarkdown"
    assert json.loads(body["msgParam"]) == {"title": "日程助手", "text": "早报"}


def test_wecom_push_reuses_live_client(monkeypatch):
    """在线 worker 优先：跨线程经 _wecom_prev 的 (client, loop) 投协程发送，
    不另建连接（同 bot 单连接红线）。"""
    chat_channels._reset_for_tests()
    sent: list = []

    class _FakeClient:
        async def send_message(self, chatid, body):
            sent.append((chatid, body))

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(chat_channels, "_wecom_prev", (_FakeClient(), loop))
        chat_channels._wecom_push("b1", "s1", "u1", "早报")
        assert sent == [("u1", {"msgtype": "markdown", "markdown": {"content": "早报"}})]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        chat_channels._drain_stopped_loop(loop)


def test_wecom_push_short_connect(monkeypatch):
    """worker 不在线（_wecom_prev=None）→ 独立短连：connect → send_message →
    disconnect 发完即断。fake aibot 模块注入（sys.modules 打桩零 SDK 依赖）。"""
    import sys
    from types import ModuleType

    chat_channels._reset_for_tests()
    calls: list = []

    class _FakeWS:
        def __init__(self, opts):
            calls.append(("init", opts.bot_id, opts.secret))

        async def connect(self):
            calls.append(("connect",))

        async def send_message(self, chatid, body):
            calls.append(("send", chatid, body))

        async def disconnect(self):
            calls.append(("disconnect",))

    fake = ModuleType("aibot")
    fake.WSClient = _FakeWS
    fake.WSClientOptions = SimpleNamespace
    monkeypatch.setitem(sys.modules, "aibot", fake)
    chat_channels._wecom_push("b1", "s1", "u1", "早报")
    assert ("send", "u1", {"msgtype": "markdown", "markdown": {"content": "早报"}}) in calls
    assert calls[-1] == ("disconnect",)  # 发完即断（不留残活连接踢 worker）


def test_push_endpoint_toggle(client, monkeypatch):
    """PUT /{provider}/push：未配置 404；配置后翻转；出参带 push_enabled/push_ready。"""
    _patch_probe(monkeypatch, 200, {"code": 0, "tenant_access_token": "t-x"})
    client.put("/api/chat-channels/feishu",
               json={"app_id": "f1", "app_secret": "fs"})
    res = client.put("/api/chat-channels/feishu/push",
                     json={"push_enabled": True})
    assert res.status_code == 200
    body = res.json()
    assert body["push_enabled"] is True and body["push_ready"] is False

    res = client.put("/api/chat-channels/dingtalk/push",
                     json={"push_enabled": True})
    assert res.status_code == 404
    # 开关落库持久
    with TestingSessionLocal() as db:
        assert chat_channels.get_channel(db, "feishu").push_enabled is True


def test_fire_push_background(monkeypatch):
    """_fire_push 后台真跑 push_to_channels（不阻塞主协程）；空文本与无事件循环
    直调均静默丢弃。"""
    from services import workflows as wf

    pushed: list = []
    monkeypatch.setattr(chat_channels, "push_to_channels",
                        lambda text: pushed.append(text))

    async def main():
        wf._fire_push("早报全文")
        await asyncio.sleep(0.05)  # 让 to_thread 后台任务收割

    asyncio.run(main())
    assert pushed == ["早报全文"]
    # 空文本丢弃（无循环上下文外先验空——不触 get_running_loop）
    wf._fire_push("   ")
    assert pushed == ["早报全文"]
    # 无事件循环直调：静默丢弃（同步测试环境天然无循环）
    wf._fire_push("非空文本但无循环")
    assert pushed == ["早报全文"]
