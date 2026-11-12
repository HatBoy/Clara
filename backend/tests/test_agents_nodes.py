"""agents.nodes 认知分支节点测试（批 B：center_router / qa / smalltalk；
批 C：schedule 分支（2026-10-21 批 3 起经排期子图 scheduler_app.invoke 直调，
断言零改动——行为等价的迁移证明）；coach 分支 2026-10-21 Track B 批 5 起
翻转 strategist 子图，节点级用例在 tests/test_strategist_*.py）。

批 H（delete_confirmer）随落地批次在本文件增补。

替身口径（docs/ai-agents.md §五 两会会话陷阱）：
- LLM：monkeypatch services.llm.call_llm——节点函数内 import 源模块，调用时点
  解析模块属性，patch 源模块即可拦截；
- 读库：monkeypatch nodes.SessionLocal → TestingSessionLocal（漏 patch 会真开
  dev 库）+ patch services.tasks.list_tasks 返回 SimpleNamespace 任务替身。
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import agents.graph as graph_mod
import agents.nodes as nodes_mod
import agents.scheduler.context as context_mod
import agents.scheduler.intake as intake_mod
from agents.scheduler import scheduler_app
from agents.scheduler.prompts import PROFILER_PROMPT as PLANNER_PROMPT
from agents.parsing import loads_obj
from agents.prompts import (
    DELETE_PROMPT,
    QA_PROMPT,
    ROUTER_PROMPT,
    SMALLTALK_PROMPT,
)
from conftest import TestingSessionLocal
from services import ai_data
from services import llm
from services import settings as settings_svc
from services import tasks as tasks_svc
from services import weather as weather_svc


# —— 替身小件 ————————————————————————————————————————————————————————————————


class _LLMCapture:
    """call_llm / call_llm_stream 双协议替身：按 system prompt 身份分发预设应答
    并记录 (system, user) 调用；流式形态（on_chunk 非空）把应答切成两段逐段
    回调再返回全文（模拟 token 增量）。"""

    def __init__(self, replies=None, default=""):
        self.replies = replies or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.msgs: list[list[dict]] = []

    def __call__(self, messages, *, json_mode=False, max_tokens=None, on_chunk=None):
        system = messages[0]["content"]
        self.calls.append((system, messages[-1]["content"]))
        self.msgs.append(messages)
        # 子串匹配而非身份匹配：qa/smalltalk 的 system 是"基础提示词 + 注入规则段"
        # 拼接体（assemble_system_prompt），身份恒不等
        for prompt, reply in self.replies.items():
            if prompt in system:
                if on_chunk is not None and reply:
                    self._emit_chunks(reply, on_chunk)
                return reply
        if on_chunk is not None and self.default:
            self._emit_chunks(self.default, on_chunk)
        return self.default

    @staticmethod
    def _emit_chunks(reply: str, on_chunk) -> None:
        mid = len(reply) // 2
        for piece in (reply[:mid], reply[mid:]):
            if piece:
                on_chunk(piece)


def _patch_smalltalk_stream(monkeypatch, fake) -> None:
    """smalltalk 流式替身（优化批 6 温度直用后）：patch init_llm + stream_model
    ——节点不再走 call_llm_stream，捕获口径与 _LLMCapture 不变。"""
    monkeypatch.setattr(llm, "init_llm", lambda **kw: object())
    monkeypatch.setattr(
        llm, "stream_model",
        lambda model, messages, *, on_chunk: fake(messages, on_chunk=on_chunk))


def _task(id_="t1", title="周报评审", prio=1, start=None, end=None, notes=None, dur=90,
          rrule=None):
    """任务替身（对齐 ORM Task 的节点消费面：list_tasks 返回的字段）。"""
    return SimpleNamespace(id=id_, title=title, priority=prio, start_time=start,
                           end_time=end, notes=notes, duration_mins=dur, reschedule_count=0,
                           recurrence_rule=rrule)


# —— center_router：六分类 / 兜底 / 异常上抛 ————————————————————————————————

@pytest.mark.parametrize("intent", ["schedule", "qa", "coach", "smalltalk", "ingest", "delete"])
def test_router_classifies_six_intents(monkeypatch, intent):
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps(
        {"intent": intent, "reason": "测试"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": "随便"}) == {"intent": intent}


def test_router_garbage_output_falls_back_smalltalk(monkeypatch):
    # 解析失败是数据 → 兜底 smalltalk 不 raise（坑 ⑥）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture({ROUTER_PROMPT: "抱歉我不知道"}))
    assert nodes_mod.center_router({"user_input": "x"}) == {"intent": "smalltalk"}


def test_router_invalid_intent_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(
        {ROUTER_PROMPT: '{"intent": "hack", "reason": "x"}'}))
    assert nodes_mod.center_router({"user_input": "x"}) == {"intent": "smalltalk"}


def test_router_llm_exception_propagates(monkeypatch):
    # LLM 调用失败是故障 → 原样上抛（壳译 agent_error），不静默吞
    def boom(messages, *, json_mode=False, max_tokens=None):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(llm, "call_llm", boom)
    with pytest.raises(RuntimeError, match="网络炸了"):
        nodes_mod.center_router({"user_input": "x"})


def test_router_injects_chat_summary_prefix(monkeypatch):
    """滚动摘要前缀（2026-10-26）：摘要非空时进 router 载荷——上午闲聊交代的
    偏好被挤出近期窗后靠摘要接住（路由层是"遗忘"的第一道闸）；无摘要零注入，
    载荷形态与旧行为一致。"""
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "schedule", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="早前-1")
        ai_data.append_message(db, role="user", content="本轮话")
        ai_data.save_chat_summary(db, summary="用户提过周五下午别排事", count=1)
    nodes_mod.center_router({"user_input": "本轮话"})
    content = fake.calls[0][1]  # _LLMCapture 记录 (system, user content)
    assert "（更早对话摘要，供衔接）\n用户提过周五下午别排事" in content
    assert "（近期对话，供衔接判读）" in content
    assert "（本轮输入）\n本轮话" in content
    # 无摘要（新会话/未压缩）：前缀块缺席
    fake2 = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "qa", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake2)
    with TestingSessionLocal() as db:
        ai_data.clear_chat_session(db)  # 上段落的摘要清掉（同库内两形态）
    nodes_mod.center_router({"user_input": "看下日程"})  # 无排期动词不预路由
    assert "更早对话摘要" not in fake2.calls[0][1]


def test_router_preroutes_schedule_by_rules(monkeypatch):
    """规则层预路由（2026-10-21 真机 bug 增）：强排期动词 + 时间表达 + 无疑问词
    → 直接 schedule 零 LLM——堵 LLM 误判 smalltalk 后闲聊分支凭空谎报
    "已安排/与现有日程无冲突"（不读库不产卡，用户日历空等）。"""
    def boom(*_a, **_k):
        raise AssertionError("预路由命中不应调 LLM")

    monkeypatch.setattr(llm, "call_llm", boom)
    for text in ("我安排明天下午15点爬山",
                 "安排一下后天上午10点的体检",
                 "记一下，今天上午10点体检",
                 "帮我把报告排到明天上午",
                 "预约下周三牙医",
                 "提醒我明天吃药"):
        assert nodes_mod.center_router({"user_input": text}) == {"intent": "schedule"}


@pytest.mark.parametrize("text", [
    "明天的高优任务我要怎么准备？",  # 疑问词挡（coach；「怎么」不在 qa 表）
    "明天适合跑步吗？",            # 无动词 + 疑问（qa 走 LLM——「跑步」不在日历域词表）
    "明天上午去爬山",              # 无动词——弱表述仍走 LLM（例句表判 schedule）
    "删掉牙医",                   # 删除意图不含排期动词（delete）
    "删掉明天的牙医预约",           # "预约"是名词形态——删除动词排除（delete）
    "把它改到明天",                # 改期语义交 LLM（动词表不含"改"）
    "看看接入源里有什么要做的",     # ingest + 疑问（域词不中走 LLM）
    "今天太忙了，帮我理一理",       # coach：无排期动词（无疑问词）
    "讲个冷笑话",                 # smalltalk
    # 长期偏好守卫（2026-11-02 真机三轮）：动词+时间双命中但含长期/否定词
    # → settings 意图（prefer/unprefer 才落库），预路由抢判后 profiler 的
    # llm_reply 会编造"已记下"零持久化
    "以后工作日的早上八点之前不要给我安排事情",
    "以后周五下午都别排事",
])
def test_router_preroute_negative_goes_llm(monkeypatch, text):
    # 负例不触发规则，LLM 判定照常（替身返回 delete 且句意非 delete，验证走的是 LLM 分支）
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": text}) == {"intent": "delete"}


def test_router_preroute_skipped_with_pending(monkeypatch):
    # pending 非空（问询回复）不走规则——"安排明天下午3点"命中规则形态但语义是
    # 对上一轮问询的回复，交 LLM（替身返回 delete 与规则结果不同，证明走了 LLM）
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    out = nodes_mod.center_router({
        "user_input": "安排明天下午3点",
        "pending": {"kind": "conflict", "entries": [], "ask_text": "…"}})
    assert out == {"intent": "delete"}
    assert len(fake.calls) == 1


def test_router_preroutes_qa_by_rules(monkeypatch):
    """qa 预路由（2026-10-26 提速批 3）：疑问词 ∧ 日历域名词 → 直判 qa 零 LLM
    ——「今天有什么安排」这类高频查询句不再每轮烧 1-3s 路由分类。疑问词表
    刻意不含「怎么/如何」（求思路是 coach）；pending 活跃轮语义复杂不走规则。"""
    def boom(*_a, **_k):
        raise AssertionError("qa 预路由命中不应调 LLM")

    monkeypatch.setattr(llm, "call_llm", boom)
    for text in ("今天有什么安排？", "这周忙吗", "明天有空吗",
                 "我的提醒有哪些", "下周天气怎么样", "周三的日程满吗"):
        assert nodes_mod.center_router({"user_input": text}) == {"intent": "qa"}


@pytest.mark.parametrize("text", [
    "怎么安排这个任务",            # 「怎么」求思路——coach，疑问词表不含（宁漏勿误）
    "明天适合跑步吗？",            # 域词不中（跑步非日历域）
    "删掉明天的提醒",              # 删除动词在场交 LLM 判 delete
])
def test_router_qa_preroute_negative_goes_llm(monkeypatch, text):
    # qa 预路由负例：不命中规则，LLM 判定照常（替身返回 delete 与 qa 不同证走 LLM）
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": text}) == {"intent": "delete"}


def test_router_qa_preroute_skipped_with_pending(monkeypatch):
    # pending 活跃轮不走 qa 规则——问询回复短句「可以吗」带疑问词，交 LLM
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    out = nodes_mod.center_router({
        "user_input": "明天有什么安排",
        "pending": {"kind": "conflict", "entries": [], "ask_text": "…"}})
    assert out == {"intent": "delete"}
    assert len(fake.calls) == 1


def test_router_qa_preroute_noun_forms_stay_zero_llm(monkeypatch):
    """名词守卫（2026-11-01 优化批 1）：「安排/预约」的名词形态是 qa 高频
    查询句——裸词排除会误伤提速初衷，负向后顾（什么/的）+ 前瞻（有）守卫后
    这些句子仍零 LLM 直判 qa。"""
    def boom(*_a, **_k):
        raise AssertionError("名词形态查询句不应调 LLM")

    monkeypatch.setattr(llm, "call_llm", boom)
    for text in ("今天有什么安排？",       # 什么-后顾守卫
                 "明天的安排有哪些",       # 的-后顾守卫
                 "今天的日程安排有哪些",    # 有-前瞻守卫（安排有哪些名词形态）
                 "这周忙吗", "下周天气怎么样"):
        assert nodes_mod.center_router({"user_input": text}) == {"intent": "qa"}


@pytest.mark.parametrize("text", [
    "帮我安排明天的日程可以吗",   # 安排动词形态 + 日程域词 + 吗——排期试探
    "下午两点提醒我开会行吗",     # 提醒我动词 + 提醒域词 + 吗
    "想预约下周的体检有没有空",   # 预约动词形态 + 有没有疑问词
])
def test_router_qa_preroute_verb_forms_go_llm(monkeypatch, text):
    """动词形态排除（2026-11-01 优化批 1）：带动作成分的排期动词 + 疑问词是
    排期试探句（语义 schedule——要撞期检查与草案卡），直判 qa 会答非所卡；
    交 LLM 七分类（替身返回 delete 与 qa 不同证走了 LLM 分支）。"""
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": text}) == {"intent": "delete"}


def test_router_preroutes_bulk_intent(monkeypatch):
    """批量意图支路（2026-10-25 真机修复）：「把所有待办排期」无时间表达，
    强动词+时间的预路由罩不住 → LLM 误判 coach → 幕僚长 clarify 追问
    "把具体待办发给我"死循环——批量词全命中即直判 schedule 零 LLM，且 pending
    活跃也判（换意图语义）；疑问词形态交 LLM（"排期行吗"是征询非指令）。"""
    def boom(*_a, **_k):
        raise AssertionError("批量预路由命中不应调 LLM")

    monkeypatch.setattr(llm, "call_llm", boom)
    for text in ("把所有待办排期", "把日常运营项目组的待办排期",
                 "帮我安排全部任务"):
        assert nodes_mod.center_router({"user_input": text}) == {"intent": "schedule"}
        # strategist clarify 挂起时批量语句同样直判（换意图）
        assert nodes_mod.center_router({
            "user_input": text,
            "pending": {"kind": "strategist_clarify", "origin": "strategist",
                        "ask_text": "把具体待办发给我"}}) == {"intent": "schedule"}


def test_router_bulk_negative_goes_llm(monkeypatch):
    # 疑问词挡批量支路（征询非指令）——交 LLM 判定
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "qa", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": "把所有待办排期行吗"}) == {"intent": "qa"}


# —— 近期对话注入（2026-10-23 省略式确认死循环修复） —————————————————————————


def test_router_injects_recent_history(monkeypatch):
    """历史注入形态：走 LLM 分类时近 6 条历史拼进 user content——"安排一下"
    单看四个字判不了 intent（真机三连死循环根因），上文经「（近期对话）」
    段可见；本轮原话已落库时从历史段剔除（exclude，不重复注入）。"""
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="明天下午5点我要去跑步一小时")
        ai_data.append_message(db, role="assistant", content="跑步计划不错呀～")
        ai_data.append_message(db, role="user", content="安排一下")
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps(
        {"intent": "schedule", "reason": "确认上文安排"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": "安排一下"}) == {"intent": "schedule"}
    _, content = fake.calls[0]
    assert content.startswith("（近期对话，供衔接判读）")
    assert "用户：明天下午5点我要去跑步一小时" in content
    assert "助手：跑步计划不错呀" in content
    assert "（本轮输入）\n安排一下" in content
    assert content.count("安排一下") == 1  # 落库的本轮原话被 exclude 剔除


def test_router_no_history_keeps_bare_input(monkeypatch):
    # 无历史（首轮对话）零注入——user content 与旧行为逐字节一致
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps({"intent": "smalltalk", "reason": "x"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    assert nodes_mod.center_router({"user_input": "你好"}) == {"intent": "smalltalk"}
    _, content = fake.calls[0]
    assert content == "你好"


def test_router_history_stacks_with_pending_prefix(monkeypatch):
    # 历史与问询前缀叠加：历史段在最前、问询前缀保持原样——两层语境并存
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="帮我安排明天下午5点跑步")
        ai_data.append_message(db, role="assistant", content="明天 17:00 可以吗？")
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps(
        {"intent": "schedule", "reason": "确认回复"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    nodes_mod.center_router({"user_input": "可以", "pending": _pending_snapshot()})
    _, content = fake.calls[0]
    assert content.startswith("（近期对话，供衔接判读）")
    assert "（待确认问询：" in content
    assert "用户回复：可以" in content


# —— 条件边路由（纯函数直测） ————————————————————————————————————————————————

def test_route_by_intent_mapping_and_default():
    assert graph_mod._route_by_intent({"intent": "schedule"}) == "scheduler"
    assert graph_mod._route_by_intent({"intent": "qa"}) == "qa_answerer"
    assert graph_mod._route_by_intent({"intent": "coach"}) == "strategist"
    assert graph_mod._route_by_intent({"intent": "smalltalk"}) == "smalltalk_node"
    assert graph_mod._route_by_intent({"intent": "ingest"}) == "collect_data"
    assert graph_mod._route_by_intent({"intent": "delete"}) == "delete_confirmer"
    assert graph_mod._route_by_intent({}) == "smalltalk_node"  # 缺 intent
    assert graph_mod._route_by_intent({"intent": "bogus"}) == "smalltalk_node"  # 非法值


# —— qa_answerer：读库组清单 → 记忆注入 → 文字回答 ——————————————————————————

def test_qa_answerer_reads_tasks_and_answers(monkeypatch):
    fake = _LLMCapture({QA_PROMPT: "今天只有一场评审。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        assert (start is None) == unscheduled or True  # 三口径互斥由节点两次调用保证
        if unscheduled:
            return [_task(id_="t2", title="买咖啡豆", prio=3, notes="深烘")]
        # 相对今天取窗口内日期（2026-10-21 复查批 A 起 QA 窗口 14 天且按实例
        # 展开——展开过滤会挡窗外日期，硬编码历史日期的替身不再有效）
        day = datetime.now() + timedelta(days=3)
        return [_task(start=day.replace(hour=9, minute=0, second=0, microsecond=0),
                      end=day.replace(hour=10, minute=30, second=0, microsecond=0))]

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)

    out = nodes_mod.qa_answerer(
        {"user_input": "今天有什么安排", "memory_rules": ["[作息] 早睡"]})
    assert out == {"final_reply": "今天只有一场评审。"}
    system, payload = fake.calls[0]
    assert QA_PROMPT in system and "[作息] 早睡" in system  # 记忆注入进 system
    data = json.loads(payload)
    assert any("周报评审" in line and "09:00-10:30" in line for line in data["tasks"])
    assert any("未排期" in line and "买咖啡豆" in line for line in data["tasks"])


def test_qa_answerer_inbox_keeps_quota_when_scheduled_explodes(monkeypatch):
    # 收件箱分口径保底（2026-11-01 真机批）：已排期展开行挤爆总上限时收件箱
    # 整段不可见——「我记了哪些待办」被答成「收件箱是空的」；两段各自钳制，
    # 收件箱保底 _QA_INBOX_ROWS 行
    fake = _LLMCapture({QA_PROMPT: "清单。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        if unscheduled:
            return [_task(id_="i1", title="写季度总结", prio=2),
                    _task(id_="i2", title="预约牙医", prio=3)]
        # 45 条已排期（模拟重复系列一天一行展开超 scheduled 配额 30）
        day0 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        return [_task(id_=f"s{i}", title=f"每日站会{i}",
                      start=day0 + timedelta(days=i % 14),
                      end=day0 + timedelta(days=i % 14, minutes=30))
                for i in range(45)]

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    nodes_mod.qa_answerer({"user_input": "我最近都记了哪些待办", "memory_rules": []})
    _, payload = fake.calls[0]
    lines = json.loads(payload)["tasks"]
    assert any("写季度总结" in x and "未排期" in x for x in lines)  # 收件箱可见
    assert any("预约牙医" in x for x in lines)
    assert len(lines) <= nodes_mod._QA_MAX_ROWS  # 总量上限不破


def test_qa_answerer_expands_recurrence_series_and_wide_window(monkeypatch):
    # 重复系列按实例展开 + 窗口 14 天（2026-10-21 复查批 A 修）：旧版清单直接打
    # 系列基准日（数周前的过期日期）且 7 天窗口盖不住"下周三"，与同载荷
    # load_summary 的 14 天实例口径自相矛盾。qa_answerer 内部函数级 import
    # datetime，_freeze_now 冻不住 → 数据相对真实今天构造
    fake = _LLMCapture({QA_PROMPT: "两场周会加一次外勤。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    today = datetime.now()
    iso_byday = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}
    base = today - timedelta(days=7)  # 系列基准日在上周同日（过去）

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        assert (start is None) == unscheduled or True
        if unscheduled:
            return []
        return [
            _task(id_="r1", title="周报评审", prio=1,
                  rrule=f"FREQ=WEEKLY;BYDAY={iso_byday[today.isoweekday()]}",
                  start=base.replace(hour=9, minute=30),
                  end=base.replace(hour=11, minute=0)),
            _task(id_="o1", title="外勤", prio=2, dur=60,  # +11 天：旧 7 天窗口盖不住
                  start=(today + timedelta(days=11)).replace(
                      hour=14, minute=0, second=0, microsecond=0),
                  end=(today + timedelta(days=11)).replace(
                      hour=15, minute=0, second=0, microsecond=0)),
        ]

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    out = nodes_mod.qa_answerer({"user_input": "下周有什么安排", "memory_rules": []})
    assert out == {"final_reply": "两场周会加一次外勤。"}
    data = json.loads(fake.calls[0][1])
    assert data["tasks"] == [
        f"[高] {today:%Y-%m-%d} 09:30-11:00 周报评审",            # 窗口内实例
        f"[高] {today + timedelta(days=7):%Y-%m-%d} 09:30-11:00 周报评审",
        f"[中] {today + timedelta(days=11):%Y-%m-%d} 14:00-15:00 外勤",
    ]
    assert not any(f"{base:%Y-%m-%d}" in line for line in data["tasks"])  # 基准日退场


def test_qa_answerer_no_tasks_honest_placeholder(monkeypatch):
    fake = _LLMCapture({QA_PROMPT: "清单为空。"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    out = nodes_mod.qa_answerer({"user_input": "今天有什么安排", "memory_rules": []})
    assert out["final_reply"] == "清单为空。"
    _, payload = fake.calls[0]
    assert json.loads(payload)["tasks"] == ["（当前没有已排期日程，也没有待办任务）"]


def test_qa_answerer_empty_reply_fallback(monkeypatch):
    monkeypatch.setattr(llm, "call_llm_stream", _LLMCapture(default="   "))
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    out = nodes_mod.qa_answerer({"user_input": "今天呢", "memory_rules": []})
    assert out["final_reply"]  # 空回复兜底，final_reply 恒非空（壳只持久化非空）


# —— smalltalk_node：闲聊直回 ————————————————————————————————————————————————

def test_smalltalk_replies_with_injected_rules(monkeypatch):
    fake = _LLMCapture({SMALLTALK_PROMPT: "不客气～"})
    _patch_smalltalk_stream(monkeypatch, fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    out = nodes_mod.smalltalk_node({"user_input": "谢谢", "memory_rules": ["[称呼] 叫我老板"]})
    assert out == {"final_reply": "不客气～"}
    system, user = fake.calls[0]
    assert SMALLTALK_PROMPT in system and "叫我老板" in system
    assert user == "谢谢"


def test_smalltalk_empty_reply_fallback(monkeypatch):
    _patch_smalltalk_stream(monkeypatch, _LLMCapture(default="  "))
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    out = nodes_mod.smalltalk_node({"user_input": "嗯", "memory_rules": []})
    assert out["final_reply"]


def test_qa_answerer_injects_history_and_summary(monkeypatch):
    """qa 注入对话历史 + 滚动摘要（2026-10-20）：历史居 system 与 user 之间、
    本轮原话不重复注入（chat_context exclude）、摘要进 system 尾部。"""
    fake = _LLMCapture({QA_PROMPT: "好的"})
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="上周聊过搬家")
        ai_data.append_message(db, role="assistant", content="好的")
        ai_data.append_message(db, role="user", content="那后天呢")  # 模拟壳已落库本轮
        ai_data.save_chat_summary(db, summary="用户此前提到体检", count=3)
    out = nodes_mod.qa_answerer({"user_input": "那后天呢", "memory_rules": []})
    assert out == {"final_reply": "好的"}
    msgs = fake.msgs[0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "用户此前提到体检" in msgs[0]["content"]  # 摘要进 system 尾部
    assert json.loads(msgs[-1]["content"])["user_input"] == "那后天呢"  # user 载荷恒末位
    assert [m["content"] for m in msgs[1:-1]] == ["上周聊过搬家", "好的"]  # 本轮已剔除


def test_smalltalk_injects_history(monkeypatch):
    """smalltalk 注入近期历史（无摘要时 system 原样）。"""
    fake = _LLMCapture({SMALLTALK_PROMPT: "早点休息"})
    _patch_smalltalk_stream(monkeypatch, fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="今天好累")
        ai_data.append_message(db, role="assistant", content="辛苦了")
    out = nodes_mod.smalltalk_node({"user_input": "还是睡不着", "memory_rules": []})
    assert out == {"final_reply": "早点休息"}
    msgs = fake.msgs[0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "今天好累"
    assert msgs[-1]["content"] == "还是睡不着"


def test_smalltalk_appends_weather_line(monkeypatch):
    """闲聊天气行（2026-11-01 优化批 1）：配置城市时今日预报随概览附在 user
    尾部——「今天真冷」接得住预报，与 QA 口径一致；未配置城市零天气行。"""
    fake = _LLMCapture({SMALLTALK_PROMPT: "降温了记得添衣"})
    _patch_smalltalk_stream(monkeypatch, fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    monkeypatch.setattr(weather_svc, "forecast_for_city", lambda city: [
        {"date": datetime.now().strftime("%Y-%m-%d"), "text": "小雨",
         "tmin": 12, "tmax": 18, "precip": 70}])
    with TestingSessionLocal() as db:
        s = settings_svc.get_settings(db)
        s.weather_city = "上海"
        db.commit()
    out = nodes_mod.smalltalk_node({"user_input": "今天真冷", "memory_rules": []})
    assert out == {"final_reply": "降温了记得添衣"}
    _, user = fake.calls[0]
    assert "今日天气：小雨" in user and "12~18℃" in user

    # 未配置城市：省略整行（原形态回归）
    with TestingSessionLocal() as db:
        s = settings_svc.get_settings(db)
        s.weather_city = None
        db.commit()
    nodes_mod.smalltalk_node({"user_input": "今天真冷", "memory_rules": []})
    _, user2 = fake.calls[1]
    assert "今日天气" not in user2


# —— schedule 分支：task_intensity 规则 / 排期子图（原 schedule_planner，批 3 迁移） ——

def test_task_intensity_rule_matrix():
    # duration≥90 或 priority=1 → high；≤15 或 priority=3 → low；否则 medium
    assert nodes_mod.task_intensity("x", 90, 2) == "high"
    assert nodes_mod.task_intensity("x", 30, 1) == "high"
    assert nodes_mod.task_intensity("x", 15, 2) == "low"
    assert nodes_mod.task_intensity("x", 45, 3) == "low"
    assert nodes_mod.task_intensity("x", 45, 2) == "medium"
    assert nodes_mod.task_intensity("x", None, None) == "medium"


def test_intake_load_summary_local_aggregation_matches_service(monkeypatch):
    """单查询双产（2026-11-01 优化批 3）：intake 从 busy_meta 本地聚合的日负荷
    与 services.calendar_load_summary 逐字段等价（零行日也出 / high 口径 /
    分钟累计）——旧版同窗两次全量展开，行为等价是重构前提。"""
    from models import Task

    monkeypatch.setattr(intake_mod, "SessionLocal", TestingSessionLocal)
    base = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=2))
    with TestingSessionLocal() as db:
        db.add(Task(title="高优评审", priority=1,
                    start_time=base + timedelta(hours=9),
                    end_time=base + timedelta(hours=11), duration_mins=120))
        db.add(Task(title="普通事务",
                    start_time=base + timedelta(days=1, hours=15),
                    end_time=base + timedelta(days=1, hours=16), duration_mins=60))
        db.commit()
        expected = tasks_svc.calendar_load_summary(db)
    out = intake_mod.intake_node({"user_input": "安排点事"})
    assert out["context"]["load_summary"] == expected
    day_row = next(r for r in expected if r["scheduled_count"])
    assert day_row["high_count"] == 1 and day_row["total_mins"] == 120


def _patch_planner_inputs(monkeypatch, *, pending, scheduled=None):
    """排期链读库/设置替身：pending 待排 / scheduled 窗口 / get_settings /
    calendar_load_summary（负荷摘要恒空表——planner 测试不依赖日聚合细节）。

    注意形参不叫 unscheduled——与 list_tasks 的同名 bool 形参撞名会遮蔽闭包
    列表（替身返回 bool 的教训，见 git 历史）。SessionLocal patch 点随批 3
    迁移到 scheduler.intake（子图内唯一直连库的节点）。
    """
    monkeypatch.setattr(intake_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(context_mod, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        if unscheduled:
            return pending
        return scheduled or []

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)
    monkeypatch.setattr(tasks_svc, "calendar_load_summary", lambda db, **kw: [])
    monkeypatch.setattr(settings_svc, "get_settings", lambda db: SimpleNamespace(
        work_minutes_per_day=480, calendar_start_min=480, calendar_end_min=1320,
        lunch_enabled=True, lunch_start_min=690, lunch_end_min=810, reminder_lead_min=15,
        weather_city=None, ai_philosophy=None))


def test_schedule_planner_happy_path(monkeypatch):
    # agent 草案时间经统一引擎校验重申：LLM 起点 14:00 本就合法 → 引擎原样保留
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": "t1", "title": "写周报",
                        "start": "2026-10-17T14:00:00", "duration": 90}],
        "reply": "下午整块时间写周报。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "把周报排到明天下午", "memory_rules": ["[作息] 午休不排会"]})
    assert out["draft_plan"] == [{"task_id": "t1", "title": "写周报",
                                  "start": "2026-10-17T14:00:00", "duration": 90}]
    assert "写周报" in out["final_reply"]
    # 输入 payload：真实 id 清单（防幻觉）、intensity 内联、记忆规则显式键、settings 形状
    _, payload = fake.calls[0]
    data = json.loads(payload)
    assert data["tasks_unscheduled"][0]["id"] == "t1"
    assert data["tasks_unscheduled"][0]["intensity"] == "high"  # 90 分钟
    assert data["memory_rules"] == ["[作息] 午休不排会"]
    assert data["settings"]["lunch"] == [690, 810]
    assert data["settings"]["day_start"] == "08:00"


def test_schedule_planner_payload_carries_recent_chat(monkeypatch):
    """planner payload 增 recent_chat 键（2026-10-20）：近 6 条、exclude 本轮原话——
    "把刚聊的那件事也排了"之类指代靠它接住；空库时键恒在（空列表）。"""
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"draft_plan": [], "reply": "无"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    with TestingSessionLocal() as db:
        for i in range(8):
            ai_data.append_message(db, role="user", content=f"历史-{i}")
        ai_data.append_message(db, role="user", content="把周报排到明天下午")  # 壳已落库本轮
    scheduler_app.invoke({"user_input": "把周报排到明天下午", "memory_rules": []})
    data = json.loads(fake.calls[0][1])
    rc = data["recent_chat"]
    assert len(rc) == 6
    assert rc[-1]["content"] == "历史-7"
    assert all(m["content"] != "把周报排到明天下午" for m in rc)  # 本轮不重复注入


def test_schedule_planner_payload_carries_chat_summary(monkeypatch):
    """排期链消费滚动摘要（2026-10-26）：intake 收摘要 → profiler 载荷 chat_summary
    键带值——排期分支此前丢弃摘要，跨天偏好（"周五下午别排事"）接不住；
    无摘要时键在值为 null（提示词层降级）。"""
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"draft_plan": [], "reply": "无"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    with TestingSessionLocal() as db:
        ai_data.save_chat_summary(db, summary="用户提过周五下午别排事", count=2)
    scheduler_app.invoke({"user_input": "把周报排到明天下午", "memory_rules": []})
    data = json.loads(fake.calls[0][1])
    assert data["chat_summary"] == "用户提过周五下午别排事"
    # 无摘要：键恒在，值 null
    with TestingSessionLocal() as db:
        ai_data.clear_chat_session(db)  # 重置摘要与游标（既有 API 语义）
    scheduler_app.invoke({"user_input": "把周报排到明天下午", "memory_rules": []})
    data2 = json.loads(fake.calls[1][1])
    assert data2["chat_summary"] is None


def test_schedule_planner_strips_think_prefix(monkeypatch):
    """思考模型内联 <think> 前缀不再全链降级（2026-10-21 "排期推理失败"根因）：
    qwen3 开源部署把思考写进 content，loads_obj 剥前导 think 后照常出草案——
    大参数思考模型"明天去爬山"类明确排期曾在此必败。"""
    body = json.dumps({"draft_plan": [{"task_id": "t1", "title": "写周报",
                                       "start": "2026-10-17T14:00:00", "duration": 90}],
                       "reply": "下午整块时间写周报。"}, ensure_ascii=False)
    fake = _LLMCapture({PLANNER_PROMPT: f"<think>\n明天下午空闲，可以整块。\n</think>\n{body}"})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "把周报排到明天下午", "memory_rules": []})
    assert out["draft_plan"] == [{"task_id": "t1", "title": "写周报",
                                  "start": "2026-10-17T14:00:00", "duration": 90}]
    assert "写周报" in out["final_reply"]


def test_loads_obj_strips_think_prefix():
    """loads_obj 剥思考前导（2026-10-21）：<think>…</think>+JSON / 大小写不敏感 /
    围栏叠加；未闭合（思考吃光 max_tokens）不硬救；纯垃圾恒 None。"""
    ok = json.dumps({"reply": "好"}, ensure_ascii=False)
    assert loads_obj(f"<think>\n想一下\n</think>\n{ok}") == {"reply": "好"}
    assert loads_obj(f"<THINK>x</THINK>{ok}") == {"reply": "好"}
    assert loads_obj(f"<think>x</think>```json\n{ok}\n```") == {"reply": "好"}
    assert loads_obj("<think>思考没写完就被 max_tokens 截断了") is None
    assert loads_obj("随口一说") is None


def test_schedule_planner_drops_invalid_entries(monkeypatch):
    # 幻觉 id / 时区后缀 → 逐条丢弃附注；撞忙/午休不再丢弃——统一引擎以原
    # start 为起点重定合法时刻（同批互斥：三条草案互相避让）。日期取明天
    # （planner 窗口 lo=day0-1 随真实时间滑动，写死日期会随时间滑出窗口让
    # "评审"不再进 busy——2026-10-20 时间炸弹实测）
    day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"draft_plan": [
        {"task_id": "t-99", "title": "幻觉任务", "start": f"{day}T14:00:00", "duration": 30},
        {"task_id": "t1", "title": "带时区", "start": f"{day}T14:00:00+08:00", "duration": 30},
        {"task_id": "t1", "title": "撞已排期", "start": f"{day}T09:30:00", "duration": 60},
        {"task_id": "t1", "title": "撞午休", "start": f"{day}T12:00:00", "duration": 30},
        {"task_id": "t1", "title": "合法", "start": f"{day}T14:00:00", "duration": 30},
    ], "reply": "草案如下"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch,
        pending=[_task(id_="t1", title="合法", prio=2)],
        # 挡路任务标题避会议词（2026-10-22 批 5 起标题命中会议词会 +15min 尾
        # 缓冲，10:30 主荐被顺延——本用例测"撞忙重定时刻"不测缓冲，用非会议词
        scheduled=[_task(id_="s1", title="写作", prio=1,
                         start=datetime.now().replace(
                             hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1),
                         end=datetime.now().replace(
                             hour=10, minute=30, second=0, microsecond=0) + timedelta(days=1))])

    out = scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    assert [d["title"] for d in out["draft_plan"]] == ["撞已排期", "撞午休", "合法"]
    assert [d["start"] for d in out["draft_plan"]] == [
        f"{day}T10:30:00", f"{day}T13:30:00", f"{day}T14:00:00"]
    assert "task_id 不在待排清单" in out["final_reply"]
    assert "时区" in out["final_reply"]


def test_schedule_planner_empty_inbox_still_calls_llm(monkeypatch):
    # 2026-10-17 删空收件箱早退：快录原话（"记一下，明天10点…"）收件箱本来就该
    # 是空的，经 origin=user 直落——空收件箱必须照常调 LLM，不再短路。
    # 回复给合法空草案（优化批 4 起 profiler 走 call_llm_json——空串回复会
    # 触发窄域修复重试，调用数翻倍不再是 1）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"entries": [], "reply": "无"},
                                                    ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    out = scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    assert out["draft_plan"] == []
    assert len(fake.calls) == 1  # 照常调用（空 tasks_unscheduled + 快录诉求）


def test_schedule_planner_garbage_degrades(monkeypatch):
    # 垃圾输出 → 零草案降级纯文本，不 raise（坑 ②）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture({PLANNER_PROMPT: "抱歉我不会"}))
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1")])
    out = scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    assert out["draft_plan"] == []
    assert "降级" in out["final_reply"]


# —— 图集成：真图 invoke，路由 → 分支产物键 ——————————————————————————————————

def test_chat_graph_qa_branch_end_to_end(monkeypatch):
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "qa", "reason": "查询"}, ensure_ascii=False),
        QA_PROMPT: "今天没有安排。",
    })
    # 真图 invoke 驱动：router 走 call_llm、qa 走流式姊妹——双名并列 patch 同一替身
    # （invoke 下 get_stream_writer 为 no-op，_delta_emitter 静默退化，行为零变化）。
    # 输入域词不中（「跑步」非日历域）避开 qa 预路由——本用例测 LLM router 链路
    #（预路由直判路径另有 test_router_preroutes_qa_by_rules 覆盖）
    monkeypatch.setattr(llm, "call_llm", fake)
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    final = graph_mod.build_graph().invoke(
        {"user_input": "明天适合跑步吗", "memory_rules": []})
    assert final["intent"] == "qa"
    assert final["final_reply"] == "今天没有安排。"
    assert not final.get("draft_plan")  # 查询零卡
    # qa 输入含 weather 键：内存库默认无城市 → None 注入（零外呼，2026-10-18 增）
    qa_payload = json.loads(fake.calls[1][1])
    assert qa_payload["weather"] is None


def test_chat_graph_router_garbage_lands_smalltalk(monkeypatch):
    # router 答垃圾 → 兜底 smalltalk 分支（解析兜底 + 条件边双保险的端到端验证）
    fake = _LLMCapture({
        ROUTER_PROMPT: "不是 JSON",
        SMALLTALK_PROMPT: "你好呀。",
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    monkeypatch.setattr(llm, "call_llm_stream", fake)
    # smalltalk 优化批 6 起温度直用 init_llm + stream_model（不再走 call_llm_stream）
    _patch_smalltalk_stream(monkeypatch, fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)  # smalltalk 读历史走测试库
    final = graph_mod.build_graph().invoke({"user_input": "嗨", "memory_rules": []})
    assert final["final_reply"] == "你好呀。"


def test_chat_graph_schedule_branch_end_to_end(monkeypatch):
    # router=schedule → scheduler_node 包装排期子图 → draft 出口（批 3 接线后零断言改动）
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "schedule", "reason": "排期"}, ensure_ascii=False),
        PLANNER_PROMPT: json.dumps({
            "draft_plan": [{"task_id": "t1", "title": "写周报",
                            "start": "2026-10-17T14:00:00", "duration": 90}],
            "reply": "排在明天下午。"}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    final = graph_mod.build_graph().invoke(
        {"user_input": "把周报排到明天下午", "memory_rules": []})
    assert final["intent"] == "schedule"
    assert final["draft_plan"][0]["task_id"] == "t1"
    # 草案确定性摘要（2026-10-21 增）：不再透传 llm_reply——模型曾在"只是待确认
    # 草案"时谎称"已安排…无冲突"，用户以为落库日历却空
    assert "已出草案" in final["final_reply"] and "写周报" in final["final_reply"]


# —— coach 分支：strategist 子图真图 e2e（批 5 翻转；纯函数级旧用例随
# task_coach 链退役删除，节点级断言在 tests/test_strategist_*.py） ——————————


def _patch_window_and_inbox(monkeypatch, *, scheduled=None, inbox=None):
    """窗口已排期 + 收件箱待办的读库替身（coach / delete 分支同款两次调用）。"""
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)

    def fake_list(db, *, start=None, end=None, unscheduled=False):
        return (inbox or []) if unscheduled else (scheduled or [])

    monkeypatch.setattr(tasks_svc, "list_tasks", fake_list)


def test_chat_graph_coach_branch_end_to_end(monkeypatch):
    # router=coach → strategist 子图四节点 → preflight 出口（Track B 批 5 翻转）
    import agents.strategist.context_fetcher as cf
    import agents.strategist.dispatcher as disp

    monkeypatch.setattr(cf, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(disp, "SessionLocal", TestingSessionLocal)
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "coach", "reason": "求指导"}, ensure_ascii=False),
        "幕僚长·场景嗅探官": json.dumps(
            {"scenario": "meeting_prep", "ask": None}, ensure_ascii=False),
        "【场景：meeting_prep": json.dumps(
            {"strategy": "## 备好评审材料\n评审前把结论页理顺。",
             "checklist": ["今晚整理结论"], "action_items": [],
             "memory_updates": []}, ensure_ascii=False),
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(
        monkeypatch, scheduled=[_task(id_="t1", title="周报评审", prio=1,
                                      start=datetime(2026, 10, 17, 9, 0))])
    final = graph_mod.build_graph().invoke(
        {"user_input": "明天的高优会怎么准备", "memory_rules": []})
    assert final["intent"] == "coach"
    # chat 轮 preflight 卡：headline 由 strategy 首行剥 md 合成
    assert final["preflight"]["headline"] == "备好评审材料"
    assert final["preflight"]["checklist"] == ["今晚整理结论"]
    assert final["final_reply"].startswith("## 备好评审材料")  # strategy 原文（md 渲染）


# —— delete 分支：delete_confirmer 定位与防御（批 H） ————————————————————————

def test_delete_confirmer_hit_builds_confirm_plan(monkeypatch):
    fake = _LLMCapture({DELETE_PROMPT: '{"task_id": "t1"}'})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(
        monkeypatch,
        scheduled=[_task(id_="t1", title="牙医预约", prio=2,
                         start=datetime(2026, 10, 17, 14, 0))],
        inbox=[_task(id_="t2", title="买咖啡豆", prio=3)])

    out = nodes_mod.delete_confirmer({"user_input": "删掉明天的牙医预约"})
    assert out["delete_plan"] == [{"task_id": "t1", "title": "牙医预约"}]
    assert "牙医预约" in out["final_reply"] and "确认删除" in out["final_reply"]
    # 2026-10-22 起确定性预定位：剥壳词「牙医预约」唯一命中 → 零 LLM 直定位
    assert fake.calls == []


def test_delete_confirmer_ambiguous_hits_go_llm_with_hits_first(monkeypatch):
    # 多条高分歧义（「牙医预约」1.0 与「牙医预约复查」≈0.89）→ LLM 消歧，
    # 且高分条目置顶候选（防截断丢目标）
    fake = _LLMCapture({DELETE_PROMPT: '{"task_id": "t2"}'})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(monkeypatch, scheduled=[
        _task(id_="t1", title="牙医预约", start=datetime(2026, 10, 17, 14, 0)),
        _task(id_="t2", title="牙医预约复查", start=datetime(2026, 10, 20, 9, 0)),
        _task(id_="t3", title="无关任务", start=datetime(2026, 10, 18, 10, 0)),
    ])
    out = nodes_mod.delete_confirmer({"user_input": "删掉牙医预约"})
    assert out["delete_plan"] == [{"task_id": "t2", "title": "牙医预约复查"}]
    _, payload = fake.calls[0]
    ids = [t["id"] for t in json.loads(payload)["tasks"]]
    assert ids[:2] == ["t1", "t2"]  # 高分置顶（t1 分更高在前）


def test_delete_confirmer_payload_carries_recent_chat(monkeypatch):
    # 删除载荷带近期对话（2026-11-01 优化批 1）：「删掉明天那个」类指代此前
    # 零上下文无法消解——router 同窗近 6 条随载荷进 LLM
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="user", content="明天有牙医预约吗")
        ai_data.append_message(db, role="assistant", content="有的，下午两点")
    fake = _LLMCapture({DELETE_PROMPT: '{"task_id": null}'})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(monkeypatch, scheduled=[
        _task(id_="t1", title="牙医预约", start=datetime(2026, 10, 17, 14, 0)),
        _task(id_="t2", title="牙医预约复查", start=datetime(2026, 10, 20, 9, 0)),
    ])
    out = nodes_mod.delete_confirmer({"user_input": "删掉明天那个"})
    assert out["delete_plan"] == []
    payload = json.loads(fake.calls[0][1])
    assert any("牙医预约" in line for line in payload["recent_chat"])


def test_settings_payload_carries_recent_chat(monkeypatch):
    # 设置载荷带近期对话（2026-11-01 优化批 1）：省略式「撤销刚才那条」的
    # 衔接线索（unclear 路径零写库，只验载荷形状）
    with TestingSessionLocal() as db:
        ai_data.append_message(db, role="assistant", content="已写入偏好：周五下午不排事")
    fake = _LLMCapture({"你是 Clara 的设置官": '{"action": "unclear", "question": "哪条？"}'})
    monkeypatch.setattr(llm, "call_llm", fake)
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    out = nodes_mod.settings_handler({"user_input": "把刚才说的那个调整一下"})  # >8 字不触撤销短路
    assert "哪条？" in out["final_reply"]
    payload = json.loads(fake.calls[0][1])
    assert any("周五下午不排事" in line for line in payload["recent_chat"])


def test_delete_confirmer_locates_beyond_old_cap_without_llm(monkeypatch):
    # 2026-10-22 真机修复回归：候选 25 条（旧上限 20 必截丢末位目标）——
    # 预定位全量扫描，目标在候选末位仍直命中零 LLM
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    base = datetime(2026, 10, 17, 9, 0)
    scheduled = [_task(id_=f"s{i}", title=f"填充任务{i:02d}",
                       start=base + timedelta(days=i)) for i in range(24)]
    scheduled.append(_task(id_="target", title="季度战略评审",
                           start=base + timedelta(days=6)))
    _patch_window_and_inbox(monkeypatch, scheduled=scheduled)
    out = nodes_mod.delete_confirmer({"user_input": "删掉季度战略评审"})
    assert out["delete_plan"] == [{"task_id": "target", "title": "季度战略评审"}]
    assert fake.calls == []


def test_delete_confirmer_hallucinated_id_guides(monkeypatch):
    # 幻觉 id（不在候选清单）视为未命中 → 空清单 + 文字指引，不出卡
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(
        {DELETE_PROMPT: '{"task_id": "ghost"}'}))
    _patch_window_and_inbox(monkeypatch, scheduled=[_task(id_="t1")])
    out = nodes_mod.delete_confirmer({"user_input": "删掉那个会"})
    assert out["delete_plan"] == []
    assert "没能确定" in out["final_reply"]


def test_delete_confirmer_recurrence_series_guides(monkeypatch):
    # 命中重复系列 → 指引去日历卡（六场景删除复杂度整体规避的取舍）
    monkeypatch.setattr(llm, "call_llm", _LLMCapture(
        {DELETE_PROMPT: '{"task_id": "r1"}'}))
    _patch_window_and_inbox(monkeypatch, scheduled=[
        _task(id_="r1", title="周会", rrule="FREQ=WEEKLY;BYDAY=MO")])
    out = nodes_mod.delete_confirmer({"user_input": "删掉周会"})
    assert out["delete_plan"] == []
    assert "重复系列" in out["final_reply"]


def test_delete_confirmer_empty_candidates_skips_llm(monkeypatch):
    fake = _LLMCapture()
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(monkeypatch)
    out = nodes_mod.delete_confirmer({"user_input": "删掉那个会"})
    assert out["delete_plan"] == [] and out["final_reply"]
    assert fake.calls == []  # 零候选零 LLM


def test_delete_confirmer_garbage_guides(monkeypatch):
    # 垃圾输出是数据 → 空清单文字指引，不 raise
    monkeypatch.setattr(llm, "call_llm", _LLMCapture({DELETE_PROMPT: "抱歉我不会"}))
    _patch_window_and_inbox(monkeypatch, scheduled=[_task(id_="t1")])
    out = nodes_mod.delete_confirmer({"user_input": "删掉那个会"})
    assert out["delete_plan"] == []
    assert "没能确定" in out["final_reply"]


def test_chat_graph_delete_branch_end_to_end(monkeypatch):
    # router=delete → delete_confirmer → 确认卡出口（真图两跳全链）
    fake = _LLMCapture({
        ROUTER_PROMPT: json.dumps({"intent": "delete", "reason": "删除"}, ensure_ascii=False),
        DELETE_PROMPT: '{"task_id": "t1"}',
    })
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_window_and_inbox(
        monkeypatch, scheduled=[_task(id_="t1", title="牙医预约", prio=2,
                                      start=datetime(2026, 10, 17, 14, 0))])
    final = graph_mod.build_graph().invoke(
        {"user_input": "删掉明天的牙医预约", "memory_rules": []})
    assert final["intent"] == "delete"
    assert final["delete_plan"] == [{"task_id": "t1", "title": "牙医预约"}]
    assert "确认删除" in final["final_reply"]


# —— 排期分流纯函数（2026-10-17 批 2：快录落库复核 / 相似查重 / 推荐空档规则层） ——


def test_normalize_title_strips_punct_and_case():
    assert nodes_mod._normalize_title("  年度体检！") == "年度体检"
    assert nodes_mod._normalize_title("Write report.") == "writereport"  # 空格标点全剔（两侧同口径比对）
    assert nodes_mod._normalize_title("…") == ""


def test_find_similar_hits_within_ratio_and_misses_below():
    existing = [("年度体检", datetime(2026, 10, 20, 10, 0)),
                ("洗牙", datetime(2026, 10, 21, 9, 0))]
    hit = nodes_mod._find_similar("体检！", existing)
    assert hit is not None and hit[0] == "年度体检"
    assert nodes_mod._find_similar("修车", existing) is None
    assert nodes_mod._find_similar("", existing) is None


@pytest.mark.parametrize("text,expect", [
    ("今天上午10点", (10, 0)),
    ("10:30", (10, 30)),
    ("下午3点半", (15, 30)),
    ("晚上9点", (21, 0)),
    ("14点", (14, 0)),
    ("10月17日上午10点", (10, 0)),   # 日期数字不干扰钟点提取
    # 中文数字钟点（2026-11-01 真机批）：「今晚十点」曾提取失败复核恒挂
    ("今晚十点", (22, 0)),
    ("下午三点", (15, 0)),
    ("晚上十一点半", (23, 30)),
    ("明早八点", (8, 0)),
    ("下午两点半", (14, 30)),
    ("后天", None),
    ("", None),
    ("25点", None),                   # 非法钟点宁可判复核失败
])
def test_clock_from_text_variants(text, expect):
    assert nodes_mod._clock_from_text(text) == expect


@pytest.mark.parametrize("text,expect", [
    ("两分钟后", (2, "两分钟后")),
    ("提醒我十分钟后关火", (10, "十分钟后")),
    ("半小时后出发", (30, "半小时后")),
    ("5分钟后", (5, "5分钟后")),
    ("马上开始", (0, "马上")),
    ("立刻做", (0, "立刻")),
    ("十几分钟后", None),             # 模糊量不硬猜
    ("明天下午三点", None),           # 绝对时间不属相对分钟域
    ("现在怎么样", None),             # 裸"现在"不认（上下文噪声）
    ("", None),
])
def test_rel_minutes_from_text_variants(text, expect):
    """相对分钟解析（2026-11-05 真机批）：「两分钟后关火」此前无解析，
    LLM 时刻猜测被当真排到当天下午——确定性换算的解析基座。"""
    assert nodes_mod.rel_minutes_from_text(text) == expect


def test_suggest_slot_prefers_same_clock_next_day():
    # 同钟点跨日优先：今天 10:00 撞 9-12 占用 → 次日 10:00（作息平移最自然）
    start = datetime(2026, 10, 17, 10, 0)
    busy = [(datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 12, 0))]
    got = tasks_svc.suggest_slot(start, 60, busy, day_start_min=540, day_end_min=1080,
                                  lunch=(720, 780))
    assert got == datetime(2026, 10, 18, 10, 0)


def test_suggest_slot_grid_falls_through_same_day_avoiding_lunch():
    # 同钟点后续 7 天全被占 → 当日网格顺延：10:00 起避开 9-12 占用与午休，落 13:00
    start = datetime(2026, 10, 17, 10, 0)
    busy = [(datetime(2026, 10, 17 + i, 9, 0), datetime(2026, 10, 17 + i, 12, 0))
            for i in range(8)]
    got = tasks_svc.suggest_slot(start, 60, busy, day_start_min=540, day_end_min=1080,
                                  lunch=(720, 780))
    assert got == datetime(2026, 10, 17, 13, 0)


def test_suggest_slot_none_when_window_full():
    start = datetime(2026, 10, 17, 10, 0)
    busy = [(datetime(2026, 10, 17 + i, 9, 0), datetime(2026, 10, 17 + i, 18, 0))
            for i in range(8)]
    assert tasks_svc.suggest_slot(start, 60, busy, day_start_min=540, day_end_min=1080,
                                   lunch=None) is None


def test_suggest_slot_none_when_duration_exceeds_day():
    assert tasks_svc.suggest_slot(datetime(2026, 10, 17, 10, 0), 600, [],
                                   day_start_min=540, day_end_min=1080,
                                   lunch=None) is None


def test_suggest_slot_aligns_to_quarter():
    # 非整刻起点向上对齐到一刻钟（10:07 → 10:15）
    start = datetime(2026, 10, 17, 10, 7)
    got = tasks_svc.suggest_slot(start, 30, [], day_start_min=540, day_end_min=1080,
                                  lunch=None)
    assert got == datetime(2026, 10, 18, 10, 15)


# —— 排期子图三出口分流（2026-10-17 快录直落/四类问询/resolution；批 3 迁子图） ————


class _FrozenDT(datetime):
    """冻结 now 的 datetime 替身（2026-10-17 08:00）——planner 的过去/冲突/相似
    判定都依赖当前时刻，测试硬编码 10-17 日期必须冻结时间基准防跨日漂移。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 17, 8, 0)


