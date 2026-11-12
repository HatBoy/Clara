"""记忆偏好 → 排期避让窗解析器（2026-10-26 提速+秘书化批 5）。

memory_rules 里「周五下午别排事 / 每天晚上不排会」这类时间偏好此前只作
文本注入提示词层——AI 可静默忽略，「周五下午别排事」之后模糊排期（"找个
时间写方案"）草案照落周五下午。批 5 起把这类偏好解析成结构化避让窗下沉
引擎硬闸（suggest_slot / is_valid_slot / candidate_slots 三函数同一份数据，
引擎/子图/直排全入口生效）。

设计约束（计划定稿）：
- 注入侧解析，不改表不扩 schema——覆盖不到的偏好形态（「工作日别排早会」
  等复杂表达）维持现状文本注入，功能无回退（宁漏勿误：解析只认高置信
  形态——星期词 + 时段词 + 避让动词三要素，动词后带限定宾语的「下午不排
  重活 / 别排太满」不产窗——那是限定描述不是全窗禁排）；
- 纯函数 + 模块级缓存（rules 元组签名 → 解析结果），规则增删下次解析自动
  失效；memory_rules 条目量 <100，正则毫秒级，缓存只是防御性免重复扫。

窗形态：{"weekday": 0-6 或 None（每天），"start_min": int, "end_min": int,
"rule": 原文}——判定口径区间重叠（槽 [t, t+duration) 与禁窗任一重叠即拒，
槽起点在窗外但跨进窗内同样拦）。
"""

import re
from datetime import datetime, timedelta

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_PERIOD_MINS: dict[str, tuple[int, int]] = {
    "上午": (0, 720), "下午": (720, 1080), "晚上": (1080, 1440),
}

# 星期可选（省略=每天）+ 时段可选（省略=全天）+ ≤6 字间隔 + 避让动词。
# 两个 alternation 分支（而非全可选组）强制星期词或时段词至少其一出现在
# 动词前的匹配内——全可选形态下懒惰间隔可吞掉时段词（「每天晚上不排」
# 从「每」起匹配成纯动词形态被闸①误拒），正则层收口比后验过滤可靠。
# 产窗条件（宁漏勿误三闸，防误报全周禁排）：
# ① 星期词或时段词至少出现一个——纯动词形态（「深度时段不排会议」这类
#    条件句，哲学人格文本）不产窗；
# ② 全句不含钟点表达（「上午 10 点前不排会」的窗是 (0,600) 而非整个上午，
#    解析粒度不到钟点级，错收比漏掉更糟）；
# ③ 动词后是句尾/标点/泛指宾语——「不排重活」「别排太满」带实义限定
#    宾语，语义非全窗禁排。
_AVOID_RE = re.compile(
    r"(?:(?:周|星期|礼拜)([一二三四五六天日])(上午|下午|晚上)?"
    r"|(上午|下午|晚上))"
    r"[^，。；;！？\s]{0,6}?"
    r"(别排|不排|留空|空着|别安排|不安排|不要排|别放|不放)")

# 复杂形态 A（2026-11-01 优化批 6）：工作日 + 时段——「工作日上午不排会」。
# 时段词必现（「工作日别排早会」无时段词，粒度不到不解析，宁漏勿误）
_AVOID_WORKDAY_RE = re.compile(
    r"(工作日|上班日)(的)?(上午|下午|晚上)"
    r"[^，。；;！？\s]{0,6}?"
    r"(别排|不排|留空|空着|别安排|不安排|不要排|别放|不放)")

# 复杂形态 B（优化批 6）：月末锚——「每月末留白」「月底别排事」。默认最后
# 2 天（30 天月的 29-30、31 天月的 30-31），时段可选（默认全天）
_MONTH_LAST_DAYS = 2
_AVOID_MONTHEND_RE = re.compile(
    r"每?个?月(底|末)(的)?(上午|下午|晚上)?"
    r"[^，。；;！？\s]{0,6}?"
    r"(别排|不排|留空|空着|别安排|不安排|不要排|别放|不留|留白)")

_CLOCK_RE = re.compile(r"[0-9一二两三四五六七八九十]+点")

_GENERIC_OBJ_RE = re.compile(r"^(?:[，。；;！？\s]|$|(?:事|会|任务|日程|安排|活动|东西))")

_CACHE: tuple[tuple[str, ...], list[dict]] | None = None


