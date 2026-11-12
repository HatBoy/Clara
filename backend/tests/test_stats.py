"""统计报告测试（routers/stats.py + services/stats.py）。

覆盖：新口径（完成待办/日程发生/待办消化率/排期提前量）、趋势空日零行+
待办日程分列/项目聚合/优先级分布/backlog/环比 delta/负载率/完成滞后/
AI 协作块、days 白名单、周期锚定（周一起/1 号起）、报告生成落库（LLM 替身与
异常回落模板、【总评】首行约定、上期结论注入）、历史列表降序。
执行节奏 execution_rhythm 直调既有实现不做重复断言。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from models import Proposal, StatsReport, Task
from services import stats as stats_svc


def _today_zero():
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def populated(client: TestClient, seeded_db):
    """在 seed 之外显式造数据载体（新口径）：
    - 3 条已排期日程（昨天 10:00 开始，已过去 = 已发生）
    - 1 条纯待办已完成（今天完成）
    - 1 条新建待办未完成（测 todos_created）
    口径：日程不勾选完成，按已开始判定发生；待办按勾选完成。"""
    day = _today_zero() + timedelta(days=-1, hours=10)
    ids = []
    for i, prio in enumerate([1, 2, 2]):
        res = client.post("/api/tasks", json={
            "title": f"统计样例{i}", "priority": prio,
            "start_time": day.isoformat(),
            "end_time": (day + timedelta(hours=1)).isoformat(),
        })
        ids.append(res.json()["id"])
        # 日程不勾完成——新口径下已开始即发生
    todo = client.post("/api/tasks", json={"title": "待办样例"}).json()["id"]
    client.patch(f"/api/tasks/{todo}", json={"done": True})
    # 新建一条未完成待办（测 todos_created）
    client.post("/api/tasks", json={"title": "待办新增"})
    return ids


def test_summary_overview_and_trend(client: TestClient, populated):
    res = client.get("/api/stats/summary", params={"days": 7})
    assert res.status_code == 200
    body = res.json()
    ov = body["overview"]
    # 新口径：events_held（已发生日程）≥3、todos_done（完成待办）≥1、
    # todos_created（新增待办）≥1、todo_digest_rate（消化率）可算
    assert ov["events_held"] >= 3 and ov["todos_done"] >= 1
    assert ov["todos_created"] >= 1
    assert ov["todo_digest_rate"] is not None
    assert ov["scheduled_minutes"] >= 180
    assert ov["inbox_backlog"] >= 0
    # 负载率：分子与排期总时长同口径，分母 = 每日工时（默认 480）× 窗口天数
    assert ov["capacity_minutes"] == 480 * 7
    assert ov["load_rate"] == round(ov["scheduled_minutes"] / (480 * 7), 2)
    # 趋势：7 天窗口恒 7 行（空日零行）；待办按 done_at、日程按实例开始日分桶
    # ——样例日程昨天开始、待办今天完成
    assert len(body["daily_done"]) == 7
    today = _today_zero().date().isoformat()
    yesterday = (_today_zero() - timedelta(days=1)).date().isoformat()
    today_row = next(r for r in body["daily_done"] if r["date"] == today)
    yest_row = next(r for r in body["daily_done"] if r["date"] == yesterday)
    assert yest_row["events"] >= 3  # 日程昨天发生
    assert today_row["todos"] >= 1  # 待办今天完成
    for r in body["daily_done"]:
        assert r["todos"] + r["events"] == r["count"]


def test_summary_by_priority_shape(client: TestClient, populated):
    body = client.get("/api/stats/summary", params={"days": 7}).json()
    assert set(body["by_priority"]) == {"high", "medium", "low"}
    # 新口径：by_priority 只统计完成待办（populated 中有 1 条中优先级待办完成）
    assert body["by_priority"]["medium"] >= 1


def test_summary_delta_load_and_lag(fresh_db):
    """环比 delta / 负载率 / 完成滞后与排期提前量：直调 svc 显式 lo/hi 绕开 seed 噪声。

    构造：7 天窗 [lo, hi)。上一窗 1 条日程发生（60 分钟排期）；本窗 2 条日程
    （各 90 分钟，共 180，昨天 10:00 开始已发生）+ 1 条待办完成 + 1 条新增待办，
    created_at 均恰在 2 整天前同时刻。"""
    from conftest import TestingSessionLocal

    today = _today_zero()
    lo, hi = today - timedelta(days=6), today + timedelta(days=1)
    with TestingSessionLocal() as db:
        # 上一窗（lo 前 6 天那天）：1 条日程，9:00-10:00（已发生）
        pday = lo - timedelta(days=6)
        db.add(Task(title="旧日程", start_time=pday + timedelta(hours=9),
                    end_time=pday + timedelta(hours=10)))
        # 本窗：2 条日程 yesterday 10:00-11:30（各 90 分钟，已发生），created 于
        # 2 天前 10:00（提前量恰 2.0 天）
        yesterday = today - timedelta(days=1)
        for i in range(2):
            db.add(Task(title=f"新日程{i}", start_time=yesterday + timedelta(hours=10),
                        end_time=yesterday + timedelta(hours=11, minutes=30),
                        created_at=yesterday - timedelta(days=2, hours=-10)))
        # 本窗：1 条待办，created 于 2 天前 12:00、done 于 today 12:00（滞后 2.0 天）
        db.add(Task(title="完成待办", created_at=today - timedelta(days=2, hours=-12),
                    done_at=today + timedelta(hours=12)))
        # 本窗：1 条新增待办未完成（测 todos_created）
        db.add(Task(title="新增待办", created_at=today - timedelta(hours=1)))
        db.commit()
        body = stats_svc.stats_summary(db, lo=lo, hi=hi)

    ov = body["overview"]
    # delta = 本窗 − 上一同长窗：日程发生 2−1、待办完成 1−0、排期分钟 180−60
    assert ov["delta"] == {"todos_done": 1, "events_held": 1, "scheduled_minutes": 120}
    # 负载率：180 分钟 /（480 × 7 天）
    assert ov["capacity_minutes"] == 3360
    assert ov["load_rate"] == 0.05
    # todos_created = 2（完成的 + 新增的），消化率 = 1/2
    assert ov["todos_created"] == 2
    assert ov["todo_digest_rate"] == 0.5
    # 完成滞后（待办）= 2.0 天；排期提前量（日程）= 2.0 天（created→实例开始）
    assert body["lag"]["todos_days"] == 2.0
    assert body["lag"]["lead_days"] == 2.0


def test_summary_ai_block(fresh_db):
    """AI 协作块：Proposal kind=schedule 落窗计数 + adoption_stats（expired 剔除）。"""
    from conftest import TestingSessionLocal

    now = datetime.now()
    with TestingSessionLocal() as db:
        db.add(Proposal(kind="schedule", payload={}, outcome="accepted",
                        created_at=now - timedelta(hours=2)))
        db.add(Proposal(kind="schedule", payload={}, outcome="rejected",
                        created_at=now - timedelta(hours=3)))
        db.add(Proposal(kind="inbox", payload={}, outcome="accepted",
                        created_at=now - timedelta(hours=4)))
        db.commit()
        body = stats_svc.stats_summary(db, days=7)

    assert body["ai"]["schedule_proposals"] == 2  # inbox 建议不计
    ad = body["ai"]["adoption"]
    assert ad["total"] == 3 and ad["accepted_modified"] == 2
    assert ad["rate"] == pytest.approx(2 / 3)


def test_summary_days_whitelist(client: TestClient, seeded_db):
    assert client.get("/api/stats/summary", params={"days": 14}).status_code == 422
    assert client.get("/api/stats/summary", params={"days": 30}).status_code == 200


def test_period_range_anchors():
    # 2026-11-06 周五 → 周报起 11-02（周一）；月报起 11-01
    now = datetime(2026, 11, 6, 15, 30)
    wk_lo, wk_hi = stats_svc.period_range("week", now)
    assert (wk_lo.month, wk_lo.day) == (11, 2) and wk_lo.weekday() == 0
    assert (wk_hi - wk_lo).days == 7
    mo_lo, mo_hi = stats_svc.period_range("month", now)
    assert (mo_lo.month, mo_lo.day) == (11, 1) and (mo_hi - mo_lo).days == 30


def test_generate_report_with_llm_and_fallback(client: TestClient, seeded_db, monkeypatch):
    calls = {}

    def fake_llm(messages, **kw):
        calls["n"] = calls.get("n", 0) + 1
        return "【总评】本周完成节奏平稳，项目投入集中。\n\n## 概述\n\nAI 总结内容。"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    res = client.post("/api/stats/reports", json={"period": "week"})
    assert res.status_code == 200
    first = res.json()
    # 首行【总评】约定（速览卡提取锚点）+ 原五节结构
    assert first["content"].startswith("【总评】") and first["period_type"] == "week"
    assert "## 概述" in first["content"]
    assert calls["n"] == 1

    # LLM 异常 → 回落纯数据模板（报告仍生成），首行同样带【总评】
    def boom(*a, **kw):
        raise RuntimeError("未配置模型")

    monkeypatch.setattr(stats_svc, "call_llm", boom)
    res = client.post("/api/stats/reports", json={"period": "month"})
    assert res.status_code == 200
    fallback = res.json()["content"]
    assert fallback.startswith("【总评】") and "数据摘要" in fallback
    # 新口径模板文案包含"待办消化率"与"日程发生"
    assert "待办消化率" in fallback and "日程发生" in fallback

    # 历史列表最新在前
    reports = client.get("/api/stats/reports").json()
    assert len(reports) == 2
    assert reports[0]["created_at"] >= reports[1]["created_at"]


def test_generate_report_injects_previous_review(client: TestClient, seeded_db, monkeypatch):
    """上期结论注入：首期零注入；有上期时【总评】+「## 建议」节注入且在统计数据段
    之前；建议节截到下一个 ##（越界节不进）；周/月跨型不串。"""
    from conftest import TestingSessionLocal

    captured = {}

    def fake_llm(messages, **kw):
        captured["c"] = messages[0]["content"]
        return "【总评】稳。\n\n## 概述\n\nx"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    # 首期：无上期 → 零注入
    client.post("/api/stats/reports", json={"period": "week"})
    assert "上期报告" not in captured["c"]

    # 落一条上期周报（period_start = 本周一 − 7 天）；建议节后故意放越界节标记词
    with TestingSessionLocal() as db:
        lo, _ = stats_svc.period_range("week")
        db.add(StatsReport(
            period_type="week", period_start=lo - timedelta(days=7),
            content="【总评】上期总评句。\n\n## 概述\n\no\n\n## 建议\n\n1. 上期建议甲\n\n"
                    "## 行为洞察\n\n越界段不应注入XYZ"))
        db.commit()

    client.post("/api/stats/reports", json={"period": "week"})
    c = captured["c"]
    assert "上期报告" in c and "上期总评句" in c and "上期建议甲" in c
    assert "越界段不应注入XYZ" not in c  # 建议节边界截断
    assert c.index("上期总评句") < c.index("统计数据：")  # 注入在数据段之前

    # 月报查月报（跨型不串）：无上月留档 → 零注入，注入计数不增
    client.post("/api/stats/reports", json={"period": "month"})
    assert captured["c"].count("上期报告") == 0


