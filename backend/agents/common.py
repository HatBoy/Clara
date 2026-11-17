"""agents 共享工具面（2026-11-17 架构批 3）：子图与主图节点的公共库。

原先这些工具以下划线私有名住在 nodes.py——子图六个节点反向伸手 import
（profiler/intake/adjudicator/finder/brain/classifier），center_router 又从
子图摘 _bulk_intent/_CRISIS_RE，环靠函数内延迟 import 压着。nodes.py 事实上
是隐式标准库（删除测试：删掉它六个子图节点全断），却从未有过正式 interface。

本 module 就是那个 interface：时间文本解析（中文数字钟点/相对分钟）、标题
归一与相似查重、草案后验钳制、问询文案模板、流式 emitter 家族、排期输入
钳制常量。nodes.py 保留旧名兼容锚点（测试 monkeypatch 与内部引用零改动），
新代码一律直连本 module。不 import 任何节点文件——环从结构上消失。
"""

import difflib
import re
from collections.abc import Callable
from datetime import datetime

from langgraph.config import get_stream_writer

from agents.parsing import is_int

def delta_emitter() -> Callable[[str], None]:
    """构造 token 增量 → delta 帧的回调（qa / smalltalk 流式共用，2026-10-16 增）。

    返回的回调把每段正文增量包成 {"type": "delta", "text": ...} 交给
    langgraph 的 StreamWriter——壳（ai_coach）以 stream(stream_mode=["custom",
    "values"]) 驱动图时，custom 帧被原样转发为 SSE delta 事件（前端打字机）。

    两种退化形态都安全（langgraph 1.2.11 源码验证）：
    - 节点被直接调用（单测形态）→ get_stream_writer 抛 RuntimeError → 静默 no-op；
    - invoke() 驱动（练习脚本等）→ writer 是官方 no-op 函数，调用无副作用。
    即同一份节点代码在流式 / 非流式驱动下行为一致，仅差打字机体感。
    """
    try:
        writer = get_stream_writer()
    except Exception:
        return lambda _text: None
    return lambda text: writer({"type": "delta", "text": text})


def status_emitter() -> Callable[[str], None]:
    """构造阶段文案 → status 帧的回调（过程反馈，2026-10-26 提速批 2 增）。

    非流式分支（schedule / coach / delete / ingest）json_mode 全程零输出——
    前端只有静态「思考中…」干等，一次 LLM 卡住即整段黑屏。重节点入口推
    {"type":"status","text":...} 一帧，前端把「思考中…」替换为当前阶段文案。
    SSE additive：壳对 custom 帧全量透传（ai_coach._drain），旧前端忽略未知
    帧无破坏；远程渠道 invoke 驱动 writer 是官方 no-op 零副作用。双退化
    防御同 delta_emitter（直调 RuntimeError → no-op；invoke → no-op writer）。
    """
    try:
        writer = get_stream_writer()
    except Exception:
        return lambda _text: None
    return lambda text: writer({"type": "status", "text": text})

def task_intensity(title: str, duration_mins: int | None, priority: int | None) -> str:
    """任务强度规则映射（排期子图 intake 内联消费的唯一实现）。

    duration≥90 分钟或高优（priority=1）→ high（深度工作/关键事项）；
    duration≤15 分钟或低优（priority=3）→ low（碎片事务）；否则 medium。
    title 当前不参与（语义级标注留待 TAGGER_PROMPT 启用，见 prompts.py 注记）。
    """
    if (duration_mins or 0) >= 90 or priority == 1:
        return "high"
    if 0 < (duration_mins or 0) <= 15 or priority == 3:
        return "low"
    return "medium"

# —— 排期子图的输入钳制与防御（后验校验与 _parse_ingest 口径对齐） ——

# 待排期待办进提示词的上限（防上下文膨胀；超出的照常留在收件箱，下轮再排）
PLANNER_MAX_CANDIDATES = 20
# 已排期对照清单上限（7 天窗口内通常远少于此）
PLANNER_MAX_SCHEDULED = 30
# notes 进提示词的截断长度
PLANNER_NOTES_CHARS = 60
# 排期轮的对话历史窄窗（2026-10-21 复查批 A 定案）：刻意小于 qa/coach 的
# K=10×200 全量窗口（ai_data.CHAT_RECENT_K/CHAT_MSG_CHARS）——任务数据是排期
# 载荷大头，历史只服务"把它改到明天"类指代衔接，窄窗够用且防 8K 档输入膨胀
PLANNER_HISTORY_K = 6
PLANNER_HISTORY_CHARS = 80

