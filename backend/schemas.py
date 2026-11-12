"""Pydantic 请求/响应模型（API 契约层）。

与 models.py（ORM 存储结构）刻意分离：Out 固定响应形状，ORM 对象不直接从 router 裸奔出去。

写路径入参模型约定：Update 模型全部字段 Optional + default=None，"显式传 null = 清空该
字段"与"键不出现 = 不动"的区分靠 Pydantic 的 model_fields_set（service 层用
exclude_unset=True 取数），因此禁用 exclude_none。Create/Update 的长度校验（标题/备注/
项目名）、RRULE 格式子集在本层声明；end>start 等需要读库合并态的业务校验在 service 层。
"""

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 枚举集中定义，service / 测试都从这里取（与前端 PRIORITY_COLORS 键对齐）
Priority = Literal["high", "medium", "low"]

# ORM 的 priority 存 INT（1/2/3），出参转字符串
_PRIORITY_BY_INT = {1: "high", 2: "medium", 3: "low"}

# 入参 priority 字符串 → 库内 INT（seed 与写路径 service 共用单一来源，与 _PRIORITY_BY_INT 互逆）
PRIORITY_INT: dict[str, int] = {"high": 1, "medium": 2, "low": 3}

# recurrence_rule 合法子集（与前端 src/lib/rrule.ts、services/seed.py 的生成口径一致）：
# FREQ=DAILY / FREQ=MONTHLY / FREQ=WEEKLY;BYDAY=<星期码列表> 三形态，均可尾随
# ;UNTIL=YYYYMMDDTHHMMSS（RFC 5545 DATE-TIME、naive 本地——"删除此日程及后续"的
# 截断尾）。宽松接受任意 BYDAY 顺序——后端透传不规范化，规范序由前端 toRrule 保证
_RRULE_PATTERN = re.compile(
    r"^(?:FREQ=DAILY|FREQ=MONTHLY"
    r"|FREQ=WEEKLY;BYDAY=(?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:MO|TU|WE|TH|FR|SA|SU))*"
    r")(?:;UNTIL=\d{8}T\d{6})?$"
)


def _to_naive_local(dt: datetime) -> datetime:
    """带时区的输入转为本地 naive；naive 输入原样返回。

    前端 JS 的 `toISOString()` 产出 UTC 带时区字符串，而 DB 统一存 naive 本地时间，
    在契约层一次性归一，下层不再关心时区。
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# —— 子任务清单（2026-11-06 批 C）：Subtask 行契约；编辑语义 = 全量替换（定义须
# 先于 TaskOut——后者引用 SubtaskOut） ——


class SubtaskIn(BaseModel):
    """清单项入参（TaskUpdate.subtasks 的元素）。id 不收（全量替换后端重生成）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=30)  # 与 Task.title 同源
    done: bool = False


class SubtaskOut(BaseModel):
    """清单项出参（TaskOut.subtasks 的元素，service 装配而非列直出）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    done: bool  # done_at 非空派生


class TaskOut(BaseModel):
    """任务响应体（所有 GET 的返回形状）。

    recurrence_rule 按 RRULE 字符串原样返回，解析在前端适配层（src/lib/rrule.ts）做。
    original_time / reschedule_count / sort_order 前端不消费，不暴露；parent_id 暴露供
    前端识别活体例外行（"仅修改此日程"实体化的单次实体，据此禁用重复设置入口——
    service 层拒绝例外行原地转系列），其余例外元数据仍不暴露。
    """

    model_config = ConfigDict(from_attributes=True)  # 允许直接从 ORM 对象构造

    id: str
    title: str
    duration_mins: Optional[int] = None
    start_time: Optional[datetime] = None  # 空 = 未排期
    end_time: Optional[datetime] = None  # 空 = 不限时
    # 全天/跨天日程（2026-11-06 批 B）：True 时 start/end 为日期区间（service 归一
    # 为 0 点 / 次日 0 点）；end 空 = 单日（渲染口径 end ?? start+24h）
    all_day: bool = False
    recurrence_rule: Optional[str] = None
    # 活体例外行锚定的主系列 id；普通任务 None（墓碑行 is_deleted 恒不出现在响应里）
    parent_id: Optional[str] = None
    # 重复系列已取消实例的日期列表（前端 occursOn 展开时跳过；非重复任务恒 None）。
    # 来源：service 聚合墓碑行（parent_id + original_time）生成，非表列直出
    cancelled_dates: Optional[list[str]] = None
    project_id: Optional[str] = None  # 空 = 纯收件箱
    priority: Priority
    notes: Optional[str] = None
    # 采集/纪要溯源键（空 = 手建）：前端纪要回看区块按 "minutes:<会议id>" 前缀
    # 匹配行动项（2026-11-10 批 2 起消费，此前落库仅后端可见）
    source_key: Optional[str] = None
    # 完成时刻（2026-10-22 增，与软删正交）：空 = 未完成。收件箱据此隐藏已完成，
    # 日历历史/夜间复盘继续可见（done 任务仍占时段——排期避让保留）
    done_at: Optional[datetime] = None
    # 子任务清单（2026-11-06 批 C）：service 批量装配（同 cancelled_dates 模式，
    # 非 ORM relationship——list_tasks 批量主路径避免 N+1）；空清单 = []
    subtasks: list[SubtaskOut] = []
    created_at: datetime
    updated_at: datetime

    @field_validator("priority", mode="before")
    @classmethod
    def _int_to_priority(cls, v: object) -> object:
        """ORM 的 priority 存 INT（1/2/3），出参转为 high/medium/low；未知值显式报错，不静默降级。"""
        if isinstance(v, int) and not isinstance(v, bool):
            if v not in _PRIORITY_BY_INT:
                raise ValueError(f"未知优先级值: {v}")
            return _PRIORITY_BY_INT[v]
        return v  # 已是字符串（非 ORM 来源）原样放行


class SearchTaskOut(TaskOut):
    """搜索结果行（2026-11-06 搜索批）：TaskOut + 项目名——结果行直显归属，
    免前端拿 project_id 二次查表。"""

    project_name: str | None = None


class SearchProjectOut(BaseModel):
    """搜索命中的项目行（点击跳项目页）"""

    id: str
    name: str


class SearchProposalItemOut(BaseModel):
    """搜索命中的数据源建议条目（未决 from_ingest 卡；点击跳数据源页）。
    idx 为条目在其卡内数组的序（inbox.items / schedule.draft_plan 各计数）。"""

    proposal_id: str
    idx: int
    kind: str  # inbox / schedule
    title: str


class SearchRangeOut(BaseModel):
    """时间词解析结果（120 分档）：前端展示「已按“上周”过滤」标签用。"""

    label: str  # 命中的时间词原文（如"上周"）
    start: datetime
    end: datetime


class SearchOut(BaseModel):
    """统一搜索响应。60 分档只有任务；100 分档扩项目/建议条目/系列实例行；
    120 分档时间词解析再扩 parsed_range（docs/search.md）。"""

    tasks: list[SearchTaskOut] = []
    projects: list[SearchProjectOut] = []
    proposal_items: list[SearchProposalItemOut] = []
    parsed_range: SearchRangeOut | None = None


class ProjectOut(BaseModel):
    """项目响应体。前端本期以 name 为引用标识，id 供未来写路径定位。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    color: str
    starred: bool
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    """新建项目入参（sort_order 由 service 层排在末位，starred 恒 False 起）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=20)  # 与前端 PROJECT_NAME_MAX_LEN 同源
    color: str = Field(min_length=1, max_length=50)  # CSS 变量串或 hex 原样透传


class ProjectUpdate(BaseModel):
    """项目部分更新入参：键不出现 = 不动；name 显式 null 由 service 拦（NOT NULL 列）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=20)
    starred: bool | None = None
    color: str | None = Field(default=None, min_length=1, max_length=50)


