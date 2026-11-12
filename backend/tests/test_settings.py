"""应用设置接口测试（基建见 conftest.py）。

覆盖：空库 get_or_create 默认值 / 部分更新语义（键不出现不动）/ 契约层与
service 层校验（枚举、分钟范围与粒度、可视跨度下限）/ 午休越界自动修复
（clamp 与容不下自动关）/ reminder 显式 null / 空 PATCH 不抬 updated_at。
"""

from fastapi.testclient import TestClient

from conftest import TestingSessionLocal
from models import AppSettings
from services.settings import DEFAULTS


def _row() -> AppSettings:
    """ORM 直读设置行（断言落库值用）。"""
    with TestingSessionLocal() as db:
        return db.get(AppSettings, 1)


# —— 采集设置：ingest_llm_budget 落库 ——————————————————————————————


def test_patch_llm_budget_persists_and_clamps(client: TestClient):
    client.get("/api/settings")
    assert client.patch("/api/settings", json={"ingest_llm_budget": 50}).json()["ingest_llm_budget"] == 50
    assert _row().ingest_llm_budget == 50
    assert client.patch("/api/settings", json={"ingest_llm_budget": 301}).status_code == 422
    assert client.patch("/api/settings", json={"ingest_llm_budget": -1}).status_code == 422


def test_patch_ingest_runs_per_day_clamps(client: TestClient):
    # 采集轮数档位（2026-10-23 频率档位）：契约层 1-4 拦外、默认 2、合法值落库
    body = client.get("/api/settings").json()
    assert body["ingest_runs_per_day"] == 2  # 默认
    assert client.patch("/api/settings", json={"ingest_runs_per_day": 4}).json()["ingest_runs_per_day"] == 4
    assert _row().ingest_runs_per_day == 4
    assert client.patch("/api/settings", json={"ingest_runs_per_day": 5}).status_code == 422
    assert client.patch("/api/settings", json={"ingest_runs_per_day": 0}).status_code == 422


# —— 读取：get_or_create 默认行 ——


def test_get_creates_default_row(client: TestClient):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    for key, value in DEFAULTS.items():
        assert body[key] == value
    # 单行资源：二次 GET 幂等，表内恒一行
    assert client.get("/api/settings").json()["updated_at"] == body["updated_at"]


# —— 部分更新语义 ——


def test_patch_single_field_others_untouched(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"theme_mode": "dark"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["theme_mode"] == "dark"
    assert body["auto_start"] is DEFAULTS["auto_start"]
    assert body["calendar_start_min"] == DEFAULTS["calendar_start_min"]


def test_patch_empty_body_noop(client: TestClient):
    client.get("/api/settings")
    before = _row().updated_at
    resp = client.patch("/api/settings", json={})
    assert resp.status_code == 200
    assert _row().updated_at == before  # 空 patch 不抬 updated_at


# —— 契约层校验（422）——


def test_patch_invalid_enum_422(client: TestClient):
    client.get("/api/settings")
    for key, bad in [
        ("close_action", "minimize"),
        ("theme_mode", "auto"),
        ("default_view", "year"),
        ("notify_style", "email"),
        ("sound", "bell"),
    ]:
        assert client.patch("/api/settings", json={key: bad}).status_code == 422


def test_patch_notify_style_corner(client: TestClient):
    # 通知形式第三档"右下角悬浮框"（2026-09-16 增）：合法枚举可持久化
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"notify_style": "corner"})
    assert resp.status_code == 200
    assert resp.json()["notify_style"] == "corner"
    assert client.get("/api/settings").json()["notify_style"] == "corner"


def test_patch_widget_transparency(client: TestClient):
    # 小部件透明度（2026-09-17 增）：百分比可持久化（越大越透），越界由契约层 Field 拦 422
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"widget_transparency": 60})
    assert resp.status_code == 200
    assert resp.json()["widget_transparency"] == 60
    assert client.get("/api/settings").json()["widget_transparency"] == 60
    assert client.patch("/api/settings", json={"widget_transparency": 9}).status_code == 422
    assert client.patch("/api/settings", json={"widget_transparency": 81}).status_code == 422


def test_patch_minute_out_of_range_422(client: TestClient):
    client.get("/api/settings")
    assert client.patch("/api/settings", json={"calendar_start_min": -30}).status_code == 422
    assert client.patch("/api/settings", json={"calendar_end_min": 1500}).status_code == 422
    assert client.patch("/api/settings", json={"work_minutes_per_day": 30}).status_code == 422
    assert client.patch("/api/settings", json={"work_minutes_per_day": 780}).status_code == 422


def test_patch_reminder_invalid_choice_422(client: TestClient):
    client.get("/api/settings")
    assert client.patch("/api/settings", json={"reminder_lead_min": 7}).status_code == 422


# —— service 层合并态校验（422）——