def test_previous_review_truncates_long_content(fresh_db):
    """护栏：上期结论提取超长时截断到 _PREV_REVIEW_MAX_CHARS（经复盘上下文单源）。"""
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        lo, hi = stats_svc.period_range("week")
        db.add(StatsReport(
            period_type="week", period_start=lo - timedelta(days=7),
            content=f"【总评】总评。\n\n## 建议\n\n{'长' * 2000}"))
        db.commit()
        summary = stats_svc.stats_summary(db, lo=lo, hi=hi)
        ctx = stats_svc.build_review_context(db, summary, period="week")
    assert ctx["prev_report"] is not None
    assert len(ctx["prev_report"]["digest"]) <= stats_svc._PREV_REVIEW_MAX_CHARS


# —— 建议结构化追踪（payload 留档 + 下期对照 + 周际成长对比）————————————


def test_report_advice_block_parsed_and_stored(client: TestClient, fresh_db, monkeypatch):
    """LLM 文末结构化建议块：剥离正文后落库，payload.advice_items + health
    快照齐备（下期对照追踪的数据源）。"""

    def fake_llm(messages, **kw):
        return ("【总评】稳。\n\n## 概述\n\nx\n\n## 建议\n\n- 建议甲\n- 建议乙\n\n"
                '```json\n{"advice_items": ["建议甲", "建议乙"]}\n```')

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    res = client.post("/api/stats/reports", json={"period": "week"})
    assert res.status_code == 200
    body = res.json()
    assert "```json" not in body["content"] and "建议甲" in body["content"]
    assert body["payload"]["advice_items"] == [
        {"text": "建议甲", "status": "proposed"},
        {"text": "建议乙", "status": "proposed"},
    ]
    assert "total" in body["payload"]["health"] and "dimensions" in body["payload"]["health"]