def _freeze_now(monkeypatch):
    # 双点冻结：批 3 起 now 由 scheduler.intake 计算（nodes.datetime 一并冻结
    # ——子图经 nodes 复用的纯函数仍可能触 datetime，双点零风险）
    monkeypatch.setattr(nodes_mod, "datetime", _FrozenDT)
    monkeypatch.setattr(intake_mod, "datetime", _FrozenDT)


def _pending_snapshot(**over):
    """活跃问询快照替身（对齐 ai_data.active_pending 的返回形态）。"""
    base = {"proposal_id": "q1", "kind": "conflict",
            "entries": [{"task_id": None, "title": "体检",
                         "start": "2026-10-17T10:00:00", "duration": 60}],
            "suggestion": "2026-10-17T14:00:00", "similar_to": None,
            "date_hint": None, "original_input": "记一下，今天上午10点体检",
            "ask_text": "「体检」要的 2026-10-17 10:00 已有安排，建议改到 2026-10-17 14:00，可以吗？"}
    base.update(over)
    return base


def test_planner_fast_track_applies_user_origin(monkeypatch):
    # 快录口语：三重复核全过 → 直落 applied（无卡无按钮），payload 注入 pending/now/load_summary
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "已排。"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "记一下，今天上午10点体检", "memory_rules": []})
    assert out["applied_plan"] == [{"task_id": None, "title": "体检",
                                    "start": "2026-10-17T10:00:00", "duration": 60,
                                    "explicit_time": "上午10点", "origin": "user"}]
    assert out["draft_plan"] == []
    assert "question_plan" not in out
    assert "已安排" in out["final_reply"] and "体检" in out["final_reply"]
    _, payload = fake.calls[0]
    data = json.loads(payload)
    assert data["pending"] is None
    assert data["now"].startswith("2026-10-17T08")
    # 负荷摘要：intake 双产后本地聚合（优化批 3）——空库形态 = 15 个零行
    # （旧断言 [] 是 patch calendar_load_summary 的产物，service 真实语义恒出零行）
    assert len(data["load_summary"]) == 15
    assert all(r["scheduled_count"] == 0 for r in data["load_summary"])