def test_patch_calendar_span_too_short_422(client: TestClient):
    client.get("/api/settings")
    # 跨度 3 小时 < 4 小时下限
    resp = client.patch(
        "/api/settings", json={"calendar_start_min": 540, "calendar_end_min": 720}
    )
    assert resp.status_code == 422


def test_patch_calendar_start_after_end_422(client: TestClient):
    client.get("/api/settings")
    resp = client.patch(
        "/api/settings", json={"calendar_start_min": 1320, "calendar_end_min": 480}
    )
    assert resp.status_code == 422


def test_patch_minute_granularity_422(client: TestClient):
    client.get("/api/settings")
    # 合同层只拦范围，粒度在 service（绕过前端下拉直接调 API 的场景）
    resp = client.patch("/api/settings", json={"calendar_start_min": 485})
    assert resp.status_code == 422


def test_patch_cross_field_merged_state_422(client: TestClient):
    # 只改一头、另一头取库内现值参与判定：终点 600 与库内起点 480 跨度不足
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"calendar_end_min": 600})
    assert resp.status_code == 422


# —— 午休越界自动修复 ——


def test_patch_calendar_shift_clamps_lunch(client: TestClient):
    client.get("/api/settings")
    # 可视范围右收到 12:30，午休 11:30-13:30 被 clamp 为 11:30-12:30
    resp = client.patch("/api/settings", json={"calendar_end_min": 750})
    assert resp.status_code == 200
    body = resp.json()
    assert body["lunch_start_min"] == 690
    assert body["lunch_end_min"] == 750
    assert body["lunch_enabled"] is True


def test_patch_calendar_shift_disables_lunch_when_no_room(client: TestClient):
    client.get("/api/settings")
    # 可视范围左收到 13:30 起：午休 11:30-13:30 clamp 后跨度 0 → 自动关闭，原值保留
    resp = client.patch(
        "/api/settings", json={"calendar_start_min": 810, "calendar_end_min": 1200}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["lunch_enabled"] is False
    assert body["lunch_start_min"] == 690  # 原值不动，重开设置页可重调
    assert body["lunch_end_min"] == 810


def test_patch_lunch_within_range_passes(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"lunch_start_min": 720, "lunch_end_min": 840})
    assert resp.status_code == 200
    assert resp.json()["lunch_start_min"] == 720
    assert resp.json()["lunch_end_min"] == 840


def test_patch_lunch_end_beyond_range_clamped(client: TestClient):
    # 午休自身越界（终点 23:00 超出可视范围）：clamp 到 22:00，不报错
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"lunch_end_min": 1380})
    assert resp.status_code == 200
    assert resp.json()["lunch_end_min"] == 1320


# —— reminder / sound ——


def test_patch_reminder_null_disables(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"reminder_lead_min": None})
    assert resp.status_code == 200
    assert resp.json()["reminder_lead_min"] is None
    # 键不出现 = 不动：后续改别的字段，不提醒状态保持
    client.patch("/api/settings", json={"sound": "chime"})
    assert client.get("/api/settings").json()["reminder_lead_min"] is None


def test_patch_default_view_roundtrip(client: TestClient):
    client.get("/api/settings")
    for view in ("week", "month", "day"):
        resp = client.patch("/api/settings", json={"default_view": view})
        assert resp.status_code == 200
        assert resp.json()["default_view"] == view
    assert _row().default_view == "day"


def test_patch_autostart_and_close_action_roundtrip(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"auto_start": True, "close_action": "quit"})
    assert resp.status_code == 200
    assert resp.json()["auto_start"] is True
    assert resp.json()["close_action"] == "quit"
    row = _row()
    assert row.auto_start is True
    assert row.close_action == "quit"


# —— 数据采集（2026-10-14 增：ingest_enabled / ingest_brief_min） ——


def test_patch_ingest_roundtrip(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"ingest_enabled": True, "ingest_brief_min": 600})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ingest_enabled"] is True
    assert body["ingest_brief_min"] == 600  # 10:00
    assert client.get("/api/settings").json()["ingest_enabled"] is True
    row = _row()
    assert row.ingest_enabled is True and row.ingest_brief_min == 600


def test_patch_ingest_minute_granularity_422(client: TestClient):
    # 粒度在 service 层拦（ingest_brief_min 走 30 分钟粒度口径）
    client.get("/api/settings")
    assert client.patch("/api/settings", json={"ingest_brief_min": 545}).status_code == 422
    assert client.patch("/api/settings", json={"ingest_brief_min": 1441}).status_code == 422  # 契约层范围拦


# —— 天气对照城市（2026-10-18 增） ——


