/**
 * 任务写路径的乐观变换单源（2026-11-17 架构批 4）。
 *
 * 「this/following 挖空截断」8 行模式此前在 App.tsx ×3 与 WidgetWindow.tsx ×2
 * 逐字复刻，「patch 合并 + 清规则连带清挖空」×4，「排期时刻重算」×2——改一次
 * 后端语义（如 until 口径）要同步五处。此处收敛为纯函数（无 React 依赖、可直测），
 * 两窗的 handler 只保留各自的状态接线 / 跨窗通知 / 请求与自愈编排。
 */
import { durationText, shiftDateKey, type Task } from "../data/tasks";

/** 重复系列范围三问（拖拽/浮层/删除共用形态） */
export type SeriesOpts = { mode: "this" | "following" | "all"; date: string };

/**
 * 系列行的乐观挖空/截断：this = 该日虚拟实例挖空（追加 cancelledDates，系列行
 * 保留）；following = 老系列 UNTIL 截断到被改/被删日前一天（承接行由后端生成，
 * 等响应再 upsert）；all / 无 opts 原样返回（走其他乐观分支）。
 */
export function carveSeriesRow(t: Task, opts?: SeriesOpts): Task {
  if (!opts) return t;
  if (opts.mode === "this") {
    return { ...t, cancelledDates: [...(t.cancelledDates ?? []), opts.date] };
  }
  if (opts.mode === "following") {
    return t.recurrence
      ? { ...t, recurrence: { ...t.recurrence, until: shiftDateKey(opts.date, -1) } }
      : t;
  }
  return t;
}

/**
 * patch 乐观合并：显式清空重复规则（recurrence: undefined）时连带清挖空日期——
 * 后端聚合只对带规则的行生成 cancelled_dates，保留会让已排期任务被 occursOn
 * 挖空在唯一实例日（全隐身）。
 */
export function mergeTaskPatch(t: Task, patch: Partial<Task>): Task {
  if ("recurrence" in patch && patch.recurrence === undefined) {
    return { ...t, ...patch, cancelledDates: undefined };
  }
  return { ...t, ...patch };
}

/**
 * 排期时刻乐观替换：start/end 齐备时本地重算 minutes 与展示文本（与后端
 * duration 派生不变式同口径，拉伸后收件箱时长展示即刻跟上）；end === undefined
 * ⟺ 原本就无结束时刻，minutes/duration 不动（规则只约束三值齐备态）。
 * dateOffset 缺省保原基准（widget 拖动场景：重复系列基准日多在过去，写实例日
 * 0 会丢早期实例）。
 */
export function applyScheduleTime(
  t: Task,
  start: number,
  end: number | undefined,
  dateOffset?: number,
): Task {
  const minutes = end !== undefined ? Math.round((end - start) * 60) : undefined;
  return {
    ...t,
    ...(dateOffset !== undefined ? { dateOffset } : {}),
    start,
    end,
    ...(minutes !== undefined ? { minutes, duration: durationText(minutes) } : {}),
  };
}

/**
 * 删除的乐观行集变换：this/following 保行挖空截断（系列行保留）、all/无 opts
 * 整行移除（后端按生命周期判定物理删/软删）。
 */
export function dropOrCarveTask(
  list: Task[],
  taskId: string,
  opts?: SeriesOpts,
): Task[] {
  const keepRow = opts?.mode === "this" || opts?.mode === "following";
  return keepRow
    ? list.map((t) => (t.id === taskId ? carveSeriesRow(t, opts) : t))
    : list.filter((t) => t.id !== taskId);
}