def clean_draft_entry(d: dict, valid_ids: set[str], *, origin: str):
    """单条草案的基础后验（宁可不排，不可排错）——返回 (清化条目, None) 或
    (None, 拒绝原因)。

    校验链与 PROFILER_PROMPT 硬约束一一对齐（提示词先讲对，这里再
    兜底）：task_id 白名单 / naive 无时区 / duration 正整数 / title≤30。
    start 允许 None 仅限 origin=agent——具体时刻由统一排期引擎决定
    （2026-10-20 统一引擎批）；origin=user 且带非空 explicit_time 时透传
    （快录落库的审计线索）；origin=agent 的 not_before（日期偏好）一并透传，
    由排期子图换算成引擎的起点。
    """
    if not (isinstance(d, dict) and isinstance(d.get("title"), str)):
        return None, "条目形状非法"
    title = d["title"].strip()
    if not title or len(title) > 30:
        return None, "标题为空或超 30 字"
    task_id = d.get("task_id")
    if task_id is not None and task_id not in valid_ids:
        return None, "task_id 不在待排清单（幻觉）"
    start = None
    if d.get("start") is not None:
        start_raw = d.get("start")
        if not isinstance(start_raw, str):
            return None, "start 非字符串"
        try:  # naive 本地时间契约：带时区后缀（含 Z）即丢——与 _parse_ingest 一查双防
            start = datetime.fromisoformat(start_raw)
            if start.tzinfo is not None:
                return None, "start 带时区后缀"
        except ValueError:
            return None, "start 无法解析"
    elif origin != "agent":
        return None, "start 缺失（仅 agent 条目可省，时间由引擎定）"
    duration = d.get("duration")
    if not is_int(duration) or duration <= 0:
        return None, "duration 非正整数"
    entry = {"task_id": task_id, "title": title,
             "start": start.isoformat() if start else None, "duration": duration}
    if origin == "user":
        explicit = d.get("explicit_time")
        if isinstance(explicit, str) and explicit.strip():
            entry["explicit_time"] = explicit.strip()
    elif origin == "agent":
        nb = d.get("not_before")
        if isinstance(nb, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", nb.strip()):
            entry["not_before"] = nb.strip()
    return entry, None

# —— 排期分流纯函数（2026-10-17：快录自动落库 / 问询判定 / 相似查重。
#    2026-10-20 推荐空档规则层提升 services.tasks 公开实现，此处别名引用） ——

# 相似日程判定阈值：标题归一化后 difflib 相似度 ≥ 此值视为"同一件事"——
# 中文短标题（"体检" vs "年度体检"）0.6 是"重复安排"与"碰巧像"的经验分界
SIMILAR_RATIO = 0.6
# 相似查重窗口（天）：短期内近名事项大概率是重复安排（如一周内两次体检）
SIMILAR_WINDOW_DAYS = 7


def normalize_title(title: str) -> str:
    """标题归一化（相似查重前置）：去首尾空白、剔除标点符号、转小写。

    "年度体检！"与"年度体检"、"Write report."与"write report" 归一后一致——
    标点噪声不剔除会把 difflib 相似度拖到阈值下，漏掉真正的重复安排。
    """
    return re.sub(r"[\W_]+", "", title, flags=re.UNICODE).lower()


def find_similar(title: str,
                  existing: list[tuple[str, datetime | None]]) -> tuple[str, float] | None:
    """在既有日程里找相似标题（返回 (命中标题, 相似度) 或 None）。

    difflib.SequenceMatcher 对归一化串做规则层近似比对——查重目标是"用户短时间
    重复安排同一件事"，标题高度重合已是强信号，不需要嵌入模型。existing 为
    (标题, 开始时间) 二元组列表，窗口过滤（未来 SIMILAR_WINDOW_DAYS 天）由
    调用方负责；多命中取相似度最高者。
    """
    norm = normalize_title(title)
    if not norm:
        return None
    best: tuple[str, float] | None = None
    for other, _dt in existing:
        ratio = difflib.SequenceMatcher(None, norm, normalize_title(other)).ratio()
        if ratio >= SIMILAR_RATIO and (best is None or ratio > best[1]):
            best = (other, ratio)
    return best

CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_num(s: str | None) -> int | None:
    """中文数字钟点解析（一到二十几，2026-11-01 真机批增）：口语「今晚十点」
    「下午三点」的「十」「三」此前正则提不出来 → 复核恒挂降级草案。只覆盖
    钟点实用域（1-2X），不做通用中文数字转换。"""
    if not s:
        return None
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十" and s[1] in CN_DIGITS:
        return 10 + CN_DIGITS[s[1]]
    if s == "二十":
        return 20
    if len(s) == 3 and s[:2] == "二十" and s[2] in CN_DIGITS:
        return 20 + CN_DIGITS[s[2]]
    if len(s) == 1:
        return CN_DIGITS.get(s)
    return None


def clock_from_text(text: str) -> tuple[int, int] | None:
    """从用户原话时间表达式提取钟点（返回 (时, 分) 或 None）。

    覆盖常见写法："上午10点"、"10:30"、"下午3点半"、"14点"、"晚上9点"、
    "今晚十点"（中文数字，2026-11-01 真机批增）——上下文词（下午/晚上等）
    参与 12 小时制换算。这是 origin=user 落库前的第三道复核（前两道：
    explicit_time 是原话子串、title 与原话有子串关系）：提取出的钟点必须与
    条目 start 一致，防 LLM 把"上午10点"抄成 22:00 之类的语义漂移；提取失败
    也返回 None，调用方按复核不通过降级走确认卡——宁可多确认不误落库。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    m = re.search(
        r"(\d{1,2}|[一两二三四五六七八九十]{1,3})\s*[:：点时]\s*"
        r"(\d{1,2}|[一两二三四五六七八九十]{1,3}|半)?", t)
    if not m:
        return None
    g1, g2 = m.group(1), m.group(2)
    hour = int(g1) if g1.isdigit() else cn_num(g1)
    if g2 is None or g2 == "半":
        minute = 30 if g2 == "半" else 0
    elif g2.isdigit():
        minute = int(g2)
    else:
        minute = cn_num(g2) or 0
    if hour is None:
        return None
    if g2 is None and t[m.end():m.end() + 1] == "半":  # 「半」紧跟钟点后缀形态
        minute = 30
    if any(w in t for w in ("下午", "中午", "傍晚", "晚上", "今晚")) and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


REL_MIN_RE = re.compile(
    r"([0-9]{1,3}|[一两二三四五六七八九十]{1,3})\s*个?\s*(?:分|分钟)后"
    r"|(半小时后)|(马上|立刻)")


def rel_minutes_from_text(text: str) -> tuple[int, str] | None:
    """相对分钟解析（2026-11-05 真机批）：「两分钟后关火」「半小时后出发」
    「马上开始」——用户以"现在 + N 分钟"指定时刻。返回 (分钟数, 命中原话)
    或 None（0 分钟 = 现在）。只认"…分钟后"封闭形态与"马上/立刻"（裸"现在"
    上下文噪声多，误认会把非时间句当相对时刻）；「十几分钟后」类模糊量不
    硬猜返回 None。真机教训：「提醒我两分钟后关火」无此解析，LLM 时刻
    猜测被当真，排到了当天下午。"""
    if not isinstance(text, str) or not text.strip():
        return None
    m = REL_MIN_RE.search(text.strip())
    if not m:
        return None
    if m.group(2):  # 半小时后
        return 30, m.group(2)
    if m.group(3):  # 马上 / 立刻
        return 0, m.group(3)
    g = m.group(1)
    minutes = int(g) if g.isdigit() else cn_num(g)
    return (minutes, m.group(0)) if minutes is not None else None

# 问询四类（question_plan.kind 合法域；壳建 kind=question 行时的形态契约）
# frequency（频率防呆）/ no_slot（零候选）2026-10-21 批 5 增——payload 内部
# kind（proposals 表恒 kind=question，前端对 question 行整体跳过渲染，不破契约）
# strategist_clarify（幕僚追问）/ memory（记忆确认）2026-10-21 Track B 批 2 增
# ——同款内部 kind，幕僚问询无排期条目（entries 恒空，壳对这两类放行空 entries）
QUESTION_KINDS = ("conflict", "similar", "no_time", "past", "frequency", "no_slot",
                  "strategist_clarify", "memory")

def settings_payload(s) -> dict:
    """设置对象 → 排期提示词的 settings 键（排期子图与 Reduce 共用
    同一形状，2026-10-17 提取——两入口的秘书视野输入不漂移）。"""
    return {
        "work_minutes_per_day": s.work_minutes_per_day,
        "day_start": f"{s.calendar_start_min // 60:02d}:{s.calendar_start_min % 60:02d}",
        "day_end": f"{s.calendar_end_min // 60:02d}:{s.calendar_end_min % 60:02d}",
        "lunch": ([s.lunch_start_min, s.lunch_end_min] if s.lunch_enabled else None),
        # 提前提醒量（2026-10-26 批 4 补键）：分钟；新排任务的提醒由系统按此
        # 生成，重要事项的"提前准备"建议可对照它（提前量不够时值得点一句）
        "reminder_lead_min": s.reminder_lead_min,
    }

def fmt_when(dt: datetime) -> str:
    """问询文案的时刻格式（"2026-10-19 14:00"——带年份免跨年歧义，可照抄回复）。"""
    return f"{dt:%Y-%m-%d %H:%M}"


def question_text(q: dict) -> str:
    """问询文案模板（凝练口径：一句问清 + 指路两种回复方式，不罗嗦）。

    多条目时 extra 列出其余标题（2026-10-21 复查批 A 改——同轮多条撞期全量
    进问询后，只给计数不给标题，用户无从知道"另 1 件"是什么）。"""
    entries = q.get("entries") or []
    e0 = entries[0] if entries else {}
    title = e0.get("title", "这件事")
    if len(entries) > 1:
        names = "、".join(str(e.get("title") or "?") for e in entries[1:])
        extra = f"（另有 {len(entries) - 1} 件待确认：{names}）"
    else:
        extra = ""
    kind = q.get("kind")
    if kind == "conflict":
        start = e0.get("start")
        when = fmt_when(datetime.fromisoformat(start)) if start else "该时间"
        sugg = q.get("suggestion")
        clash = [str(t) for t in (q.get("clash_titles") or []) if t]
        # 撞到的既有任务直说（"已安排「写作时间」"——用户才好判断让不让位；
        # 无名回落旧文案），至多列 2 个防刷屏
        busy_txt = f"已安排「{'」「'.join(clash)}」" if clash else "已有安排"
        if sugg:
            return (f"「{title}」要的 {when} {busy_txt}，建议改到 "
                    f"{fmt_when(datetime.fromisoformat(sugg))}，可以吗？也可以直接给新时间，"
                    f"不能改就回复“坚持原时间”。{extra}")
        return f"「{title}」要的 {when} {busy_txt}，近一周没找到空档——改到什么时间？{extra}"
    if kind == "similar":
        return (f"未来一周已有相似日程「{q.get('similar_to') or '同题事项'}」，"
                f"还要再安排「{title}」吗？回复“坚持安排”或给新时间。{extra}")
    if kind == "past":
        start = e0.get("start")
        when = fmt_when(datetime.fromisoformat(start)) if start else "该时间"
        return f"「{title}」的时间 {when} 已经过去——改到什么时间？{extra}"
    if kind == "frequency":
        # 批 5 频率防呆：距上次同类太近（extra_payload 带 last=上次时刻 /
        # interval_days=建议间隔天数，文案直说）
        last = q.get("last_done")
        when = fmt_when(datetime.fromisoformat(last)) if last else "不久前"
        gap = q.get("interval_days")
        head = f"距上次（{when}）间隔不足" + (f"建议的 {gap} 天" if gap else "常规间隔")
        return (f"「{title}」{head}——确认还要再安排一次吗？"
                f"回复“确认安排”或“先不排”。{extra}")
    if kind == "no_slot":
        # 批 5 零候选：窗口/硬约束筛到没有任何可用槽（文案给两条出路：
        # 收待办 或 用户点名挪走低优先级日程——自动挪期登记技术债不做）
        return (f"未来一周的空档实在挤不出「{title}」的位置——回复“加待办”先收进"
                f"收件箱，或回复“坚持安排”忽略每日工时上限再找一次；也可以告诉"
                f"我挪走哪件低优先级的事。{extra}")
    hint = q.get("date_hint")
    head = f"「{title}」安排在{hint}几点？" if hint else f"「{title}」安排在哪天几点？"
    # 政务/银行类落周末提醒（2026-11-05 真机批）：no_time 问询本身不拦（缺
    # 钟点先问几点是正路），但窗口客观上仅工作日开放——秘书该在问时就提醒，
    # 不等用户答完时间才在直落附注里说（真机「这周六办护照」问几点零提示）
    note = f"另外提醒：{q['weekend_note']}。" if q.get("weekend_note") else ""
    return f"{head}{note}也可以回复“加待办”先收进收件箱。{extra}"