def test_patch_weather_city_roundtrip_and_blank_normalizes(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"weather_city": "杭州"})
    assert resp.status_code == 200
    assert resp.json()["weather_city"] == "杭州"
    assert _row().weather_city == "杭州"
    # 空白串归一 None（停用）；显式 null 同停用；键不出现不动
    assert client.patch("/api/settings", json={"weather_city": "   "}).json()["weather_city"] is None
    assert _row().weather_city is None
    client.patch("/api/settings", json={"weather_city": "北京"})
    client.patch("/api/settings", json={"theme_mode": "dark"})
    assert _row().weather_city == "北京"
    assert client.patch("/api/settings", json={"weather_city": None}).json()["weather_city"] is None
    # 契约层长度拦 422
    assert client.patch("/api/settings", json={"weather_city": "x" * 51}).status_code == 422


# —— AI 个性化：风格 / 哲学预设 / 自定义（2026-10-20 增） ——


def test_patch_ai_style_roundtrip_and_invalid_422(client: TestClient):
    client.get("/api/settings")
    assert client.patch("/api/settings", json={"ai_style": "concise"}).json()["ai_style"] == "concise"
    assert _row().ai_style == "concise"
    assert client.patch("/api/settings", json={"ai_style": "rigorous"}).json()["ai_style"] == "rigorous"
    assert client.patch("/api/settings", json={"ai_style": "aggressive"}).status_code == 422


def test_patch_ai_philosophy_segments_normalized(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"ai_philosophy": " mit-3 , , deep-work ,nope "})
    assert resp.status_code == 200
    # 拆段去空再 join；不做 id 存在性校验（nope 保留，注入层静默忽略）
    assert resp.json()["ai_philosophy"] == "mit-3,deep-work,nope"
    # 空串/纯逗号归一 None
    assert client.patch("/api/settings", json={"ai_philosophy": " , , "}).json()["ai_philosophy"] is None
    # 重复 id 保序去重（2026-10-21 复查批 D2：曾致同一哲学文本注入两遍）
    dedup = client.patch("/api/settings",
                         json={"ai_philosophy": "mit-3, deep-work ,mit-3"}).json()["ai_philosophy"]
    assert dedup == "mit-3,deep-work"
    # 超过 6 段截断（预设全集现 6 则）
    ids = ",".join(f"p{i}" for i in range(10))
    assert len(client.patch("/api/settings", json={"ai_philosophy": ids}).json()["ai_philosophy"].split(",")) == 6
    assert client.patch("/api/settings", json={"ai_philosophy": "x" * 101}).status_code == 422


def test_patch_ai_philosophy_custom_blank_normalizes(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"ai_philosophy_custom": "  先完成再完美  "})
    assert resp.json()["ai_philosophy_custom"] == "先完成再完美"
    assert client.patch("/api/settings", json={"ai_philosophy_custom": "   "}).json()["ai_philosophy_custom"] is None
    # 契约层长度拦 422
    assert client.patch("/api/settings", json={"ai_philosophy_custom": "x" * 1001}).status_code == 422


def test_weather_test_endpoint(client: TestClient, monkeypatch):
    # 城市探测回执（2026-10-21 复查批 D2）：命中 → resolved 文案；未命中 422 引导
    # 换名（错拼静默失效不再无从发现）；probe 打外网，替身隔离
    import services.weather as weather_mod

    monkeypatch.setattr(weather_mod, "probe_city",
                        lambda city: {"name": "杭州", "admin1": "浙江"})
    resp = client.get("/api/settings/weather-test", params={"city": "杭州"})
    assert resp.status_code == 200 and resp.json()["resolved"] == "杭州, 浙江"

    monkeypatch.setattr(weather_mod, "probe_city", lambda city: None)
    resp = client.get("/api/settings/weather-test", params={"city": "杭洲"})
    assert resp.status_code == 422 and "未识别城市" in resp.json()["detail"]


# —— 夜间工作流报点时刻（2026-10-20 增：nightly_start_min） ————————————————————


def test_patch_nightly_roundtrip(client: TestClient):
    client.get("/api/settings")
    resp = client.patch("/api/settings", json={"nightly_start_min": 1140})
    assert resp.status_code == 200
    assert resp.json()["nightly_start_min"] == 1140  # 19:00
    assert _row().nightly_start_min == 1140


def test_patch_nightly_minute_granularity_422(client: TestClient):
    # 粒度在 service 层拦（nightly_start_min 走 30 分钟粒度口径）
    assert client.patch("/api/settings", json={"nightly_start_min": 1145}).status_code == 422
    assert client.patch("/api/settings", json={"nightly_start_min": 1441}).status_code == 422  # 契约层范围拦


# —— 助手消息轻提示音（2026-11-04 通知分轨：assistant_chime）——————————————————


def test_patch_assistant_chime_roundtrip(client: TestClient):
    """通知分轨配套新列：默认关，PATCH 开生效、GET 带出。"""
    base = client.get("/api/settings").json()
    assert base["assistant_chime"] is False
    resp = client.patch("/api/settings", json={"assistant_chime": True})
    assert resp.status_code == 200 and resp.json()["assistant_chime"] is True
    assert _row().assistant_chime is True
