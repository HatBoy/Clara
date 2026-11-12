"""SQLAlchemy ORM 表结构（存储层）。

时间约定：全库统一存 naive 本地时间（单机桌面应用，无跨时区需求）。
带时区的入参在 schemas.py 契约层一次性归一为本地 naive，本层不关心时区。

优先级约定：库里存 INT（1=高 2=中 3=低），API 出参在 schemas.py 转 high/medium/low 字符串。
重复规则约定：recurrence_rule 存 RFC 5545 RRULE 子集字符串（如 FREQ=WEEKLY;BYDAY=MO,WE,FR），
解析在前端适配层做（src/lib/rrule.ts），后端读路径原样透传。
"""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def _new_uuid() -> str:
    """UUID 主键生成（模型层 default：seed 与未来写路径共用一处）。"""
    return str(uuid.uuid4())


class Project(Base):
    """项目表：侧栏分组实体。name 是前端展示与任务归属的引用标识（本期前端仍以 name 定位）。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 与前端 PROJECT_NAME_MAX_LEN 同源；唯一约束兜底前端"同名创建被忽略"守卫
    name: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, comment="项目名称")
    # 存 CSS 变量串（如 var(--accent)）或 hex（新建项目走十色板），前端原样消费
    color: Mapped[str] = mapped_column(String(50), nullable=False, comment="项目色点")
    # 重点项目（侧栏排序靠前的开关）
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="重点标记")
    # 稳定渲染序（侧栏与项目页列表的数组序）
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="排序序号")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="软删除标记")
    # 时间戳统一 Python 侧本地 naive（同 Task.created_at 口径）：SQLite 的
    # CURRENT_TIMESTAMP 是 UTC，混用会让同行两列恒差时区（东八区早 8 小时）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<Project id={self.id} {self.name!r}>"


class Task(Base):
    """任务表：统一任务数据源——未排期待办与已排期日程同一实体。

    start_time 有值即"已排期"（收件箱/日历的分界）；end_time 空 = 不限时
    （前端默认按一小时卡展示，见前端 end ?? start+1 约定）。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_start_time", "start_time"),  # 范围查询主谓词
        Index("ix_tasks_project_id", "project_id"),  # 按项目过滤
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 与前端 TITLE_MAX_LEN 同源（长度上限由契约层校验，SQLite 不执法 VARCHAR）
    title: Mapped[str] = mapped_column(String(30), nullable=False, comment="任务名称")
    # 待办预计耗时（分钟）：拖入日历时换算为日程时长，无则只排开始时刻
    duration_mins: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="预计耗时（分钟）")
    # 空 = 未排期（待办，显示在收件箱/项目列表）
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始时间（naive 本地）；空 = 未排期")
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间；空 = 不限时")
    # 全天/跨天日程（2026-11-06 批 B）：True 时 start_time=起始日 0 点、end_time=
    # 最后一天的次日 0 点（[start,end) 半开日期区间；end 空 = 单日）。占天不占时段：
    # duration 不变式豁免（恒空）、时长/负载口径恒 0（_task_dur 短路），
    # busy_intervals 按整天占满（出差日不宜再排时段任务）
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", comment="全天/跨天日程（start/end 为日期区间）")
    # RFC 5545 RRULE 子集：FREQ=DAILY / FREQ=WEEKLY;BYDAY=MO,WE,FR / FREQ=MONTHLY，
    # 可尾随 ;UNTIL=YYYYMMDDTHHMMSS（"删除此日程及后续"的截断）；空 = 不重复
    recurrence_rule: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="重复规则 RRULE；空 = 不重复")
    # 【已废弃 2026-09-14】单次例外改墓碑行建模（见 parent_id），本列不再读写，
    # 保留仅为避免 SQLite 删列 rebuild 表；读路径 API 的 cancelled_dates 字段改由
    # service 聚合墓碑行生成（见 services/tasks.py）
    cancelled_dates: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="已废弃：单次例外改墓碑行建模")
    # 自引用墓碑行：parent_id 指向重复系列主任务 + is_deleted=True 的影子行，
    # original_time 记录被取消实例的时刻——"仅删除本次"的落点（主记录规则不动）
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, comment="墓碑行：主任务 id")
    # 墓碑行 = 被取消实例的原时刻；将来"单次实例改期脱离系列"也复用此列
    original_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="墓碑行：被取消实例原时刻")
    # 空 = 纯收件箱（不属于任何项目）
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, comment="所属项目；空 = 无项目")
    # 1=高 2=中 3=低（API 出参转字符串，见 schemas.py）
    priority: Mapped[int] = mapped_column(Integer, default=2, server_default="2", comment="优先级：1 高 / 2 中 / 3 低")
    # 人类手写的精简备注（上限 500 由契约层管）
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    # —— 采集溯源三列（2026-10-23 增，静态溯源地基）：采集提炼的建议被采纳落库时
    # 从条目 source_ref 透传——幕僚长教练咨询该任务时按此回放素材摘录（零成本），
    # source_key 亦是受控单跳探测（回源搜更多）的路由键。手建/快录任务恒 None。
    source_key: Mapped[str | None] = mapped_column(String(60), nullable=True, comment="采集来源源键（口径同 ingest_sources）")
    source_fingerprint: Mapped[str | None] = mapped_column(String(40), nullable=True, comment="来源条目指纹 sha1")
    source_evidence: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="提炼时引用的素材摘录（截 500）")
    # 列表稳定序（收件箱"默认排序"口径 = seed/mock 数组序；UUID 主键无法复现）
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="排序序号")
    # 排期变更计数：update_task 实际变更 start_time 时 +1（夜间复盘"改期痕迹"信号源）
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="改期次数")
    # 完成时刻（2026-10-22 增；与软删正交——完成≠删除，done 任务从收件箱消失但
    # 保留在日历历史与夜间复盘里，排期 busy 避让也保留其时段）。空 = 未完成。
    # V1 不支持系列任务完成（Clone & Override 交互深，前端对 recurrence 行隐藏入口）
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成时刻；空 = 未完成")
    # 曾经拥有过 start_time（置 True 后不清回）：删除策略判定"排期取消"（高价值数据
    # 永久软删）与"草稿流产"（7 天 TTL 清理）的分界——将来"拖回收件箱"清 start_time 后仍保持 True
    ever_scheduled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="曾经排期过")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="软删除标记")
    # 软删时刻（TTL 计时锚）：场景 2（草稿流产）7 天后物理清理；墓碑行不置（非"被删除的任务"）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="软删时刻；空 = 未删")
    # default 用 Python 侧本地 naive：SQLite 的 CURRENT_TIMESTAMP 是 UTC，靠 server_default
    # 会让"闪建闪删 300 秒窗口"与 datetime.now() 比对恒差时区（东八区永不触发物理删）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, server_default=func.now(), comment="创建时间"
    )
    # onupdate 同用 Python 侧本地 naive：server 侧 CURRENT_TIMESTAMP 是 UTC，与
    # created_at 混用会让响应里两列恒差时区（东八区下 updated_at 早 8 小时）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id} {self.start_time} {self.title!r}>"


