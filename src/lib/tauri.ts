// Tauri 原生能力的调用守卫层：系统级行为（关闭窗口行为 / 开机自启 / 系统通知 /
// 提醒独立窗口）的 JS→Rust 通道。纯浏览器 dev（npm run dev 无 Tauri 宿主）下所有
// 调用直接 no-op + warn，不报错——设置数据本体仍照常走 HTTP 边车持久化，只是原生
// 效果缺席。
import { invoke } from "@tauri-apps/api/core";
import { emitTo, listen, type UnlistenFn } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { disable, enable } from "@tauri-apps/plugin-autostart";
import { open, save } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";
import type { ReminderEvent } from "./api";
import type { CloseAction } from "./settings";

// 是否运行在 Tauri 宿主内（WebView 注入的内部对象；纯浏览器恒 false）
export const isTauri = (): boolean => "__TAURI_INTERNALS__" in window;

/**
 * 把关闭主窗口行为同步给 Rust（CloseBehavior state，CloseRequested 拦截读它）。
 * App 启动拉到设置后调一次 + 设置页切换时再调；幂等。
 */
export function setCloseBehavior(action: CloseAction): void {
  if (!isTauri()) {
    console.warn("非 Tauri 环境，跳过关闭行为同步");
    return;
  }
  invoke("set_close_behavior", { behavior: action }).catch((e) =>
    console.error("同步关闭行为到 Rust 失败", e),
  );
}

/**
 * 把开机自启偏好同步到 OS（Windows 注册表 Run 键，autostart 插件落盘）。
 * App 启动拉到设置后对齐一次 + 设置页切换时再调；enable/disable 幂等。
 */
export async function syncAutostart(enabled: boolean): Promise<void> {
  if (!isTauri()) {
    console.warn("非 Tauri 环境，跳过开机自启同步");
    return;
  }
  try {
    if (enabled) {
      await enable();
    } else {
      await disable();
    }
  } catch (e) {
    console.error("同步开机自启失败", e);
  }
}

/**
 * 读 Tauri 侧服务端口配置（端口真源 = app_config_dir/server_port.json，Rust 管理，
 * 不进 SQLite——后端绑定端口先于读库，存库即死结）。非 Tauri 环境或读取失败返回
 * null，调用方保持默认 8000/8086。
 */
export async function fetchServerPort(): Promise<number | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<number>("get_server_port");
  } catch (e) {
    console.error("读取服务端口配置失败", e);
    return null;
  }
}

/**
 * 写 Tauri 侧服务端口配置（设置页"服务端口"行提交用；修改后重启应用生效——
 * 已在跑的边车不迁移）。返回是否成功。
 */
export async function setServerPort(port: number): Promise<boolean> {
  if (!isTauri()) {
    console.warn("非 Tauri 环境，跳过服务端口写入");
    return false;
  }
  try {
    await invoke("set_server_port", { port });
    return true;
  } catch (e) {
    console.error("写入服务端口配置失败", e);
    return false;
  }
}

// 左右栏宽记忆（真源 = Tauri 侧 layout.json，Rust 管理——窗口状态本就由 Rust
// 恢复，布局同域；后端缺席（端口冲突继续启动）时也能恢复）。main.tsx render
// 前调 loadPanelWidths() 填充，App 首渲染经 lazy useState 读到记忆值（ES live
// binding，无首帧跳变）；null = 无记忆 / 非 Tauri / 读取失败，App 用默认栏宽。
export let panelWidths: { leftWidth: number; rightWidth: number } | null = null;

/** render 前读取栏宽记忆填入 panelWidths（任何失败保持 null，不阻塞启动） */
export async function loadPanelWidths(): Promise<void> {
  if (!isTauri()) return;
  try {
    const saved = await invoke<{ left_width: number; right_width: number } | null>(
      "get_layout",
    );
    if (saved) panelWidths = { leftWidth: saved.left_width, rightWidth: saved.right_width };
  } catch (e) {
    console.error("读取栏宽记忆失败", e);
  }
}

/** 写栏宽记忆（App 栏宽变更防抖后调；非 Tauri no-op） */
export function savePanelWidths(leftWidth: number, rightWidth: number): void {
  if (!isTauri()) return;
  invoke("set_layout", { leftWidth, rightWidth }).catch((e) =>
    console.error("写入栏宽记忆失败", e),
  );
}