def test_report_garbage_advice_block_fallback(client: TestClient, fresh_db, monkeypatch):
    """垃圾 json 块解析失败：正文原样保留（块不剥离），advice_items 空表
    ——报告永远以纯 Markdown 形态可用。"""

    def fake_llm(messages, **kw):
        return "【总评】稳。\n\n## 建议\n\n- 甲\n\n```json\n{坏的}\n```"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    res = client.post("/api/stats/reports", json={"period": "week"})
    assert res.status_code == 200
    assert "```json" in res.json()["content"]
    assert res.json()["payload"]["advice_items"] == []


def test_advice_items_payload_preferred_over_text(fresh_db):
    """上期建议提取：payload.advice_items 结构化优先；无 payload 的旧行回落
    正文「## 建议」节按行拆（剥列表记号）。"""
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        row_new = StatsReport(
            period_type="week", period_start=datetime.now() - timedelta(days=1),
            content="【总评】新。\n\n## 建议\n\n- 正文建议（不应被选中）",
            payload={"advice_items": ["结构化建议A", "结构化建议B"]})
        db.add(row_new)
        db.commit()
        items = stats_svc._advice_items_of(row_new)
        assert items == ["结构化建议A", "结构化建议B"]

        row_old = StatsReport(
            period_type="week", period_start=datetime.now() - timedelta(days=2),
            content="【总评】旧。\n\n## 建议\n\n- 旧建议一\n2. 旧建议二\n\n普通行也算\n\n## 概述\n\nx")
        db.add(row_old)
        db.commit()
        items_old = stats_svc._advice_items_of(row_old)
        assert items_old == ["旧建议一", "旧建议二", "普通行也算"]


def test_weekly_growth_metrics(fresh_db):
    """周报成长对比：_calculate_growth_metrics 支持 week（本周 vs 上周），
    结构与月报同构；其他 period 值返回 None。"""
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        lo, hi = stats_svc.period_range("week")
        summary = stats_svc.stats_summary(db, lo=lo, hi=hi)
        growth = stats_svc._calculate_growth_metrics(db, summary, "week")
        none = stats_svc._calculate_growth_metrics(db, summary, "quarter")
    assert growth is not None
    assert set(growth) >= {"metrics_change", "health_change", "goal_achievement",
                           "behavior_improvement", "prev_period"}
    assert none is None