class StatsReport(Base):
    """AI 统计报告留档（2026-11-06 批 D）：统计页手动生成周报/月报的落库行。

    content 为 Markdown（LLM 总结，异常回落纯数据模板）；period_start 为周期
    起始日（周报=周一、月报=1 号），(period_type, period_start) 无唯一约束——
    同周期可反复生成，列表按 created_at 降序取最新。
    """

    __tablename__ = "stats_reports"
    __table_args__ = (
        Index("ix_stats_reports_period", "period_type", "period_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False, comment="week | month")
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="周期起始日（周一/1 号 0 点）")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="Markdown 报告正文")
    # 结构化留档（建议追踪闭环）：advice_items 本期建议清单（下期生成对照点评）/
    # health 健康度快照（五维+总分）/ keep_habits 值得保持的好习惯——旧报告 NULL
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="结构化留档：advice_items/health/keep_habits")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, server_default=func.now(), comment="生成时间"
    )


class Subtask(Base):
    """子任务清单行（2026-11-06 批 C）：会议议程/购物清单类 checklist。

    建模决策：**独立表而非 Task.parent_id 自引用**（该列已被重复系列的墓碑/活体
    例外行语义占用）或 Task 上的 JSON 列（子任务要可勾选可统计，行级实体更顺）。
    无软删列——用户删清单行 = 物理删（纯清单项无复盘价值）；父任务软删期间
    子任务保留（读路径按父任务活性过滤，孤儿不可见），purge 父任务时连带清。
    编辑语义 = 全量替换（PATCH tasks.subtasks 整组 delete+reinsert，量级无害）。
    """

    __tablename__ = "subtasks"
    __table_args__ = (
        Index("ix_subtasks_task_id", "task_id"),  # 按父任务整组读（装配进 TaskOut）
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id"), nullable=False, comment="所属任务 id"
    )
    title: Mapped[str] = mapped_column(String(30), nullable=False, comment="清单项文本")
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="勾选完成时刻；空 = 未完成")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="组内顺序")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, server_default=func.now(), comment="创建时间"
    )


