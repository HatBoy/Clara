"""应用设置接口（单行资源，无 id 路径段）。

薄壳层——调 service、按约定翻译状态码（ValueError→422）；不含任何业务逻辑。

端点一览：
- GET /api/settings    读取设置（空库时 get_or_create 落默认行）
- PATCH /api/settings  部分更新（键不出现不动；返回全量供前端回填修复值）
- GET /api/settings/weather-test  天气城市探测回执（设置页「测试」钮）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import SettingsOut, SettingsUpdate
from services import settings as settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return settings_service.get_settings(db)


@router.patch("", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    try:
        return settings_service.update_settings(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/weather-test")
def test_weather_city(city: str):
    """探测城市名能否被 geocoding 解析（错拼静默失效不再无从发现）。命中回
    "已识别：城市, 地区"；未命中 422 提示换名——排期链与探测同源，探测过即真可用。"""
    from services.weather import probe_city

    hit = probe_city(city)
    if hit is None:
        raise HTTPException(status_code=422,
                            detail=f"未识别城市「{city}」：请检查拼写，或换用更通行的名称")
    where = hit["name"] + (f", {hit['admin1']}" if hit["admin1"] else "")
    return {"ok": True, "resolved": where}
