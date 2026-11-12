"""聊天通讯渠道接入接口（前缀 /api/chat-channels，2026-10-23 增）。

薄壳层——调 service、按约定翻译状态码（ValueError→422 / LookupError→404）。
全同步 def：probe 是同步 httpx 调用，FastAPI 同步路由自动跑线程池
（cli_tools 先例），不占事件循环。

端点一览：
- GET    /api/chat-channels                 三渠道目录 + 接入行状态（单一事实源）
- POST   /api/chat-channels/wechat/login    发起微信扫码登录（出二维码）
- GET    /api/chat-channels/wechat/login/status?ticket=…&verify_code=… 轮询扫码
                                             状态（confirmed 即落库拉起 worker）
- PUT    /api/chat-channels/{provider}      保存/更新凭据（保存即探测；wechat 422）
- POST   /api/chat-channels/{provider}/test 重测已存凭据（未配置 404）
- PUT    /api/chat-channels/{provider}/enabled 启停 worker（未配置 404）
- DELETE /api/chat-channels/{provider}      删除接入（204；未配置 404）

wechat/login 两端点为字面量路由，注册于 /{provider} 系之前（cli_tools 先例——
同段数时模板路由会吞字面量路径）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    ChatChannelEnabledBody,
    ChatChannelOut,
    ChatChannelPushBody,
    ChatChannelUpsertBody,
)
from services import chat_channels

router = APIRouter(prefix="/api/chat-channels", tags=["chat-channels"])


@router.get("", response_model=list[ChatChannelOut])
def list_channels(db: Session = Depends(get_db)):
    return chat_channels.list_channels(db)


@router.post("/wechat/login")
def start_wechat_login():
    """发起扫码：返回 {ticket, qr_url}——qr_url 为二维码载体（URL 字符串），
    前端渲染成二维码图。服务端错误翻 422 直显弹层。"""
    try:
        return chat_channels.start_wechat_login()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/wechat/login/status")
def poll_wechat_login(ticket: str, verify_code: str | None = None,
                      db: Session = Depends(get_db)):
    """轮询扫码状态（wait/scaned/confirmed/expired/need_verifycode 等，见
    service 状态机 docstring）。verify_code 仅 need_verifycode 挑战时由前端带回。"""
    try:
        return chat_channels.poll_wechat_login(db, ticket, verify_code)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.put("/{provider}", response_model=ChatChannelOut)
def upsert_channel(provider: str, body: ChatChannelUpsertBody, db: Session = Depends(get_db)):
    try:
        chat_channels.upsert_channel(db, provider, body.app_id, body.app_secret)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return chat_channels.describe_channel(db, provider)


@router.post("/{provider}/test", response_model=ChatChannelOut)
def test_channel(provider: str, db: Session = Depends(get_db)):
    try:
        chat_channels.test_channel(db, provider)
    except LookupError:
        raise HTTPException(status_code=404, detail="该渠道尚未配置凭据")
    return chat_channels.describe_channel(db, provider)


@router.put("/{provider}/enabled", response_model=ChatChannelOut)
def set_enabled(provider: str, body: ChatChannelEnabledBody, db: Session = Depends(get_db)):
    try:
        chat_channels.set_enabled(db, provider, body.enabled)
    except LookupError:
        raise HTTPException(status_code=404, detail="该渠道尚未配置凭据")
    return chat_channels.describe_channel(db, provider)


@router.put("/{provider}/push", response_model=ChatChannelOut)
def set_push_enabled(provider: str, body: ChatChannelPushBody, db: Session = Depends(get_db)):
    """定时任务推送开关（与 enabled 正交：监听关、推送仍可开）。"""
    try:
        chat_channels.set_push_enabled(db, provider, body.push_enabled)
    except LookupError:
        raise HTTPException(status_code=404, detail="该渠道尚未配置凭据")
    return chat_channels.describe_channel(db, provider)


@router.delete("/{provider}", status_code=204)
def delete_channel(provider: str, db: Session = Depends(get_db)):
    try:
        chat_channels.delete_channel(db, provider)
    except LookupError:
        raise HTTPException(status_code=404, detail="该渠道尚未配置凭据")
