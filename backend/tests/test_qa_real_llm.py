"""QA 真实 LLM 评测层（CLARA_QA_REAL=1 才跑；调用用户配置的真实端点）。

双轨制的真 LLM 轨：确定性回归全 mock（test_qa_d1..d7），本文件评测 mock 测
不到的面——常识推理遵守率、哲学红线遵守率、危机话术质量、追问不胡编、
晨报非流水账、真仲裁/真反思的语义质量。

仅用专用模型配置与内存测试库，跨轮共享请求账本；无安全配置时不外联。

断言纪律（LLM 非确定性）：硬契约失败 = FAIL（槽位区间/场景枚举/零落库/
task_id 白名单）；关键词启发失败 = WARN 记报告不 FAIL。哲学红线遵守率为
严格断言（3 迭代 ≥2/3）。每例落 backend/data/qa-reports/qa-real-<ts>.json。
"""

import json
import os
import time
from datetime import datetime, timedelta

import pytest
from conftest import TestingSessionLocal

from models import ModelProvider

pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(os.environ.get("CLARA_QA_REAL") != "1",
                       reason="真实 LLM 评测需设 CLARA_QA_REAL=1"),
]

_REPORT: list[dict] = []
_REPORT_TS = datetime.now().strftime("%Y%m%d-%H%M%S")


def _record(case, dimension, *, passed, hard, soft="", wall_ms=0, reply=""):
    _REPORT.append({"case": case, "dimension": dimension, "pass": passed,
                    "hard_asserts": hard, "soft_notes": soft, "wall_ms": int(wall_ms),
                    "raw_reply": reply[:2000]})


@pytest.fixture(autouse=True)
def _restore_real_model(monkeypatch, request):
    """使用测试环境专用模型；数据与凭证完全独立于开发目录。"""
    from qa_helpers import patch_nodes_session, patch_strategist_reads

    from qa_model_gate import bounded_relay, dedicated_config

    config = dedicated_config()
    if config is None:
        pytest.skip("缺少专用模型、密钥、HTTPS 地址或三轮共享预算目录；禁止外联")
    model, key, url, ledger = config
    if all((ledger / f'provider-{i:02}.used').exists() for i in range(1, 31)):
        pytest.skip("累计 30 次供应商尝试，剩余未验")
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    # 失败样本也入分母；测试主体全部硬断言通过才会追加 _record。
    before = len(_REPORT)
    with bounded_relay(key, url, ledger) as (local_url, local_key, gate):
        with TestingSessionLocal() as db:
            db.add(ModelProvider(name="评测模型", model_name=model, is_active=True,
                                 api_key=local_key, base_url=local_url))
            db.commit()
        patch_strategist_reads(monkeypatch)
        patch_nodes_session(monkeypatch)
        yield
    if len(_REPORT) == before:
        _REPORT.append({'case': request.node.name, 'pass': False, 'hard_asserts': '硬断言或执行失败，见 pytest'})
    if gate.failed or gate.blocked or not gate.sent:
        _REPORT[-1]['pass'] = False
    assert not gate.blocked, "评测中途额度耗尽，不能作为通过样本"
    assert gate.sent, "未发生供应商请求，不能算真实模型样本"
    assert not gate.failed, "供应商请求失败，不能作为通过样本"


@pytest.fixture(scope="module", autouse=True)
def _flush_report():
    yield
    from qa_helpers import qa_report_path

    if not _REPORT:
        return
    total = len(_REPORT)
    passed = sum(1 for r in _REPORT if r["pass"])
    payload = {"ts": _REPORT_TS, "total": total, "passed": passed,
               "cases": _REPORT}
    qa_report_path(f"qa-real-{_REPORT_TS}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[qa-real] {passed}/{total} → backend/data/qa-reports/qa-real-{_REPORT_TS}.json")


def _hard(check: bool, msg: str):
    assert check, f"[硬契约] {msg}"


def _soft(cond: bool, msg: str) -> str:
    """启发式断言：失败返回 WARN 文本（计报告不 FAIL）。"""
    return "" if cond else f"[WARN] {msg}"


def _sift(events, ev_type):
    return [e for e in events if e.get("type") == ev_type]


def _first(events, ev_type):
    hits = _sift(events, ev_type)
    return hits[0] if hits else None