# —— 复盘产出回流记忆（结构化尾部 + Tier 2 事实沉淀）————————————————————


def test_report_persists_facts_and_keep_habits(client: TestClient, fresh_db, monkeypatch):
    """报告结构化尾部：fact_candidates 落 Tier 2（source=stats_reflect）、
    keep_habits 入 payload；重复生成同 topic+fact 印证不堆行。"""
    from conftest import TestingSessionLocal
    from models import EpisodicFact

    def fake_llm(messages, **kw):
        return ("【总评】稳。\n\n## 建议\n\n- 甲\n\n"
                '```json\n{"advice_items": ["甲"], "keep_habits": ["晨间专注"], '
                '"fact_candidates": [{"topic": "效率", "fact": "周五下午完成率低", "expires_days": 90}]}\n```')

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    client.post("/api/stats/reports", json={"period": "week"})
    client.post("/api/stats/reports", json={"period": "week"})  # 同事实二次生成 → 印证

    with TestingSessionLocal() as db:
        facts = db.query(EpisodicFact).filter(EpisodicFact.source == "stats_reflect").all()
    assert len(facts) == 1  # 同 topic+fact 印证不堆行
    assert facts[0].topic == "效率" and facts[0].confidence == 0.6

    reports = client.get("/api/stats/reports").json()
    assert reports[0]["payload"]["keep_habits"] == ["晨间专注"]


def test_insight_strips_tail_and_persists_facts(fresh_db, monkeypatch):
    """洞察结构化尾部：fact_candidates 落库；缓存与返回的是剥离后的正文
    （页面渲染不出现 ```json 块）。"""
    from conftest import TestingSessionLocal
    from models import EpisodicFact

    _seed_activity()  # 非零数据绕过零数据早退
    stats_svc._INSIGHT_CACHE.clear()

    def fake_llm(messages, **kw):
        return ("【总评】稳。\n\n【习惯洞察】\n- 保持节奏\n\n"
                '```json\n{"fact_candidates": [{"topic": "节奏", "fact": "周三上午效率最高"}]}\n```')

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    with TestingSessionLocal() as db:
        out = stats_svc.generate_insight(db, days=7)
    stats_svc._INSIGHT_CACHE.clear()

    assert "```json" not in out and "保持节奏" in out
    with TestingSessionLocal() as db:
        facts = db.query(EpisodicFact).filter(EpisodicFact.source == "stats_reflect").all()
    assert len(facts) == 1 and facts[0].topic == "节奏"


# —— 教练闭环（目标 / 建议打卡 / 个人分位基线 / 复盘引导）——————————————


def test_set_advice_status_flow(client: TestClient, fresh_db, monkeypatch):
    """建议打卡全链：生成（dict 形态 proposed）→ 打卡 completed（回包持久）→
    越界 422 / 不存在 404；_advice_items_of 渲染 completed 标注、dropped 跳过。"""
    from conftest import TestingSessionLocal

    def fake_llm(messages, **kw):
        return ('【总评】稳。\n\n## 建议\n\n- 甲\n- 乙\n\n'
                '```json\n{"advice_items": ["甲", "乙"]}\n```')

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    report_id = client.post("/api/stats/reports", json={"period": "week"}).json()["id"]

    res = client.post(f"/api/stats/reports/{report_id}/advice",
                      json={"index": 0, "status": "completed"})
    assert res.status_code == 200
    items = res.json()["payload"]["advice_items"]
    assert items[0] == {"text": "甲", "status": "completed"}
    assert items[1]["status"] == "proposed"

    res = client.post(f"/api/stats/reports/{report_id}/advice",
                      json={"index": 1, "status": "dropped"})
    assert res.status_code == 200
    res = client.post(f"/api/stats/reports/{report_id}/advice",
                      json={"index": 5, "status": "completed"})
    assert res.status_code == 422  # 序号越界
    res = client.post("/api/stats/reports/none/advice",
                      json={"index": 0, "status": "completed"})
    assert res.status_code == 404

    with TestingSessionLocal() as db:
        row = db.get(StatsReport, report_id)
        rendered = stats_svc._advice_items_of(row)
    assert rendered == ["甲（你确认已完成）"]  # dropped 的乙不再追问


def test_goal_rules_injected_into_insight(fresh_db, monkeypatch):
    """改进目标注入：category=改进目标 的规则进洞察「目标进度」段与复盘上下文。"""
    from conftest import TestingSessionLocal
    from models import MemoryRule

    _seed_activity()  # 非零数据绕过零数据早退
    stats_svc._INSIGHT_CACHE.clear()
    with TestingSessionLocal() as db:
        db.add(MemoryRule(category="改进目标", content="本月把待办消化率提到 80%",
                          source="user_manual", confidence=1.0))
        db.commit()
        captured = {}

        def fake_llm(messages, **kw):
            captured["c"] = messages[0]["content"]
            return "【总评】稳。"

        monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
        stats_svc.generate_insight(db, days=7)
    stats_svc._INSIGHT_CACHE.clear()
    assert "改进目标" in captured["c"] and "消化率提到 80%" in captured["c"]