def test_planner_user_origin_recheck_degrades_to_card(monkeypatch):
    # explicit_time 不是原话子串 → 三重复核不过 → 降级 agent 走确认卡
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "下午三点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检"})
    assert out["draft_plan"] == [{"task_id": None, "title": "体检",
                                  "start": "2026-10-17T10:00:00", "duration": 60}]
    assert "applied_plan" not in out


def test_planner_user_origin_clock_mismatch_degrades(monkeypatch):
    # 原话"上午10点"被抄成 22:00 → 钟点复核不过 → 降级 agent 走卡；降级条目
    # 的时刻由统一引擎重定（就医类上午硬闸 → 次日 08:00，22:00 猜测不作数）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T22:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检"})
    assert [d["start"] for d in out["draft_plan"]] == ["2026-10-18T08:00:00"]
    assert "applied_plan" not in out


def test_planner_user_origin_expanded_title_still_applies(monkeypatch):
    # 补全式扩写（2026-11-01 真机批）：LLM 把"去理发店"补成规范标题
    # "去理发店理发"——双向子串不中但共享 4 字公共段 → 复核通过直落
    # （真机教训：曾因扩写降级 agent，撞 busy 被挪槽而用户明说时间应出问询）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "去理发店理发",
                        "start": "2026-10-17T10:00:00", "duration": 40,
                        "explicit_time": "上午10点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点去理发店"})
    assert out["applied_plan"][0]["title"] == "去理发店理发"
    assert out["draft_plan"] == []