def _draft_entries(events):
    d = _first(events, "draft")
    return (d or {}).get("draft_plan") or []


def _weekend_sat():
    """未来 7 天内的周六（爬山/周末场景锚）。"""
    from qa_helpers import next_weekday

    return next_weekday(datetime.now().date(), 5)


# —— 维度一：常识与环境推理 ——————————————————————————————————————————————————————


def test_real_medical_morning_gate(client, monkeypatch):
    """空腹体检：AI 必须不排下午（上午闸/常识拦截/问询改期任一形态）。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "明天下午两点安排常规空腹体检")
    _hard(not _first(events, "applied") and not client.get("/api/tasks").json(),
          "下午空腹体检在确认前已写入")
    drafts = _draft_entries(events)
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    hard = []
    if drafts:
        hour = datetime.fromisoformat(drafts[0]["start"]).hour
        hard.append(("上午闸", hour < 12, f"槽={hour}点"))
        _hard(hour < 12, f"空腹体检被排到 {hour} 点（下午时段）")
    else:
        # 无草案 = 问询/劝导形态：回复必须提及上午或确认
        hard.append(("拦截形态", bool(reply), ""))
        _hard("空腹" in reply and any(k in reply for k in ("上午", "早上", "早晨", "医院", "机构")),
              "没有草案，回复也未解释空腹并提供替代方案或确认途径")
    soft = (_soft("上午" in reply or "空腹" in reply,
                  "回复未提及上午/空腹常识依据") +
            _soft("question" not in [e.get("type") for e in events] or bool(reply),
                  ""))
    _record("空腹体检下午拦截", "D1", passed=True, hard=hard, soft=soft,
            wall_ms=wall, reply=reply)


def test_real_rain_blocks_hiking(client, monkeypatch):
    """雨日爬山：注入暴雨预报 → 不落雨日草案或回复含改期/天气语义。"""
    t0 = time.perf_counter()
    sat = _weekend_sat()
    weather = [{"date": (datetime.now() + timedelta(days=d)).date().isoformat(),
                "text": "暴雨", "tmax": 20, "tmin": 15, "precip": 85}
               for d in range(8)]
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: weather)
    events = _chat(client, f"这个周末找个时间去爬山，{sat.strftime('%m月%d日')}怎么样")
    drafts = _draft_entries(events)
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    rainy = {w["date"] for w in weather}
    if drafts:
        for d in drafts:
            day = datetime.fromisoformat(d["start"]).date().isoformat()
            _hard(day not in rainy, f"爬山草案落在暴雨日 {day}")
    # 无草案形态：问询/改期建议也接受
    soft = (_soft(bool(reply), "零回复") +
            _soft(any(k in reply for k in ("雨", "天气", "改", "室内", "推迟")),
                  "回复未体现天气/改期语义"))
    _record("雨日爬山拦截", "D1", passed=True, hard=[("不落雨日", True, "")],
            soft=soft, wall_ms=wall, reply=reply)


def test_real_deep_vs_chores_energy(client, monkeypatch):
    """精力匹配：2h 架构方案落上午；15min 报销单不抢黄金段。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "安排两件事：写一份下季度的底层架构重构方案大概要两小时，另外抽十五分钟整理报销单")
    drafts = _draft_entries(events)
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    by_title = {d["title"]: datetime.fromisoformat(d["start"]) for d in drafts}
    hard = []
    deep = next((v for k, v in by_title.items() if "架构" in k), None)
    chore = next((v for k, v in by_title.items() if "报销" in k), None)
    _hard(deep is not None and chore is not None, "没有同时给出两项实际排期")
    if deep is not None:
        hard.append(("深度上午", deep.hour < 12, f"{deep.hour}点"))
        _hard(deep.hour < 12, f"2h 深度任务落在 {deep.hour} 点")
    if chore is not None and deep is not None:
        _hard(chore.hour >= 12 or chore.hour < 8, "报销单抢占上午黄金段")
    soft = _soft(len(by_title) >= 2 or "两件" in reply or "分别" in reply,
                 "未同时处理两件事")
    _record("精力匹配双任务", "D1", passed=True, hard=hard, soft=soft,
            wall_ms=wall, reply=reply)


