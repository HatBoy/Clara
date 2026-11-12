"""应用设置业务层：单行资源的读取与部分更新。

分层红线：router 只翻译状态码（ValueError→422），业务规则与日志在这里。
跨字段校验（时间粒度、可视跨度下限、午休 ⊆ 可视范围）在"库值 ∪ patch"的
合并态上做——只改一头时另一头取库内现值参与判定。午休越界不走报错而走
自动修复（clamp 进新可视范围；容不下则自动关闭），PATCH 返回全量真实落库值，
前端以响应回填（乐观值与修复值分叉毫秒级被纠正）。
"""

import logging

from sqlalchemy.orm import Session

from models import AppSettings
from schemas import REMINDER_LEAD_CHOICES, SettingsUpdate

logger = logging.getLogger(__name__)

# 单行 id（恒 1）
SETTINGS_ROW_ID = 1

# 可视时间范围的最小跨度（分钟）：4 小时——防窄条画布把时间轴挤得不可用
_CALENDAR_MIN_SPAN = 240

# 默认值（与 ORM 列 default 同源；get_or_create 与测试断言共用单一来源）
DEFAULTS: dict = {
    "auto_start": False,
    "close_action": "hide",
    "theme_mode": "system",
    "default_view": "day",
    "widget_transparency": 20,
    "calendar_start_min": 480,
    "calendar_end_min": 1320,
    "lunch_enabled": True,
    "lunch_start_min": 690,
    "lunch_end_min": 810,
    "work_minutes_per_day": 480,
    "reminder_lead_min": 15,
    "notify_style": "floating",
    "sound": "off",
    "ai_enabled": True,
    "morning_brief_min": 510,
    "ingest_enabled": False,
    "ingest_brief_min": 540,
    "ingest_runs_per_day": 2,
    "ingest_llm_budget": 30,
    "weather_city": None,
    # AI 个性化（2026-10-20）：balanced 默认零注入；哲学 id 串/自定义空 = 未配置
    "ai_style": "balanced",
    "ai_philosophy": None,
    "ai_philosophy_custom": None,
    # 夜间工作流报点（2026-10-20）：nightly/prep/hardtask 三流共用门槛，默认 20:00
    "nightly_start_min": 1200,
    # 四流独立开关（2026-10-21 复查批 D1）：默认全开，与 ai_enabled 正交
    "morning_enabled": True,
    "nightly_enabled": True,
    "prep_enabled": True,
    "hardtask_enabled": True,
    # 周报自动生成（2026-11-07 统计 120 分档）：周日晚随夜间报点（复用
    # nightly_start_min，不新增时刻列）；与 nightly_enabled 正交
    "weekly_report_enabled": True,
    # 对话上下文自动压缩（2026-10-26 增）：默认开维持既有行为；布尔字段仅此一处
    # （merged 按 DEFAULTS 键遍历自动纳入校验与落库，无需动 _validate_and_repair）
    "chat_autocompact_enabled": True,
    # 教练溯源深探（2026-11-01 优化批 9）：有界工具循环（至多 2 探测目标），
    # 与补采共享每源每日帽；默认关（解禁时的三重规避之一）
    "coach_probe_deep": False,
    # 助手消息轻提示音（2026-11-04 通知分轨）：默认关——AI 定时任务产物走
    # 收件箱轨（聊天框+徽标），声音是可选增强
    "assistant_chime": False,
}

# 参与粒度与跨字段校验的分钟字段（work_minutes 同走 30 粒度口径）
_MINUTE_FIELDS = (
    "calendar_start_min",
    "calendar_end_min",
    "lunch_start_min",
    "lunch_end_min",
    "work_minutes_per_day",
    "morning_brief_min",
    "ingest_brief_min",
    "nightly_start_min",
)


def get_settings(db: Session) -> AppSettings:
    """读取设置行；无则按 DEFAULTS 落行（GET 即 get_or_create，二次幂等）。"""
    row = db.get(AppSettings, SETTINGS_ROW_ID)
    if row is None:
        row = AppSettings(id=SETTINGS_ROW_ID, **DEFAULTS)
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info("初始化设置行：按默认值落库 %s", DEFAULTS)
    return row