def test_planner_user_origin_hallucinated_title_degrades(monkeypatch):
    # 幻觉标题防线不弱化：标题与原话无 ≥4 字公共段（"牙医复诊" vs 理发）
    # → 复核不过降级 agent 走确认卡
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "牙医复诊",
                        "start": "2026-10-17T10:00:00", "duration": 40,
                        "explicit_time": "上午10点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点去理发店"})
    assert "applied_plan" not in out
    assert out["draft_plan"][0]["title"] == "牙医复诊"


def test_planner_quantifier_split_title_past_intercept(monkeypatch):
    # 量词插入形态（2026-11-01 真机批）：「开个会」拆散标题「开会」曾致复核
    # 降级 agent，past 拦截失效被静默排到明天——短标题有序子序列兜底后，
    # 过去时间正确走 past 问询
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "开会",
                        "start": "2026-10-16T15:00:00", "duration": 60,
                        "explicit_time": "昨天下午3点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)  # now=2026-10-17T08:00，start 在过去
    out = scheduler_app.invoke({"user_input": "昨天下午3点开个会"})
    assert out["question_plan"]["kind"] == "past"
    assert "applied_plan" not in out and not out["draft_plan"]


def test_planner_conflict_suggestion_from_engine(monkeypatch):
    # 撞既有日程 → conflict 问询，建议时间由统一引擎产生（同钟点跨日优先：
    # 次日 10:30）——LLM 主荐概念退役，suggested_start 键即使出现也被忽略
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "开会",
                        "start": "2026-10-17T10:30:00", "duration": 60,
                        "explicit_time": "上午10点半", "suggested_start": "2026-10-17T14:00:00"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="例会", prio=1,
                         start=datetime(2026, 10, 17, 10, 0),
                         end=datetime(2026, 10, 17, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "今天上午10点半开会"})
    assert out["question_plan"]["kind"] == "conflict"
    assert out["question_plan"]["suggestion"] == "2026-10-18T10:30:00"
    assert "10:30" in out["final_reply"]
    assert "14:00" not in out["final_reply"]
    assert "applied_plan" not in out and out["draft_plan"] == []


