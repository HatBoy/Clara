"""Node4 裁决器配套测试（批 6，2026-10-21）：build_adjudicator_prompt 动态
段裁剪（精力/哲学/天气/通勤按画像与上下文裁——避免大而全注入）。换槽/回落
e2e 见 test_scheduler_graph.py。"""

from agents.scheduler.prompts import build_adjudicator_prompt

_CTX = {"busy": [], "load_summary": [], "weather": None, "memory_rules": []}
_DEEP = {"category": "深度脑力", "energy_drain": "高"}
_OUTDOOR = {"category": "外出体力", "energy_drain": "中", "env_deps": ["需要好天气"]}
_TRAVEL = {"category": "差旅通勤", "energy_drain": "中"}


def _prompt(entries, philosophy=(), weather=None):
    ctx = dict(_CTX, weather=weather)
    system, user = build_adjudicator_prompt(
        [{"title": "t", "duration": 60, "profile": p} for p in entries],
        [{"key": "a0", "title": "t", "slot": "2026-10-19T09:00:00"}],
        ctx, list(philosophy))
    return system, user


def test_energy_section_only_for_deep_or_high_drain():
    system, _ = _prompt([_DEEP])
    assert "精力匹配" in system
    system2, _ = _prompt([{"category": "机械杂事", "energy_drain": "低"}])
    assert "精力匹配" not in system2


def test_weather_section_needs_outdoor_and_forecast():
    sunny = [{"date": "2026-10-19", "text": "晴", "tmax": 22, "tmin": 14, "precip": 10}]
    system, _ = _prompt([_OUTDOOR], weather=sunny)
    assert "天气对照" in system
    # 户外但未配置城市（weather None）→ 段不注入，不构成空对照
    system2, _ = _prompt([_OUTDOOR], weather=None)
    assert "天气对照" not in system2
    # 有预报但条目全室内 → 段不注入
    system3, _ = _prompt([_DEEP], weather=sunny)
    assert "天气对照" not in system3


def test_commute_section_for_travel_category():
    system, _ = _prompt([_TRAVEL])
    assert "通勤预留" in system
    system2, _ = _prompt([_DEEP])
    assert "通勤预留" not in system2


def test_philosophy_section_single_source_and_two_minute_excluded():
    system, _ = _prompt([_DEEP], philosophy=("deep-work", "two-minute"))
    assert "深度工作" in system        # 文本单源取 PHILOSOPHIES
    assert "两分钟法则" not in system  # two-minute 管小事不排期，与选槽无关
    system2, _ = _prompt([_DEEP], philosophy=())
    assert "管理哲学" not in system2


def test_organize_section_conditions():
    """批 5 组织段三信号：汇报类标题 / 常识条款含跨时区 / 同 category ≥2 条；
    全不满足不注入（段落裁剪口径与其他段一致）。"""
    from agents.scheduler.prompts import _needs_organize

    assert _needs_organize([{"title": "季度述职汇报"}], [{}])
    assert _needs_organize(
        [{"title": "x"}], [{"common_sense_rules": ["跨时区会议注意对方本地时间"]}])
    assert _needs_organize([{"title": "a"}, {"title": "b"}],
                           [{"category": "深度脑力"}, {"category": "深度脑力"}])
    assert not _needs_organize([{"title": "写周报"}], [{"category": "深度脑力"}])


def test_organize_section_in_prompt_when_briefing():
    # 汇报类标题 → 组织段注入（倒推/时区/聚类三指引合一）；普通条目不注入
    system, _ = build_adjudicator_prompt(
        [{"title": "年度路演", "duration": 60,
          "profile": {"category": "沟通协作", "energy_drain": "中"}}],
        [{"key": "a0", "title": "年度路演", "slot": "2026-10-19T09:00:00"}],
        dict(_CTX), [])
    assert "排期组织" in system
    assert "提前 1-2 天" in system
    system2, _ = _prompt([_DEEP])
    assert "排期组织" not in system2
