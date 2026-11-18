/**
 * 任务数据外部 store（2026-11-17 架构批 6）：tasks/projects/loadedFromDay 的
 * 单源与两窗共用原语。
 *
 * 此前 App.tsx 与 WidgetWindow.tsx 各持 useState 副本——upsertTasks/upsertProjects
 * 逐字重复两份、peer 分派体同构、写 handler 的乐观 setTasks 散落 15 处。store 用
 * useSyncExternalStore 订阅（React 19，本仓首个自定义 hook 先例）；**每个 WebView
 * 窗口一个模块实例**（主窗/widget 是独立 JS 上下文），跨窗一致性仍靠 notifyPeer
 * 广播 + 各窗重拉自愈（协议见 lib/sync.ts，本 store 不碰网络广播——notifyPeer
 * 时机留在写 handler 编排层）。
 *
 * 变更原语（写路径乐观段直接消费，纯变换在 lib/taskWrites.ts）；重拉两形态
 * （主窗剪枝重拉 / widget 今日窗）内置 in-flight 序号守卫（StrictMode 双跑与
 * 竞态重入丢弃迟到结果——取代 effect 局部 cancelled 标记）。窗口左边界
 * loadedFromDay 收进 store 后，挂载一次的监听闭包经 store 读最新值——
 * loadedFromDayRef 的防陈旧补丁结构性消失。
 */
import { useSyncExternalStore } from "react";

import type { Project, Task } from "../data/tasks";
import { isScheduled } from "../data/tasks";
import {
  dayStartOf,
  epochDayOf,
  fetchProjects,
  fetchTasks,
  toFrontendProject,
  toFrontendTask,
} from "./api";
import type { SyncPayload } from "./sync";

// 启动历史回看天数（主窗窗口左边界初值）
export const INITIAL_LOOKBACK_DAYS = 30;

export type TaskStoreSnapshot = {
  tasks: Task[];
  projects: Project[];
  loadedFromDay: number;
};

let tasks: Task[] = [];
let projects: Project[] = [];
let loadedFromDay = epochDayOf(new Date()) - INITIAL_LOOKBACK_DAYS;
let snapshot: TaskStoreSnapshot = { tasks, projects, loadedFromDay };
const listeners = new Set<() => void>();

function commit(): void {
  snapshot = { tasks, projects, loadedFromDay };
  for (const cb of listeners) cb();
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function getSnapshot(): TaskStoreSnapshot {
  return snapshot;
}

export function useTaskStore(): TaskStoreSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}

// —— 变更原语 ——————————————————————————————————————————————————————————

/** 累积式 upsert：Map 按 id 合并（旧保位、新追加）。保序是收件箱"默认排序"
 *（后端 sort_order 序）的前提——该组只由 unscheduled 批构成，窗口批（start
 * 非空）不混入；不淘汰旧数据（翻回已加载窗口秒显，内存增长有界于翻过的范围）。 */
export function upsertTasks(incoming: Task[]): void {
  if (incoming.length === 0) return;
  const byId = new Map(tasks.map((t) => [t.id, t]));
  for (const t of incoming) byId.set(t.id, t);
  tasks = [...byId.values()];
  commit();
}

/** 项目累积式 upsert（口径同 upsertTasks）：Map 按标识合并保序（id 缺省回退
 * name——项目均来自后端 id 正常恒在）；桌面小部件同步（widget 侧建项目）入列用。 */
export function upsertProjects(incoming: Project[]): void {
  if (incoming.length === 0) return;
  const byId = new Map(projects.map((p) => [p.id ?? p.name, p]));
  for (const p of incoming) byId.set(p.id ?? p.name, p);
  projects = [...byId.values()];
  commit();
}

/** 项目全量替换（启动批 / 重拉批纠偏——重命名/删除/星标失败也靠这里回真值）。 */
export function setProjects(list: Project[]): void {
  projects = list;
  commit();
}

/** 项目列表函数式更新（新建入列防重 / 重命名 / 删除 / 星标的乐观段）。 */
export function updateProjects(fn: (prev: Project[]) => Project[]): void {
  projects = fn(projects);
  commit();
}

/** 按 id 移除（peer delete 载荷；本窗删除的乐观段走 mapTasks 整表变换）。 */
export function removeTasks(ids: string[]): void {
  const dead = new Set(ids);
  tasks = tasks.filter((t) => !dead.has(t.id));
  commit();
}