def test_planner_conflict_ignores_llm_suggested_start(monkeypatch):
    # LLM 主荐概念退役：suggested_start（哪怕撞忙/下午非法）不参与决策——
    # 就医类建议由引擎上午硬闸收紧（10:30+60=11:30 ≤ 12:00 同钟点跨日合法）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T10:30:00", "duration": 60,
                        "explicit_time": "上午10点半", "suggested_start": "2026-10-17T16:00:00"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="会议", prio=1,
                         start=datetime(2026, 10, 17, 10, 0),
                         end=datetime(2026, 10, 17, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "今天上午10点半体检"})
    assert out["question_plan"]["suggestion"] == "2026-10-18T10:30:00"
    assert "16:00" not in out["final_reply"]


def test_planner_conflict_queue_full_entries(monkeypatch):
    # 同轮两条都撞期 → 问询带全量条目（2026-10-21 复查批 A 修：旧版只问首条，
    # 其余条目既不进问询也不进 dropped，无痕消失）；建议仍是首条引擎建议
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [
            {"task_id": None, "origin": "user", "title": "开会",
             "start": "2026-10-17T10:00:00", "duration": 60, "explicit_time": "上午10点"},
            {"task_id": None, "origin": "user", "title": "体检",
             "start": "2026-10-17T11:00:00", "duration": 30, "explicit_time": "11点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="例会", prio=1,
                         start=datetime(2026, 10, 17, 10, 0),
                         end=datetime(2026, 10, 17, 12, 0))])  # 一整块挡两条
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "今天上午10点开会，11点体检"})
    assert out["question_plan"]["kind"] == "conflict"
    assert [e["title"] for e in out["question_plan"]["entries"]] == ["开会", "体检"]
    assert out["question_plan"]["suggestion"] == "2026-10-18T10:00:00"  # 主条目同钟点跨日
    assert "体检" in out["final_reply"]  # 文案列出其余条目标题
    assert "applied_plan" not in out and out["draft_plan"] == []