class TaskCreate(BaseModel):
    """新建任务入参（快捷录入框 + inbox 建议采纳批量落库）。notes 供 inbox 卡
    采纳时携带上下文（2026-10-14 增，additive 缺省 None 不影响快捷录入）；
    priority 缺省 None 走库默认 2（medium）——AI 自动落库链按用户明示/记忆
    画像信号直判 1（2026-10-22 增），快捷录入仍不带（需要时 PATCH 补）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=30)  # 与前端 TITLE_MAX_LEN 同源
    duration_mins: int | None = Field(default=None, ge=1, le=1440)
    start_time: datetime | None = None
    end_time: datetime | None = None
    # 全天/跨天（2026-11-06 批 B）：True 要求 start_time 非空；service 归一时刻并
    # 豁免 duration 重算（duration 恒空，占天不占时段）
    all_day: bool = False
    recurrence_rule: str | None = Field(default=None, max_length=100, pattern=_RRULE_PATTERN)
    project_id: str | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    notes: str | None = Field(default=None, max_length=500)  # 与 TaskUpdate.notes 同源
    # 采集溯源三键（2026-10-23 增，additive）：inbox 建议（自动收录/手动采纳/快录
    # 直落）携带——条目 source_ref 透传，静态溯源地基（幕僚长教练按此回放素材）
    source_key: str | None = Field(default=None, max_length=60)
    source_fingerprint: str | None = Field(default=None, max_length=40)
    source_evidence: str | None = Field(default=None, max_length=500)

    @field_validator("notes")
    @classmethod
    def _empty_notes_to_none(cls, v: str | None) -> str | None:
        """空串归一 None（同 TaskUpdate 口径）。"""
        return None if v == "" else v

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def _normalize_tz(cls, v: datetime | None) -> datetime | None:
        """body 内联时间与时区归一（同 TaskUpdate 口径）。"""
        return _to_naive_local(v) if v is not None else v


class TaskUpdate(BaseModel):
    """任务部分更新入参：键不出现 = 不动，显式 null = 清空（notes/duration_mins/
    end_time/recurrence_rule/project_id；start_time 置 null = 取消排期，schema 开放、
    本期前端不发）。title/priority 显式 null 由 service 拦（NOT NULL 列）。排期字段
    （dateOffset/start/end 小时小数）由前端 api.ts 换算为绝对 naive 本地时间后传输。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=30)  # 与前端 TITLE_MAX_LEN 同源
    notes: str | None = Field(default=None, max_length=500)  # 与前端 NOTE_MAX_LEN 同源
    priority: Priority | None = None
    duration_mins: int | None = Field(default=None, ge=1, le=1440)
    start_time: datetime | None = None
    end_time: datetime | None = None
    # 全天/跨天开关（2026-11-06 批 B）：False = 转回普通日程（时刻保留已归一的
    # 0 点，用户可再拖出具体钟点）；无 null 清空语义，service 对显式 null 弹出不动
    all_day: bool | None = None
    recurrence_rule: str | None = Field(default=None, max_length=100, pattern=_RRULE_PATTERN)
    project_id: str | None = None
    # 完成态开关（2026-10-22 增）：True = 完成（done_at=now）/ False = 撤销完成（done_at
    # = None）/ 不出现 = 不动。系列任务（recurrence_rule 非空）由 service 拦 422（V1 不支持）
    done: bool | None = None
    # 子任务清单（2026-11-06 批 C）：键出现 = 全量替换（delete 旧行 + reinsert），
    # 空列表 = 清空清单；不出现 = 不动。重复系列编辑时与其他字段同走三问分流
    subtasks: list[SubtaskIn] | None = None

    @field_validator("notes")
    @classmethod
    def _empty_notes_to_none(cls, v: str | None) -> str | None:
        """trim 后空串归一为 None（清空语义）——前端清空备注即发 null，空串视同清空。"""
        return None if v == "" else v

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def _normalize_tz(cls, v: datetime | None) -> datetime | None:
        """body 内联时间与时区归一（同查询参数口径，router 不逐参包）。"""
        return _to_naive_local(v) if v is not None else v


# ---- 一键智能安排（统一排期引擎，2026-10-20 增）----
# 收件箱单卡按钮 POST /api/tasks/{id}/auto-schedule（单 TaskOut，纯规则直落；
# 同日决策：批量「安排全部」按钮退役，多任务批量排期全走对话草案卡）。
# ---- 应用设置（单行资源 /api/settings）----


# ---- 应用设置（单行资源 /api/settings）----
# 枚举集中定义（与前端 src/lib/settings.ts 的字面量联合对齐；非法值由 Literal 在契约层拦 422）

CloseAction = Literal["hide", "quit"]  # 关闭主窗口：隐藏到托盘 / 完全退出
ThemeMode = Literal["system", "light", "dark"]  # 外观模式
DefaultView = Literal["day", "week", "month"]  # 启动默认视图：日 / 周 / 月
NotifyStyle = Literal["system", "floating", "corner"]  # 通知形式：系统通知 / 居中悬浮框 / 右下角悬浮框
Sound = Literal["chime", "bowl", "water", "off"]  # 提示音：铃声 / 颂钵 / 水滴 / 关闭
# AI 回复风格四档（2026-10-20）：均衡默认 / 简洁 / 详细 / 严谨
AiStyle = Literal["balanced", "concise", "detailed", "rigorous"]

# 合法提醒提前量（None = 不提醒不在此列，由 nullable 承载）
REMINDER_LEAD_CHOICES = (0, 5, 10, 15, 30)


