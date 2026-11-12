// 应用设置的前端形状与主题驱动（"通用偏好"9 项）。
// 时间口径：前端统一消费小时小数（与时间轴 11.5 这类值同源），后端存分钟整数，
// 换算双程收敛在 api.ts 的 toFrontendSettings / toSettingsPatch。
// 枚举字面量与后端 schemas.py 的 Literal 定义对齐（CloseAction/ThemeMode/NotifyStyle/Sound）。

export type CloseAction = "hide" | "quit";
export type ThemeMode = "system" | "light" | "dark";
export type DefaultView = "day" | "week" | "month"; // 与 ScheduleCanvas 的 viewMode 同构
export type NotifyStyle = "system" | "floating" | "corner";
export type Sound = "chime" | "bowl" | "water" | "off";
// AI 回复风格四档（balanced = 默认均衡，零提示词注入）
export type AiStyle = "balanced" | "concise" | "detailed" | "rigorous";

export type Settings = {
  // 系统与启动
  autoStart: boolean;
  closeAction: CloseAction;
  // 外观
  themeMode: ThemeMode;
  defaultView: DefaultView;
  // 小部件透明度（%，10–80，越大越透；tint alpha = 1 - t/100，20 即 0.80）
  widgetTransparency: number;
  // 日历与排期（小时小数：dayStart=8 / lunchStart=11.5 / workHours=8）
  dayStart: number;
  dayEnd: number;
  lunchEnabled: boolean;
  lunchStart: number;
  lunchEnd: number;
  workHours: number;
  // 提醒与通知（提醒调度器未实现，先存偏好）
  reminderLeadMin: number | null; // null = 不提醒；0 = 准时
  notifyStyle: NotifyStyle;
  sound: Sound;
  // AI 助手（阶段二）：主开关 + 晨间简报时刻（小时小数，默认 8.5 = 08:30）
  // + 每日数据采集（开关默认关；时刻默认 9 = 09:00；轮数 1-4 档默认 2，
  //   后续槽当天顺延，无新数据轮静默）
  // + inbox 建议自动收录（M2-③，默认关，开启资格后端校验）
  // + 采集 LLM 调用日上限（M §十-3，0 = 不限，默认 30）
  aiEnabled: boolean;
  morningBriefAt: number;
  ingestEnabled: boolean;
  ingestAt: number;
  ingestRunsPerDay: number;
  ingestLlmBudget: number;
  // 夜间工作流报点时刻（小时小数，默认 20 = 20:00；夜间整理/会前准备/高难协助
  // 三流共用门槛，前两者无空闲条件）
  nightlyStartAt: number;
  // 四流独立开关（2026-10-21 复查批 D1）：与 aiEnabled 正交，关 = 对应流永不触发
  morningEnabled: boolean;
  nightlyEnabled: boolean;
  prepEnabled: boolean;
  hardtaskEnabled: boolean;
  // 周报自动生成（2026-11-07 统计 120 分档）：周日晚随夜间报点（复用
  // nightlyStartAt），与 nightlyEnabled 正交；默认开
  weeklyReportEnabled: boolean;
  // 会后纪要邀请（2026-11-10 批 3）：重要会议结束 15~120 分钟内询问补记纪要
  //（每日至多一次）；默认开，与 nightlyEnabled 正交
  minutesEnabled: boolean;
  // 对话上下文自动压缩开关（2026-10-26）：关 = 轮末后台压缩早退（仅 /compact
  // 手动可用）；默认开维持既有滚动摘要行为
  chatAutocompactEnabled: boolean;
  coachProbeDeep: boolean; // 教练溯源深探（优化批 9，默认关）
  assistantChime: boolean; // 助手消息轻提示音（2026-11-04 通知分轨，默认关）
  // 排期天气对照城市（空 = 停用；Open-Meteo geocoding 解析坐标）
  weatherCity: string | null;
  // AI 个性化（2026-10-20）：回复风格 + 内置管理哲学（预设 id 数组，后端逗号串）
  // + 自定义哲学文本（null = 未配置）
  aiStyle: AiStyle;
  aiPhilosophy: string[];
  aiPhilosophyCustom: string | null;
};

// 默认值（与后端 services/settings.py 的 DEFAULTS 同源；后端未起时的兜底初值）
export const DEFAULT_SETTINGS: Settings = {
  autoStart: false,
  closeAction: "hide",
  themeMode: "system",
  defaultView: "day",
  widgetTransparency: 20,
  dayStart: 8,
  dayEnd: 22,
  lunchEnabled: true,
  lunchStart: 11.5,
  lunchEnd: 13.5,
  workHours: 8,
  reminderLeadMin: 15,
  notifyStyle: "floating",
  sound: "off",
  aiEnabled: true,
  morningBriefAt: 8.5,
  ingestEnabled: false,
  ingestAt: 9,
  ingestRunsPerDay: 2,
  ingestLlmBudget: 30,
  nightlyStartAt: 20,
  morningEnabled: true,
  nightlyEnabled: true,
  prepEnabled: true,
  hardtaskEnabled: true,
  weeklyReportEnabled: true,
  minutesEnabled: true,
  chatAutocompactEnabled: true,
  coachProbeDeep: false,
  assistantChime: false,
  weatherCity: null,
  aiStyle: "balanced",
  aiPhilosophy: [],
  aiPhilosophyCustom: null,
};

/**
 * 主题驱动：dark 直取深色；system 跟随系统偏好（matchMedia 实时值）。
 * 通过 html[data-theme="dark"] 挂钩 tokens.css 的暗色变量套；亮色移除属性回落 :root。
 * App 在 themeMode 变化与系统偏好变化时都调它（监听清理在调用方 effect）。
 */
export function applyTheme(mode: ThemeMode): void {
  const dark =
    mode === "dark" ||
    (mode === "system" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (dark) {
    document.documentElement.dataset.theme = "dark";
  } else {
    delete document.documentElement.dataset.theme;
  }
}