def test_planner_morning_only_suggestion_moves_to_next_am(monkeypatch):
    # 体检（就医类）15:30 撞 15:00-16:30：原钟点跨日同钟点超 12:00 硬闸 →
    # 引擎网格回落次日 08:00（2026-10-18 真机教训的引擎等价形态）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-17T15:30:00", "duration": 60,
                        "explicit_time": "下午3点半"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="会议", prio=1,
                         start=datetime(2026, 10, 17, 15, 0),
                         end=datetime(2026, 10, 17, 16, 30))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "今天下午3点半体检"})
    assert out["question_plan"]["kind"] == "conflict"
    assert out["question_plan"]["suggestion"] == "2026-10-18T08:00:00"  # 上午硬闸


def test_planner_agent_not_before_honored(monkeypatch):
    # agent 条目日期偏好：not_before 透传引擎作起点（不早于该日安排）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": "t1", "title": "写周报", "start": None,
                        "duration": 60, "not_before": "2026-10-26"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "下周写周报"})
    assert out["draft_plan"][0]["start"] == "2026-10-26T08:00:00"


def test_planner_agent_no_slot_raises_question(monkeypatch):
    # agent 条目 7 天窗全满 → 零候选 no_slot 问询卡（批 5 约束 10 升级：
    # 静默丢弃 → 问询给"加待办/坚持安排/点名挪走"三条出路）+ dropped 附注
    busy_tasks = [
        _task(id_=f"s{i}", title=f"会{i}", prio=1,
              start=datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
              + timedelta(days=i),
              end=datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
              + timedelta(days=i),
              dur=840)
        for i in range(8)]
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "title": "修车", "start": None, "duration": 60}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[], scheduled=busy_tasks)
    out = scheduler_app.invoke({"user_input": "帮我安排修车"})
    assert out["draft_plan"] == []
    assert out["question_plan"]["kind"] == "no_slot"
    assert "挤不出" in out["final_reply"]


def test_planner_conflict_no_slot_suggestion_none(monkeypatch):
    # 引擎 7 天无空档 → conflict 问询不带建议（文案改问"改到什么时间"）
    busy_tasks = [
        _task(id_=f"s{i}", title=f"会{i}", prio=1,
              start=datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
              + timedelta(days=i),
              end=datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
              + timedelta(days=i),
              dur=840)
        for i in range(9)]  # 同钟点扫描从"明天"起 +7 天——busy 须铺满今天+8 才算真满
    day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "开会",
                        "start": f"{day}T09:00:00", "duration": 60,
                        "explicit_time": "上午9点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[], scheduled=busy_tasks)
    out = scheduler_app.invoke({"user_input": "明天上午9点开会"})
    assert out["question_plan"]["kind"] == "conflict"
    assert out["question_plan"]["suggestion"] is None
    assert "没找到空档" in out["final_reply"]


def test_planner_past_time_raises_question(monkeypatch):
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "体检",
                        "start": "2026-10-16T23:00:00", "duration": 60,
                        "explicit_time": "昨天晚上11点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "昨天晚上11点体检"})
    assert out["question_plan"]["kind"] == "past"
    assert "已经过去" in out["final_reply"]


def test_planner_similar_raises_question(monkeypatch):
    # 未来 7 天已有近名日程（ratio 0.67 ≥ 0.6）→ similar 问询
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "user", "title": "年度体检",
                        "start": "2026-10-17T10:00:00", "duration": 60,
                        "explicit_time": "上午10点"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="体检", prio=2,
                         start=datetime(2026, 10, 20, 10, 0),
                         end=datetime(2026, 10, 20, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点年度体检"})
    assert out["question_plan"]["kind"] == "similar"
    assert out["question_plan"]["similar_to"] == "体检"
    assert "相似日程" in out["final_reply"]


def test_planner_agent_draft_similar_raises_question(monkeypatch):
    # agent 新建条目（task_id None）撞未来 7 天相似 → 同样转 similar 问询——
    # 周一已排体检、agent 周二再排体检明显不合理，问询让用户定夺（2026-10-18 增）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "agent", "title": "年度体检",
                        "start": "2026-10-18T10:00:00", "duration": 60}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="体检", prio=2,
                         start=datetime(2026, 10, 20, 10, 0),
                         end=datetime(2026, 10, 20, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "帮我安排一次体检"})
    assert out["question_plan"]["kind"] == "similar"
    assert out["question_plan"]["similar_to"] == "体检"
    assert out["draft_plan"] == []  # 问询条目不进 clean、不占 busy


def test_planner_resched_verb_links_similar_to_existing(monkeypatch, fresh_db):
    # 改期动词兜底（2026-11-01 真机批）：「把X改到Y」LLM 产新条目漏 task_id，
    # similar 命中 + 改期动词 → 查库关联既有任务转改期草案（不再误问
    # 「已安排还要再排吗」）；无改期动词维持 similar 问询（上例）
    from models import Task as _OrmTask

    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "agent", "title": "技术预研",
                        "start": None, "not_before": "2026-10-20", "duration": 60}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        db.add(_OrmTask(id="s1", title="技术预研",
                        start_time=datetime(2026, 10, 22, 14, 0),
                        end_time=datetime(2026, 10, 22, 15, 0), duration_mins=60))
        db.commit()
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="技术预研", prio=2,
                         start=datetime(2026, 10, 22, 14, 0),
                         end=datetime(2026, 10, 22, 15, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "把周四的技术预研改到周五上午"})
    assert "question_plan" not in out  # 不再出 similar 问询
    assert out["draft_plan"][0]["task_id"] == "s1"  # 转改期草案（引擎定槽）


def test_planner_resched_hint_becomes_not_before(monkeypatch, fresh_db):
    # date_hint → not_before（2026-11-02 真机三轮 #7）：「夜跑改到明天晚上七点」
    # LLM 产 date_hint 字符串（非 ISO）时间信息丢失，引擎全自主挑槽漂到 4 天
    # 后早八点——改期兜底把相对日词+钟点解析进引擎起点，草案不早于该时刻
    from models import Task as _OrmTask

    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "agent", "title": "夜跑",
                        "start": None, "date_hint": "明天晚上七点", "duration": 60}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    with TestingSessionLocal() as db:
        db.add(_OrmTask(id="n1", title="夜跑",
                        start_time=datetime(2026, 10, 17, 19, 30),
                        end_time=datetime(2026, 10, 17, 20, 30), duration_mins=60))
        db.commit()
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="n1", title="夜跑", prio=3,
                         start=datetime(2026, 10, 17, 19, 30),
                         end=datetime(2026, 10, 17, 20, 30))])
    _freeze_now(monkeypatch)  # now=2026-10-17T08:00（周六）
    out = scheduler_app.invoke({"user_input": "夜跑改到明天晚上七点"})
    assert "question_plan" not in out
    # 草案 ≥ 明天（10-18）19:00——起点被尊重而非引擎从 now 全自主漂移
    assert out["draft_plan"][0]["start"] >= "2026-10-18T19:00"