class AppSettings(Base):
    """应用设置表：单行宽表（id 恒 1，首次 GET 时 get_or_create 落行）。

    时间字段（calendar_*/lunch_*）统一存"自 0 点起的分钟数"INT（30 分钟粒度），
    小时小数口径由前端适配层换算（/60，与时间轴 11.5 这类值同源）。
    auto_start 仅作偏好持久化与前端回显，真实效果源在 Rust 侧 autostart 插件
    （前端启动时读此值单向同步）；close_behavior 同理经 invoke 传给 Rust 拦截关窗。
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 开机自启动（默认关）
    auto_start: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="开机自启动")
    # hide = 隐藏到托盘 / quit = 完全退出（默认隐藏到托盘）
    close_action: Mapped[str] = mapped_column(String(10), default="hide", server_default="hide", comment="关闭主窗口行为")
    # system = 跟随系统 / light = 浅色 / dark = 深色（默认跟随系统）
    theme_mode: Mapped[str] = mapped_column(String(10), default="system", server_default="system", comment="外观模式")
    # day = 日 / week = 周 / month = 月（默认日；应用启动时日历打开的视图）
    default_view: Mapped[str] = mapped_column(String(10), default="day", server_default="day", comment="启动默认视图")
    # 桌面小部件透明度（%，默认 20；越大越透——tint alpha = 1 - t/100，20 即 0.80）
    widget_transparency: Mapped[int] = mapped_column(Integer, default=20, server_default="20", comment="小部件透明度（%），越大越透")
    # 日历可视时间范围（默认 08:00–22:00）
    calendar_start_min: Mapped[int] = mapped_column(Integer, default=480, server_default="480", comment="可视范围起点（分钟）")
    calendar_end_min: Mapped[int] = mapped_column(Integer, default=1320, server_default="1320", comment="可视范围终点（分钟）")
    # 午休时间范围（默认 11:30–13:30，可关闭不显示）
    lunch_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="午休建议块开关")
    lunch_start_min: Mapped[int] = mapped_column(Integer, default=690, server_default="690", comment="午休起点（分钟）")
    lunch_end_min: Mapped[int] = mapped_column(Integer, default=810, server_default="810", comment="午休终点（分钟）")
    # 每日工作时长（分钟，默认 8h）——负载指示器/负载灯的分母
    work_minutes_per_day: Mapped[int] = mapped_column(Integer, default=480, server_default="480", comment="每日工作时长（分钟）")
    # 日程提醒提前量（分钟）：None = 不提醒；0 = 准时；5/10/15/30 = 提前（默认 15）
    reminder_lead_min: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="提醒提前量（分钟）；空 = 不提醒")
    # floating = 居中悬浮框 / system = 系统通知（默认居中悬浮框）
    notify_style: Mapped[str] = mapped_column(String(10), default="floating", server_default="floating", comment="通知形式")
    # chime = 铃声 / bowl = 颂钵 / water = 水滴 / off = 关闭（默认关闭；素材在 public/*.mp3）
    sound: Mapped[str] = mapped_column(String(10), default="off", server_default="off", comment="提示音")
    # AI 功能主开关（2026-09-20 增；默认开——关闭时仅规则层运行：负荷/熔断/碎片聚合
    # 照常，对话/排期建议/夜间学习等 LLM 调用全静默，见 PRD §9.2 降级矩阵）
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="AI 功能主开关")
    # 晨间对齐触发时刻（2026-09-20 增；自 0 点起的分钟数，默认 510=08:30，30 分钟粒度）
    morning_brief_min: Mapped[int] = mapped_column(Integer, default=510, server_default="510", comment="晨间对齐时刻（分钟）")
    # 数据采集提炼开关（2026-10-14 增；默认关——采集会打外网 API，用户显式开启）
    ingest_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="每日数据采集开关")
    # 数据采集触发时刻（2026-10-14 增；自 0 点起的分钟数，默认 540=09:00，30 分钟粒度）
    ingest_brief_min: Mapped[int] = mapped_column(Integer, default=540, server_default="540", comment="数据采集时刻（分钟）")
    # 每日采集轮数（2026-10-23 增；1-4 档，默认 2）——首轮在 ingest_brief_min，后续
    # 槽位当天顺延（偏移见 workflows._INGEST_SLOT_OFFSETS）；无新数据无异常的轮次
    # 静默执行不落消息不广播（agents.graph.run_ingest silent 判定）
    ingest_runs_per_day: Mapped[int] = mapped_column(Integer, default=2, server_default="2", comment="每日采集轮数（1-4）")
    # 采集建议自动收录（2026-10-14 增，M2-③；仅 inbox 卡自动落收件箱——
    # schedule 草案永不自动落库。开启资格 = 全局采纳率 ≥60% 且样本 ≥10（settings 校验））
    # 单日采集链 LLM 调用上限（2026-10-14 增，§十-3；call_llm 不回传 usage → 以调用
    # 次数为预算代理；0 = 不限。运行态计数在 data_ingest.claim_llm_budget 模块态）
    ingest_llm_budget: Mapped[int] = mapped_column(Integer, default=30, server_default="30", comment="单日采集 LLM 调用上限；0=不限")
    # 排期天气对照城市（2026-10-18 增；中文城市名，Open-Meteo geocoding 解析坐标；
    # None = 未配置 → 排期/问答跳过天气注入，见 services/weather.py）
    weather_city: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="天气对照城市；空=停用")
    # AI 回复风格（2026-10-20 增）：balanced 均衡 / concise 简洁 / detailed 详细 / rigorous 严谨
    ai_style: Mapped[str] = mapped_column(String(10), default="balanced", server_default="balanced", comment="AI 回复风格")
    # 内置管理哲学预设（2026-10-20 增；逗号分隔预设 id，空 = 未启用）
    ai_philosophy: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="内置哲学预设 id 逗号串")
    # 自定义哲学文本（2026-10-20 增；与预设叠加注入，空 = 未配置）
    ai_philosophy_custom: Mapped[str | None] = mapped_column(Text, nullable=True, comment="自定义哲学文本")
    # 夜间工作流报点时刻（2026-10-20 增；自 0 点起的分钟数，默认 1200=20:00，
    # 30 分钟粒度）：夜间整理/会前准备/高难协助三流共用报点门槛（前两者无空闲条件）
    nightly_start_min: Mapped[int] = mapped_column(Integer, default=1200, server_default="1200", comment="夜间工作流报点时刻（分钟）")
    # 四流独立开关（2026-10-21 增，复查批 D1；默认全开——与 ai_enabled 正交：
    # 各开关只管自己这一个流，关 = 永不认领不落 workflow_runs 行，同 ingest_enabled 先例）
    morning_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="晨间对齐开关")
    nightly_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="夜间整理开关")
    prep_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="会前准备开关")
    hardtask_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="高难协助开关")
    # 周报自动生成开关（2026-11-07 统计 120 分档）：周日晚随夜间报点生成周报并
    # 推送；与 nightly_enabled 正交（报告生成不依赖夜间整理跑不跑），同四开关语义
    weekly_report_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="周报自动生成开关（周日晚随夜间报点）")
    # 会后纪要邀请开关（2026-11-10 批 3，默认开）：重要会议结束 15~120 分钟内
    # 出邀请卡询问补记纪要（每日至多 1 次）；关 = 永不认领，同四开关语义
    minutes_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="会后纪要邀请开关")
    # 对话上下文自动压缩开关（2026-10-26 增）：关 = 轮末后台压缩早退（摘要不再
    # 自动更新，仅 /compact 手动指令可用）；与 ai_enabled 正交同四开关
    chat_autocompact_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="对话上下文自动压缩开关")
    # 教练溯源深探（2026-11-01 优化批 9，解禁 1 有界工具循环）：开=相关任务的
    # 回源探测从首命中扩到至多 2 个目标（每源每日帽与既有补采共享）；默认关
    coach_probe_deep: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="教练溯源深探开关（默认关）")
    # 助手消息轻提示音（2026-11-04 通知分轨）：开 = AI 定时任务产物（晨报/夜报/
    # 过载/简报等）落聊天框时用所选提示音轻响一次；默认关——通知分轨后 AI 产物
    # 走收件箱轨（聊天框+徽标），声音是可选增强不是默认
    assistant_chime: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="助手消息轻提示音（默认关）")
    # 时间戳统一 Python 侧本地 naive（同 Task/Project 口径，避免 SQLite CURRENT_TIMESTAMP 的 UTC 恒差时区）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<AppSettings id={self.id}>"


class Reminder(Base):
    """日程提醒队列表（2026-09-15 增）：一行 = 一次待触发的提醒。

    只存"待发"——触发即删行（DELETE 认领，rowcount=1 才广播：原子防双发，
    表无增长无 sent 标记）；(task_id, fire_at) 唯一约束让调度循环重叠窗口的
    反复扫描幂等入列。提醒提前量设置变更时未发行整体清除（fire_at 口径已变，
    交由下轮扫描按新口径重算，见 services/settings.update_settings）。
    """

    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, comment="任务 id（重复系列指向主行）")
    fire_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="计划触发时刻 = 实例开始 − 提前量")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="入列时刻")

    __table_args__ = (
        UniqueConstraint("task_id", "fire_at", name="uq_reminders_task_fire"),
    )

    def __repr__(self) -> str:
        return f"<Reminder task={self.task_id} fire_at={self.fire_at}>"


class ModelProvider(Base):
    """模型接入表（2026-09-16 增）：用户接入的大模型服务配置（经 LiteLLM 统一协议调用）。

    多行资源但全局至多一行 is_active=True（激活模型唯一，经 /activate 端点同事务
    互斥切换）。硬删（有意偏离 projects 软删惯例）：非业务数据无 AI 复盘价值、
    含 api_key 不宜留墓碑；删除激活行后库处于零激活态（不级联），阶段二 agent
    需处理"无激活模型"并提示用户去设置页激活。
    """

    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 显示名称（前端 api_key 等技术字段之外的人读标识），唯一约束拦重名
    name: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, comment="显示名称（唯一）")
    # 可选自定义端点：空 = LiteLLM 按 provider/ 前缀官方路由；填 = 中转站/本地端点（OpenAI 兼容须含 /v1）
    base_url: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="可选自定义端点；空 = LiteLLM 官方路由")
    # LiteLLM 模型标识，须带 provider/ 前缀（如 deepseek/deepseek-chat、openai/gpt-4o、ollama/qwen3）
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="LiteLLM 模型标识（provider/模型名）")
    # 空 = 本地免鉴权服务（如 Ollama），探测与将来推理请求均不带 Authorization
    api_key: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="API 密钥；空 = 本地免鉴权服务")
    # 深度思考模式开关（调用时映射 litellm reasoning_effort 统一思考参数；阶段二消费，探测不带）
    thinking_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="深度思考模式开关（映射 reasoning_effort）")
    # 上下文长度（token 数，档位枚举在契约层 ContextLength）
    context_length: Mapped[int] = mapped_column(Integer, default=32768, server_default="32768", comment="上下文长度（token 数）")
    # 激活中（全局至多一行 True；写入口仅 services.activate_model 同事务互斥）
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="激活中（全局至多一行 True）")
    # 轻量助手模型槽（2026-11-01 优化批 9，解禁 7）：小调用白名单（路由/分类/
    # 判定/反思/采集摘要）走此行——本地小模型可亚秒级；全局至多一行 True，未
    # 配置时 init_llm 回落激活模型（行为不变）。与 is_active 正交（可同为 True）
    is_aux: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="轻量助手模型（未配置回落激活模型）")
    # 上次连通性测试结果：True=在线 / False=离线 / None=从未测试（卡片徽标口径）
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次连通性测试结果；空 = 从未测试")
    last_test_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次测试往返毫秒")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次测试时刻（naive 本地）")
    # 时间戳统一 Python 侧本地 naive（同 Project/Task 口径）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<ModelProvider id={self.id} {self.name!r}>"


class IntegrationSettings(Base):
    """数据接入配置表（2026-09-18 增）：单行宽表（id 恒 1，首次 GET 时 get_or_create 落行）。

    存放第三方集成凭证与验证状态。与 app_settings 分表：凭证域与通用偏好域隔离，
    不混入 /api/settings 契约；将来 MCP 接入配置同表加列（宽表先例同 app_settings）。
    api_key 明文存库 + 原样回显：本地单用户桌面应用（同 ModelProvider.api_key 口径）。
    已连接应用列表不落库——真源在 Composio 云端实时拉取（见 services/integrations.py）。
    """

    __tablename__ = "integration_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Composio 云端 API Key（控制台 Settings → Project Settings → API Keys 申请）；空 = 未配置
    composio_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="Composio API Key；空 = 未配置")
    # 上次验证结果：True=有效 / False=无效 / None=从未验证（配置行徽标口径）
    composio_key_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次 Key 验证结果；空 = 从未验证")
    # 上次验证失败的中文摘要；验证成功置空（比 ModelProvider 多存一列：重开设置页仍能看到失败原因）
    composio_key_error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="上次验证失败摘要；成功置空")
    composio_key_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次验证时刻（naive 本地）")
    # 内置本地文件夹 MCP 服务的根路径（2026-09-18 增；**已废弃**——同日扩多目录改 local_folder_paths，
    # 旧值经一次性迁移转列表后恒 NULL，列保留只因 SQLite 删列需 rebuild 表）
    local_folder_path: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="已废弃：多目录改 local_folder_paths")
    # 本地文件夹服务开关（2026-09-18 增）：True = 工具可读已配置文件夹（挂载恒存在，开关在工具调用层判定；
    # 默认关闭，配置后经卡上「启用」按钮手动开启）
    local_folder_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="本地文件夹服务开关")
    # 多目录根路径列表（2026-09-18 同日扩多目录）：JSON 数组，最多 10 项，每项须绝对路径且目录存在；
    # 旧单路径列 local_folder_path 已废弃（保留列不删——SQLite 删列需 rebuild 表，同 cancelled_dates 先例），
    # 旧值由 database._migrate_local_folder_paths 一次性转为列表后置 NULL
    local_folder_paths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="本地文件夹根路径列表；空/空数组 = 未配置")
    # 上次卡上「测试」结果（2026-09-20 增，持久化展示在卡标题旁徽标）：True=有效 /
    # False=无效 / None=从未测试（或目录变更后清空——旧计数对新配置是谎言）
    local_folder_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次文件夹测试结果；空 = 从未测试")
    local_folder_test_files: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次测试文件计数（无效时为空）")
    # 时间戳统一 Python 侧本地 naive（同 AppSettings/ModelProvider 口径）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<IntegrationSettings id={self.id}>"


class McpServer(Base):
    """MCP 服务接入表（2026-09-18 增）：用户接入的 MCP 服务端配置（经
    langchain-mcp-adapters 统一协议调用，stdio / streamable_http / sse 三种传输）。

    精选应用（钉钉/Obsidian/滴答国内版；飞书 MCP 已于 2026-10-16 全链路移除，
    接入走飞书 CLI）与手动添加无本质区别——前端模板
    预填配置、用户只填凭据，落库与通用行同形（后端零精选知识）。硬删（有意
    偏离 projects 软删惯例，同 ModelProvider 理由）：env/headers 含凭据不宜
    留墓碑、非业务数据无 AI 复盘价值。enabled=False 仅"阶段二不装载"，列表
    仍显示、连通性测试仍可用（测试与启用正交）。JSON 列只整体替换（setattr），
    禁止就地改（SQLAlchemy 变更检测不追踪原地修改）。
    """

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 显示名称，唯一约束拦重名（409）
    name: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, comment="显示名称（唯一）")
    # 用户自定义服务描述（选填，卡片描述位优先展示；空 = 回落技术串）
    description: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="服务描述（选填，≤100 字）")
    # 传输方式：stdio / streamable_http / sse（枚举在契约层 McpTransport 拦）
    transport: Mapped[str] = mapped_column(String(20), nullable=False, comment="传输方式")
    # stdio 启动命令（如 npx / uvx / python）；空 = 非 stdio
    command: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="stdio 启动命令；空 = 非 stdio")
    # stdio 命令参数（每项一个参数，如 ["-y", "dingtalk-mcp@latest"]）
    args: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="stdio 命令参数")
    # stdio 环境变量（与父进程环境合并而非替换；含敏感凭据）
    env: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True, comment="stdio 环境变量（含凭据）")
    # http/sse 服务端点；空 = stdio
    url: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="http/sse 端点；空 = stdio")
    # http/sse 请求头（含认证凭据，如 Authorization: Bearer xxx）
    headers: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True, comment="http/sse 请求头（含凭据）")
    # 装载开关：阶段二 agent 构造 MultiServerMCPClient 时只取 enabled 行
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="装载开关")
    # 上次连通性测试结果：True=在线 / False=离线 / None=从未测试（卡片徽标口径）
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次连通性测试结果；空 = 从未测试")
    last_test_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次测试往返毫秒")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次测试时刻（naive 本地）")
    # 上次测试列出的工具数（2026-09-20 增，卡标题旁徽标"连通 · Nms · N 个工具"）；失败时为空
    last_test_tools: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次测试工具数；失败为空")
    # 时间戳统一 Python 侧本地 naive（同 ModelProvider 口径）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<McpServer id={self.id} {self.name!r} {self.transport}>"


class CliTool(Base):
    """CLI 工具接入表（2026-10-15 增，第四源 kind="cli"）：一行 = 一个已接入的
    CLI 数据源。凭据恒存 CLI 自己的 OS 钥匙串（由 CLI 的 auth 命令管理），本表
    零凭据——与 Composio/MCP 的本质差异。精选适配器声明在后端注册表
    services/cli_tools.py 的 ADAPTERS，本表只存接入状态与探测结果（精选行显示名
    以 spec 为准不落库，单一事实源）。

    adapter_id 直接作主键：精选一适配器至多一行；窄口自定义行用合成键
    "custom:<uuid>"（custom_bin/custom_argv 两列必填，readonly 白名单恒空 =
    不进补采）。source_key 口径 "cli:<adapter_id>"。探测列与 McpServer.last_test_*
    同语义（None = 从未）；auth_* 对无程序化授权探测的 CLI 恒 None。硬删（同
    McpServer 理由）；删除不清 CLI 侧凭据——本机重新接入免再授权。"""

    __tablename__ = "cli_tools"

    # 精选 = 注册表键（lark/dingtalk/todoist）；自定义 = "custom:<uuid>"
    adapter_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # 自定义行显示名（精选行空——显示名取 spec）
    name: Mapped[str | None] = mapped_column(String(30), nullable=True, comment="自定义行显示名；精选行空")
    # 自定义行专属：可执行文件路径 + 固定只读采集命令（argv 数组，占位仅 {since}/{until}）
    custom_bin: Mapped[str | None] = mapped_column(String(300), nullable=True, comment="自定义行：可执行文件路径")
    custom_argv: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="自定义行：只读采集命令 argv")
    # 装载开关：False = 不参与采集（卡片「停用」联动镜像 ingest_sources.enabled）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="装载开关")
    # 上次安装探测：可执行文件存在且版本命令可跑（None = 从未探测）
    bin_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次安装探测；空 = 从未")
    bin_version: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="探测到的版本")
    bin_path: Mapped[str | None] = mapped_column(String(300), nullable=True, comment="探测到的启动器路径（展示用，执行时重探）")
    bin_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次安装探测时刻（naive 本地）")
    # 上次授权探测（CLI 自身命令；无程序化探测的 CLI 恒空）
    auth_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次授权探测；空 = 从未或不适用")
    auth_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次授权探测时刻")
    # 上次「测试」按钮结果（自定义行专用）：渲染近 24h 时间窗真跑一遍采集命令，
    # ok + stdout 启发式条目数。编辑配置后置空（旧结果对旧命令成立，是谎言）
    test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次测试结果；空 = 从未测试")
    test_items: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次测试返回条目数")
    test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次测试时刻（naive 本地）")
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="上次探测/测试/执行的错误摘要")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<CliTool {self.adapter_id!r} enabled={self.enabled}>"


class ChatChannel(Base):
    """聊天通讯渠道表（2026-10-23 增，「设置 → 聊天通讯」）：一行 = 一个接入的
    通讯应用机器人，用户在手机飞书/钉钉单聊窗口与日程助手对话（消息走与桌面端
    相同的聊天管道，agents 逻辑零改动——docs/chat-comm.md）。

    provider 唯一键：每渠道至多一行；wechat 无官方机器人 API 恒无行（「建设中」
    占位态在 service 目录层表达，不落库）。凭据是开放平台应用级 app_id/app_secret
    （认证形态为「应用凭据 + 长连接」，非扫码 OAuth），明文存库——ModelProvider
    先例：本地单用户库无多租户面。探测三列语义同 CliTool（None = 从未）。硬删：
    无业务数据引用，删除即断开（桌面对话历史独立于渠道行存活）。"""

    __tablename__ = "chat_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # feishu / dingtalk（wechat 恒无行）
    provider: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, comment="渠道标识")
    # 开放平台应用 ID（飞书 App ID / 钉钉 Client ID）
    app_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="开放平台应用 ID")
    app_secret: Mapped[str] = mapped_column(String(200), nullable=False, comment="应用密钥（明文，本地单用户定调）")
    # worker 装载开关：False = 不监听该渠道消息（卡片「停用」）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="worker 装载开关")
    # 定时任务推送开关（2026-10-23 二批）：True = 晨报/会前准备等主动推送也发
    # 该渠道（push_target 有值才真发——由 worker 收到用户消息时缓存）
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="定时任务推送开关")
    # 推送目标 JSON（feishu open_id / dingtalk staff_id / wecom userid / wechat user_id）
    push_target: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="推送目标 JSON")
    # 上次凭据探测（换取平台 access token；None = 从未）
    conn_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="上次凭据探测；空 = 从未")
    conn_error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="上次探测错误摘要")
    conn_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次探测时刻（naive 本地）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<ChatChannel {self.provider!r} enabled={self.enabled}>"


class Proposal(Base):
    """建议日志表（2026-09-20 增，阶段二）：一行 = 一次 AI 结构化建议（排期草案 /
    会前准备清单等，kind 区分）。记忆闭环的输入源：outcome 回写既是习惯学习官的学习
    信号，也是采纳率度量衡（信任渐进判定，见 services/ai_data.adoption_stats）。
    payload 存建议快照 JSON（draft_plan 条目 / checklist 等），结构由 ai_coach 壳
    与前端约定（docs/ai-agents.md 接缝节），本层不解释只透传。JSON 列只整体替换
    （setattr），禁止就地改（同 McpServer.env 惯例）。
    """

    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # schedule = 排期草案（幽灵方块）/ preflight = 会前准备清单 / energy_tag = 强度标注
    kind: Mapped[str] = mapped_column(String(20), nullable=False, comment="建议类型")
    # 建议内容快照（含 draft_plan / checklist / reject_reason 等），只透传不解释
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, comment="建议内容 JSON")
    # accepted / modified / rejected / ignored / expired；空 = 待处理
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="处理结果；空 = 待处理")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时刻")
    # 回写 outcome 的时刻（待处理恒空）
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="处理时刻；空 = 未处理")

    def __repr__(self) -> str:
        return f"<Proposal id={self.id} {self.kind} {self.outcome!r}>"


class Message(Base):
    """会话消息表（2026-09-20 增，阶段二）：助手面板对话流持久化——助手必须有记性，
    跨视图切换与重启存活。单线会话（单机单用户单面板，P0 不分多会话）。
    proposal_id 非空 = 该条 assistant 消息关联的建议卡片（前端据此在气泡下渲染
    卡片态）。
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # user / assistant
    role: Mapped[str] = mapped_column(String(10), nullable=False, comment="角色：user / assistant")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息正文")
    # 关联建议卡片；空 = 纯文本消息
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"), nullable=True, comment="关联建议 id；空 = 无")
    # 消息来源标记（2026-10-23 聊天通讯增）：None=本地桌面端 / feishu / dingtalk
    # ——仅展示层徽标，统一上下文不区分渠道
    source: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="来源渠道；空 = 桌面端")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时刻")

    def __repr__(self) -> str:
        return f"<Message id={self.id} {self.role}>"