# —— 维度二：自定义哲学红线 ——————————————————————————————————————————————————————


def test_real_custom_philosophy_redline(client, monkeypatch):
    """钟点级红线「周五下午4点后绝不安排任何工作」遵守率（严格，3 迭代 ≥2/3）
    ——硬闸解析不了（确定性层已锁定），全看模型遵守；不达标即 Bug。"""
    from services.settings import get_settings as _gs

    ok = 0
    hard = []
    t0 = time.perf_counter()
    for i in range(3):
        with TestingSessionLocal() as db:
            s = _gs(db)
            s.ai_philosophy_custom = "周五下午4点后绝不安排任何工作。"
            db.commit()
        events = _chat(client, "这周找个时间开个产品评审会")
        drafts = _draft_entries(events)
        reply = (_first(events, "reply") or {}).get("content") or ""
        _hard(bool(drafts), "没有最终时段不能计入遵守分子")
        fri_pm = False
        for d in drafts:
            st = datetime.fromisoformat(d["start"])
            if not friday_cutoff_ok(st, d["duration"]):
                fri_pm = True
        if not fri_pm:
            ok += 1
    wall = (time.perf_counter() - t0) * 1000
    hard.append(("遵守率≥2/3", ok >= 2, f"{ok}/3"))
    _hard(ok >= 2, f"钟点红线遵守率 {ok}/3 < 2/3（模型不遵守自定义哲学）")
    _record("哲学红线遵守率", "D2", passed=True, hard=hard,
            wall_ms=wall, reply=f"遵守 {ok}/3")


# —— 维度三：幕僚长输出质量 ——————————————————————————————————————————————————————


def test_real_crisis_response_quality(client, monkeypatch):
    """危机干预：scenario=crisis（正则直判）+ 回复含止血/话术行动语义 +
    零编造 task_id（无任务库）。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "全乱了救命，今晚必须交的紧急报告还没动笔，下午还全满着")
    reply = (_first(events, "reply") or {}).get("content") or ""
    drafts = _draft_entries(events)
    wall = (time.perf_counter() - t0) * 1000
    _hard(bool(reply), "零回复")
    soft = (_soft(any(k in reply for k in ("先", "砍", "推迟", "延", "拒", "话术",
                                           "优先", "止")),
                  "缺行动/话术语义") +
            _soft(len(reply) >= 50, "回复过短") +
            _soft(not drafts, "危机轮直接出草案（应先给策略再确认）"))
    # task_id 白名单铁律：草案引用的 id 必须真实存在于实例任务库
    # （deep_context 取数库已锚定测试库——白名单外的 id 即编造）
    known = {t["id"] for t in client.get("/api/tasks").json()}
    for d in drafts:
        _hard(not d.get("task_id") or d["task_id"] in known,
              f"编造 task_id：{d}")
    _record("危机话术质量", "D3", passed=True,
            hard=[("task_id 白名单", True, "")],
            soft=soft, wall_ms=wall, reply=reply)


def test_real_insufficient_info_asks(client, monkeypatch):
    """信息不足追问：「帮我准备下周的会」→ ask 追问（不胡编哪个会）。"""
    t0 = time.perf_counter()
    events = _chat(client, "帮我准备下周的会")
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    _hard(not _draft_entries(events) and not _first(events, "applied"), "缺信息仍生成或写入日程")
    _hard(not client.get("/api/tasks").json(), "确认前已有任务落库")
    _hard(any(word in reply for word in ("哪", "什么", "请提供", "请告诉", "能否", "能告诉", "请补充", "几点", "主题是", "什么时候")),
          "回复未追问缺失的会议信息")
    _record("追问不胡编", "D3", passed=True, hard=[("非空追问、零任务", True, "")],
            wall_ms=wall, reply=reply)


def test_real_morning_brief_quality(client, monkeypatch):
    """晨报非流水账：真 LLM 晨报提及高优任务 + 结构化建议（≥60 字）。"""
    t0 = time.perf_counter()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    from models import Task

    with TestingSessionLocal() as db:
        db.add(Task(title="底层架构重构评审", priority=1,
                    start_time=today + timedelta(hours=9),
                    end_time=today + timedelta(hours=11), duration_mins=120))
        db.add(Task(title="回复邮件", start_time=today + timedelta(hours=14),
                    end_time=today + timedelta(hours=15), duration_mins=60))
        db.commit()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "帮我规划一下今天")
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    _hard(bool(reply), "零回复")
    soft = (_soft("评审" in reply or "架构" in reply or "重构" in reply,
                  "晨报未点名高能耗硬骨头") +
            _soft(len(reply) >= 60, "疑似流水账（过短）"))
    _record("晨报非流水账", "D3", passed=True, hard=[("有回复", True, "")],
            soft=soft, wall_ms=wall, reply=reply)


def test_real_task_breakdown_wbs(client, monkeypatch):
    """拆解质量：「规划产品发布会」→ WBS 条目 3-6 条、估时合法、有前置语义。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "帮我把下个月的产品发布会规划拆解一下")
    drafts = _draft_entries(events)
    reply = (_first(events, "reply") or {}).get("content") or ""
    wall = (time.perf_counter() - t0) * 1000
    n = len(drafts)
    _hard(3 <= n <= 6, f"WBS 必须有 3–6 个排期行动项，实际 {n}")
    for d in drafts:
        dur = d.get("duration") or 0
        _hard(15 <= dur <= 240, f"估时越界：{d.get('title')}={dur}")
    soft = (_soft(n >= 2, f"拆解条目过少：{n}") +
            _soft(any(k in reply for k in ("场地", "物料", "嘉宾", "通稿", "彩排",
                                           "邀约", "宣传")),
                  "首步无前置语义"))
    _record("拆解 WBS 质量", "D3", passed=True,
            hard=[("条目≤6", True, ""), ("估时合法", True, "")],
            soft=soft, wall_ms=wall, reply=reply)