def test_warnings_personal_baseline():
    """个人基线预警：负载超个人 P75 触发（文案带「个人基线」）；p50=0 的
    冷启动信号不触发（防空报）。"""
    summary = {
        "overview": {"inbox_backlog": 0, "load_rate": 0.9, "todo_digest_rate": 0.8,
                     "todos_done": 10, "todos_created": 12},
        "lag": {"todos_days": 1.0},
    }
    hot = {"load_rate": {"p25": 0.4, "p50": 0.5, "p75": 0.6}, "todos_done": None,
           "periods": 8}
    warnings = stats_svc._calculate_warnings(summary, None, baseline=hot)
    assert any("个人基线" in w["message"] and "负载率" in w["message"] for w in warnings)

    cold = {"load_rate": {"p25": 0.0, "p50": 0.0, "p75": 0.0}, "todos_done": None,
            "periods": 8}
    warnings_cold = stats_svc._calculate_warnings(summary, None, baseline=cold)
    assert not any("个人基线" in w["message"] for w in warnings_cold)


def test_warnings_overload_range_days():
    """负载率 >100% 超载预警：range.from/to 是 isoformat 串（summary_base 构造即
    序列化），天数相减须先 fromisoformat 解析——回归 2026-11-10 线上 str-str
    TypeError（该超载分支长期无数据触发、未被任何用例覆盖）。"""
    summary = {
        "overview": {"inbox_backlog": 0, "load_rate": 1.25, "todo_digest_rate": 0.8,
                     "todos_done": 10, "todos_created": 12,
                     "scheduled_minutes": 3000, "capacity_minutes": 2400},
        "lag": {"todos_days": 1.0},
        "range": {"from": "2026-11-01", "to": "2026-11-30"},
    }
    warnings = stats_svc._calculate_warnings(summary, None)
    over = next(w for w in warnings if "超出容量" in w["message"])
    # 文案完整生成（不再抛 TypeError）+ 每日增工时算对：600min=10h / 29 天 ≈ 0.3h
    assert "每日增加" in over["message"]
    assert "0.3" in over["message"]


def test_baseline_quantiles_cold_start(fresh_db):
    """分位基线冷启动：空库样本不足 → 各指标分位 None，periods 恒在。"""
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        baseline = stats_svc._calculate_baseline(db, days=7)
    assert baseline["periods"] == 8
    for key in ("todos_done", "events_held", "todo_digest_rate", "load_rate"):
        assert baseline[key] is None


def test_ask_review_mode_directive_and_facts_frame(client: TestClient, fresh_db, monkeypatch):
    """复盘引导模式：system 追加引导指令；总结轮结构化尾部剥离——reply 帧
    为纯正文、facts 帧带候选。"""
    from routers import stats as stats_router

    _activate_model()
    seen = {}

    def fake_stream(messages, *, on_chunk, **kw):
        seen["sys"] = messages[0]["content"]
        on_chunk("总结")
        return ("复盘总结……\n\n"
                '```json\n{"fact_candidates": [{"topic": "卡点", "fact": "周五下午容易松懈"}]}\n```')

    monkeypatch.setattr(stats_router, "call_llm_stream", fake_stream)
    events = _ask_events(client, {"days": 7, "question": "开始复盘", "mode": "review"})
    assert "复盘引导" in seen["sys"] and "fact_candidates" in seen["sys"]
    types = [e["type"] for e in events]
    assert types == ["delta", "reply", "facts", "done"]
    assert "```json" not in events[1]["content"]
    assert events[2]["candidates"][0]["topic"] == "卡点"


def test_stats_facts_endpoint(client: TestClient, fresh_db):
    """反思沉淀端点：确认后落 Tier 2（source=stats_chat、confidence 0.7）；
    空字段 422。"""
    from conftest import TestingSessionLocal
    from models import EpisodicFact

    res = client.post("/api/stats/facts",
                      json={"topic": "节奏", "fact": "晨间专注两小时效率最高"})
    assert res.status_code == 200 and res.json()["ok"] is True
    res = client.post("/api/stats/facts", json={"topic": " ", "fact": "x"})
    assert res.status_code == 422
    with TestingSessionLocal() as db:
        row = db.query(EpisodicFact).filter(EpisodicFact.source == "stats_chat").one()
    assert row.confidence == 0.7 and row.topic == "节奏"


# —— 反思批（2026-11-09）：行为模式推断 + 洞察零数据早退 ——————————————