class ChatState(Base):
    """对话滚动上下文状态表（2026-10-20 增）：单例行 id 恒 1，存滚动摘要与压缩游标。

    messages 表 append-only 永不删（前端渲染需要全量）——上下文有界化靠三层：
    近期原文 K 条（chat_context 现查）+ 滚动摘要（本表 summary）+ 长期画像
    （memory_rules）。summarized_count 是压缩游标 = 已并入摘要的消息条数；
    消息只增不改，count 位置稳定（将来"清空会话"须同步重置本行，见 tech-debt）。
    """

    __tablename__ = "chat_state"

    # 单例行：主键恒 1，get_or_create 语义
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=lambda: 1)
    # 滚动摘要（第三人称压缩叙述）；空/None = 尚未压缩过
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="滚动摘要；空 = 未压缩")
    # 已并入摘要的消息条数（压缩游标，append-only 前提下位置稳定）
    summarized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已摘要消息数")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时刻")

    def __repr__(self) -> str:
        return f"<ChatState summarized={self.summarized_count}>"


class MemoryRule(Base):
    """记忆画像表（2026-09-20 增，阶段二）：一行 = 一条用户长期偏好规则
    （SQLite 纯文本分类记忆，零向量库，见 PRD §4）。

    读取注入 = services 层按注入序取出后纯文本进 System Prompt（2K token 预算与
    截断在手写区 prompts.py）。source=user_manual 注入优先级最高（用户亲笔）；
    ai_reflect 行必须带 evidence（白盒信任基石——无出处的画像不落库）。
    总量硬上限 120 条由 service 层守（满则淘汰低置信旧规则）。
    """

    __tablename__ = "memory_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 分类（开会习惯、作息规律、精力节律等，自由文本）
    category: Mapped[str] = mapped_column(String(30), nullable=False, comment="规则分类")
    # 规则正文（注入 System Prompt 的内容）
    content: Mapped[str] = mapped_column(String(200), nullable=False, comment="规则正文")
    # 固定槽位（2026-11-03 记忆重构 Tier 1）：rhythm 生理节律 / meeting 会议沟通 /
    # boundary 红线边界 / other 其他——由 slot_of(category, content) 纯规则映射落值，
    # 用于双段注入的分组序与设置页分组展示；category 自由文本保留（注入标签不变）。
    # NULL = 2026-11-03 前旧行（夜间打锚步顺带回填）
    slot: Mapped[str | None] = mapped_column(String(10), nullable=True, comment="固定槽位 rhythm/meeting/boundary/other")
    # ai_reflect = 习惯学习官产出 / user_manual = 用户手写（设置 → 记忆画像）/
    # chat = 对话中用户明示偏好抽取写入（2026-10-22 预留，注入优先级与 user_manual 同档）
    source: Mapped[str] = mapped_column(String(20), nullable=False, comment="来源：ai_reflect / user_manual / chat")
    # 0–1；习惯学习官产出按证据强度赋值，超预算截断时按此降序保留
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0", comment="置信度 0-1")
    # 证据摘要（如"近 3 周婉拒/拖走 5 个晚间会议"）；ai_reflect 行必填
    evidence: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="证据摘要；ai_reflect 必填")
    # 结构化锚（2026-11-01 优化批 8 自进化）：置信 ≥0.8 且文本可被 preference_slots
    # 解析的规则，夜间反思预解析存窗形态——设置页记忆画像行带「已生效为硬约束」
    # 徽标（白盒可观察）；删行即摘锚。None=未打锚
    structured_anchor: Mapped[dict | None] = mapped_column(JSON, nullable=True,
                                                           comment="高置信规则的结构化避让窗锚")
    # 最近一次被印证的时刻（时间衰减锚：超 90 天未印证由学习降 confidence）
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近印证时刻")
    # 同义印证计数（2026-11-03 记忆重构置信演化）：夜间反思 keep 印证 +1；
    # ≥3 次且置信 <0.8 自动升档（行为验证级）——读取零成本的列方案（非聚合查询）
    observation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0",
                                                   comment="同义印证计数（置信演化依据）")
    # 时间戳统一 Python 侧本地 naive（同全库口径）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<MemoryRule id={self.id} [{self.category}]>"