/**
 * 读小部件显隐记忆（真源 = Rust 侧 widget.json——显隐写入口三个：托盘 toggle /
 * CloseRequested 拦截 / X 按钮命令，两个在 Rust，localStorage 够不着）。非 Tauri /
 * 失败一律 false（与 Rust 读侧兜底同口径）。widget 几何恢复 effect 的 finally 中
 * 调用，true 则自窗 show（几何就绪后再显示，防默认位置闪现跳动）。
 */
export async function fetchWidgetVisible(): Promise<boolean> {
  if (!isTauri()) return false;
  try {
    return await invoke<boolean>("get_widget_visible");
  } catch (e) {
    console.error("读取小部件显隐状态失败", e);
    return false;
  }
}

/**
 * X 按钮关闭：Rust 写 visible=false + hide 原子完成；失败向上抛由调用方回落
 * JS 直调 hide（窗口关不掉比状态脏一拍更糟，状态由下一任一显隐入口自愈）。
 */
export async function hideWidgetPersisted(): Promise<void> {
  if (!isTauri()) return;
  await invoke("hide_widget");
}

// ── 全局快捷键（真源 = Rust 侧 shortcuts.json，与 layout/widget 同域不进 SQLite：
// Rust 启动即需读表注册热键，且后端缺席时窗口级热键必须照常可用。前端只读表与
// 改键，不参与触发——按下由 Rust handler 反查动作后 emit 到 main 窗口）──

/** 快捷键表（Rust ShortcutState 序列化形态）：键位表 + 注册失败集合——failed 内
 *  动作的绑定键被外部应用抢占，设置页标橙「被占用」引导改键（不做运行期自愈，
 *  改键成功或重启后清除）。 */
export interface ShortcutTable {
  bindings: Record<string, string>;
  failed: string[];
}

/** 读全部动作的键位表与注册失败集合。非 Tauri / 失败返回 null，
 *  调用方（快捷键设置页）据此提示「仅在桌面应用中可配置」。 */
export async function fetchShortcuts(): Promise<ShortcutTable | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<ShortcutTable>("get_shortcuts");
  } catch (e) {
    console.error("读取快捷键配置失败", e);
    return null;
  }
}

/** 改单个动作的键位：Rust 侧 unregister 旧键 + register 新键，成功才落盘。
 *  返回 null 表示成功，否则为可展示的错误文案（目标键被其他动作占用 / 注册失败
 *  ——如系统保留键或已被其他应用抢注，此时旧键保持不变）。 */
export async function updateShortcut(
  action: string,
  combo: string,
): Promise<string | null> {
  if (!isTauri()) return "仅在桌面应用中可配置";
  try {
    await invoke("update_shortcut", { action, combo });
    return null;
  } catch (e) {
    return typeof e === "string" ? e : "改键失败";
  }
}

/** 全表恢复默认键并重注册（failed 集合随之重算），返回恢复后的表；非 Tauri /
 *  失败返回 null */
export async function resetShortcuts(): Promise<ShortcutTable | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<ShortcutTable>("reset_shortcuts");
  } catch (e) {
    console.error("恢复默认快捷键失败", e);
    return null;
  }
}

/** 订阅 Rust 派发的窗内快捷键动作（事件载荷 = action id；返回取消订阅函数，
 *  非 Tauri 返回空函数，effect 清理安全——形态同 sync.ts onPeerSync） */
export function onAppShortcut(handler: (action: string) => void): Promise<UnlistenFn> {
  if (!isTauri()) return Promise.resolve(() => {});
  return listen<string>("app-shortcut", (e) => handler(e.payload));
}

/**
 * 发系统通知（"通知形式 = 系统通知"时日程提醒的呈现通道）。
 * 首次调用补权限请求（Windows 通常已授权，无感）；非 Tauri 环境 no-op + warn。
 */
export async function sendSystemNotification(title: string, body: string): Promise<void> {
  if (!isTauri()) {
    console.warn("非 Tauri 环境，跳过系统通知");
    return;
  }
  try {
    let granted = await isPermissionGranted();
    if (!granted) {
      granted = (await requestPermission()) === "granted";
    }
    if (granted) sendNotification({ title, body });
  } catch (e) {
    console.error("发送系统通知失败", e);
  }
}

/**
 * 系统文件夹选择对话框（数据接入页"本地文件夹"MCP 服务的"浏览"按钮）。
 * 返回选中目录的绝对路径；用户取消 / 非 Tauri / 失败均返回 null——调用方回落
 * 手输路径（纯浏览器 dev 下"浏览"按钮本身不渲染，此返回值是防御兜底）。
 */
export async function pickDirectory(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const picked = await open({ directory: true, multiple: false });
    return typeof picked === "string" ? picked : null;
  } catch (e) {
    console.error("打开文件夹选择对话框失败", e);
    return null;
  }
}

