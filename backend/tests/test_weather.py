"""天气对照服务测试（services/weather.py；网络替身 mock _fetch_json，零真实外呼）。

覆盖：空城市 None / geocoding 未命中 None / 两跳正常返回行形状与 WMO 文案 /
坐标与预报双层缓存命中（第二次调用零外呼）/ TTL 过期重新取数 / 网络异常静默 None /
_reset_cache_for_tests 隔离。
"""

from datetime import datetime, timedelta

import pytest

from services import weather


@pytest.fixture(autouse=True)
def _clean_cache():
    weather._reset_cache_for_tests()
    yield
    weather._reset_cache_for_tests()


def _install_fetch(monkeypatch, *, geo=None, forecast=None, fail_on=None):
    """网络替身：按 URL 分流 geocoding/forecast；fail_on 命中即抛（模拟网络失败）。
    返回调用计数 dict（断言缓存命中用）。"""
    calls = {"geo": 0, "forecast": 0}

    def fake(url, params):
        if fail_on and fail_on in url:
            raise ConnectionError("网络不可达（测试注入）")
        if "geocoding" in url:
            calls["geo"] += 1
            return geo if geo is not None else {}
        calls["forecast"] += 1
        return forecast if forecast is not None else {}

    monkeypatch.setattr(weather, "_fetch_json", fake)
    return calls


_GEO = {"results": [{"latitude": 30.25, "longitude": 120.17}]}
_FORECAST = {
    "daily": {
        "time": ["2026-10-18", "2026-10-19"],
        "weather_code": [0, 61],
        "temperature_2m_max": [26.0, 21.0],
        "temperature_2m_min": [18.0, 15.0],
        "precipitation_probability_max": [10, 80],
    }
}


def test_empty_city_returns_none(monkeypatch):
    calls = _install_fetch(monkeypatch)
    assert weather.forecast_for_city(None) is None
    assert weather.forecast_for_city("   ") is None
    assert calls["geo"] == 0  # 空城市零外呼


def test_geocode_miss_returns_none(monkeypatch):
    _install_fetch(monkeypatch, geo={})  # results 空
    assert weather.forecast_for_city("不存在的城市") is None


def test_normal_two_hop_shape_and_text(monkeypatch):
    _install_fetch(monkeypatch, geo=_GEO, forecast=_FORECAST)
    rows = weather.forecast_for_city("杭州")
    assert rows == [
        {"date": "2026-10-18", "text": "晴", "tmax": 26.0, "tmin": 18.0, "precip": 10},
        {"date": "2026-10-19", "text": "小雨", "tmax": 21.0, "tmin": 15.0, "precip": 80},
    ]


def test_unknown_wmo_code_falls_back(monkeypatch):
    fc = {"daily": {"time": ["2026-10-18"], "weather_code": [42],
                    "temperature_2m_max": [1.0], "temperature_2m_min": [0.0],
                    "precipitation_probability_max": [None]}}
    _install_fetch(monkeypatch, geo=_GEO, forecast=fc)
    rows = weather.forecast_for_city("杭州")
    assert rows[0]["text"] == "天气码 42" and rows[0]["precip"] is None


def test_cache_hit_no_second_call(monkeypatch):
    calls = _install_fetch(monkeypatch, geo=_GEO, forecast=_FORECAST)
    weather.forecast_for_city("杭州")
    weather.forecast_for_city("杭州")  # 第二次走缓存
    assert calls["geo"] == 1 and calls["forecast"] == 1
    weather.forecast_for_city("  杭州 ")  # strip+lower 归一同键，仍走缓存
    assert calls["forecast"] == 1


def test_ttl_expiry_refetches(monkeypatch):
    calls = _install_fetch(monkeypatch, geo=_GEO, forecast=_FORECAST)
    weather.forecast_for_city("杭州")
    # 手动把缓存时间戳拨旧 30h（超 _STALE_LIMIT 24h——2026-10-26 SWR 起 6h~24h
    # 走旧值+后台刷新，超限才同步重拉）→ 下次调用同步重新外呼
    key = "杭州"
    ts, rows = weather._forecast_cache[key]
    weather._forecast_cache[key] = (ts - timedelta(hours=30), rows)
    assert datetime.now() - weather._forecast_cache[key][0] > weather._STALE_LIMIT
    weather.forecast_for_city("杭州")
    assert calls["forecast"] == 2


def test_stale_cache_returns_old_and_refreshes_background(monkeypatch):
    # SWR（2026-10-26 提速批 1）：过期（6h~24h）立即返回旧值零阻塞 + 后台线程刷新
    # 缓存——8s 同步外呼不再横在排期/问答关键路径上
    import time as _time

    calls = _install_fetch(monkeypatch, geo=_GEO, forecast=_FORECAST)
    old = [{"date": "2026-10-01", "text": "旧值", "tmax": 1.0, "tmin": 0.0, "precip": 0}]
    weather._geo_cache["杭州"] = (30.25, 120.17)
    weather._forecast_cache["杭州"] = (datetime.now() - timedelta(hours=7), old)
    got = weather.forecast_for_city("杭州")
    assert got is old  # 旧值先回（同步返回，不等网络）
    for _ in range(200):  # 等后台刷新线程落缓存（daemon 线程，4s 上限轮询）
        if calls["forecast"] >= 1 and weather._forecast_cache["杭州"][1] is not old:
            break
        _time.sleep(0.02)
    assert calls["forecast"] == 1  # 后台真刷了且只刷一次
    new = weather.forecast_for_city("杭州")  # 新值已入缓存（TTL 内直接命中）
    assert new is not old and new[0]["text"] == "晴"


def test_network_failure_silent_none(monkeypatch):
    _install_fetch(monkeypatch, fail_on="geocoding")
    assert weather.forecast_for_city("杭州") is None  # geocoding 失败静默
    calls = _install_fetch(monkeypatch, geo=_GEO, fail_on="forecast")
    assert weather.forecast_for_city("杭州") is None  # forecast 失败静默
    # fail_on 的 raise 先于替身计数（forecast 恒 0）；geo==1 证明已过坐标解析走到预报步
    assert calls["geo"] == 1 and calls["forecast"] == 0


def test_empty_daily_rows_returns_none(monkeypatch):
    _install_fetch(monkeypatch, geo=_GEO, forecast={"daily": {}})
    assert weather.forecast_for_city("杭州") is None


# —— 法定假日静态年表（优化批 6）———————————————————————————————————————


def test_holiday_table_lookups():
    """假日年表（优化批 6）：已知假日命名 / 前瞻窗口 / 表外年份降级 None
    （不猜不编）；调休表恒空（不录不错，录错更糟）。"""
    from datetime import date

    from services.holidays import (
        holiday_name,
        is_extra_workday,
        upcoming_holidays,
    )

    assert holiday_name(date(2026, 10, 1)) == "中秋国庆"
    assert holiday_name(date(2026, 1, 4)) is None       # 普通周日
    assert holiday_name(date(2027, 1, 1)) is None       # 表外年份降级
    assert not is_extra_workday(date(2026, 2, 14))
    ups = upcoming_holidays(days=10, today=date(2026, 9, 28))
    assert ups and ups[0]["name"] == "中秋国庆" and ups[0]["in_days"] == 3
    assert upcoming_holidays(days=5, today=date(2026, 11, 10)) == []