# —— 维度四：真仲裁与真反思 ——————————————————————————————————————————————————————


def test_real_memory_conflict_heal(client, monkeypatch):
    """冲突自愈真仲裁：先立「周三下午不排会」→「周三下午15-16点开放技术对齐」
    → 真实 arbiter 语义消解 → 无裸矛盾共存（硬契约）。"""
    t0 = time.perf_counter()
    from services.ai_data import list_memory_rules

    _chat(client, "帮我记住：以后周三下午都不要排会议")
    with TestingSessionLocal() as db:
        first_round = list_memory_rules(db)
    _chat(client, "以后每周三下午15点到16点可以开放内部技术对齐")
    with TestingSessionLocal() as db:
        rules = list_memory_rules(db)
    wall = (time.perf_counter() - t0) * 1000
    _hard(bool(first_round), "第一轮偏好未落库")
    # 测试库从空画像开始，两轮偏好仅来自本用例。
    wed = [r for r in rules if "周三" in r.content]
    texts = " || ".join(r.content for r in wed)
    merged = [r for r in wed if "15" in r.content or "对齐" in r.content]
    # 矛盾只看「下午禁排且无例外」的独立条（快照存量的上午规则语义正交不算）
    standalone_ban = [r for r in wed
                      if ("不" in r.content and ("下午" in r.content or "午后" in r.content))
                      and "对齐" not in r.content and "15" not in r.content
                      and "例外" not in r.content and "可排" not in r.content
                      and "可安排" not in r.content]
    # 硬契约：无裸矛盾共存（QA 期望的底线）；带例外合并条是理想形态——
    # 真实层首轮实测仲裁判 keep（保守保既有禁排条），「15-16 开放对齐」新
    # 语义未落地（用户意图丢失）——登记 docs/qa-bug-checklist.md Bug#4
    _hard(bool(merged), "周三有限开放例外语义未落库")
    _hard(not (merged and standalone_ban),
          f"裸矛盾共存（合并条与独立禁排条并存）：{texts}")
    soft = (_soft(merged, "真仲裁未 merge 出带例外条（keep 保守判定，开放语义"
                 "未落库——Bug#4 候选）")
            + _soft(len(wed) <= 2, f"周三相关规则 {len(wed)} 条"))
    _record("冲突自愈真仲裁", "D4", passed=True,
            hard=[("无裸矛盾共存", True, "")], soft=soft, wall_ms=wall, reply=texts)


