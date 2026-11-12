"""天气对照服务：Open-Meteo 免费无 key API（中文城市名 geocoding → 7 天逐日预报）。

排期智能的天气输入源（2026-10-18 增，用户需求：户外活动排期应对照天气——"明天
上午爬山但预报有雨"要能提醒）。消费方三处：排期子图（对话
排期）、agents/graph._reduce_phase（晨间批量推荐）、qa_answerer（"明天适合爬山吗"）
——恒经本模块取数，节点不直接摸 httpx。

设计约束：
- 永不抛出：城市未配置/未命中/网络失败/解析失败一律返回 None + warning 日志——
  天气是增强信号不是排期依赖，绝不能阻塞排期链
- 进程内双缓存：坐标（城市名 → lat/lon，地名坐标极稳定，进程内永久）与预报
  （TTL 6h——多轮对话/晨间批量重复请求不重复外呼；免费档 10k 次/天余量巨大）
- 同步 httpx：消费节点均为同步函数（FastAPI 线程池跑），不引 async 依赖
"""

import logging
import threading
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

# 预报缓存 TTL：6 小时（天气日内变化少，同会话重复排期不重复外呼）
_TTL = timedelta(hours=6)
# 旧值上限（2026-10-26 SWR 增）：过期缓存先回旧值后台刷新；超过该限视为彻底
# 失效，不再回旧值（走同步重拉——持续网络故障下不让用户看无限旧的预报）
_STALE_LIMIT = timedelta(hours=24)
# 单请求超时：两个端点都轻，8s 足够（超时即静默 None，不重试——下次调用再试）
_TIMEOUT_S = 8.0
# 预报天数：排期的冲突/相似窗口集中在一周内
_FORECAST_DAYS = 7

# 坐标缓存 {城市名(lower): (lat, lon)}
_geo_cache: dict[str, tuple[float, float]] = {}
# 预报缓存 {城市名(lower): (取数时刻, 预报行列表)}
_forecast_cache: dict[str, tuple[datetime, list[dict]]] = {}
# 后台刷新去重集合（SWR：同 key 并发调用只投一个刷新线程）
_refreshing: set[str] = set()

# WMO weather interpretation code → 中文文案（Open-Meteo daily.weather_code 取值
# 子集；未覆盖码回落"天气码 N"——LLM 仍可结合 precip 降水概率判断）
_WMO_TEXT = {
    0: "晴", 1: "大部晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}


def _fetch_json(url: str, params: dict) -> dict:
    """单层 GET → JSON（trust_env=False 铁律：本机代理会拦掉对外 API 请求）。"""
    with httpx.Client(timeout=_TIMEOUT_S, trust_env=False) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _geocode(city: str) -> tuple[float, float] | None:
    """城市名（支持中文）→ (lat, lon)。geocoding 取首个结果；未命中/失败 → None。"""
    key = city.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        data = _fetch_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": city.strip(), "count": 1, "language": "zh", "format": "json"},
        )
        results = data.get("results") or []
        if not results:
            logger.warning("天气城市「%s」未在 geocoding 命中，天气对照停用", city)
            return None
        coord = (float(results[0]["latitude"]), float(results[0]["longitude"]))
        _geo_cache[key] = coord
        return coord
    except Exception as exc:  # 永不抛出：网络/解析失败 = 天气不可用（下次调用再试）
        logger.warning("天气城市「%s」geocoding 失败：%s", city, exc)
        return None


def probe_city(city: str) -> dict | None:
    """城市探测（设置页「测试」钮，2026-10-21 复查批 D2）：geocoding 命中 →
    {"name", "admin1"}（城市与地区名，供回执文案"已识别：杭州, 浙江"）；未命中 /
    网络失败 → None。命中即顺带预热坐标缓存（探测过 = 排期链零首跑延迟）。
    与 _geocode 分立不共用缓存读取（缓存只存坐标不存名称，共用会互吃信息）。"""
    try:
        data = _fetch_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": city.strip(), "count": 1, "language": "zh", "format": "json"},
        )
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        _geo_cache[city.strip().lower()] = (float(r["latitude"]), float(r["longitude"]))
        return {"name": str(r.get("name") or ""), "admin1": str(r.get("admin1") or "")}
    except Exception:
        logger.warning("天气城市探测失败：%s", city, exc_info=True)
        return None


def forecast_for_city(city: str | None) -> list[dict] | None:
    """城市未来 7 天逐日预报（排期/问答提示词的 weather 键）。

    返回行形如 {"date": "2026-10-19", "text": "小雨", "tmax": 21.0, "tmin": 15.0,
    "precip": 80}（precip = 当日最大降水概率 %，缺测为 None）。city 空/未命中/
    网络失败 → None（调用方以 null 注入提示词，提示词声明 null=未配置城市）。

    stale-while-revalidate（2026-10-26 增）：缓存过期（6h~24h）时返回旧值 +
    后台线程刷新——8s 同步外呼不再横在 qa/intake/refine/context_fetcher 关键
    路径上（排期链被网络抖动卡住的唯一通路根除）；超过 _STALE_LIMIT 或无缓存
    才同步等待（首拉语义不变）。
    """
    if not city or not city.strip():
        return None
    key = city.strip().lower()
    hit = _forecast_cache.get(key)
    age = datetime.now() - hit[0] if hit else None
    if hit is not None and age < _TTL:
        return hit[1]
    if hit is not None and age < _STALE_LIMIT:
        if key not in _refreshing:  # 并发调用只投一个刷新线程
            _refreshing.add(key)
            threading.Thread(
                target=_refresh_in_background, args=(city, key), daemon=True).start()
        logger.info("天气预报缓存过期（city=%s），先回旧值后台刷新", city)
        return hit[1]
    return _fetch_forecast(city, key)


def _refresh_in_background(city: str, key: str) -> None:
    """后台刷新线程体：成功即更新缓存（下次调用命中新值），失败保持旧值等下次再试。"""
    try:
        _fetch_forecast(city, key)
    finally:
        _refreshing.discard(key)


def _fetch_forecast(city: str, key: str) -> list[dict] | None:
    """同步取预报并写缓存（首拉与后台刷新共用体；永不抛出）。"""
    coord = _geocode(city)
    if coord is None:
        return None
    try:
        data = _fetch_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": coord[0],
                "longitude": coord[1],
                "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                          "precipitation_probability_max"),
                "timezone": "auto",
                "forecast_days": _FORECAST_DAYS,
            },
        )
        daily = data.get("daily") or {}
        rows = [{
            "date": d,
            "text": _WMO_TEXT.get(code, f"天气码 {code}"),
            "tmax": tmax,
            "tmin": tmin,
            "precip": precip,
        } for d, code, tmax, tmin, precip in zip(
            daily.get("time") or [],
            daily.get("weather_code") or [],
            daily.get("temperature_2m_max") or [],
            daily.get("temperature_2m_min") or [],
            daily.get("precipitation_probability_max") or [],
        )]
        rows = [r for r in rows if r.get("date")]
        if not rows:
            logger.warning("天气预报响应无有效日行（city=%s），本轮天气对照跳过", city)
            return None
        _forecast_cache[key] = (datetime.now(), rows)
        return rows
    except Exception as exc:  # 永不抛出（同 _geocode 口径）
        logger.warning("天气预报获取失败（city=%s）：%s", city, exc)
        return None


def _reset_cache_for_tests() -> None:
    """测试隔离：清空模块级缓存（防跨用例命中上例缓存短路网络替身）。"""
    _geo_cache.clear()
    _forecast_cache.clear()
    _refreshing.clear()
