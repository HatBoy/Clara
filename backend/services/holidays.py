"""中国法定节假日静态年表（2026-11-01 优化批 6）。

设计口径（docs/ai-optimization.md §六 解禁项 2——「API 年维护表不做」红线的
静态替代形态）：
- 纯静态数据零 API：法定假日日期区间高置信收录；**调休上班日不收录**
  （各年调休安排变数多，录错「明天要上班」比不说更糟）——提示词层指引
  「调休以官方为准」；用户可在每年 11 月官方发布后补 _EXTRA_WORKDAYS 两行
  （一年一更，同频率防呆键 {洗牙:180d} 先例）；
- 表外年份降级：year 超出收录范围时 holiday_name 恒 None（提示词层自然忽略，
  不猜不编）；
- 只提供「知道明天是什么日子」的智能（QA / 前夜综述 / 排期剖析），不做
  「假日禁排」硬闸——个人日历在假日排个人事务（爬山/家事）天经地义，
  政务/银行类的工作日约束排期子图已有专门硬闸。
"""

from datetime import date, timedelta

# 2026 年法定假日（国务院 2025-11 发布的放假安排，休息日区间）
_HOLIDAYS: dict[date, str] = {}
for _d, _name in (
    # 元旦：1 月 1 日至 3 日
    [(date(2026, 1, 1) + timedelta(days=i), "元旦") for i in range(3)]
    # 春节：2 月 15 日至 22 日（含除夕 2/16 前?——2/15 起连休 8 天）
    + [(date(2026, 2, 15) + timedelta(days=i), "春节") for i in range(8)]
    # 清明：4 月 4 日至 6 日
    + [(date(2026, 4, 4) + timedelta(days=i), "清明") for i in range(3)]
    # 劳动节：5 月 1 日至 5 日
    + [(date(2026, 5, 1) + timedelta(days=i), "劳动节") for i in range(5)]
    # 端午：6 月 19 日至 21 日
    + [(date(2026, 6, 19) + timedelta(days=i), "端午") for i in range(3)]
    # 中秋国庆：10 月 1 日至 8 日连休
    + [(date(2026, 10, 1) + timedelta(days=i), "中秋国庆") for i in range(8)]
):
    _HOLIDAYS[_d] = _name

# 调休上班日（周末但要上班）：留空待官方核对补全——不录不错，录错更糟
_EXTRA_WORKDAYS: dict[date, str] = {}

_COVERED_YEARS = {d.year for d in _HOLIDAYS}


def holiday_name(d: date) -> str | None:
    """该日是哪个法定假日（None=普通日或表外年份）。"""
    if d.year not in _COVERED_YEARS:
        return None
    return _HOLIDAYS.get(d)


def is_extra_workday(d: date) -> bool:
    """调休上班日（周末但需上班）。当前表空恒 False——见模块 docstring。"""
    return d in _EXTRA_WORKDAYS


def upcoming_holidays(days: int = 14, today: date | None = None) -> list[dict]:
    """未来 days 天内的假日前瞻 [{date, name, in_days}]（升序）——QA「明天要
    上班吗」与前夜综述「明日休假提醒」的数据源；空表=近段无假日。"""
    base = today or date.today()
    out: list[dict] = []
    for i in range(1, days + 1):
        d = base + timedelta(days=i)
        name = holiday_name(d)
        if name:
            out.append({"date": d.isoformat(), "name": name, "in_days": i})
    return out
