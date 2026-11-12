"""数据接入接口（Composio，2026-09-18 增）。

薄壳层——调 service、按约定翻译状态码；不含任何业务逻辑。

端点一览：
- GET    /api/integrations/key                 Key 配置读（get_or_create 落行）
- PUT    /api/integrations/key                 保存/清除 Key（保存即验证，失败也是 200——失败是数据）
- POST   /api/integrations/key/verify          对已存 Key 手动重验
- GET    /api/integrations/apps?search=…       搜索 Composio 应用市场
- POST   /api/integrations/connect             发起 OAuth 连接（返回重定向 URL 与轮询锚）
- GET    /api/integrations/connect/{ca_id}     连接状态（前端轮询）
- GET    /api/integrations/connections         已连接应用列表（真源 = Composio 云端；带本地采集开关）
- POST   /api/integrations/connections/{ca_id}/test  测试连接（失败也是 200——失败是数据）
- PUT    /api/integrations/connections/{ca_id}/enabled 采集启停（镜像 ingest_sources）
- DELETE /api/integrations/connections/{ca_id} 断开连接（连带清理本地启停镜像行）

状态码翻译：ConflictError/KeyNotConfiguredError→409、UpstreamError→502、
ValueError→422、不存在→404。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    ComposioAppOut,
    ComposioConnectIn,
    ComposioConnectOut,
    ComposioConnectStatusOut,
    ComposioConnectionOut,
    ComposioConnectionTestOut,
    ComposioEnabledIn,
    ComposioKeyIn,
    ComposioKeyOut,
)
from services import integrations as integration_service
from services.integrations import ConflictError, KeyNotConfiguredError, UpstreamError

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# 409 类：业务冲突（已连接）与未配置 Key（需先完成前置配置）
_CONFLICT_ERRORS = (ConflictError, KeyNotConfiguredError)


@router.get("/key", response_model=ComposioKeyOut)
def get_key(db: Session = Depends(get_db)):
    return integration_service.get_key_info(db)


@router.put("/key", response_model=ComposioKeyOut)
def save_key(body: ComposioKeyIn, db: Session = Depends(get_db)):
    # 验证失败也是 200：失败是数据（key_ok=False + key_error），同模型探测口径
    return integration_service.save_api_key(db, body)


@router.post("/key/verify", response_model=ComposioKeyOut)
def verify_key(db: Session = Depends(get_db)):
    return integration_service.reverify_key(db)


@router.get("/apps", response_model=list[ComposioAppOut])
def search_apps(
    search: str = Query(min_length=1, max_length=60),
    db: Session = Depends(get_db),
):
    try:
        return integration_service.search_apps(db, search)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/connect", response_model=ComposioConnectOut, status_code=201)
def connect_app(body: ComposioConnectIn, db: Session = Depends(get_db)):
    try:
        return integration_service.start_connect(db, body)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/connect/{connected_account_id}", response_model=ComposioConnectStatusOut)
def connect_status(connected_account_id: str, db: Session = Depends(get_db)):
    try:
        status = integration_service.get_connect_status(db, connected_account_id)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if status is None:
        raise HTTPException(status_code=404, detail="连接不存在")
    return status


@router.get("/connections", response_model=list[ComposioConnectionOut])
def list_connections(db: Session = Depends(get_db)):
    try:
        return integration_service.list_connections(db)
    except KeyNotConfiguredError:
        # 未配 Key 的已连接列表恒空（2026-11-04 真机日志批）：GET 是设置页挂载的
        # 被动拉取，409 冲突语义留给 POST 类主动作（search/connect 需引导配置）；
        # 旧版 409 每次打开数据接入页必刷一行错误日志（前端虽静默归空，日志噪音
        # 与语义双输）。service 层不动——data_ingest 源枚举依赖该异常跳过 Composio
        return []
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/connections/{connected_account_id}/test", response_model=ComposioConnectionTestOut)
def test_connection(connected_account_id: str, db: Session = Depends(get_db)):
    try:
        result = integration_service.test_connection(db, connected_account_id)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail="连接不存在")
    return result


@router.put("/connections/{connected_account_id}/enabled", status_code=204)
def set_connection_enabled(
    connected_account_id: str, body: ComposioEnabledIn, db: Session = Depends(get_db)
):
    try:
        ok = integration_service.set_connection_enabled(db, connected_account_id, body.enabled)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="连接不存在")


@router.delete("/connections/{connected_account_id}", status_code=204)
def delete_connection(connected_account_id: str, db: Session = Depends(get_db)):
    try:
        ok = integration_service.disconnect(db, connected_account_id)
    except _CONFLICT_ERRORS as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="连接不存在")