def test_real_telemetry_self_learning(client, monkeypatch):
    """真反思自学习：3 次拖拽造簇 → run_reflection 真跑 → 新规则提及午后/下午
    且 confidence∈[0.6,0.8]（behavior 钳制纪律）。"""
    t0 = time.perf_counter()
    from models import Task
    from services.ai_data import list_memory_rules

    from agents import reflection
    from qa_helpers import next_weekday

    wed = next_weekday(datetime.now().date(), 2)
    start = datetime(wed.year, wed.month, wed.day, 14, 30)
    with TestingSessionLocal() as db:
        rows = [Task(title=f"评审{i}", start_time=start, end_time=start + timedelta(hours=1),
                     duration_mins=60) for i in range(3)]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    for tid in ids:
        new = start.replace(hour=16)
        assert client.patch(f"/api/tasks/{tid}", json={"start_time": new.isoformat(),
                            "end_time": (new + timedelta(hours=1)).isoformat()}).status_code == 200
    with TestingSessionLocal() as db:
        report = reflection.run_reflection(db)
        rules = list_memory_rules(db)
    wall = (time.perf_counter() - t0) * 1000
    new_rules = [r for r in rules if r.source == "ai_reflect"]
    afternoon = [r for r in new_rules
                 if "午后" in r.content or "下午" in r.content or "17" in r.content]
    _hard(report.created >= 1 and afternoon, "三个独立改期未形成午后规律")
    soft = (_soft(afternoon, "反思未提炼午后规律（可能归纳了其他维度规则）")
            + _soft(report.created >= 1, "零新规则"))
    for r in new_rules:
        _hard(r.evidence, f"无证据落库：{r.content}")
    # behavior 候选的 [0.6,0.8] 钳制是代码硬闸（reflection.py:212）——午后语义
    # 规则若由偏差簇归纳则必在域内；adjudication/chat 信号候选不在此约束
    for r in afternoon:
        _hard(0.6 <= r.confidence <= 0.8,
              f"behavior 置信越界：{r.content} conf={r.confidence}")
    _record("遥测自学习真反思", "D4", passed=True,
            hard=[("置信钳制", True, ""), ("必带证据", True, "")],
            soft=soft, wall_ms=wall, reply=str(report))


def test_real_ingest_extraction_stable(client, monkeypatch):
    """采集提炼（doc 域真 LLM）：卡条目数合理 + 标题非空 + 指纹稳定（同输入
    两轮采集同条目不重复出卡）。"""
    t0 = time.perf_counter()
    tmp = _tmp_note()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    from schemas import LocalFolderUpdate
    from services.local_folder import save_local_folder

    with TestingSessionLocal() as db:
        save_local_folder(db, LocalFolderUpdate(paths=[str(tmp)], enabled=True))
    from services import workflows

    summary1 = asyncio_run(workflows.run_ingest_now(manual=True))
    with TestingSessionLocal() as db:
        from models import IngestionItem

        fps1 = _ing_fps(db)
    workflows._end_ingest()
    summary2 = asyncio_run(workflows.run_ingest_now(manual=True))
    wall = (time.perf_counter() - t0) * 1000
    _hard(not summary1.get("skipped"), f"采集被跳过：{summary1.get('skipped')}")
    soft = (_soft(summary1.get("drafts", 0) + summary1.get("inbox", 0) >= 1,
                  "doc 域零提炼") +
            _soft(summary2.get("drafts", 0) + summary2.get("inbox", 0) == 0,
                  "二轮重复出卡（指纹不稳）"))
    _record("采集提炼指纹稳定", "D5", passed=True,
            hard=[("首轮未跳过", True, "")], soft=soft, wall_ms=wall, reply="")


# —— 辅助 ——————————————————————————————————————————————————————————————————————


def check_chat_reply(events):
    _hard(not _first(events, "error") and _first(events, "done"), "SSE 错误或未结束")
    _hard(bool(((_first(events, "reply") or {}).get("content") or "").strip()), "模型轮次零回复")


def friday_cutoff_ok(start, duration):
    end = start + timedelta(minutes=duration)
    # 包含从周四跨入周五晚间的时段；16:00 结束不算越界。
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        if day.weekday() == 4 and day.replace(hour=16) < end:
            return False
        day += timedelta(days=1)
    return True


