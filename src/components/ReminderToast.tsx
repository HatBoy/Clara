// 日程提醒悬浮卡片（"通知形式 = 居中悬浮框"时呈现）：入场淡入、到时淡出后移除。
// 纯受控展示组件——items 由消费侧（独立提醒窗口 ReminderWindow，或 App 的纯浏览器
// dev 页内兜底）管理；出列节拍（可见时长 / 淡出时长 / 离场标记）经 scheduleToastExit
// / beginToastExit 收口于此，两个消费侧共用同一口径。默认布局给独立提醒窗口用
//（窗口透明、定位于屏幕上方水平居中，容器满幅垂直居中）；inline 变体 = 主窗口
// 页内兜底（居中偏上）。
import { BellRing, Check, Clock, X } from "lucide-react";
import type { ReminderEvent } from "../lib/api";
import "./ReminderToast.css";

// 可见时长（入场后停留）与淡出时长：12s 兼顾看得清与不打扰（8→15→12 定）
export const TOAST_DISPLAY_MS = 12000;
export const TOAST_FADING_MS = 450;

export interface ReminderToastItem {
  key: number;
  title: string;
  timeText: string; // 实例开始 "HH:MM"
  leadText: string; // "准时开始" / "提前 N 分钟"
  projectName?: string;
  note?: string; // 附加副文案（2026-10-20：会前准备已生成的会议引导去助手面板）
  leaving?: boolean; // 离场标记：挂上即播淡出动画，淡完由消费侧移除
  // —— 操作位（2026-10-22 批 2）：真实日程提醒才可操作（AI 工作流伪提醒
  // task_id 是 "ai-*" 假 id，PATCH 会 404——toToastItem 据此置空）——
  taskId?: string; // 完成 / 稍后按钮的目标任务 id；空 = 无操作（伪提醒）
  refStartIso?: string; // 原实例开始时刻（稍后提醒透传给后端做「原定」展示）
  series?: boolean; // 重复系列：隐藏"完成"按钮（V1 不支持系列完成），保留稍后
  manual?: boolean; // 已是「稍后提醒」事件（文案提示）
}

// 提醒卡操作（2026-11-17 批 1 改）：done = 标记完成；snooze10/60 = **只推迟
// 提醒、不移动日程**（调 /api/reminders/{id}/snooze，后端插 manual 提醒行）
export type ReminderAction = "done" | "snooze10" | "snooze60";

// ReminderEvent（SSE 载荷）→ 悬浮卡片条目：开始时刻与提前量文案在此换算，
// 两个消费侧（独立窗口 / 页内兜底）共用同一口径
export function toToastItem(ev: ReminderEvent, key: number): ReminderToastItem {
  const startAt = new Date(ev.start_at);
  const hh = String(startAt.getHours()).padStart(2, "0");
  const mm = String(startAt.getMinutes()).padStart(2, "0");
  const real = !ev.task_id.startsWith("ai-"); // AI 工作流伪提醒不可操作
  return {
    key,
    title: ev.title,
    timeText: `${hh}:${mm}`,
    // 仅声音事件（lead_min=null）在 App 分流后不会进队列，?? 0 作类型防御
    leadText: ev.manual
      ? "稍后提醒"
      : (ev.lead_min ?? 0) > 0 ? `提前 ${ev.lead_min} 分钟` : "准时开始",
    projectName: ev.project_name ?? undefined,
    note: ev.prep_proposal_id ? "准备清单已生成 · 见助手面板" : undefined,
    taskId: real ? ev.task_id : undefined,
    refStartIso: real ? ev.start_at : undefined,
    series: real ? ev.recurrence : undefined,
    manual: ev.manual === true,
  };
}

// 标记离场（淡出动画启动），淡完后从队列移除——X 手动关与到时自动关共用
export function beginToastExit(
  key: number,
  update: (fn: (prev: ReminderToastItem[]) => ReminderToastItem[]) => void,
): void {
  update((prev) => prev.map((t) => (t.key === key ? { ...t, leaving: true } : t)));
  window.setTimeout(
    () => update((prev) => prev.filter((t) => t.key !== key)),
    TOAST_FADING_MS,
  );
}

// 到时自动关：先停留 TOAST_DISPLAY_MS 再走离场淡出
export function scheduleToastExit(
  key: number,
  update: (fn: (prev: ReminderToastItem[]) => ReminderToastItem[]) => void,
): void {
  window.setTimeout(() => beginToastExit(key, update), TOAST_DISPLAY_MS);
}

function ReminderToast({
  items,
  onClose,
  onAction,
  inline = false,
  corner = false,
}: {
  items: ReminderToastItem[];
  onClose: (key: number) => void;
  // 完成/推迟按钮回调（2026-10-22 批 2）；未传则不渲染按钮（消费侧可退化纯展示）
  onAction?: (item: ReminderToastItem, action: ReminderAction) => void;
  inline?: boolean;
  corner?: boolean; // 右下角悬浮框：卡片贴窗口底、多卡向上堆叠（独立窗口用）
}) {
  if (items.length === 0) return null;
  return (
    <div
      className={`reminder-toasts${inline ? " inline" : ""}${corner ? " corner" : ""}`}
    >
      {items.map((it) => (
        <div key={it.key} className={`reminder-toast${it.leaving ? " leaving" : ""}`}>
          <span className="reminder-toast-icon">
            <BellRing size={18} strokeWidth={2} />
          </span>
          <div className="reminder-toast-body">
            <span className="reminder-toast-title">{it.title}</span>
            <span className="reminder-toast-meta">
              {it.timeText} · {it.leadText}
              {it.projectName ? ` · ${it.projectName}` : ""}
              {it.note ? ` · ${it.note}` : ""}
            </span>
            {onAction && it.taskId ? (
              <span className="reminder-toast-actions">
                {!it.series && (
                  <button
                    className="reminder-toast-act"
                    onClick={() => onAction(it, "done")}
                  >
                    <Check size={12} strokeWidth={2.5} />
                    完成
                  </button>
                )}
                <button
                  className="reminder-toast-act"
                  onClick={() => onAction(it, "snooze10")}
                >
                  <Clock size={12} strokeWidth={2} />
                  10 分钟后
                </button>
                <button
                  className="reminder-toast-act"
                  onClick={() => onAction(it, "snooze60")}
                >
                  <Clock size={12} strokeWidth={2} />
                  1 小时后
                </button>
              </span>
            ) : null}
          </div>
          <button
            className="reminder-toast-close"
            onClick={() => onClose(it.key)}
            aria-label="关闭提醒"
          >
            <X size={14} strokeWidth={2} />
          </button>
        </div>
      ))}
    </div>
  );
}

export default ReminderToast;