class SettingsOut(BaseModel):
    """设置响应体（GET/PATCH 共用；字段与 ORM 同名直出，分钟口径由前端换算）。"""

    model_config = ConfigDict(from_attributes=True)

    auto_start: bool
    close_action: CloseAction
    theme_mode: ThemeMode
    default_view: DefaultView
    widget_transparency: int
    calendar_start_min: int
    calendar_end_min: int
    lunch_enabled: bool
    lunch_start_min: int
    lunch_end_min: int
    work_minutes_per_day: int
    reminder_lead_min: Optional[int] = None  # 空 = 不提醒
    notify_style: NotifyStyle
    sound: Sound
    ai_enabled: bool
    morning_brief_min: int
    ingest_enabled: bool
    ingest_brief_min: int
    ingest_runs_per_day: int  # 每日采集轮数（1-4 档，后续槽位当天顺延）
    ingest_llm_budget: int  # 单日采集 LLM 调用上限（0=不限）
    weather_city: Optional[str] = None  # 排期天气对照城市（空=停用）
    ai_style: AiStyle = "balanced"  # 回复风格（balanced=默认零注入）
    ai_philosophy: Optional[str] = None  # 内置哲学预设 id 逗号串
    ai_philosophy_custom: Optional[str] = None  # 自定义哲学文本
    nightly_start_min: int  # 夜间工作流报点时刻（分钟，30 粒度）
    # 四流独立开关（2026-10-21 复查批 D1；与 ai_enabled 正交，默认全开）
    morning_enabled: bool
    nightly_enabled: bool
    prep_enabled: bool
    hardtask_enabled: bool
    # 周报自动生成开关（2026-11-07 统计 120 分档；周日晚随夜间报点）
    weekly_report_enabled: bool
    # 会后纪要邀请开关（2026-11-10 批 3；重要会议结束后询问补记纪要）
    minutes_enabled: bool
    # 对话上下文自动压缩开关（2026-10-26 增；关 = 仅 /compact 手动可用）
    chat_autocompact_enabled: bool
    # 教练溯源深探（2026-11-01 优化批 9）：有界工具循环开关（默认关）
    coach_probe_deep: bool
    # 助手消息轻提示音（2026-11-04 通知分轨）：AI 定时任务产物落聊天框时轻响
    assistant_chime: bool
    updated_at: datetime


class SettingsUpdate(BaseModel):
    """设置部分更新入参：键不出现 = 不动（model_fields_set）；显式 null 仅
    reminder_lead_min 合法（= 改为不提醒），其余 NOT NULL 字段由 Literal/Field 结构上
    不可能传 null。跨字段约束（跨度下限、午休 ⊆ 可视范围）在 service 合并态校验。
    """

    auto_start: bool | None = None
    close_action: CloseAction | None = None
    theme_mode: ThemeMode | None = None
    default_view: DefaultView | None = None
    # 小部件透明度（%，越大越透；默认 20 = tint alpha 0.80）
    widget_transparency: int | None = Field(default=None, ge=10, le=80)
    # 分钟字段：0..1440 且 30 分钟粒度（粒度校验在 service，与跨字段约束一起出统一报错）
    calendar_start_min: int | None = Field(default=None, ge=0, le=1440)
    calendar_end_min: int | None = Field(default=None, ge=0, le=1440)
    lunch_enabled: bool | None = None
    lunch_start_min: int | None = Field(default=None, ge=0, le=1440)
    lunch_end_min: int | None = Field(default=None, ge=0, le=1440)
    # 1..12 小时（粒度随分钟口径走 30 分钟）
    work_minutes_per_day: int | None = Field(default=None, ge=60, le=720)
    reminder_lead_min: Optional[int] = None
    notify_style: NotifyStyle | None = None
    sound: Sound | None = None
    # AI 功能主开关（2026-09-20 增，阶段二）
    ai_enabled: bool | None = None
    # 晨间对齐时刻（分钟口径；30 分钟粒度校验随 _MINUTE_FIELDS）
    morning_brief_min: int | None = Field(default=None, ge=0, le=1440)
    # 每日数据采集开关与时刻（分钟口径，粒度校验随 _MINUTE_FIELDS）
    ingest_enabled: bool | None = None
    ingest_brief_min: int | None = Field(default=None, ge=0, le=1440)
    # 每日采集轮数（1-4 档；首轮在 ingest_brief_min，后续槽当天顺延）
    ingest_runs_per_day: int | None = Field(default=None, ge=1, le=4)
    # 单日采集 LLM 调用上限（0 = 不限；上限 300 防误填天文数字）
    ingest_llm_budget: int | None = Field(default=None, ge=0, le=300)
    # 排期天气对照城市（空白串在 service 归一为 None = 停用；显式 null 同停用）
    weather_city: Optional[str] = Field(default=None, max_length=50)
    # —— AI 个性化（2026-10-20）——
    ai_style: AiStyle | None = None
    # 内置哲学预设 id 逗号串（service 拆段规整 ≤8 段；不做 id 存在性校验防反向依赖）
    ai_philosophy: Optional[str] = Field(default=None, max_length=100)
    # 自定义哲学文本（空白串在 service 归一为 None）
    ai_philosophy_custom: Optional[str] = Field(default=None, max_length=1000)
    # 夜间工作流报点时刻（分钟口径；30 分钟粒度校验随 _MINUTE_FIELDS）
    nightly_start_min: int | None = Field(default=None, ge=0, le=1440)
    # 四流独立开关（2026-10-21 复查批 D1）：关 = 永不认领不落 workflow_runs 行
    morning_enabled: bool | None = None
    nightly_enabled: bool | None = None
    prep_enabled: bool | None = None
    hardtask_enabled: bool | None = None
    # 周报自动生成开关（2026-11-07 统计 120 分档；周日晚随夜间报点）
    weekly_report_enabled: bool | None = None
    # 会后纪要邀请开关（2026-11-10 批 3）：关 = 永不认领不落 workflow_runs 行
    minutes_enabled: bool | None = None
    # 对话上下文自动压缩开关（2026-10-26 增）：关 = 轮末后台压缩早退
    chat_autocompact_enabled: bool | None = None
    # 教练溯源深探（2026-11-01 优化批 9）：开 = 回源探测扩到至多 2 目标
    coach_probe_deep: bool | None = None
    # 助手消息轻提示音（2026-11-04 通知分轨）：开 = AI 定时任务产物落聊天框时轻响
    assistant_chime: bool | None = None


class PhilosophyOut(BaseModel):
    """内置管理哲学预设（GET /api/ai/philosophies；text 不出——提示词只留后端单源）。"""

    id: str
    name: str
    desc: str
    # 与之理念冲突的其他预设 id（前端拦截互选；后端 PATCH 不校验——防反向依赖）
    conflicts: list[str] = []


# ---- 模型接入（/api/models）----
# 上下文长度档位（token 数）：8K / 16K / 32K / 64K / 128K / 200K / 256K / 1M（前端下拉档同源）
ContextLength = Literal[8192, 16384, 32768, 65536, 131072, 204800, 262144, 1048576]

# Base URL 可选：空 = LiteLLM 按 provider/ 前缀官方路由（空串经 validator 归 None）；
# 有值时须为 http(s) 起始的绝对地址（是否含 /v1 等细节由连通性测试结果反馈，契约层不猜）
_BASE_URL_PATTERN = re.compile(r"^(https?://\S+)?$")


