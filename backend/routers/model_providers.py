"""模型接入读/写/激活/测速接口。

薄壳层——调 service、按约定翻译状态码（ConflictError→409、ValueError→422、
不存在→404）；不含任何业务逻辑。文件名与路由前缀不对仗（/api/models），
为避免与根 models.py 同名（口径见设置页"模型接入"计划）。

端点一览：
- GET /api/models                模型全量（按创建序）
- POST /api/models               新建（201；重名 409）
- PATCH /api/models/{id}         部分更新（键不出现不动；不开放激活/测速字段）
- DELETE /api/models/{id}        硬删（204；删激活行后库处于零激活态）
- POST /api/models/{id}/activate 激活（同事务互斥，全局至多一行）
- POST /api/models/{id}/test     对已保存模型测速（结果落库；探测失败也是 200）
- POST /api/models/test          草稿参数纯探测（不读库不落库）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    ModelProviderCreate,
    ModelProviderOut,
    ModelProviderTestIn,
    ModelProviderTestOut,
    ModelProviderUpdate,
)
from services import model_providers as model_service
from services.model_providers import ConflictError

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelProviderOut])
def list_models(db: Session = Depends(get_db)):
    return model_service.list_models(db)


@router.post("", response_model=ModelProviderOut, status_code=201)
def create_model(body: ModelProviderCreate, db: Session = Depends(get_db)):
    try:
        return model_service.create_model(db, body)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.patch("/{model_id}", response_model=ModelProviderOut)
def update_model(model_id: str, body: ModelProviderUpdate, db: Session = Depends(get_db)):
    try:
        model = model_service.update_model(db, model_id, body)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return model


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str, db: Session = Depends(get_db)):
    if not model_service.delete_model(db, model_id):
        raise HTTPException(status_code=404, detail="模型不存在")


@router.post("/{model_id}/activate", response_model=ModelProviderOut)
def activate_model(model_id: str, db: Session = Depends(get_db)):
    model = model_service.activate_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return model


@router.post("/{model_id}/aux", response_model=ModelProviderOut)
def set_aux_model(model_id: str, db: Session = Depends(get_db)):
    """设/取消轻量助手模型（2026-11-01 优化批 9，toggle）：小调用白名单
    （路由/分类/判定/反思/采集摘要）走此行——本地小模型可亚秒级；未配置
    回落激活模型（行为不变）。"""
    try:
        return model_service.set_aux_model(db, model_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="模型不存在")


@router.post("/{model_id}/test", response_model=ModelProviderTestOut)
def test_model(model_id: str, db: Session = Depends(get_db)):
    result = model_service.test_model(db, model_id)
    if result is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return result


@router.post("/test", response_model=ModelProviderTestOut)
def test_model_draft(body: ModelProviderTestIn):
    # 草稿探测：表单"测试连通性"按钮用当前填写参数，纯函数调用即可（无 db）
    return model_service.probe_model(body.model_name, body.api_key, body.base_url)