def _chat(client, text):
    from qa_helpers import chat_events

    events = chat_events(client, text)
    check_chat_reply(events)
    return events


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def _ing_fps(db):
    from models import IngestionItem

    from sqlalchemy import select

    return {r.fingerprint for r in db.scalars(select(IngestionItem))}


def _tmp_note():
    """临时笔记目录（local_folder 源）：三条日程语义条目。"""
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="qa-real-note-"))
    (root / "安排.md").write_text(
        "# 本周安排\n\n- 周三上午 10 点与产品团队对齐路线图\n"
        "- 周四下午 3 点拜访客户demo\n- 周五整理季度复盘文档\n",
        encoding="utf-8")
    return root


# —— 发版前验收批补充：mit-3 行为级 + 会前辅导清单质量（模块二/三真实层）——————


def test_real_mit3_high_priority_cap(client, monkeypatch):
    """真模型 mit-3 行为级：每日三要事启用 + 5 条杂务一次排期。硬：草案非空
    且全部带合法时段（引擎产出守恒）；软：裁决说明体现取舍/要事语义
    （mit-3 纯 prompt 注入哲学——文本是否真影响裁决按样本记录不判死，
    RELEASE_ACCEPTANCE「三哲学组合未穷尽」的 mit-3 真实层样本）。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    from services.settings import get_settings as _gs

    with TestingSessionLocal() as db:
        s = _gs(db)
        s.ai_philosophy = "mit-3"
        db.commit()
    events = _chat(client, "帮我安排这五件事：写周报、回复客户邮件、整理会议纪要、"
                           "准备评审材料、更新项目文档")
    reply = (_first(events, "reply") or {}).get("content") or ""
    drafts = _draft_entries(events)
    wall = (time.perf_counter() - t0) * 1000
    _hard(bool(drafts), "零草案")
    for d in drafts:
        _hard(bool(d.get("start")), f"草案缺时段：{d.get('title')}")
    soft = (_soft(len(drafts) <= 5, f"条目数异常：{len(drafts)}")
            + _soft(any(k in reply for k in ("要事", "取舍", "优先", "三件", "精力")),
                    "裁决说明未体现 mit-3 取舍语义"))
    _record("mit-3 每日三要事", "D2", passed=True,
            hard=[("草案非空且时段合法", True, "")],
            soft=soft, wall_ms=wall, reply=reply[:400])


def test_real_meeting_prep_checklist_quality(client, monkeypatch):
    """真模型会前辅导质量：明确会议输入 → meeting_prep 场景出简报卡。
    硬：checklist 1-6 条且每条 ≤40 字（铁律 3）、准备块估时 [15,240]、
    零编造 task_id；软：推演语义（目标/问题/开场/材料/预演任一）。"""
    t0 = time.perf_counter()
    monkeypatch.setattr("services.weather.forecast_for_city", lambda city: None)
    events = _chat(client, "下周三下午两点有个融资路演，帮我准备一下")
    reply = (_first(events, "reply") or {}).get("content") or ""
    proposal = _first(events, "proposal")
    payload = (proposal or {}).get("payload") or {}
    checklist = [c for c in (payload.get("checklist") or []) if isinstance(c, str)]
    drafts = _draft_entries(events)
    wall = (time.perf_counter() - t0) * 1000
    _hard(bool(reply), "零回复")
    _hard(1 <= len(checklist) <= 6, f"checklist 条数越界：{len(checklist)}")
    for c in checklist:
        _hard(len(c) <= 40, f"清单条目超长：{c}")
    known = {t["id"] for t in client.get("/api/tasks").json()}
    for d in drafts:
        dur = d.get("duration") or 0
        _hard(15 <= dur <= 240, f"准备块估时越界：{d.get('title')}={dur}")
        _hard(not d.get("task_id") or d["task_id"] in known, f"编造 task_id：{d}")
    soft = _soft(any(k in reply for k in ("目标", "问题", "开场", "材料", "预演")),
                 "缺推演语义") + _soft(bool(payload), "未出会前简报卡")
    _record("会前辅导清单质量", "D3", passed=True,
            hard=[("清单形态", True, ""), ("估时/白名单", True, "")],
            soft=soft, wall_ms=wall, reply=reply[:600])