def parse_avoid_windows(rules: list[str]) -> list[dict]:
    """memory_rules 文本列表 → 避让窗列表（模块级缓存，规则变更自动失效）。

    返回共享列表调用方只读不写。示例：「周五下午别排事」→
    {"weekday": 4, "start_min": 720, "end_min": 1080, "rule": "周五下午别排事"}；
    「每天晚上不排」→ weekday None；「工作日上午不排会」→ workdays=True（批 6）；
    「每月末留白」→ month_last_days=2（批 6）；「晚上别排太满」/「深度时段不排
    会议」/「上午10点前不排会」/「工作日别排早会」（无时段词）→ 不产窗
    （见 _AVOID_RE 注释三闸）。
    """
    global _CACHE
    key = tuple(rules or [])
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]

    def _gated(m) -> bool:  # 闸②③（与基础形态同口径）
        return (_CLOCK_RE.search(raw) is None
                and bool(_GENERIC_OBJ_RE.match(raw[m.end():m.end() + 3])))

    windows: list[dict] = []
    for raw in key:
        win: dict | None = None
        # 复杂形态先行（批 6）：「工作日上午不排会」里的「上午…不排」同时命中
        # 基础形态——若基础先行会把工作日语义降级成"每天上午"，A/B 判定须在前
        m = _AVOID_WORKDAY_RE.search(raw)  # 形态 A：工作日 + 时段
        if m and _gated(m):
            lo, hi = _PERIOD_MINS[m.group(3)]
            win = {"workdays": True, "start_min": lo, "end_min": hi,
                   "rule": raw.strip()}
        if win is None:  # 形态 B：月末锚
            m = _AVOID_MONTHEND_RE.search(raw)
            if m and _gated(m):
                lo, hi = _PERIOD_MINS.get(m.group(3), (0, 1440))
                win = {"month_last_days": _MONTH_LAST_DAYS,
                       "start_min": lo, "end_min": hi, "rule": raw.strip()}
        if win is None:  # 基础形态：星期/时段 + 避让动词
            for m in _AVOID_RE.finditer(raw):  # 多处避让表达各自成窗
                if not _gated(m):
                    continue
                # 无时段词（带星期分支）= 全天
                lo, hi = _PERIOD_MINS.get(m.group(2) or m.group(3), (0, 1440))
                win = {"weekday": _WEEKDAY.get(m.group(1)),  # None=每天
                       "start_min": lo, "end_min": hi, "rule": raw.strip()}
                break  # 一条规则取首个合法表达（规范陈述一句话一窗）
        if win is not None:
            windows.append(win)
    _CACHE = (key, windows)
    return windows


def _month_last_days_hit(d) -> bool:
    """d 是否落在其所在月的最后 N 天（N=2，月末窗的日期判定小件）。"""
    import calendar

    last = calendar.monthrange(d.year, d.month)[1]
    return last - d.day < _MONTH_LAST_DAYS


def hits_window(start: datetime, duration: int, windows: list[dict]) -> dict | None:
    """槽 [start, start+duration) 是否撞任一避让窗（区间重叠判定）。

    撞上返回命中的窗（附注引用 rule 原文），未撞返回 None——route_user_entry
    直落复核（用户明给时间不拦、附注提醒）与 _gates_ok 引擎硬闸（2026-11-01
    批 6 起单源调用本函数，判定口径不再两处复制）共用。

    窗形态三类：weekday（周几或每天）/ workdays（周一至周五，批 6）/
    month_last_days（每月最后 N 天，批 6）。跨午夜顺修（批 6）：槽尾进入次日
    时，次日 weekday 的时段窗按次日侧分钟判定——周五 23:30-00:30 的槽会被
    周六上午窗拦（旧版只按起点 weekday 判漏拦）。
    """
    end = start + timedelta(minutes=duration)
    s_min = start.hour * 60 + start.minute
    cross = end.date() != start.date()
    e_min = end.hour * 60 + end.minute + (24 * 60 if cross else 0)
    for w in windows or []:
        # 起点侧适用性（窗形态三类；跨午夜槽的次日侧判定在下方独立处理）
        if w.get("workdays"):
            start_fit = start.weekday() < 5  # 周末不受工作日窗约束
        elif w.get("month_last_days") is not None:
            start_fit = _month_last_days_hit(start.date())
        else:
            start_fit = (w.get("weekday") is None
                         or start.weekday() == w["weekday"])
        if start_fit and s_min < w["end_min"] and e_min > w["start_min"]:
            return w
        # 跨午夜顺修：次日侧的时段窗按次日分钟判定（周五 23:30 的槽撞周六
        # 上午窗——旧版被起点 weekday 判定短路漏拦）
        if (cross and w.get("weekday") is not None
                and end.weekday() == w["weekday"]):
            e_next = e_min - 24 * 60
            if 0 < e_next < w["end_min"] and e_next > w["start_min"]:
                return w
    return None
