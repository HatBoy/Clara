// 日程提醒悬浮卡片（"通知形式 = 居中悬浮框"时呈现）：入场淡入、到时淡出后移除。
// 纯受控展示组件——items 由消费侧（独立提醒窗口 ReminderWindow，或 App 的纯浏览器
// dev 页内兜底）管理；出列节拍（可见时长 / 淡出时长 / 离场标记）经 scheduleToastExit
// / beginToastExit 收口于此，两个消费侧共用同一口径。默认布局给独立提醒窗口用
//（窗口透明、定位于屏幕上方水平居中，容器满幅垂直居中）；inline 变体 = 主窗口
// 页内兜底（居中偏上）。
import { BellRing, Check, Clock, X } from "lucide-react";
import { toNaiveLocalISO, type ReminderEvent, type TaskPatchBody } from "../lib/api";
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
  taskId?: string; // 完成 / 推迟按钮的目标任务 id；空 = 无操作（伪提醒）
  startAtIso?: string; // 推迟 15 分钟的时刻换算锚（naive 本地 ISO）
  endAtIso?: string | null; // 结束时刻；null = 不限时（推迟只挪开始）
  series?: boolean; // 重复系列：隐藏"完成"按钮（V1 不支持系列完成），保留推迟
}

// 提醒卡操作（2026-10-22 批 2）：done = PATCH done:true 标记完成；
// snooze = start/end 各 +15 分钟改期（旧提醒行因改期校验自然失效，新时刻
// 由后端扫描按新口径重新入列——推迟期间到点会再次提醒）
export type ReminderAction = "done" | "snooze";

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
    leadText: (ev.lead_min ?? 0) > 0 ? `提前 ${ev.lead_min} 分钟` : "准时开始",
    projectName: ev.project_name ?? undefined,
    note: ev.prep_proposal_id ? "准备清单已生成 · 见助手面板" : undefined,
    taskId: real ? ev.task_id : undefined,
    startAtIso: real ? ev.start_at : undefined,
    endAtIso: real ? ev.end_at : undefined,
    series: real ? ev.recurrence : undefined,
  };
}

// 完成/推迟按钮的 PATCH 请求构造（2026-10-22 批 2）：两个消费侧（独立提醒窗 /
// 页内兜底）共用同一换算，避免 +15 分钟算术漂移成两套口径。推迟 = start/end
// 各 +15 分钟（跨天由 Date 算术自然进位）；系列实例用 mode=this 实体化单次例外
//（mode=all 会挪整个系列的基准时刻），date 取实例开始时刻的日期段。只做数据
// 构造不碰 API，保持本文件纯组件定位（toNaiveLocalISO 是 naive ISO 唯一口径，
// 防 toISOString 的 UTC 坑，必须复用不能手写）
export function toastActionPatch(
  item: ReminderToastItem,
  action: ReminderAction,
): { body: TaskPatchBody; opts?: { mode: "this"; date: string } } {
  if (action === "done") return { body: { done: true } };
  const SNOOZE_MIN = 15;
  const start = new Date(item.startAtIso ?? "");
  const end = item.endAtIso ? new Date(item.endAtIso) : null;
  const body: TaskPatchBody = {
    start_time: toNaiveLocalISO(new Date(start.getTime() + SNOOZE_MIN * 60_000)),
  };
  if (end) body.end_time = toNaiveLocalISO(new Date(end.getTime() + SNOOZE_MIN * 60_000));
  return {
    body,
    opts: item.series
      ? { mode: "this", date: (item.startAtIso ?? "").slice(0, 10) }
      : undefined,
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
                  onClick={() => onAction(it, "snooze")}
                >
                  <Clock size={12} strokeWidth={2} />
                  推迟 15 分
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