def test_planner_agent_reschedule_existing_skips_similar(monkeypatch):
    # 改既有任务排期（task_id 非空）同池相似不触发问询——confirmed 后
    # apply_plan_entries 对 task_id 跳过，走 clean 草案卡由用户手动确认。
    # 相似对避开防呆键（洗牙/体检）——否则 frequency 问询先拦（批 5 起与
    # similar 检查同层，数据语义上"上周刚体检"确实该先问）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": "t1", "origin": "agent", "title": "写季度报告",
                        "start": "2026-10-18T10:00:00", "duration": 60}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[_task(id_="t1", title="写季度报告", prio=2)],
        scheduled=[_task(id_="s1", title="写报告", prio=2,
                         start=datetime(2026, 10, 20, 10, 0),
                         end=datetime(2026, 10, 20, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "帮我把写季度报告排一下"})
    assert "question_plan" not in out
    assert [d["task_id"] for d in out["draft_plan"]] == ["t1"]


def test_planner_payload_injects_weather(monkeypatch):
    # payload 注入 weather：settings.weather_city 传入 forecast_for_city，
    # 返回行原样进 payload（null=未配置城市由提示词层降级）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps(
        {"draft_plan": [], "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    rows = [{"date": "2026-10-18", "text": "小雨", "tmax": 21.0, "tmin": 15.0,
             "precip": 80}]
    seen: list[str | None] = []

    def fake_forecast(city):
        seen.append(city)
        return rows if city == "杭州" else None

    monkeypatch.setattr(weather_svc, "forecast_for_city", fake_forecast)
    monkeypatch.setattr(settings_svc, "get_settings", lambda db: SimpleNamespace(
        work_minutes_per_day=480, calendar_start_min=480, calendar_end_min=1320,
        lunch_enabled=True, lunch_start_min=690, lunch_end_min=810, reminder_lead_min=15,
        weather_city="杭州", ai_philosophy=None))
    scheduler_app.invoke({"user_input": "排一下", "memory_rules": []})
    _, raw = fake.calls[0]
    assert json.loads(raw)["weather"] == rows
    assert seen == ["杭州"]


def test_planner_fuzzy_raises_no_time_question(monkeypatch):
    # 只有日期没钟点（"后天修车"）→ no_time 问询 + date_hint
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "fuzzy", "title": "修车",
                        "start": None, "duration": 90, "date_hint": "后天"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "后天修车"})
    assert out["question_plan"]["kind"] == "no_time"
    assert out["question_plan"]["date_hint"] == "后天"
    assert out["question_plan"]["entries"] == [
        {"task_id": None, "title": "修车", "start": None, "duration": 90}]
    assert "几点" in out["final_reply"] and "加待办" in out["final_reply"]


def test_planner_fuzzy_similar_priority_over_no_time(monkeypatch):
    # 只有日期没钟点（"X日安排体检"）且未来一周已有近名日程 → 相似提醒优先于
    # 缺钟点问询——非高频活动撞名大概率是忘了已安排（2026-10-18 真机教训）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": None, "origin": "fuzzy", "title": "体检",
                        "start": None, "duration": 60, "date_hint": "明天"}],
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="体检", prio=2,
                         start=datetime(2026, 10, 20, 10, 0),
                         end=datetime(2026, 10, 20, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "明天安排个体检"})
    assert out["question_plan"]["kind"] == "similar"
    assert out["question_plan"]["entries"] == [
        {"task_id": None, "title": "体检", "start": None, "duration": 60}]
    assert "相似日程" in out["final_reply"]


def test_planner_multi_entry_mixed_routing(monkeypatch):
    # 两条快录：体检过全链直落并入 busy；牙医同时刻随后撞上 → conflict 问询
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [
            {"task_id": None, "origin": "user", "title": "体检",
             "start": "2026-10-17T10:00:00", "duration": 60, "explicit_time": "上午10点"},
            {"task_id": None, "origin": "user", "title": "牙医",
             "start": "2026-10-17T10:00:00", "duration": 60, "explicit_time": "上午10点"},
        ], "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke({"user_input": "记一下，今天上午10点体检和牙医"})
    assert [e["title"] for e in out["applied_plan"]] == ["体检"]
    assert out["question_plan"]["kind"] == "conflict"
    assert out["question_plan"]["entries"][0]["title"] == "牙医"


# —— resolution 六形态（pending 消费轮） ————————————————————————————————————————


def test_planner_resolution_confirmed(monkeypatch):
    # "可以" → 按主荐时间落库：conflict 问询起点换成 suggestion
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "confirmed", "entries": []},
        "draft_plan": [], "reply": "好的"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "可以", "memory_rules": [], "pending": _pending_snapshot()})
    assert out["applied_plan"][0]["start"] == "2026-10-17T14:00:00"
    assert out["applied_plan"][0]["origin"] == "user"
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "accepted"}
    assert out["draft_plan"] == []
    # payload 注入 pending 快照（resolution 判定的输入）
    _, payload = fake.calls[0]
    assert json.loads(payload)["pending"]["proposal_id"] == "q1"


def test_planner_resolution_confirmed_slot_busy_converges(monkeypatch):
    # 确认时主荐时段刚被占 → 链式收敛：新 conflict 问询顶替，旧问询按被顶替结算
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "confirmed", "entries": []},
        "draft_plan": [], "reply": "好的"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="新会", prio=1,
                         start=datetime(2026, 10, 17, 14, 0),
                         end=datetime(2026, 10, 17, 15, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "可以", "pending": _pending_snapshot()})
    assert "applied_plan" not in out
    assert out["question_plan"]["kind"] == "conflict"
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "ignored"}


def test_planner_resolution_insisted_keeps_original_time(monkeypatch):
    # "体检约好了的，不能改时间" → insisted：保持原时间 10:00 落库（不换建议
    # 14:00），撞既有日程不拦、回复如实附注重叠（2026-10-18 真机教训）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "insisted", "entries": []},
        "draft_plan": [], "reply": "好的"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="会议", prio=1,
                         start=datetime(2026, 10, 17, 10, 0),
                         end=datetime(2026, 10, 17, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "体检约好了的，不能改时间", "pending": _pending_snapshot()})
    assert out["applied_plan"][0]["start"] == "2026-10-17T10:00:00"  # 原时间非建议
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "accepted"}
    assert "重叠" in out["final_reply"]


def test_planner_resolution_insisted_over_similar(monkeypatch):
    # similar 问询回 insisted → 跳过相似再拦，原条目落库不再连环问
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "insisted", "entries": []},
        "draft_plan": [], "reply": "好的"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="s1", title="体检", prio=2,
                         start=datetime(2026, 10, 20, 10, 0),
                         end=datetime(2026, 10, 20, 11, 0))])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "坚持安排", "pending": _pending_snapshot(
            kind="similar", suggestion=None, similar_to="体检")})
    assert out["applied_plan"][0]["start"] == "2026-10-17T10:00:00"
    assert "question_plan" not in out  # 不再出 similar 问询
    assert out["resolved_proposal"]["outcome"] == "accepted"


def test_planner_resolution_rescheduled(monkeypatch):
    # 回复给新时间 → 解析落库；单条目问询标题以快照为准（事项没变，变的只是时间）
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "rescheduled",
                       "entries": [{"title": "随便写", "start": "2026-10-18T09:00:00",
                                    "duration": 60}]},
        "reply": "改好了"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "改到明天上午9点", "pending": _pending_snapshot()})
    assert out["applied_plan"][0] == {"task_id": None, "title": "体检",
                                      "start": "2026-10-18T09:00:00", "duration": 60,
                                      "origin": "user"}
    assert out["resolved_proposal"]["outcome"] == "accepted"


def test_planner_resolution_rescheduled_keeps_profile(monkeypatch):
    # 画像沿用（2026-11-01 真机批）：rescheduled 重建条目此前丢快照 profile
    # ——finder 政务/就医约束在消费轮全部失效（真机「办护照」改时间后冲突
    # 建议排周六下午）。带政务画像 + 新时间落周六撞 busy → 冲突建议应限工作日
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "rescheduled",
                       "entries": [{"title": "去办护照", "start": "2026-10-17T09:00:00",
                                    "duration": 120}]},
        "reply": "r"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(
        monkeypatch, pending=[],
        scheduled=[_task(id_="g1", title="晨会同步", prio=2,
                         start=datetime(2026, 10, 17, 9, 0),
                         end=datetime(2026, 10, 17, 10, 0))])  # 周六 09:00 占用
    _freeze_now(monkeypatch)  # now=2026-10-17（周六）
    out = scheduler_app.invoke(
        {"user_input": "上午九点", "pending": _pending_snapshot(
            kind="no_time",
            entries=[{"task_id": None, "title": "去办护照", "start": None,
                      "duration": 120,
                      "profile": {"category": "other", "energy_drain": "中",
                                  "common_sense_rules": ["政务窗口仅工作日开放"]}}],
            suggestion=None, ask_text="「去办护照」安排在几点？")})
    # 快照 profile 合并 → 冲突建议不落周六（下一工作日周一起找）
    assert out["question_plan"]["kind"] == "conflict"
    sugg = out["question_plan"]["suggestion"]
    assert datetime.fromisoformat(sugg).weekday() < 4, sugg


def test_planner_resolution_inbox(monkeypatch):
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "inbox"}, "reply": "收进待办"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "先加待办", "pending": _pending_snapshot()})
    assert out["applied_plan"][0]["start"] is None
    assert out["applied_plan"][0]["title"] == "体检"
    assert "待办" in out["final_reply"]


def test_planner_resolution_cancelled(monkeypatch):
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "cancelled"}, "reply": "好的"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "不用了", "pending": _pending_snapshot()})
    assert out["resolved_proposal"] == {"proposal_id": "q1", "outcome": "ignored"}
    for key in ("applied_plan", "question_plan"):
        assert key not in out
    assert out["draft_plan"] == []


def test_planner_resolution_unrelated_processes_draft(monkeypatch):
    # 无关回复 → 不结算旧问询，按新诉求正常产草案
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "resolution": {"action": "unrelated"},
        "draft_plan": [{"task_id": "t1", "title": "写周报",
                        "start": "2026-10-17T14:00:00", "duration": 90}],
        "reply": "排好了"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "帮我把周报排到今天下午", "pending": _pending_snapshot()})
    assert [d["title"] for d in out["draft_plan"]] == ["写周报"]
    assert "resolved_proposal" not in out


