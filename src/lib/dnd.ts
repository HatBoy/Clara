// 待办拖拽共享工具：InboxPanel（拖拽源）、ScheduleCanvas（日历放置目标）、Sidebar（项目放置目标）三方共用
import type { DragEvent as ReactDragEvent } from "react";

// 自定义 MIME：dragover 里用 types.includes 判别，外部拖拽（文件等）不触发
export const TODO_MIME = "application/x-clara-todo";

// 待办经 dataTransfer 传递的载荷（InboxPanel onDragStart 序列化，放置端解析）。
// start = 已排期条目的原起始时刻（小时小数，2026-11-12 agenda 拖拽批增）：
// agenda 日程行拖动跨日改期的标记——带 start 的载荷落日卡时保时刻改期而非
// 重新排期；收件箱/数据源拖入不带（undefined = 既有排期链路）。
// rec/fromOffset = 重复系列行标记与原实例日（2026-11-12 拖拽三问批）：drop 侧
// 据此弹范围三问（仅此/及后续/所有）而非静默重锚定全系列
export type DraggedTodo = {
  id: string;
  title: string;
  minutes: number | null;
  start?: number;
  rec?: boolean;
  fromOffset?: number;
};

// 数据源建议条目的附加载荷（2026-10-24 直排批）：数据源页条目卡拖向日历时随
// TODO_MIME 一并携带——面板侧组装好建任务所需全部字段（溯源/优先级/备注），
// 放置端（App.handleSuggestDrop）零查找直接建任务 + settle-items 结算
export type SuggestedDrop = {
  proposalId: string;
  index: number;
  duration: number | null;
  priority: number | null;
  notes: string | null;
  source: { source_key: string; fingerprint?: string } | null;
};

export type DraggedTodoWithSuggest = DraggedTodo & { suggest?: SuggestedDrop };

// dragover 阶段判别是否待办拖拽（此阶段浏览器禁止 getData，只能查 types）
export function isTodoDrag(e: ReactDragEvent): boolean {
  return e.dataTransfer.types.includes(TODO_MIME);
}

// drop 阶段解析载荷：getData + JSON.parse + 逐字段类型校验，失败返回 null（调用方静默放弃）
export function parseTodoDrag(e: ReactDragEvent): DraggedTodoWithSuggest | null {
  try {
    const raw = e.dataTransfer.getData(TODO_MIME);
    if (!raw) return null;
    const data = JSON.parse(raw) as {
      id?: unknown;
      title?: unknown;
      minutes?: unknown;
      start?: unknown;
      rec?: unknown;
      fromOffset?: unknown;
      suggest?: unknown;
    };
    if (typeof data.id !== "string" || typeof data.title !== "string") {
      return null;
    }
    const todo: DraggedTodoWithSuggest = {
      id: data.id,
      title: data.title,
      minutes: typeof data.minutes === "number" ? data.minutes : null,
    };
    if (typeof data.start === "number") todo.start = data.start;
    if (data.rec === true) todo.rec = true;
    if (typeof data.fromOffset === "number") todo.fromOffset = data.fromOffset;
    // suggest 附加载荷防御透传：结构不符静默丢弃（退化为普通待办拖拽，放置端
    // onScheduleChange 按 id 查不到任务 → 静默放弃，不炸）
    const s = data.suggest as Partial<SuggestedDrop> | null | undefined;
    if (
      s &&
      typeof s === "object" &&
      typeof s.proposalId === "string" &&
      typeof s.index === "number"
    ) {
      todo.suggest = s as SuggestedDrop;
    }
    return todo;
  } catch {
    return null;
  }
}