/** 单任务乐观变换（写 handler 的 this/following 挖空、patch 合并、时刻替换）。 */
export function mapTask(id: string, fn: (t: Task) => Task): void {
  tasks = tasks.map((t) => (t.id === id ? fn(t) : t));
  commit();
}

/** 列表级乐观变换（删除三分支的 dropOrCarveTask、项目重命名/删除的全表
 * project 字段重写）。 */
export function mapTasks(fn: (list: Task[]) => Task[]): void {
  tasks = fn(tasks);
  commit();
}

/** 历史窗口向左扩展（App handleVisibleRangeChange 消费）：批次 upsert + 边界
 * 左移同批落（触发条件清除无循环；调用方 fetch 失败不调本函数，边界不动，
 * 下次导航自然重试自愈）。 */
export function extendWindow(fromDay: number, batch: Task[]): void {
  upsertTasks(batch);
  if (fromDay < loadedFromDay) {
    loadedFromDay = fromDay;
    commit();
  }
}

// —— peer 分派（数据半边）—————————————————————————————————————————————

/** 跨窗载荷的数据分派（upsert/delete）；reload 与 settings 是窗口编排职责
 *（主窗 reloadTasks / widget 今日窗 + setSettings），由各窗 switch 分流后
 * 再调本函数。 */
export function applyPeer(payload: SyncPayload): void {
  if (payload.kind === "upsert") {
    if (payload.tasks) upsertTasks(payload.tasks);
    if (payload.projects) upsertProjects(payload.projects);
  } else if (payload.kind === "delete") {
    removeTasks(payload.taskIds);
  }
}

// —— 重拉（in-flight 序号守卫：迟到结果丢弃）———————————————————————————

let reloadSeq = 0;

/** 主窗整窗重拉纠偏（自包含：项目名映射用当次请求结果；unscheduled 全量 +
 * 已加载窗口起点的已排期批 upsert 浅替换整行——乐观删掉的行也会被后端行复活）。
 * 剪枝谓词：只对"本次请求覆盖范围"剪（fresh 替换），更早的历史窗口（翻页扩展
 * 加载）保留。窗口起点读 store 当前值——挂载一次的监听闭包不再需要 ref 防陈旧。
 * 自愈请求失败静默（后端不在时无数据可纠，下次写失败再试）。 */
export function reloadPruned(): void {
  const seq = ++reloadSeq;
  const fromDay = loadedFromDay;
  fetchProjects()
    .then((apiProjects) => {
      if (seq !== reloadSeq) return;
      const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
      // 项目列表同批回填：重命名/删除/星标失败也靠这里纠偏
      setProjects(apiProjects.map(toFrontendProject));
      return Promise.all([
        fetchTasks({ unscheduled: true }),
        fetchTasks({ start: dayStartOf(fromDay) }),
      ]).then((batches) => {
        if (seq !== reloadSeq) return;
        const fresh = batches.flat().map((t) => toFrontendTask(t, nameById));
        const freshIds = new Set(fresh.map((t) => t.id));
        tasks = [
          ...tasks.filter((t) =>
            freshIds.has(t.id) ? false : isScheduled(t) && t.dateOffset < fromDay),
          ...fresh,
        ];
        commit();
      });
    })
    .catch(() => {});
}

/** widget 今日窗重拉（恒今天无翻页）：项目替换 + unscheduled 全量 + 今日最小
 * 有界窗口（今天 0 点 ~ 明天 0 点；重复系列恒全量随窗口返回）合并 upsert——
 * 口径同 widget 原数据 effect（accumulate 不剪枝：行移除靠 peer delete 载荷）。 */
export function reloadToday(): void {
  const seq = ++reloadSeq;
  const n = new Date();
  const dayStart = new Date(n.getFullYear(), n.getMonth(), n.getDate());
  const dayEnd = new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1);
  Promise.all([
    fetchProjects(),
    fetchTasks({ unscheduled: true }),
    fetchTasks({ start: dayStart, end: dayEnd }),
  ])
    .then(([apiProjects, apiUnscheduled, apiToday]) => {
      if (seq !== reloadSeq) return;
      setProjects(apiProjects.map(toFrontendProject));
      const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
      upsertTasks(
        [...apiUnscheduled, ...apiToday].map((t) => toFrontendTask(t, nameById)),
      );
    })
    .catch((e) => console.error("小部件加载数据失败", e));
}