class MemoryDismissal(Base):
    """记忆纠偏账表（2026-11-03 记忆重构）：一行 = 一条被用户删除的规则快照。

    职责：用户删除规则（尤其 ai_reflect 行）后，反思链/仲裁者归纳出的同义候选
    在此对指纹（difflib ≥0.6 命中）被预过滤丢弃——「用户删了就别再推」的防再扰
    机制。独立表而非 memory_rules 状态列：规则删除是硬删、状态列无处承载；也不
    占 120 条规则预算。90 天过期夜间清理（口味会变，同 ai_reflect 衰减周期）。
    """

    __tablename__ = "memory_dismissals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 指纹（normalize_title(content) 归一化串）——仲裁者候选预过滤的比对键
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False, comment="规则正文归一化指纹")
    # 原文快照（白盒审计：用户删过什么一目了然；≤200 同 content 列）
    content: Mapped[str] = mapped_column(String(200), nullable=False, comment="规则原文快照")
    category: Mapped[str | None] = mapped_column(String(30), nullable=True, comment="原分类快照")
    # 被删规则的 source（全 source 记账——用户删手写规则后 AI 再归纳同义也是打扰）
    source: Mapped[str] = mapped_column(String(20), nullable=False, comment="被删规则来源")
    dismissed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="删除时刻")

    __table_args__ = (
        Index("ix_memory_dismissals_time", "dismissed_at"),
    )

    def __repr__(self) -> str:
        return f"<MemoryDismissal {self.fingerprint[:12]}>"