def test_behavior_insights_rules():
    """行为模式推断四规则：触发态命中 + 门槛不足静默（宁缺毋滥不牵强）。"""
    hot = {
        "habits": {"priority_completion": {
            "high": {"created": 5, "done": 1, "rate": 0.2},
            "medium": {"created": 4, "done": 2, "rate": 0.5},
            "low": {"created": 4, "done": 3, "rate": 0.75}}},
        "rhythm": {"planned_count": 10, "done_count": 2, "done_rate": 0.2,
                   "window_days": 14,
                   "top_deferred": [{"title": "年度汇报", "count": 4, "priority": 1}]},
        "behavior": {"avg_delta_min": 25, "done_events": 5, "event_count": 9},
    }
    out = stats_svc._calculate_behavior_insights(hot)
    assert len(out) == stats_svc._PATTERN_MAX  # 四规则全触发也只出前 3 条（防失控兜底）
    assert any("要事第一" in p for p in out)  # 1 要事后置
    assert any("年度汇报" in p and "逃避要事" in p for p in out)  # 2 要事霸占拖延榜
    assert any("估时偏乐观" in p for p in out)  # 3 估时乐观
    # 4 计划执行脱节（被上限截断，单独构造验证）
    only4 = {"habits": {}, "behavior": {},
             "rhythm": {"planned_count": 8, "done_count": 2, "done_rate": 0.25,
                        "window_days": 14, "top_deferred": []}}
    got = stats_svc._calculate_behavior_insights(only4)
    assert len(got) == 1 and "排期与实际执行脱节" in got[0]

    # 门槛不足全静默：高优样本不足 / 拖延榜首非高优且次数少 / 偏差小样本少 /
    # 排得少完成率高
    quiet = {
        "habits": {"priority_completion": {
            "high": {"created": 2, "done": 1, "rate": 0.5},
            "low": {"created": 3, "done": 2, "rate": 0.67}}},
        "rhythm": {"planned_count": 3, "done_count": 2, "done_rate": 0.67,
                   "top_deferred": [{"title": "杂事", "count": 2, "priority": 3}]},
        "behavior": {"avg_delta_min": 5, "done_events": 2, "event_count": 4},
    }
    assert stats_svc._calculate_behavior_insights(quiet) == []


def test_insight_empty_data_early_return(fresh_db, monkeypatch):
    """零数据早退：核心指标全零 → 固定引导文案，不进 LLM、不写缓存。"""
    from conftest import TestingSessionLocal

    stats_svc._INSIGHT_CACHE.clear()
    calls = {"n": 0}

    def fake_llm(messages, **kw):
        calls["n"] += 1
        return "不应被调用"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    with TestingSessionLocal() as db:
        out = stats_svc.generate_insight(db, days=7)
    assert out.startswith("【总评】") and "先用几天" in out
    assert calls["n"] == 0
    assert 7 not in stats_svc._INSIGHT_CACHE


def test_report_period_validation(client: TestClient, seeded_db):
    res = client.post("/api/stats/reports", json={"period": "year"})
    assert res.status_code == 422