class ModelProviderOut(BaseModel):
    """模型接入响应体。api_key 原样回显——本地单用户桌面应用，编辑表单免重填。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    base_url: Optional[str] = None  # 空 = LiteLLM 官方路由
    model_name: str
    api_key: Optional[str] = None  # 空 = 本地免鉴权服务
    thinking_mode: bool
    context_length: int
    is_active: bool
    is_aux: bool = False  # 轻量助手模型槽（优化批 9）
    last_test_ok: Optional[bool] = None  # 空 = 从未测试
    last_test_ms: Optional[int] = None
    last_test_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ModelProviderCreate(BaseModel):
    """新建模型入参。api_key / base_url 可空（未填写 = 免鉴权 / LiteLLM 官方
    路由）；trim 后空串归一 None。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=30)
    base_url: Optional[str] = Field(default=None, max_length=200, pattern=_BASE_URL_PATTERN)
    model_name: str = Field(min_length=1, max_length=100)
    api_key: Optional[str] = Field(default=None, max_length=200)
    thinking_mode: bool = False
    context_length: ContextLength = 32768

    @field_validator("api_key", "base_url")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        """trim 后空串归一为 None（未填写语义，同 TaskUpdate.notes 口径）。"""
        return None if v == "" else v


class ModelProviderUpdate(BaseModel):
    """模型部分更新入参：键不出现 = 不动；api_key 显式 null = 清空。

    刻意不开放 is_active / last_test_*——激活走 POST /{id}/activate、测速走
    POST /{id}/test，互斥不变量的写入口唯一（PATCH 只管配置六字段）。
    name/model_name 显式 null 由 service 拦（NOT NULL 列）；base_url 显式
    null = 清回官方路由，合法。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=30)
    base_url: str | None = Field(default=None, max_length=200, pattern=_BASE_URL_PATTERN)
    model_name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: Optional[str] = Field(default=None, max_length=200)
    thinking_mode: bool | None = None
    context_length: ContextLength | None = None

    @field_validator("api_key", "base_url")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        """trim 后空串视同清空（同 Create 口径）。"""
        return None if v == "" else v


class ModelProviderTestIn(BaseModel):
    """草稿连通性测试入参（POST /api/models/test）：用表单当前填写参数探测，
    不读库也不落库；三字段校验同 Create。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    base_url: Optional[str] = Field(default=None, max_length=200, pattern=_BASE_URL_PATTERN)
    model_name: str = Field(min_length=1, max_length=100)
    api_key: Optional[str] = Field(default=None, max_length=200)

    @field_validator("api_key", "base_url")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return None if v == "" else v


class ModelProviderTestOut(BaseModel):
    """连通性测试结果（探测失败也是 200——失败是数据不是错误）。"""

    ok: bool
    latency_ms: Optional[int] = None  # ok 时必有
    error: Optional[str] = None  # 失败时的分类摘要（中文）


# ---- 集成接入（/api/integrations，Composio，2026-09-18 增）----
# 已连接应用列表不落库——真源在 Composio 云端实时拉取，service 层负责 SDK 调用与
# 名称/图标元数据补全（connected_accounts 内嵌 toolkit 只有 slug）


class ComposioKeyOut(BaseModel):
    """Composio Key 配置响应（GET/PUT/POST verify 共用）。api_key 原样回显——
    本地单用户桌面应用，编辑免重填（同 ModelProviderOut.api_key 口径）。
    由 service 显式映射构造：ORM 列名带 composio_ 前缀与字段名不一致，
    from_attributes 按字段名取属性会静默落 None。"""

    api_key: Optional[str] = None  # 空 = 未配置
    key_ok: Optional[bool] = None  # 空 = 从未验证
    key_error: Optional[str] = None  # 失败摘要；验证成功为 None
    key_checked_at: Optional[datetime] = None


class ComposioKeyIn(BaseModel):
    """保存/清除 Key 入参（PUT /key）。api_key 缺省 / 显式 null / trim 后空串 =
    清除（validator 归一）——验证三列随保存流程重算。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    api_key: Optional[str] = Field(default=None, max_length=200)

    @field_validator("api_key")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        """trim 后空串归一 None（未配置语义，同 ModelProviderCreate.api_key 口径）。"""
        return None if v == "" else v


class ComposioAppOut(BaseModel):
    """Composio 应用（搜索结果）。logo 为远程 URL，前端直接 img 加载（csp 为 null）。"""

    slug: str
    name: str
    description: Optional[str] = None
    logo: Optional[str] = None
    tools_count: Optional[int] = None


class ComposioConnectIn(BaseModel):
    """发起连接入参（POST /connect）。toolkit_slug 为 Composio 应用的 slug 标识。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    toolkit_slug: str = Field(min_length=1, max_length=100)


class ComposioConnectOut(BaseModel):
    """发起连接响应：redirect_url 交系统浏览器完成 OAuth，connected_account_id
    供前端轮询 GET /connect/{id}。"""

    connected_account_id: str
    redirect_url: Optional[str] = None


class ComposioConnectStatusOut(BaseModel):
    """连接状态（轮询响应）。status 为 Composio 原生值：
    INITIALIZING/INITIATED/ACTIVE/FAILED/EXPIRED/INACTIVE/REVOKED。"""

    connected_account_id: str
    status: str
    toolkit_slug: Optional[str] = None