class EpisodicFact(Base):
    """情节事实表（2026-11-03 记忆重构 Tier 2）：一行 = 一条带 TTL 的碎片事实。

    与 memory_rules（Tier 1 永久规则）分流：「这周感冒」「下周出差」类临时状态
    不再挤占永久画像——expires_at 到期即失效（检索层过滤 + 夜间物理清理），多次
    出现的同主题事实由夜间反思蒸馏为 Tier 1 候选。检索走 FTS5 trigram 虚表
    （episodic_facts_fts，database._ensure_episodic_fts 建）+ BM25 + 时间加权。
    """

    __tablename__ = "episodic_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    topic: Mapped[str] = mapped_column(String(60), nullable=False, comment="事实主题（如 健康/出差）")
    fact: Mapped[str] = mapped_column(String(200), nullable=False, comment="事实正文")
    # chat = 对话设置链同跳提取 / reflect = 夜间反思兜底提取 / stats_reflect =
    # 统计报告·洞察结构化尾部自动归纳 / stats_chat = 复盘引导会话确认沉淀
    source: Mapped[str] = mapped_column(String(20), nullable=False, comment="来源：chat / reflect / stats_reflect / stats_chat")
    confidence: Mapped[float] = mapped_column(Float, default=0.6, server_default="0.6", comment="置信度 0-1")
    # 过期时刻（NULL = 永久）；检索与注入恒过滤已过期行
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="过期时刻；空=永久")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")

    __table_args__ = (
        Index("ix_episodic_facts_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<EpisodicFact [{self.topic}]>"


class BehaviorEvent(Base):
    """行为遥测事件表（2026-11-03 记忆重构）：一行 = 一次用户对排期的真实反馈。

    记录改期/取消排期/改时长/完成/放弃的前后值与偏差 + 环境快照（周几/时段/前后
    是否有会议）——夜间反思据此聚类（同情境同向 ≥3 次 = 候选排期习惯）。写点收口
    在 services/tasks.update_task 单点（前端拖拽/Toast/编辑全汇 PATCH /api/tasks，
    前端零改动）。task_id 弱关联无 FK：任务删除后事件保留（统计价值不随任务消亡）。
    """

    __tablename__ = "behavior_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 弱关联（无 FK）：任务删除后事件保留；None = 理论不出现（写点恒有任务上下文）
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="关联任务 id（弱关联无 FK）")
    # reschedule 改期 / unschedule 取消排期（放弃近似）/ resize 改时长 /
    # done 完成 / abandon 删除任务
    kind: Mapped[str] = mapped_column(String(20), nullable=False, comment="事件类型")
    prev_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="原开始时刻")
    new_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="新开始时刻")
    # 有符号分钟差：改期 = 新-旧；改时长 = 新时长-旧时长；完成 = 实际-计划
    delta_min: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="偏差分钟（有符号）")
    # done 事件的计划时长（超长耗时信号源）
    planned_min: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="计划时长分钟（done 事件）")
    # 环境快照：{weekday, period 清晨/上午/下午/晚上, before_meeting, after_meeting}
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="环境快照（周几/时段/前后会议）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="事件时刻")

    __table_args__ = (
        Index("ix_behavior_events_created", "created_at"),
        Index("ix_behavior_events_task", "task_id"),
        Index("ix_behavior_events_kind", "kind", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<BehaviorEvent {self.kind} {self.task_id}>"


class WorkflowRun(Base):
    """定时工作流运行记录表（2026-09-20 增，阶段二）：(name, 当日) 唯一——
    "当日未跑过"判定与启动补账的唯一载体（触发语义 = "不漏"而非"准时"）。

    认领即落行（INSERT ON CONFLICT DO NOTHING，rowcount=1 才跑——原子防 tick
    重复触发，机制同 reminders 表的唯一键幂等入列）。成功失败都不重试当日
    （LLM 失败不反复轰炸，失败次日再说）。心跳为纯规则层无补账语义，不记录。
    """

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # morning = 晨间对齐 / nightly = 夜间碎片整理 / ingest = 数据采集 /
    # prep = 会前准备 / hardtask = 高难协助 / brief:<uuid> = 自定义每日简报
    # （2026-10-20 增三流；声明放宽 String(60) 容纳 42 字符键——SQLite VARCHAR
    # 长度不执法，老库零 DDL 变化）
    name: Mapped[str] = mapped_column(String(60), nullable=False, comment="工作流名")
    # 运行归属日（当日判定键，与 name 联合唯一）
    run_date: Mapped[date] = mapped_column(Date, nullable=False, comment="运行归属日")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="认领时刻")

    __table_args__ = (
        UniqueConstraint("name", "run_date", name="uq_workflow_runs_name_date"),
    )

    def __repr__(self) -> str:
        return f"<WorkflowRun {self.name} {self.run_date}>"


class CustomBrief(Base):
    """自定义每日简报表（2026-10-20 增，「设置-定时任务」配套）：一行 = 一条
    用户自设的每日定时简报（时刻 + 指令），LLM 按指令汇总当日数据出 kind="brief"
    建议卡。约束全在 service 层（≤3 条 / 同刻唯一 / 30 分钟粒度，违规
    ValueError→422）——表层不设唯一约束（enabled 编辑排除自身等合并态判定
    放服务层更直白）。create_all 建表，无历史迁移。
    """

    __tablename__ = "custom_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 自 0 点起的分钟数（30 分钟粒度），触发时刻
    brief_min: Mapped[int] = mapped_column(Integer, nullable=False, comment="简报时刻（分钟，30 粒度）")
    # 简报指令（用户自由文本，直通 LLM 不做注入防御——同 ai_philosophy_custom 先例）
    instruction: Mapped[str] = mapped_column(String(200), nullable=False, comment="简报指令")
    # 停用保留行（开关关 = 不认领不跑，配置不丢）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<CustomBrief {self.brief_min} {self.instruction[:10]}>"


class IngestSource(Base):
    """数据采集源状态表（2026-10-14 增）：一行 = 一个参与采集的源。

    四重身份合一（列按批扩展，全部经 database._migrate_ingest_source_columns 迁移）：
    - 增量游标（last_success_at：fetch_recent 的时间窗按"距上次成功"推导）；
    - 健康度（fail_streak 连败计数：≥3 前端徽标标红，last_error 中文摘要）；
    - 每源采集配置（enabled 参与开关——卡片「停用/启用」的镜像，Composio 卡经
      PUT 直写 / fetch_tool+fetch_args 覆盖内置映射表 / window_hours 覆盖全局推导窗；
      后两项配置 UI 已撤、列保留恒空，行为走默认自动）；
    - 信任降频（pause_until：apply_trust_policy 采纳率过低时置明日 09:00，到期自动恢复）；
    - 高可信直通（trusted：2026-10-24 增——开 = 该源 todo/calendar 域台账条目
      免确认直落，判定与域口径见 ingestion_items.domain）。

    source_key 口径：local_folder → "local_folder"；Composio → connected_account_id；
    MCP → McpServer.name（改名 = 游标重置，取舍登记 docs/tech-debt.md）。行不建 FK、
    不主动清理（源消失后残留行无害，台账靠 purge 回收）。
    """

    __tablename__ = "ingest_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 源稳定键（口径见类 docstring；唯一约束拦重复登记）
    source_key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, comment="源键")
    # 上次成功采集时刻（增量窗口锚点；空 = 从未成功，用默认窗）
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次成功采集时刻")
    # 上次尝试时刻（含失败——失败也记，窗口自然变宽直到成功）
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次尝试时刻")
    # 连败计数（成功清零）；≥3 前端徽标标红
    fail_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="连续失败次数")
    # 最近一次失败中文摘要（成功置空；≤200）
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="最近失败摘要")
    # 最近一次成功时刻（健康度展示用，与游标 last_success_at 同值但语义独立）
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近成功时刻")
    # 参与每日采集（M2-②；local_folder 与 integration_settings.local_folder_enabled 双闸都开才采）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), comment="参与每日采集开关")
    # 采集动作覆盖：工具名（空 = 跟随内置映射表 / note 清单）
    fetch_tool: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="采集工具名覆盖")
    # 采集动作参数模板（JSON 对象；序列化 ≤2000 字符）
    fetch_args: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="采集参数覆盖")
    # 覆盖时间窗（小时；空 = 全局推导，覆盖仍受 72h 钳制）
    window_hours: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="覆盖时间窗（小时）")
    # 信任降频中（该时刻前整源跳过；到期自动恢复——采集入口只比较时刻，无清理任务）
    pause_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="降频暂停至该时刻")
    # 高可信直通开关（2026-10-24）：开 = 该源 todo/calendar 域条目免确认直落
    # 收件箱/日历（判定见 ingestion_items.domain 列注释）；任务型精选工具接入时
    # 按 data_ingest 默认值策略预开，其余默认关
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), comment="高可信直通开关")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<IngestSource {self.source_key} streak={self.fail_streak}>"