def _seed_one_report() -> str:
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        row = StatsReport(
            period_type="week",
            period_start=stats_svc.period_range("week")[0],
            content="【总评】导出测试。\n\n## 概述\n\n正文内容。",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def test_export_report_happy_path(client: TestClient, fresh_db, tmp_path):
    report_id = _seed_one_report()
    target = tmp_path / "clara-周报.md"
    res = client.post(f"/api/stats/reports/{report_id}/export", json={"path": str(target)})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["size_bytes"] > 0 and body["path"] == str(target)
    assert target.read_text(encoding="utf-8").startswith("【总评】导出测试。")


def test_export_report_validations(client: TestClient, fresh_db, tmp_path):
    report_id = _seed_one_report()
    # 非 .md 后缀 → 422
    res = client.post(
        f"/api/stats/reports/{report_id}/export", json={"path": str(tmp_path / "a.txt")})
    assert res.status_code == 422
    # 相对路径 → 422
    res = client.post(f"/api/stats/reports/{report_id}/export", json={"path": "rel/a.md"})
    assert res.status_code == 422
    # 不存在的报告 → 404
    res = client.post(
        f"/api/stats/reports/none/export", json={"path": str(tmp_path / "b.md")})
    assert res.status_code == 404


# —— 页面模式专属块（warnings/prev_avg/habits/health）与预警动作收敛 ——————————


def test_summary_page_mode_blocks_and_warning_actions(client: TestClient, populated):
    """页面滚动窗口模式附带四块；预警 action 只剩真实跳转（open_inbox/view_week）
    且不再携带从未被消费的 target 字段。"""
    body = client.get("/api/stats/summary", params={"days": 7}).json()
    for key in ("warnings", "prev_avg", "habits", "health"):
        assert key in body
    for w in body["warnings"]:
        assert w["action"] in ("open_inbox", "view_week", None)
        assert "target" not in w


def test_summary_report_mode_skips_page_blocks(fresh_db):
    """报告周期模式（显式 lo/hi）不带页面专属四块（留档报告只做周期聚合）。"""
    from conftest import TestingSessionLocal

    today = _today_zero()
    with TestingSessionLocal() as db:
        body = stats_svc.stats_summary(db, lo=today - timedelta(days=6), hi=today + timedelta(days=1))
    for key in ("warnings", "prev_avg", "habits", "health"):
        assert key not in body


def test_prev_avg_batched_shape(fresh_db):
    """历史基线（加宽窗分桶版）：形状与聚合口径不变——todos_done/events_held 为
    周期均值、消化率/负载率可 None。"""
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        avg = stats_svc._calculate_prev_periods_avg(db, days=7, periods=4)
    assert set(avg) == {"todos_done", "events_held", "todo_digest_rate", "load_rate"}
    assert avg["todos_done"] == 0 and avg["events_held"] == 0
    assert avg["todo_digest_rate"] is None and avg["load_rate"] is not None


# —— AI 动态洞察（一卡三段 + 缓存 + 规则降级） ————————————————————————


def test_insight_llm_cached_and_fallback(fresh_db, monkeypatch):
    from conftest import TestingSessionLocal

    _seed_activity()  # 非零数据绕过零数据早退
    stats_svc._INSIGHT_CACHE.clear()
    calls = {"n": 0}

    def fake_llm(messages, **kw):
        calls["n"] += 1
        return "【总评】节奏平稳。\n\n【本期建议】\n- 保持 [高优]"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    with TestingSessionLocal() as db:
        first = stats_svc.generate_insight(db, days=7)
        second = stats_svc.generate_insight(db, days=7)  # TTL 内命中缓存
    assert first.startswith("【总评】") and calls["n"] == 1 and second == first
    with TestingSessionLocal() as db:
        third = stats_svc.generate_insight(db, days=7, refresh=True)  # 手动刷新跳过缓存读
    assert calls["n"] == 2 and third == first  # refresh 重新生成并刷新缓存
    stats_svc._INSIGHT_CACHE.clear()

    # LLM 异常 → 规则文案降级（三段结构、不缓存——下次调用重试 LLM）
    def boom(*a, **kw):
        raise RuntimeError("未配置模型")

    monkeypatch.setattr(stats_svc, "call_llm", boom)
    with TestingSessionLocal() as db:
        fallback = stats_svc.generate_insight(db, days=7)
    assert fallback.startswith("【总评】")
    assert 7 not in stats_svc._INSIGHT_CACHE


# —— 统计问答独立通道（POST /api/stats/ask，SSE 流式） ————————————————————


def _activate_model() -> None:
    import uuid

    from conftest import TestingSessionLocal
    from models import ModelProvider

    with TestingSessionLocal() as db:
        db.add(ModelProvider(
            name=f"m-{uuid.uuid4().hex[:6]}",
            model_name="deepseek/deepseek-chat",
            is_active=True,
        ))
        db.commit()


def _ask_events(client: TestClient, payload: dict) -> list[dict]:
    import json

    events: list[dict] = []
    with client.stream("POST", "/api/stats/ask", json=payload) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_ask_streams_delta_reply_done(client: TestClient, fresh_db, monkeypatch):
    from routers import stats as stats_router

    _activate_model()

    def fake_stream(messages, *, on_chunk, **kw):
        # 上下文断言：system 帧注入统计 JSON 与天数，question 在末位 user 帧
        assert messages[0]["role"] == "system" and "近 7 天" in messages[0]["content"]
        assert messages[-1] == {"role": "user", "content": "我这周完成得怎么样"}
        on_chunk("完成")
        on_chunk("不错")
        return "完成不错"

    monkeypatch.setattr(stats_router, "call_llm_stream", fake_stream)
    events = _ask_events(client, {"days": 7, "question": "我这周完成得怎么样"})
    assert [e["type"] for e in events] == ["delta", "delta", "reply", "done"]
    assert events[2]["content"] == "完成不错"
    # 独立通道：不落 ChatTurn（日程助手上下文零污染）
    from conftest import TestingSessionLocal
    from services import ai_data

    with TestingSessionLocal() as db:
        assert ai_data.list_messages(db) == []


def test_ask_carries_history(client: TestClient, fresh_db, monkeypatch):
    from routers import stats as stats_router

    _activate_model()
    seen = {}

    def fake_stream(messages, *, on_chunk, **kw):
        seen["roles"] = [m["role"] for m in messages]
        return "答"

    monkeypatch.setattr(stats_router, "call_llm_stream", fake_stream)
    _ask_events(client, {
        "days": 30, "question": "那时间呢",
        "history": [
            {"role": "user", "content": "完成得怎么样"},
            {"role": "assistant", "content": "完成 12 项"},
        ],
    })
    # system + 历史 2 轮 + 本次提问
    assert seen["roles"] == ["system", "user", "assistant", "user"]


def test_ask_disabled_without_model(client: TestClient, fresh_db):
    events = _ask_events(client, {"days": 7, "question": "怎么样"})
    assert [e["type"] for e in events] == ["error", "done"]
    assert events[0]["code"] == "no_model"


# —— 月报成长指标：health / warnings 单算修复（此前恒 None/0） ————————————


def test_growth_health_and_warnings_present(fresh_db):
    from conftest import TestingSessionLocal

    with TestingSessionLocal() as db:
        lo, hi = stats_svc.period_range("month")
        current = stats_svc.stats_summary(db, lo=lo, hi=hi)
        growth = stats_svc._calculate_growth_metrics(db, current, "month")
    assert growth is not None
    # 报告周期模式的 summary 不带 health/warnings 块，但成长指标各自单算有值
    assert "health" not in current and "warnings" not in current
    assert growth["health_change"] is not None
    assert set(growth["health_change"]) == {"current", "prev", "diff"}
    assert "warnings_reduced" in growth["goal_achievement"]


# —— 记忆注入（画像 / 相关事实 / 上期结论进三个 AI 调用点）————————————————


def _seed_activity() -> None:
    """非零活动造数：1 条今天完成的纯待办 + 1 条昨天已发生日程——绕过洞察
    零数据早退（2026-11-09 起），洞察注入类用例共用。"""
    from conftest import TestingSessionLocal
    from models import Task

    with TestingSessionLocal() as db:
        today = _today_zero()
        db.add(Task(title="洞察样例待办", created_at=today - timedelta(days=1),
                    done_at=datetime.now()))
        yesterday = today - timedelta(days=1)
        db.add(Task(title="洞察样例日程", start_time=yesterday + timedelta(hours=10),
                    end_time=yesterday + timedelta(hours=11), created_at=yesterday))
        db.commit()


def _seed_memory_and_report() -> None:
    """画像素材：1 条 AI 学习规则（低置信 → 注入参考段）+ 1 条未过期事实 +
    1 条带上期建议的周报留档。"""
    from conftest import TestingSessionLocal
    from models import EpisodicFact, MemoryRule

    with TestingSessionLocal() as db:
        db.add(MemoryRule(category="作息规律",
                          content="周三上午效率最高，重要任务优先排该时段",
                          source="ai_reflect", confidence=0.6, evidence="近 3 周完成分布"))
        db.add(EpisodicFact(topic="状态", fact="本周感冒需要多休息", source="chat"))
        lo, _ = stats_svc.period_range("week")
        db.add(StatsReport(
            period_type="week", period_start=lo - timedelta(days=7),
            content="【总评】上期总评句。\n\n## 建议\n\n- 上期建议甲"))
        db.commit()


def test_insight_injects_memory_and_previous_review(fresh_db, monkeypatch):
    """洞察注入：用户画像（Tier1 参考段 + Tier2 事实）、最近报告结论、
    纵向数据键（rhythm/prev_avg/health）齐进 prompt。"""
    from conftest import TestingSessionLocal

    _seed_memory_and_report()
    _seed_activity()  # 非零数据绕过零数据早退
    stats_svc._INSIGHT_CACHE.clear()
    captured = {}

    def fake_llm(messages, **kw):
        captured["c"] = messages[0]["content"]
        return "【总评】稳。"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    with TestingSessionLocal() as db:
        stats_svc.generate_insight(db, days=7)
    stats_svc._INSIGHT_CACHE.clear()

    c = captured["c"]
    assert "周三上午效率最高" in c  # Tier 1 参考段
    assert "本周感冒需要多休息" in c  # Tier 2 事实
    assert "上期建议甲" in c  # 上期结论（建议条目清单，执行情况对照）
    for key in ("rhythm", "prev_avg", "health", "by_project_top", "deviations"):
        assert key in c  # 纵向数据键


def test_report_injects_profile_health_habits(client: TestClient, fresh_db, monkeypatch):
    """报告注入：用户画像段 + 数据含 health / habits_text 键（报告周期模式
    summary 本不带四块，由 generate_report 单算补齐）。"""
    captured = {}

    def fake_llm(messages, **kw):
        captured["c"] = messages[0]["content"]
        return "【总评】稳。\n\n## 概述\n\nx"

    monkeypatch.setattr(stats_svc, "call_llm", fake_llm)
    _seed_memory_and_report()
    res = client.post("/api/stats/reports", json={"period": "week"})
    assert res.status_code == 200
    c = captured["c"]
    assert "用户画像" in c and "周三上午效率最高" in c
    assert '"health"' in c and '"habits_text"' in c


def test_ask_injects_profile_facts_and_review(client: TestClient, fresh_db, monkeypatch):
    """问答注入：system 带用户画像 / 与问题相关的情节事实 / 最近报告结论；
    空库时三段全不出现（形状稳定）。"""
    from routers import stats as stats_router

    _activate_model()
    seen = {}

    def fake_stream(messages, *, on_chunk, **kw):
        seen["sys"] = messages[0]["content"]
        return "答"

    monkeypatch.setattr(stats_router, "call_llm_stream", fake_stream)
    # 空库：无任何记忆段（断言段落标题全串——system 提示词本身含「用户画像」措辞）
    _ask_events(client, {"days": 7, "question": "我最近为什么老是拖延感冒"})
    assert "用户画像（个性化判断依据）" not in seen["sys"]
    assert "与问题相关的近期事实" not in seen["sys"]
    assert "最近报告结论" not in seen["sys"]

    _seed_memory_and_report()
    _ask_events(client, {"days": 7, "question": "我最近为什么老是拖延感冒"})
    sys = seen["sys"]
    assert "用户画像（个性化判断依据）" in sys and "周三上午效率最高" in sys
    assert "与问题相关的近期事实" in sys and "本周感冒需要多休息" in sys  # 「感冒」2-gram 召回
    assert "最近报告结论" in sys and "上期建议甲" in sys