/**
 * 用系统默认浏览器打开外部链接（数据接入页"探索更多"指向 MCP Registry）。
 * Tauri 下必须走 opener 插件——WebView 内 <a href> 默认导航会把应用窗口本身
 * 带去外部站点；纯浏览器 dev 降级 window.open 新标签。失败仅日志（点不开不
 * 致命，用户可手动复制地址栏）。
 */
export function openExternal(url: string): void {
  if (!isTauri()) {
    window.open(url, "_blank", "noopener");
    return;
  }
  openUrl(url).catch((e) => console.error("打开外部链接失败", e));
}

// ── 数据备份（2026-11-06 批 A，关于页）──

/** 备份导出的保存对话框（含默认文件名）。返回用户选定路径；取消 / 非 Tauri /
 *  失败均 null——调用方据此放弃导出（dialog:default 权限组已含 allow-save）。 */
export async function saveBackupPath(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const picked = await save({
      defaultPath: `clara-backup-${today}.zip`,
      filters: [{ name: "Clara 备份包", extensions: ["zip"] }],
    });
    return typeof picked === "string" ? picked : null;
  } catch (e) {
    console.error("打开保存对话框失败", e);
    return null;
  }
}

/** 恢复入口的文件选择对话框（仅 zip）。返回选定路径；取消 / 非 Tauri / 失败 null。 */
export async function pickBackupZip(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const picked = await open({
      multiple: false,
      filters: [{ name: "Clara 备份包", extensions: ["zip"] }],
    });
    return typeof picked === "string" ? picked : null;
  } catch (e) {
    console.error("打开备份选择对话框失败", e);
    return null;
  }
}

/** ics 导入的文件选择对话框（仅 .ics）。返回选定路径；取消 / 非 Tauri / 失败 null。 */
export async function pickIcsFile(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const picked = await open({
      multiple: false,
      filters: [{ name: "日历文件", extensions: ["ics"] }],
    });
    return typeof picked === "string" ? picked : null;
  } catch (e) {
    console.error("打开日历选择对话框失败", e);
    return null;
  }
}

/** 统计报告导出的保存对话框（.md，默认文件名随报告周期，2026-11-07 120 分档）。
 *  返回用户选定路径；取消 / 非 Tauri / 失败均 null（调用方放弃导出）。 */
export async function saveStatsReportPath(defaultPath: string): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const picked = await save({
      defaultPath,
      filters: [{ name: "Markdown 文档", extensions: ["md"] }],
    });
    return typeof picked === "string" ? picked : null;
  } catch (e) {
    console.error("打开报告导出对话框失败", e);
    return null;
  }
}

/**
 * 备份恢复的进程编排（打包版）：杀本应用管理的边车 → 等端口释放 → 重新拉起，
 * 新进程启动时应用 restore_staging 完成数据替换。dev 形态 / 复用外部实例时
 * 返回错误文案（数据已就绪，只差重启——提示用户手动操作）。
 */
export async function restartBackend(): Promise<{ ok: boolean; message: string }> {
  if (!isTauri()) return { ok: false, message: "非桌面环境，请手动重启后端进程" };
  try {
    await invoke("restart_backend");
    return { ok: true, message: "" };
  } catch (e) {
    return { ok: false, message: typeof e === "string" ? e : "后端重启失败，请手动重启应用" };
  }
}

/**
 * 把提醒事件转发给独立悬浮窗（"通知形式 = 居中悬浮框"）并**由主窗口驱动显示**
 * ——事件 emitTo 与 getByLabel("reminder").show() 并行：主窗口最小化/被遮挡也能
 * 浮在屏幕中央；show 权限在 capabilities（core:window:allow-show，core:window:
 * default 不含操作类命令，缺它会静默失败——曾有 bug）。显隐控制权在主窗口，
 * 失败错误在主窗口 console 可见可诊断。返回是否成功；非 Tauri 环境或窗口已
 * 销毁返回 false（预期路径而非异常，调用方回落主窗口页内呈现）。
 */
export async function forwardReminderEvent(payload: ReminderEvent): Promise<boolean> {
  if (!isTauri()) return false;
  const win = await WebviewWindow.getByLabel("reminder");
  if (!win) return false; // reminder 窗口不存在（被销毁）→ 回落页内呈现
  try {
    await Promise.all([emitTo("reminder", "reminder-event", payload), win.show()]);
    return true;
  } catch (e) {
    console.error("转发提醒事件到悬浮窗失败", e);
    return false;
  }
}
