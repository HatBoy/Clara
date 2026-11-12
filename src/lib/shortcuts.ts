// 全局快捷键的前端侧元数据与动作总线。
//
// 触发链路（单一方向）：用户按下 → Rust handler 反查绑定表得 action id →
// 窗口级动作 Rust 直接执行 / 其余 show_main 后 emit "app-shortcut" → App.tsx
// 订阅处理（视图切换、右栏折叠等在 App 自持 state 的动作）→ 组件级动作经本文件
// 的 window CustomEvent 总线转派给 ScheduleCanvas / QuickAdd / CopilotPanel
// （App 与它们之间没有既有 props 通路，建新事件总线框架不值，一次 dispatch 够用）。
//
// 本表的 id 与默认键串必须与 Rust `SHORTCUT_DEFAULTS`（src-tauri/src/lib.rs）逐行
// 对齐——Rust 侧是落库与注册的权威源，本表只为设置页提供中文名称与分组展示。
export type ShortcutGroupId = "window" | "view" | "compose" | "data";

export interface ShortcutActionMeta {
  id: string;
  /** 设置页行首名称 */
  label: string;
  group: ShortcutGroupId;
  /** 默认键串（"恢复默认"用，与 Rust 侧同源同值） */
  defaultCombo: string;
  /** 设置页行副文案（语义说明：是否唤窗、光标落点等） */
  desc: string;
}

export const SHORTCUT_GROUPS: { id: ShortcutGroupId; label: string }[] = [
  { id: "window", label: "窗口" },
  { id: "view", label: "视图与导航" },
  { id: "compose", label: "录入与助手" },
  { id: "data", label: "数据" },
];

export const SHORTCUT_ACTIONS: ShortcutActionMeta[] = [
  {
    id: "toggle-main",
    label: "显示 / 隐藏主窗口",
    group: "window",
    defaultCombo: "Ctrl+Alt+C",
    desc: "窗口在前台时隐藏，否则唤回并聚焦",
  },
  {
    id: "toggle-widget",
    label: "显示 / 隐藏桌面小部件",
    group: "window",
    defaultCombo: "Alt+\\",
    desc: "与托盘菜单同一动作",
  },
  {
    id: "view-schedule",
    label: "切换到日程",
    group: "view",
    defaultCombo: "Ctrl+Alt+F1",
    desc: "窗口隐藏时先唤回",
  },
  {
    id: "view-inbox",
    label: "切换到收件箱",
    group: "view",
    defaultCombo: "Ctrl+Alt+F2",
    desc: "窗口隐藏时先唤回",
  },
  {
    id: "view-datasource",
    label: "切换到数据源",
    group: "view",
    defaultCombo: "Ctrl+Alt+F3",
    desc: "窗口隐藏时先唤回",
  },
  {
    id: "toggle-right",
    label: "展开 / 关闭右侧栏",
    group: "view",
    defaultCombo: "Ctrl+Alt+R",
    desc: "日程页即日程助手与右侧待办的开合",
  },
  {
    id: "view-day",
    label: "日视图",
    group: "view",
    defaultCombo: "Ctrl+Alt+D",
    desc: "切换日历视图档位",
  },
  {
    id: "view-week",
    label: "周视图",
    group: "view",
    defaultCombo: "Ctrl+Alt+W",
    desc: "切换日历视图档位",
  },
  {
    id: "view-month",
    label: "月视图",
    group: "view",
    defaultCombo: "Ctrl+Alt+M",
    desc: "切换日历视图档位",
  },
  {
    id: "go-today",
    label: "回到今天",
    group: "view",
    defaultCombo: "Ctrl+Alt+T",
    desc: "与工具栏「今天」按钮同一动作",
  },
  {
    id: "open-search",
    label: "打开搜索",
    group: "view",
    defaultCombo: "Ctrl+Alt+F",
    desc: "唤回主窗并打开搜索（页内等价 Ctrl+K）",
  },
  {
    id: "prev-period",
    label: "日历向前翻一段",
    group: "view",
    defaultCombo: "Alt+[",
    desc: "日视图 ±1 天 / 周 ±7 天 / 月 ±1 月",
  },
  {
    id: "next-period",
    label: "日历向后翻一段",
    group: "view",
    defaultCombo: "Alt+]",
    desc: "日视图 ±1 天 / 周 ±7 天 / 月 ±1 月",
  },
  {
    id: "quick-add",
    label: "添加日程",
    group: "compose",
    defaultCombo: "Ctrl+Alt+N",
    desc: "唤回主窗并把光标定位到快捷录入框",
  },
  {
    id: "open-assistant",
    label: "进入日程助手",
    group: "compose",
    defaultCombo: "Ctrl+Alt+Z",
    desc: "唤回主窗、展开助手侧栏、光标落到对话框",
  },
  {
    id: "sync-now",
    label: "手动同步数据源",
    group: "data",
    defaultCombo: "Ctrl+Alt+S",
    desc: "不唤窗，后台跑一次全源采集，结果以系统通知回执",
  },
];

// 组件级动作的进程内总线（不经 Tauri——同一 WebView 内 App 与组件直接通信）
const CMD_EVENT = "clara-shortcut-cmd";

/** App 转发组件级动作（同一事件带 action id，订阅方各取所需） */
export function dispatchShortcutCmd(action: string): void {
  window.dispatchEvent(new CustomEvent(CMD_EVENT, { detail: action }));
}

/** 订阅某个组件级动作，返回清理函数（effect 直接 return 它） */
export function onShortcutCmd(action: string, handler: () => void): () => void {
  const onCmd = (e: Event) => {
    if ((e as CustomEvent<string>).detail === action) handler();
  };
  window.addEventListener(CMD_EVENT, onCmd);
  return () => window.removeEventListener(CMD_EVENT, onCmd);
}

// 搜索弹层 → 画布跳转（2026-11-06 搜索批）：点搜索结果的日程行后切日视图并定位
// 该日期。带载荷（dateISO）不走上面的字符串总线，单独一对事件；消费方
// ScheduleCanvas 无条件挂载（常驻），无 sync-now 那种条件挂载竞态。
const JUMP_EVENT = "clara-canvas-jump";

/** 请求画布定位：dateISO = "YYYY-MM-DDTHH:mm:ss" 形态（naive 本地） */
export function dispatchCanvasJump(dateISO: string): void {
  window.dispatchEvent(new CustomEvent(JUMP_EVENT, { detail: dateISO }));
}

/** 订阅画布跳转，返回清理函数 */
export function onCanvasJump(handler: (dateISO: string) => void): () => void {
  const onJump = (e: Event) => handler((e as CustomEvent<string>).detail);
  window.addEventListener(JUMP_EVENT, onJump);
  return () => window.removeEventListener(JUMP_EVENT, onJump);
}