def _validate_and_repair(merged: dict) -> None:
    """合并态校验 + 午休自动修复；非法值抛 ValueError（router 翻 422）。

    merged 会被就地修改（午休修复写回），调用方随后统一 setattr 落库。
    """
    # 30 分钟粒度（前端下拉天然 30 分钟档，直接 API 调用在这里拦）
    for key in _MINUTE_FIELDS:
        if merged[key] % 30 != 0:
            raise ValueError(f"{key} 需要 30 分钟粒度，收到 {merged[key]}")

    # 可视范围：起点 < 终点且跨度 ≥ 4 小时
    if merged["calendar_start_min"] >= merged["calendar_end_min"]:
        raise ValueError("可视范围起点必须早于终点")
    if merged["calendar_end_min"] - merged["calendar_start_min"] < _CALENDAR_MIN_SPAN:
        raise ValueError(f"可视范围跨度至少 {_CALENDAR_MIN_SPAN // 60} 小时")

    # 午休 ⊆ 可视范围：越界自动修复（clamp），clamp 后容不下 30 分钟则整体关闭
    start = max(merged["lunch_start_min"], merged["calendar_start_min"])
    end = min(merged["lunch_end_min"], merged["calendar_end_min"])
    if (start, end) != (merged["lunch_start_min"], merged["lunch_end_min"]):
        if end - start >= 30:
            logger.info(
                "午休时间越界自动修复：%d-%d → %d-%d（可视范围 %d-%d）",
                merged["lunch_start_min"], merged["lunch_end_min"], start, end,
                merged["calendar_start_min"], merged["calendar_end_min"],
            )
            merged["lunch_start_min"] = start
            merged["lunch_end_min"] = end
        else:
            logger.info(
                "可视范围 %d-%d 容不下午休，自动关闭午休建议块（原值保留）",
                merged["calendar_start_min"], merged["calendar_end_min"],
            )
            merged["lunch_enabled"] = False

    # 提醒提前量档位（None = 不提醒合法）
    lead = merged["reminder_lead_min"]
    if lead is not None and lead not in REMINDER_LEAD_CHOICES:
        raise ValueError(f"提醒提前量仅支持 {REMINDER_LEAD_CHOICES} 分钟或不提醒，收到 {lead}")

    # 采集轮数档位（前端下拉天然限档，直调 API 在此拦；2026-10-23 频率档位）
    runs = merged["ingest_runs_per_day"]
    if runs not in (1, 2, 3, 4):
        raise ValueError(f"每日采集轮数仅支持 1-4 档，收到 {runs}")


def update_settings(db: Session, patch: SettingsUpdate) -> AppSettings:
    """部分更新：exclude_unset 取数（键不出现 = 不动）→ 合并态校验/修复 → 落库。"""
    row = get_settings(db)
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return row  # 空 patch 短路：不发 UPDATE、不误抬 updated_at（照 projects 惯例）

    # 天气城市空白归一：strip 后空串 → None（停用）；前端"清空输入框保存"即停用
    if isinstance(data.get("weather_city"), str):
        data["weather_city"] = data["weather_city"].strip() or None

    # AI 个性化归一（2026-10-20；2026-10-21 复查批 D2 修正）：哲学 id 串拆段去空
    # → 保序去重（重复 id 曾导致同一哲学文本注入两遍）→ 截 6 段（预设全集现 6 则，
    # 旧上限 8 已失真）；不做 id 存在性校验（防 services→agents 反向依赖，未知 id
    # 在注入层 philosophy_texts 静默忽略）。自定义文本空白 → None（同上先例）
    if isinstance(data.get("ai_philosophy"), str):
        segs = [s.strip() for s in data["ai_philosophy"].split(",") if s.strip()]
        data["ai_philosophy"] = ",".join(dict.fromkeys(segs[:6])) or None
    if isinstance(data.get("ai_philosophy_custom"), str):
        data["ai_philosophy_custom"] = data["ai_philosophy_custom"].strip() or None

    # 合并态 = 库内现值 ∪ 本次 patch（校验看整体，落库只写本次动过的键 + 可能的午休修复）
    old_lead = row.reminder_lead_min  # setattr 前捕获（判"实际变更"用）
    old_sound = row.sound
    merged = {key: getattr(row, key) for key in DEFAULTS}
    merged.update(data)
    _validate_and_repair(merged)

    for key in data:
        setattr(row, key, merged[key])
    # 午休修复可能动了未出现在 patch 里的键，比对现值补写
    for key in ("lunch_start_min", "lunch_end_min", "lunch_enabled"):
        if getattr(row, key) != merged[key]:
            setattr(row, key, merged[key])
    # 提醒口径实际变更即清待发表：提前量变 → fire_at 平移旧口径作废；提示音变 →
    # "不提醒"档的零调度开关翻转（旧队列可能全是已无消费者的仅声音行）——清除后
    # 交由调度循环下轮扫描按新口径重算（局部导入防循环：reminders 顶层已依赖本模块）
    lead_changed = "reminder_lead_min" in data and data["reminder_lead_min"] != old_lead
    sound_changed = "sound" in data and data["sound"] != old_sound
    if lead_changed or sound_changed:
        from services.reminders import clear_pending

        removed = clear_pending(db)
        if removed:
            logger.info("提醒口径变更（提前量/提示音），清除待发提醒 %d 项", removed)
    db.commit()
    db.refresh(row)
    logger.info("更新设置：本次字段 %s", sorted(data))
    return row