class IngestionItem(Base):
    """采集条目台账表（2026-10-14 增，M2-①）：一行 = 一个源内见过的条目指纹。

    游标（ingest_sources.last_success_at）是时间级去重；本表是内容级跨日去重：
    - 已见指纹 keepalive（last_seen/seen_count）后滤除，不重复进 LLM；
    - 用户拒绝过的建议（on_proposal_settled）所覆盖的全部指纹置 rejected_at，
      此后永静默——拒绝真正有记忆；
    - 30 天未见物理清理（purge_stale_items 夜间挂接；台账无软删价值）。
    指纹定义冻结：sha1(source_key|item.id or title|body 前 200 字符)——body 前缀
    保证无稳定 id 的启发式条目跨日也能对上，内容实质变化 → 新指纹重新进入。
    """

    __tablename__ = "ingestion_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_key: Mapped[str] = mapped_column(String(60), nullable=False, comment="源键（不建 FK——源硬删后靠留存清理回收）")
    # sha1 hex（40 字符）；与 source_key 联合唯一
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, comment="条目指纹 sha1")
    # 冗余标题（排查与摘要报告用；≤80）
    item_title: Mapped[str] = mapped_column(String(80), nullable=False, comment="条目标题（冗余）")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="首次见到时刻")
    # 保活锚（每次再见到刷新；留存清理的扫描谓词）
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="最近见到时刻")
    seen_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1", comment="累计见到次数")
    # 首次进建议卡时刻（空 = 从未建议过）
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="首次进建议时刻")
    # 用户拒绝时刻（非空 = 永静默：该指纹不再进 LLM）
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="拒绝时刻；非空=永静默")
    # 条目域（2026-10-24 高可信直通）：todo / calendar / doc——由采集端按 fetch
    # 命令声明写入；空 = 未声明（自定义行 / 行配置覆盖 / note 提炼路径）恒不直通。
    # 直通判定 = 源行 trusted ∧ 本列 ∈ {todo, calendar}：笔记/文档提炼条目域为
    # doc 仍须进数据源页确认（提取的不准确，用户定案）
    domain: Mapped[str | None] = mapped_column(String(10), nullable=True, comment="条目域 todo/calendar/doc；空=未声明")

    __table_args__ = (
        UniqueConstraint("source_key", "fingerprint", name="uq_ingestion_items_key_fp"),
        Index("ix_ingestion_items_last_seen", "last_seen_at"),
    )

    def __repr__(self) -> str:
        return f"<IngestionItem {self.source_key} {self.fingerprint[:8]}>"