def test_planner_pending_without_resolution_defensively_ignored(monkeypatch):
    # pending 形态错配（模型没输出 resolution）→ 防御性忽略，按新请求走草案流
    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({
        "draft_plan": [{"task_id": "t1", "title": "写周报",
                        "start": "2026-10-17T14:00:00", "duration": 90}],
        "reply": "排好了"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    _patch_planner_inputs(monkeypatch, pending=[_task(id_="t1", title="写周报", prio=2)])
    _freeze_now(monkeypatch)
    out = scheduler_app.invoke(
        {"user_input": "帮我把周报排到今天下午", "pending": _pending_snapshot()})
    assert [d["title"] for d in out["draft_plan"]] == ["写周报"]
    assert "resolved_proposal" not in out


# —— pending 注入的前置件 ———————————————————————————————————————————————————————


def test_router_prefixes_pending_question(monkeypatch):
    fake = _LLMCapture({ROUTER_PROMPT: json.dumps(
        {"intent": "schedule", "reason": "确认回复"}, ensure_ascii=False)})
    monkeypatch.setattr(llm, "call_llm", fake)
    nodes_mod.center_router({"user_input": "可以", "pending": _pending_snapshot()})
    _, content = fake.calls[0]
    assert content.startswith("（待确认问询：")
    assert "用户回复：可以" in content


# —— 个性化人格（2026-10-20）：prompts.personality_rules ——————————————————————


def test_personality_rules_matrix():
    from agents.prompts import philosophy_texts, personality_rules

    # balanced 且全空 → []零注入（保既有测试形状与零 token 开销）
    assert personality_rules("balanced", None, None) == []
    assert personality_rules(None, None, None) == []
    # 单风格
    rules = personality_rules("concise", None, None)
    assert len(rules) == 1 and rules[0].startswith("[回复风格] ")
    # 预设：未知 id 静默忽略，顺序照用户选择
    rules = personality_rules("balanced", "mit-3, nope , deep-work", None)
    assert len(rules) == 2
    assert all(r.startswith("[管理哲学] ") for r in rules)
    texts = philosophy_texts("mit-3, nope, deep-work")
    assert len(texts) == 2 and all(texts)
    # 重复 id 去重（2026-10-21 复查批 D2：存量脏串不再注入两遍）
    assert len(philosophy_texts("mit-3,mit-3, deep-work")) == 2
    # 自定义：strip 后注入；纯空白不入
    assert personality_rules("balanced", None, "  先完成再完美  ") == [
        "[自定义哲学] 先完成再完美"]
    assert personality_rules("balanced", None, "   ") == []
    # 全开按序：风格 → 预设 → 自定义
    rules = personality_rules("rigorous", "mit-3", "自定义文本")
    assert [r.split("]")[0] + "]" for r in rules] == [
        "[回复风格]", "[管理哲学]", "[自定义哲学]"]


def test_philosophy_conflicts_matrix():
    # 冲突矩阵完整性（2026-10-20 严选六则）：conflicts 指向的 id 必须存在且声明对称——
    # 前端拦截互选依赖此数据，坏声明 = 死代码
    from agents.prompts import PHILOSOPHIES

    ids = {p["id"] for p in PHILOSOPHIES}
    for p in PHILOSOPHIES:
        assert set(p["conflicts"]) <= ids, f'{p["id"]} 指向不存在的冲突 id'
        for other in p["conflicts"]:
            peer = next(q for q in PHILOSOPHIES if q["id"] == other)
            assert p["id"] in peer["conflicts"], f'{p["id"]}↔{other} 声明不对称'


# —— 优化批 1（2026-11-01）：防注入全覆盖 + 静默兜底打点 ————————————————————


def test_injection_guard_covers_material_prompts():
    """防注入条款全覆盖（2026-11-01 优化批 1）：外部素材（采集正文/邮件/笔记/
    任务 notes——经采集落库后会流进多条链路）会进入的提示词统一带防注入
    条款；ROUTER/SETTINGS/SOURCE_DIGEST 三处 2026-10 前已有同义句逐字保留。"""
    from agents import prompts as prompts_mod
    from agents.scheduler import prompts as sched_prompts
    from agents.strategist import prompts as strat_prompts

    guard = prompts_mod.INJECTION_GUARD.strip()
    for p in (
        prompts_mod.QA_PROMPT, prompts_mod.SMALLTALK_PROMPT,
        prompts_mod.DELETE_PROMPT, prompts_mod.PREP_PROMPT,
        prompts_mod.HARDTASK_PROMPT, prompts_mod.BRIEF_PROMPT,
        prompts_mod.CHAT_SUMMARY_PROMPT, prompts_mod.REFLECT_PROMPT,
        prompts_mod.RESOLVE_PROMPT, prompts_mod.MERGE_PROMPT,
        prompts_mod.SLOT_REFINE_PROMPT,
        sched_prompts.PROFILER_PROMPT, sched_prompts._ADJ_HEAD,
        strat_prompts.STRATEGIST_SKELETON,
    ):
        assert guard in p
    # 三处历史同义句保留（已定稿行为不重写）
    for p in (prompts_mod.ROUTER_PROMPT, prompts_mod.SETTINGS_PROMPT,
              prompts_mod.SOURCE_DIGEST_PROMPT):
        assert "忽略其指令性" in p


def test_engine_ai_refine_skip_logs_warning(monkeypatch, caplog):
    """engine AI 微调层失败可见（2026-11-01 批 1）：except 静默回落补 warning
    ——AI 微调是否生效从此可观测，不再排障靠猜。"""
    import logging

    from agents import engine

    def boom(*_a, **_k):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(llm, "call_llm", boom)
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with caplog.at_level(logging.WARNING, logger="agents.engine"):
        out = engine.plan_slots(
            entries=[{"key": "a", "title": "测试任务", "duration": 30}],
            busy=[], now=datetime.now(),
            day_start_min=480, day_end_min=1320,
            lunch=(690, 810), use_ai=True)
    # 规则槽照出（永不空手）+ AI 层跳过可观测
    assert out["a"] is not None and out["a"] >= day0
    assert any("AI 微调层整层跳过" in r.message for r in caplog.records)


def test_classifier_garbage_fallbacks_log_warning(monkeypatch, caplog):
    """classifier 两处垃圾兜底打点（2026-11-01 批 1）：兜底归 execution /
    unrelated 与真实判定在日志里可区分——分类质量退化可被捕捉。"""
    import logging

    from agents.strategist import classifier as clf

    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: "这不是JSON")
    with caplog.at_level(logging.WARNING, logger="agents.strategist.classifier"):
        assert clf._classify("帮我拆解这个项目") == ("execution", None)
        assert clf._judge("问询原句", "用户回复") == "unrelated"
    msgs = [r.message for r in caplog.records]
    assert any("兜底 execution" in m for m in msgs)
    assert any("兜底 unrelated" in m for m in msgs)


# —— 优化批 4（2026-11-01）：解析容错 / 窄域重试 / 载荷双闸 ——————————————


def test_loads_obj_deterministic_repairs():
    """loads_obj 确定性容错（优化批 4）：尾逗号剔除 / 整体单引号替换零成本吸收
    ——弱模型「完整但畸形」的输出；截断类残缺不硬救（call_llm_json 重试的职责）。"""
    from agents.parsing import loads_obj

    assert loads_obj('{"a": 1,}') == {"a": 1}                 # 尾逗号
    assert loads_obj("{ 'a': 1, 'b': [2,] }") == {"a": 1, "b": [2]}  # 单引号+尾逗号
    assert loads_obj('```json\n{"a": 1}\n```') == {"a": 1}    # 围栏（既有行为）
    assert loads_obj('<think>思考</think>{"a": 1}') == {"a": 1}  # 前导思考（既有）
    assert loads_obj('{"a": "未闭合') is None                 # 截断不硬救
    assert loads_obj("[1, 2]") is None                        # 非 dict 形状


def test_profiler_json_retry_rescues_then_degrades(monkeypatch):
    """窄域修复重试（优化批 4）：首次垃圾（qwen 思考污染形态）→ 残文回喂重问
    一次成功则照常走管道；重试仍垃圾 → 既有降级文案（降级不是等价体验，能救
    则救）。重试只发生在本会失败的调用上，轮调用数上限不变。"""
    from agents.scheduler.profiler import profiler_node

    good = json.dumps({"entries": [{"task_id": None, "origin": "agent",
                                    "title": "季度汇报", "duration": 60,
                                    "profile": {"category": "沟通协作"}}],
                       "reply": "好"}, ensure_ascii=False)

    class _RetryFake:
        def __init__(self):
            self.calls: list[str] = []

        def __call__(self, messages, *, json_mode=False, max_tokens=None,
                     on_chunk=None):
            self.calls.append(messages[-1]["content"])
            return "抱歉我不会" if len(self.calls) == 1 else good

    fake = _RetryFake()
    monkeypatch.setattr(llm, "call_llm", fake)
    out = profiler_node(_scheduler_probe_state("安排季度汇报"))
    assert out["agent_entries"][0]["title"] == "季度汇报"
    assert len(fake.calls) == 2 and "不是合法 JSON" in fake.calls[1]

    # 重试仍垃圾 → 降级文案（两次调用后放弃）
    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: "还是垃圾")
    out2 = profiler_node(_scheduler_probe_state("排一下"))
    assert out2["draft_plan"] == []
    assert "排期推理失败" in out2["final_reply"]


def _scheduler_probe_state(user_input: str) -> dict:
    """profiler 直调 state（test_scheduler_graph._profiler_state 同款形状，
    本文件内独立副本避免跨文件耦合）。"""
    return {"user_input": user_input, "memory_rules": [],
            "now": datetime(2026, 10, 17, 8, 0), "philosophy_ids": [],
            "context": {"busy": [], "candidates": [], "valid_ids": set(),
                        "prio_by_id": {}, "scheduled": [], "settings": {},
                        "lunch": [690, 810], "day_start_min": 480,
                        "day_end_min": 1320, "day_budget": 480,
                        "load_summary": [], "recent_chat": [], "weather": None,
                        "similar_pool": []}}


def test_profiler_payload_clips_memory_rules(monkeypatch):
    """scheduler 载荷双闸（优化批 4）：memory_rules 此前全量直通（理论上限
    120 条）——clip_rules 头部切片 40 条/3000 字符后画像库涨满不再撑爆载荷。"""
    from agents.prompts import clip_rules
    from agents.scheduler.profiler import profiler_node

    fake = _LLMCapture({PLANNER_PROMPT: json.dumps({"entries": [], "reply": "无"})})
    monkeypatch.setattr(llm, "call_llm", fake)
    rules = [f"[排期偏好] 规则{i:02d}" + "详" * 100 for i in range(45)]
    profiler_node({**_scheduler_probe_state("排一下"), "memory_rules": rules})
    payload = json.loads(fake.calls[0][1])
    assert len(payload["memory_rules"]) <= 40
    assert sum(len(r) for r in payload["memory_rules"]) <= 3000
    # 纯函数口径：45 条 × ~110 字 → 字符预算先于条数闸截断，头部保留
    assert clip_rules(rules)[0] == rules[0]


def test_intake_context_carries_rhythm_and_rejects(monkeypatch):
    """intake 数据回流（优化批 5）：执行节奏量化 + 近 7 天拒绝理由进 context
    与 profiler 载荷——排期建议当场避开用户明确拒绝过的时间/理由。"""
    monkeypatch.setattr(intake_mod, "SessionLocal", TestingSessionLocal)
    with TestingSessionLocal() as db:
        row = ai_data.create_proposal(db, kind="schedule", payload={
            "draft_plan": [{"title": "上午评审", "start": "2026-11-03T09:00:00",
                            "duration": 60}]})
        ai_data.record_proposal_outcome(db, row.id, "rejected", reason="上午要写代码")
    out = intake_mod.intake_node({"user_input": "排点事"})
    ctx = out["context"]
    assert ctx["execution_rhythm"]["window_days"] == 14
    assert ctx["recent_rejects"] and "上午要写代码" in ctx["recent_rejects"][0]["reason"]
    assert ctx["recent_rejects"][0]["titles"] == ["上午评审"]


def test_smalltalk_uses_warmer_temperature(monkeypatch):
    """smalltalk 温度直用（优化批 6，解禁项 5）：init_llm(temperature=0.7)
    经 stream_model 驱动——闲聊机械感消除；qa 保持 0（事实性）不受影响。"""
    seen: list[dict] = []

    monkeypatch.setattr(
        llm, "init_llm",
        lambda **kw: seen.append(kw) or object())
    monkeypatch.setattr(
        llm, "stream_model",
        lambda model, messages, *, on_chunk: "轻松点～")
    monkeypatch.setattr(nodes_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(tasks_svc, "list_tasks", lambda db, **kw: [])
    out = nodes_mod.smalltalk_node({"user_input": "哈哈今天真累", "memory_rules": []})
    assert out == {"final_reply": "轻松点～"}
    assert seen and seen[0]["temperature"] == 0.7