class ComposioConnectionOut(BaseModel):
    """已连接应用条目。name/logo/description 由 service 层 enrich（toolkits.get(slug)），
    单应用元数据拉不到时容忍为 None。enabled 为本地采集开关（镜像 ingest_sources，
    key=连接 ID，行缺省视为启用——同 MCP/CLI 停用语义：停用只是不参与每日采集，
    云端连接本体不动）。created_at/updated_at 云端 ISO 带时区，经 validator 归一
    本地 naive。"""

    id: str
    status: str
    toolkit_slug: str
    name: Optional[str] = None
    logo: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _naive_local(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return _to_naive_local(v)


class ComposioConnectionTestOut(BaseModel):
    """已连接应用「测试」结果（失败也是 200——失败是数据，前端徽标展示）：与采集
    同源的动作解析（行配置 > 内置映射），execute 真调云端数条目（不过时间窗——
    测试报原始返回数，时间窗会把"有数据"砍成"0 条"造成误判）；未内置采集命令的
    应用退化为仅验连接状态（note 说明）。结果不落库（云端真源无本地行）。"""

    ok: bool
    latency_ms: Optional[int] = None
    items_count: Optional[int] = None
    note: Optional[str] = None
    error: Optional[str] = None


class ComposioEnabledIn(BaseModel):
    """连接启停入参（卡片「停用/启用」）。"""

    enabled: bool


# ---- MCP 服务接入（/api/mcp，2026-09-18 增）----
# 精选应用（钉钉/Obsidian/滴答国内版）与手动添加同走通用 CRUD——前端模板
# 预填配置，后端零精选知识。env/headers 明文存库 + 原样回显（本地单用户，同
# ModelProviderOut.api_key 口径）。

McpTransport = Literal["stdio", "streamable_http", "sse"]


class McpServerOut(BaseModel):
    """MCP 服务响应体。列名与字段名一致，from_attributes 直出安全（区别于
    ComposioKeyOut 的显式映射）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None  # 用户自定义描述；空 = 卡片回落技术串
    transport: McpTransport
    command: Optional[str] = None  # 空 = 非 stdio
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None  # 原样回显免重填
    url: Optional[str] = None  # 空 = stdio
    headers: Optional[dict[str, str]] = None  # 原样回显免重填
    enabled: bool
    last_test_ok: Optional[bool] = None  # 空 = 从未测试
    last_test_ms: Optional[int] = None
    last_test_at: Optional[datetime] = None
    last_test_tools: Optional[int] = None  # 2026-09-20 增：卡标题旁徽标"连通 · Nms · N 个工具"
    created_at: datetime
    updated_at: datetime


def _mcp_args_valid(args: list[str] | None) -> list[str] | None:
    """args 单项长度校验（每行一个参数，单参数 ≤500 字符）。"""
    if args is None:
        return args
    bad = next((a for a in args if len(a) > 500), None)
    if bad is not None:
        raise ValueError("单个命令参数不能超过 500 字符")
    return args


def _mcp_kv_valid(kv: dict[str, str] | None) -> dict[str, str] | None:
    """env/headers 容器校验：≤50 项、key ≤100、value ≤2000（容 JWT/Bearer 长凭据）。"""
    if kv is None:
        return kv
    if len(kv) > 50:
        raise ValueError("条目数不能超过 50")
    for k, v in kv.items():
        if len(k) > 100:
            raise ValueError("条目名不能超过 100 字符")
        if len(v) > 2000:
            raise ValueError("条目值不能超过 2000 字符")
    return kv


class McpServerCreate(BaseModel):
    """新建 MCP 服务入参。transport 分支必填（stdio 须 command、http/sse 须 url）
    在 model_validator 拦——Create 无合并态，契约层拦即可 422。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=30)
    description: Optional[str] = Field(default=None, max_length=100)
    transport: McpTransport
    command: Optional[str] = Field(default=None, max_length=200)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = Field(default=None, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("command", "url", "description")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        """trim 后空串归一 None（未填写语义，同 ModelProviderCreate 口径）。"""
        return None if v == "" else v

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str] | None) -> list[str] | None:
        return _mcp_args_valid(v)

    @field_validator("env", "headers")
    @classmethod
    def _kv(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return _mcp_kv_valid(v)

    @model_validator(mode="after")
    def _transport_fields(self) -> "McpServerCreate":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 传输必须填写启动命令")
        if self.transport != "stdio" and not self.url:
            raise ValueError("该传输方式必须填写服务 URL")
        return self


class McpServerUpdate(BaseModel):
    """MCP 服务部分更新入参：键不出现 = 不动；显式 null = 清空（可空列）。

    transport 分支必填与 name/transport 显式 null 拦截在 service 合并态做（Update
    是部分更新，schema 层看不到库中现值）。刻意不开放 last_test_*——写入口唯一 =
    POST /{id}/test（同 ModelProviderUpdate 先例）。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=30)
    description: Optional[str] = Field(default=None, max_length=100)
    transport: McpTransport | None = None
    command: Optional[str] = Field(default=None, max_length=200)
    args: Optional[list[str]] = Field(default=None, max_length=100)
    env: Optional[dict[str, str]] = None
    url: Optional[str] = Field(default=None, max_length=500)
    headers: Optional[dict[str, str]] = None
    enabled: bool | None = None

    @field_validator("command", "url", "description")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return None if v == "" else v

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str] | None) -> list[str] | None:
        return _mcp_args_valid(v)

    @field_validator("env", "headers")
    @classmethod
    def _kv(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return _mcp_kv_valid(v)


class McpServerTestIn(BaseModel):
    """草稿连通性测试入参（POST /api/mcp/test）：表单现值探测，不读库不落库。
    字段 = Create 减 name/enabled，含同款分支必填校验。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    transport: McpTransport
    command: Optional[str] = Field(default=None, max_length=200)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = Field(default=None, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("command", "url")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return None if v == "" else v

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str] | None) -> list[str] | None:
        return _mcp_args_valid(v)

    @field_validator("env", "headers")
    @classmethod
    def _kv(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return _mcp_kv_valid(v)

    @model_validator(mode="after")
    def _transport_fields(self) -> "McpServerTestIn":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 传输必须填写启动命令")
        if self.transport != "stdio" and not self.url:
            raise ValueError("该传输方式必须填写服务 URL")
        return self


class McpServerTestOut(BaseModel):
    """MCP 连通性测试结果（探测失败也是 200——失败是数据不是错误）。"""

    ok: bool
    latency_ms: Optional[int] = None  # ok 时必有
    tools_count: Optional[int] = None  # ok 时必有：发现的工具数
    error: Optional[str] = None  # 失败时的分类摘要（中文）


class LocalFolderOut(BaseModel):
    """本地文件夹配置响应（多目录）。由 service 显式构造：ORM 列名 local_folder_*
    与字段名不一致，from_attributes 按字段名取属性会静默落 None（同 ComposioKeyOut 坑）。"""

    paths: list[str]  # 空列表 = 未配置
    enabled: bool
    # 上次卡上「测试」结果（2026-09-20 增，持久化展示在卡标题旁徽标）：
    # True=有效 / False=无效 / None=从未测试（或目录变更后清空）；test_files 无效时为空
    test_ok: Optional[bool] = None
    test_files: Optional[int] = None


class LocalFolderUpdate(BaseModel):
    """本地文件夹配置入参（PUT /api/mcp/local-folder，多目录）。UI 恒发全量两键；
    paths 显式 null / 空列表 = 清除全部。启用与路径的跨字段约束在 service 合并态
    校验（需读库现值：只发 enabled=true 时路径来自库）。"""

    model_config = ConfigDict(str_strip_whitespace=True)  # 对 list[str] 逐项生效

    paths: Optional[list[str]] = Field(default=None, max_length=10)  # 项数上限 10
    enabled: bool | None = None

    @field_validator("paths")
    @classmethod
    def _clean_paths(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for p in v:
            if not p:
                raise ValueError("路径不能为空串")
            if len(p) > 500:
                raise ValueError("单个路径不能超过 500 字符")
        return v


class LocalFolderTestIn(BaseModel):
    """本地文件夹草稿探测入参（POST /api/mcp/local-folder/test）。paths 传值 =
    测配置面板当前填写的目录列表（不读库不落库，选完文件夹即可测，无需先保存）；
    缺省 / null = 测库中已保存路径。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    paths: Optional[list[str]] = Field(default=None, max_length=10)

    @field_validator("paths")
    @classmethod
    def _clean_paths(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for p in v:
            if not p:
                raise ValueError("路径不能为空串")
            if len(p) > 500:
                raise ValueError("单个路径不能超过 500 字符")
        return v


class LocalFolderTestOut(BaseModel):
    """本地文件夹有效性测试结果（失败也是 200——失败是数据）。"""

    ok: bool
    files_count: Optional[int] = None  # ok 时必有（多目录合计）
    error: Optional[str] = None


# ---- CLI 工具接入（第四源 kind="cli"，2026-10-15 增）----
# 官方 CLI 适配器（飞书 lark-cli / 钉钉 dws / Todoist td）的精选声明在后端注册表
# services/cli_tools.py 的 ADAPTERS——命令声明必须与执行器同源，放前端会漂移（与
# MCP 精选模板放前端的差异是有意的）。本节契约一半字段来自 spec、一半来自
# cli_tools 表行，service 层显式构造 dict，不走 from_attributes。

CliAuthMode = Literal["auto", "manual", "none"]  # auto=程序化授权流（捕 URL+轮询）/ manual=终端自理后点检测 / none=授权状态不可探测（直接接入）


class CliToolOut(BaseModel):
    """CLI 工具响应体：注册表声明 + 接入行状态合并。custom=True 为窄口自定义只读
    命令行（无注册表条目，auth_* 恒空）。fetch_ready=False 表示采集命令待真机实测
    （源走 note 清单路径，不阻塞接入）。actions 供补采工具清单与采集卡展示共用。"""

    adapter_id: str
    name: str
    description: str = ""
    custom: bool = False
    install_url: Optional[str] = None  # 官方文档外链（安装命令不内嵌，以文档为准）
    auth_mode: CliAuthMode
    connected: bool  # DB 行存在 = 已接入
    enabled: bool
    bin_ok: Optional[bool] = None  # 空 = 从未探测
    bin_version: Optional[str] = None
    bin_path: Optional[str] = None
    bin_at: Optional[datetime] = None
    auth_ok: Optional[bool] = None  # 空 = 从未探测或不适用
    auth_at: Optional[datetime] = None
    test_ok: Optional[bool] = None  # 自定义行上次「测试」结果；空 = 从未测试（精选行恒空）
    test_items: Optional[int] = None
    custom_argv: Optional[list[str]] = None  # 自定义行命令参数（编辑表单回填用）
    last_error: Optional[str] = None
    fetch_ready: bool
    actions: list[dict] = []  # [{name, description}] 声明的只读命令（零子进程）


class CliProbeOut(BaseModel):
    """安装+授权探测结果（失败也是 200——失败是数据；已接入行同时落库）。"""

    ok: bool  # bin_ok 且授权探测未明示失败
    bin_ok: bool
    bin_version: Optional[str] = None
    bin_path: Optional[str] = None
    auth_ok: Optional[bool] = None
    error: Optional[str] = None


class CliAuthStartOut(BaseModel):
    """发起授权：auto 档 ok=True 带 url（前端开系统浏览器+轮询）；manual 档带
    manual_command（用户终端自理）；ok=False 带 error。"""

    ok: bool
    url: Optional[str] = None
    manual_command: Optional[str] = None
    error: Optional[str] = None


class CliAuthStatusOut(BaseModel):
    """授权状态轮询（GET）：authenticated 由 CLI 自身 auth 命令判定。"""

    authenticated: bool
    error: Optional[str] = None


class ChatChannelOut(BaseModel):
    """聊天通讯渠道响应体：目录声明 + 接入行状态合并。app_secret 原样回显
    供编辑表单回填（Composio key 先例：本地单用户、明文存库随快照导出，
    GET 本地回环无新增暴露面）；前端密码态显示 + 眼睛按需查看。
    placeholder=True 为「建设中」占位渠道（微信，恒 configured=False）。"""

    provider: str
    name: str
    desc: str = ""
    setup_url: Optional[str] = None  # 官方文档外链（凭据申请指引）
    placeholder: bool = False
    configured: bool  # DB 行存在 = 已填凭据
    app_id: Optional[str] = None  # 未配置恒空；编辑表单回填用
    app_secret: Optional[str] = None  # 同上；未配置恒空
    enabled: bool
    push_enabled: bool = False  # 定时任务推送开关（卡片独立小开关）
    push_ready: bool = False  # 推送目标已缓存（用户与机器人有过对话）
    conn_ok: Optional[bool] = None  # 上次凭据探测；空 = 从未
    conn_error: Optional[str] = None
    conn_at: Optional[datetime] = None


class ChatChannelUpsertBody(BaseModel):
    """保存/更新渠道凭据（保存即探测，结果落 conn_* 回显）。app_secret 允许空串
    ——编辑形态留空 = 保持旧值（secret 不回显，前端只回填 app_id）。"""

    app_id: str = Field(min_length=1, max_length=100)
    app_secret: str = Field(max_length=200)


class ChatChannelEnabledBody(BaseModel):
    """启停渠道 worker（False = 不监听消息，凭据保留）。"""

    enabled: bool


class ChatChannelPushBody(BaseModel):
    """定时任务推送开关（True = 晨报/会前准备等主动推送也发该渠道；
    与 enabled 正交——监听关、推送仍可开）。"""

    push_enabled: bool


class CliToolUpdate(BaseModel):
    """CLI 行更新：启停（联动镜像 ingest_sources.enabled——卡片「停用」即源的退出
    手段）+ 自定义行编辑（name/bin_path/argv 三字段全量同给——表单编辑态发全量，
    同 McpServer 编辑先例；精选行拒绝编辑三字段）。deny-list 校验在 service 层
    （同 CustomCliBody 口径）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: Optional[bool] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=30)
    bin_path: Optional[str] = Field(default=None, min_length=1, max_length=300)
    argv: Optional[list[str]] = Field(default=None, min_length=1, max_length=8)

    @field_validator("argv")
    @classmethod
    def _argv_item_len(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return None
        bad = next((a for a in v if len(a) > 100), None)
        if bad is not None:
            raise ValueError("单个命令参数不能超过 100 字符")
        return v


class CliCustomTestOut(BaseModel):
    """自定义命令「测试」结果（失败也是 200——失败是数据）：渲染近 24h 时间窗
    真跑一遍采集命令。结果同时落库（CliTool.test_*，徽标展示）。"""

    ok: bool
    latency_ms: Optional[int] = None
    items_count: Optional[int] = None
    error: Optional[str] = None


class CustomCliBody(BaseModel):
    """窄口自定义只读命令入参：argv 全静态 token（占位仅 {since}/{until}，禁
    {arg_*}——自定义行不进补采无值可灌），deny-list 特征词在 service 层拦
    （ValueError → 422；执行时静态 token 二次过闸，纵深防御）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=30)
    bin_path: str = Field(min_length=1, max_length=300)
    argv: list[str] = Field(min_length=1, max_length=8)

    @field_validator("argv")
    @classmethod
    def _argv_item_len(cls, v: list[str]) -> list[str]:
        bad = next((a for a in v if len(a) > 100), None)
        if bad is not None:
            raise ValueError("单个命令参数不能超过 100 字符")
        return v


# ---- AI 智能体（/api/ai，2026-09-20 增，阶段二）----
# 记忆画像 / 会话消息 / 建议日志三表契约；推理逻辑在 agents/（手写区），本节只管
# 数据读写形状。载荷字段 snake_case 直消费（同 models/mcp 节简化口径，不做适配层）。

MemorySource = Literal["ai_reflect", "user_manual", "chat"]  # 规则来源：习惯学习官产出 / 用户手写 / 对话明示偏好（2026-11-03 补 chat——chat_config 早已写该值而 Literal 漏登，与 ProposalKind 启动 500 同型病，根治）
ProposalKind = Literal["schedule", "preflight", "energy_tag", "inbox", "delete", "question", "prep", "hardtask", "brief", "settings_change", "minutes", "minutes_invite"]  # 建议：排期草案 / 会前准备清单 / 强度标注 / 收件箱清单（2026-10-14 增）/ 删除确认 / 对话式问询（冲突·缺时间·相似待确认，2026-10-17 增，不作卡渲染）/ 会前准备清单卡 / 高难任务协助卡 / 定时简报卡（2026-10-20 增后三值）/ 对话设置撤销锚（2026-10-22 批 3 落地时 chat_config 已写该 kind 而 Literal 漏登——真机首轮库内出现该行后 GET /proposals 整体 500，2026-11-02 补）/ 会后纪要行动项卡（2026-11-10 增）/ 会后纪要邀请卡（批 3 tick 流）
ProposalOutcome = Literal["accepted", "modified", "rejected", "ignored", "expired"]
MessageRole = Literal["user", "assistant"]


class MemoryRuleOut(BaseModel):
    """记忆画像响应体（白盒信任基石：evidence 必须可见）。structured_anchor
    非空 = 该规则已生效为排期硬约束（优化批 8：设置页据此显「硬约束」徽标）；
    slot 供设置页槽位分组、observation_count 供验证级徽标（2026-11-03 记忆重构）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    content: str
    slot: Optional[str] = None
    source: MemorySource
    confidence: float
    evidence: Optional[str] = None
    structured_anchor: Optional[dict] = None
    last_confirmed_at: Optional[datetime] = None
    observation_count: int = 0
    created_at: datetime
    updated_at: datetime


class MemoryRuleCreate(BaseModel):
    """用户手写规则入参（设置 → 记忆画像）：source 恒 user_manual（ai_reflect 只经
    习惯学习官落库，不走本入口）；confidence 恒 1.0（亲笔即最高优先）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=30)
    content: str = Field(min_length=1, max_length=200)
    evidence: Optional[str] = Field(default=None, max_length=200)  # 手写可空（亲笔不需要证据链）

    @field_validator("evidence")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        """trim 后空串归一 None（同 ModelProviderCreate.api_key 口径）。"""
        return None if v == "" else v


class MemoryRuleUpdate(BaseModel):
    """记忆规则部分更新入参：键不出现 = 不动；evidence 显式 null = 清空。
    用户手动编辑 AI 推导行时 service 层自动升 source=user_manual / confidence=1.0
    （2026-11-03 记忆重构：亲笔改动即最高裁决）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    category: str | None = Field(default=None, min_length=1, max_length=30)
    content: str | None = Field(default=None, min_length=1, max_length=200)
    evidence: Optional[str] = Field(default=None, max_length=200)

    @field_validator("evidence")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return None if v == "" else v


class RuleConflictCheckIn(BaseModel):
    """手动新增/编辑保存前的冲突预检入参（2026-11-03 记忆重构，用户拍板「冲突
    提示用户确认」形态）：exclude_id = 编辑场景排除自身（新值为旧值本身不冲突）。"""

    category: str = Field(min_length=1, max_length=30)
    content: str = Field(min_length=1, max_length=200)
    exclude_id: Optional[str] = None


class RuleConflictItem(BaseModel):
    """单条疑似冲突规则（相似度 ≥0.6，前端弹「合并覆盖 / 两者并存」二选一）。"""

    id: str
    category: str
    content: str
    source: MemorySource
    similarity: float


class RuleConflictCheckOut(BaseModel):
    """冲突预检结果：conflicts 空数组 = 无冲突可直接保存。"""

    conflicts: list[RuleConflictItem]


class EpisodicFactOut(BaseModel):
    """情节事实响应体（Tier 2 白盒清单：设置页展示 + 手动删除）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    topic: str
    fact: str
    source: str
    confidence: float
    expires_at: Optional[datetime] = None
    created_at: datetime


class MessageOut(BaseModel):
    """会话消息响应体（助手面板启动加载历史）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: MessageRole
    content: str
    proposal_id: Optional[str] = None
    source: Optional[str] = None  # 消息来源渠道（None=桌面端 / feishu / dingtalk）
    created_at: datetime


class ChatIn(BaseModel):
    """对话入参（POST /api/ai/chat，响应为 SSE 事件流）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=2000)


class MinutesIn(BaseModel):
    """会后纪要提炼入参（POST /api/ai/meeting-minutes，2026-11-10 增）。
    task_id 可空——聊天纯文本入口无关联会议；text 为用户自己记录的纪要原文
    （提取不生成，README 反定位红线：AI 只提炼行动项，不转写不代笔）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    task_id: Optional[str] = None
    text: str = Field(min_length=10, max_length=8000)


class SessionCompactOut(BaseModel):
    """手动压缩响应体（POST /api/ai/session/compact，/compact 指令直调）。
    reason 词汇表沿用 chat error 帧口径：no_model / disabled / partial（块级
    中断但部分成果已落库，再执行 /compact 从游标断点续压）/ error。"""

    compacted: int = 0  # 本轮并入滚动摘要的消息条数
    reason: Optional[str] = None  # None = 成功执行到无待摘要


class ProposalOut(BaseModel):
    """建议日志响应体。payload 结构随 kind 而异（schedule 带 draft_plan、preflight
    带 checklist），前端按 kind 分支渲染。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: ProposalKind
    payload: dict
    outcome: Optional[ProposalOutcome] = None  # 空 = 待处理
    created_at: datetime
    resolved_at: Optional[datetime] = None


class ProposalOutcomeIn(BaseModel):
    """建议回写入参（采纳/拒绝/忽略）。reason 仅拒绝时填写（习惯学习官的学习信号，
    并入 payload.reject_reason）。applied_entries（2026-11-01 优化批 4，modified
    回流）：修改后采纳时前端传编辑后的条目清单——后端与 payload 快照比对生成
    modified_diff 并列新键（原快照不回写），反思链据此学习「用户改成了什么」。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    outcome: ProposalOutcome
    reason: Optional[str] = Field(default=None, max_length=200)
    applied_entries: Optional[list[dict]] = Field(default=None, max_length=50)


class ProposalSettleItemsIn(BaseModel):
    """条目级部分结算入参（2026-10-24 数据源页单条操作批）：indexes = 卡内条目
    序号（0 起，去重后至少 1 个）；outcome 限定三值——expired/ignored 是系统语义
    不开放给条目结算。卡清空时才按 outcome 回写整卡。applied_entries 同
    ProposalOutcomeIn（modified 回流，按 indexes 对应条目比对）。"""

    indexes: list[int] = Field(min_length=1, max_length=50)
    outcome: Literal["accepted", "modified", "rejected"]
    applied_entries: Optional[list[dict]] = Field(default=None, max_length=50)


# ---- 数据采集（/api/ingest，2026-10-14 增）----
# 每源采集配置（GET/PUT /api/ingest/sources）；采集动作本身是 POST /api/ingest/run
# （202 + 既有 ingest 广播呈现，无响应体契约）。


class IngestSourceOut(BaseModel):
    """采集源状态响应体：游标 / 健康度 / 配置三合一 + 可用工具清单（补采与配置
    下拉共用一份数据）。available_tools 空表示该源尚未探测到工具清单（先点卡上
    「测试」刷新）。service 层返回 dict（list_ingest_sources），model_validate 直填。"""

    key: str  # 源稳定键（口径见 IngestSource.source_key 注释；与 fetch_recent 条目的 key 一致）
    source: str  # 显示名（本地文件夹 / Composio 应用名 / MCP 服务名 / CLI 适配器名）
    kind: str  # local_folder / composio / mcp / cli
    enabled: bool
    trusted: bool  # 高可信直通开关（2026-10-24）：todo/calendar 域条目免确认直落
    fetch_tool: Optional[str] = None
    fetch_args: Optional[dict] = None
    window_hours: Optional[int] = None
    pause_until: Optional[datetime] = None
    fail_streak: int
    last_error: Optional[str] = None
    last_ok_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    available_tools: list[dict] = []  # [{name, description}]


class IngestSourceUpdate(BaseModel):
    """每源采集配置部分更新入参：键不出现 = 不动。fetch_tool 空串归 None
    （回落"跟随内置映射"）；fetch_args 显式 null = 清除覆盖。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: bool | None = None
    trusted: bool | None = None  # 高可信直通开关（2026-10-24）：键不出现 = 不动
    fetch_tool: Optional[str] = Field(default=None, max_length=100)
    fetch_args: Optional[dict] = None
    window_hours: Optional[int] = Field(default=None, ge=1, le=168)

    @field_validator("fetch_tool")
    @classmethod
    def _empty_tool_to_none(cls, v: str | None) -> str | None:
        return None if v == "" else v


# .ics 日历文件导入（POST /api/ingest/ics，2026-11-11 批）：前端 Tauri 对话框选
# 路径、后端读盘（与备份恢复 BackupRequest.path 同构）；导入语义是"默认信任直落"
# ——不走数据源建议结算，详细口径见 services/ics_import.py 模块头。


class IcsImportRequest(BaseModel):
    """导入请求：本地 .ics 文件绝对路径。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    path: str = Field(min_length=4, max_length=500)


class IcsImportOut(BaseModel):
    """导入结果汇总（弹窗结果行数据源）：parsed_* 是文件内组件总数（含坏/重/降级），
    created_* 是实际落库数；差值去向见三个 skipped/degraded 计数。"""

    ok: bool  # 恒 True（失败走 422）
    calendar: str  # 日历名（X-WR-CALNAME 优先，文件名 stem 兜底）
    parsed_events: int
    parsed_todos: int
    created_events: int  # 落库日程数（start_time 非空）
    created_todos: int  # 落库待办数（start_time 空）
    skipped_duplicates: int  # 指纹命中（库内或文件内重复）跳过
    skipped_invalid: int  # 坏条目跳过（缺字段/时间非法/已取消/系列例外行等）
    degraded_recurring: int  # 重复规则无法精确表达 → 降级单次（仍创建）


# ---- 定时任务（/api/jobs，2026-10-20 增）------------------------------------------
# 内置任务注册表（GET /api/jobs：文案后端单源，前端不硬编码）+ 自定义每日简报
# CRUD（/api/jobs/briefs：≤3 条 / 同刻唯一 / 30 分钟粒度，违规 ValueError→422）。


class JobOut(BaseModel):
    """单个定时任务行：静态描述 + 运行状态。key 稳定（morning / ingest / nightly /
    prep / hardtask / heartbeat / brief:<uuid>）；time_min 空 = 非时刻驱动（心跳）；
    last_run_at 取 workflow_runs 最近认领时刻（无记录 None，不谎报成功）。"""

    key: str
    title: str
    desc: str
    enabled: bool
    requires_ai: bool  # True = 受 ai_enabled 总闸（关时页面盖「已随 AI 关闭」角标）
    time_min: int | None = None  # 自 0 点起的分钟数；None = 非时刻驱动
    time_note: str | None = None  # 时刻之外的补充说明（如"空闲时""每 3 小时"）
    last_run_at: Optional[datetime] = None


class JobsOut(BaseModel):
    jobs: list[JobOut]


class CustomBriefOut(BaseModel):
    """自定义每日简报行响应体。"""

    model_config = ConfigDict(from_attributes=True)  # 允许直接从 ORM 对象构造

    id: str
    brief_min: int
    instruction: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class CustomBriefCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    brief_min: int = Field(ge=0, le=1440)
    instruction: str = Field(min_length=1, max_length=200)


class CustomBriefUpdate(BaseModel):
    """部分更新入参：键不出现 = 不动。enabled=False 停用保留行。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    brief_min: int | None = Field(default=None, ge=0, le=1440)
    instruction: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None


# —— 统计报告（2026-11-06 批 D）——


class StatsReportOut(BaseModel):
    """AI 周/月报留档行（summary 聚合是自由 dict 不设契约模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    period_type: str  # week | month
    period_start: datetime
    content: str  # Markdown 正文
    payload: dict | None = None  # 结构化留档：advice_items / health 快照 / keep_habits
    created_at: datetime


# —— 关于页（2026-11-06 批 A）：检查更新 / 备份导出 / 恢复准备 ——


class LatestReleaseOut(BaseModel):
    """GitHub 最新 Release 探测结果。available=False 时 reason 必填（前端直显）。"""

    available: bool
    tag: str | None = None
    html_url: str | None = None
    reason: str | None = None


class BackupRequest(BaseModel):
    """导出入口：path 来自前端原生保存对话框；version 前端 getVersion() 传回
    （后端不知道应用版本号），仅写入 manifest 留档。"""

    path: str = Field(min_length=4, max_length=500)
    version: str | None = Field(default=None, max_length=40)


class BackupOut(BaseModel):
    ok: bool
    size_bytes: int
    files: list[str]


class RestoreRequest(BaseModel):
    """恢复入口：path 来自前端原生文件选择对话框（zip 过滤）。"""

    path: str = Field(min_length=4, max_length=500)


class RestoreOut(BaseModel):
    """恢复第一阶段结果：恒要求重启（打包版走 Rust restart_backend，
    dev 形态提示手动重启 uvicorn）。"""

    ok: bool
    requires_restart: bool
    message: str
