// 后端 FastAPI 数据接入与形状转换层：所有"后端形状 ↔ 前端形状"的适配收敛于此，
// 组件继续消费 src/data/tasks.ts 的相对时间体系（dateOffset + start/end 小时小数），UI 零改动。
// 读路径按窗口增量拉取（窗口状态在 App.tsx）；写路径（增删改）的请求体构造也收敛在此。
import type { Project, Task } from "../data/tasks";
import { durationText } from "../data/tasks";
import { parseRrule, toRrule } from "./rrule";
import type { Settings } from "./settings";
import { fetchServerPort, isTauri } from "./tauri";

// Python 边车监听地址。基础值 = dev 现状固定 8000（CORS 已放行）；打包版由
// initApiBase() 在入口 render 前按 Tauri 配置端口改写——ES live binding 保证
// import 方读到更新值，消费点仅本文件 getJson/request 与 App.tsx 的 EventSource
export let API_BASE = "http://127.0.0.1:8000";

// 入口 render 前 await 一次：打包版（Tauri + PROD）invoke 读 Tauri 配置端口；
// 纯浏览器 / tauri dev 保持 8000 现状；失败回退默认
export async function initApiBase(): Promise<void> {
  if (!(isTauri() && import.meta.env.PROD)) return;
  const port = await fetchServerPort();
  if (port !== null) API_BASE = `http://127.0.0.1:${port}`;
}

// —— 后端出参形状（schemas.py TaskOut / ProjectOut，只声明消费的字段）——
export type ApiTask = {
  id: string;
  title: string;
  priority: "high" | "medium" | "low";
  duration_mins: number | null;
  notes: string | null;
  start_time: string | null; // naive 本地 ISO（无时区后缀）
  end_time: string | null;
  recurrence_rule: string | null;
  all_day: boolean; // 全天/跨天：start=起始日 0 点、end=最后一天次日 0 点（空 = 单日）
  subtasks: { id: string; title: string; done: boolean }[]; // 子任务清单（批 C；空 = 无）
  parent_id: string | null; // 活体例外行锚定的主系列 id；普通任务 null
  cancelled_dates: string[] | null; // 重复系列已取消实例日期（"YYYY-MM-DD"；后端墓碑行聚合生成）
  done_at: string | null; // 完成时刻；null = 未完成（与软删正交，见 Task.done 注释）
  project_id: string | null;
  source_key: string | null; // 采集/纪要溯源键（null = 手建；纪要回看区块消费，2026-11-10）
};
export type ApiProject = { id: string; name: string; color: string; starred: boolean };

// 日程提醒 SSE 事件载荷（GET /api/reminders/stream 的 data 行；字段见后端
// services/reminders.claim_due）
export type ReminderEvent = {
  task_id: string;
  title: string;
  start_at: string; // 实例开始时刻（naive 本地 ISO）
  end_at: string | null; // 结束时刻（null = 不限时；2026-10-22 批 2 供推迟按钮）
  lead_min: number | null; // 任务级有效提前量（重要会 60/差旅 40/全局档）；null = "不提醒"档的仅提示音事件（无视觉呈现）
  recurrence: boolean; // 重复系列（前端据此隐藏"完成"按钮——系列 V1 不支持完成）
  project_name: string | null;
  // 会前重推标记（2026-10-20 增）：该会议有未决 prep 建议卡时后端附带，前端
  // 提醒文案分叉「准备清单已生成」；卡已决/无卡无此键，普通提醒
  prep_proposal_id?: string;
};

// export 供 lib/ai.ts（AI 助手数据接入）复用同一请求底座与错误口径
export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`GET ${path} 失败: ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchProjects = () => getJson<ApiProject[]>("/api/projects");

// naive 本地 ISO（"2026-09-13T10:00:00"，无 Z 后缀）→ new Date 按本地时区解析，与后端存-naive-本地对齐
const dayStartMs = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

// Date → 本地 naive ISO 串（请求参数用）。不可用 toISOString()——产 UTC 串，
// UTC+8 下本地 0 点会变前一日 16:00，窗口整体左移一天
export const toNaiveLocalISO = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}T` +
  `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:00`;

// 绝对日序号（epoch day = 本地 0 点 ms / 86400000）：任务窗口的记账单位。
// 不可记相对今天的 offset——应用常开跨午夜后 offset 整体漂移，已加载边界会静默失准；
// epoch day 是绝对值不受影响。反向必须 new Date(1970, 0, 1 + day)（本地 0 点），
// 不可 new Date(day * 86400000)（UTC 解释，getHours 会偏时区差）
export const epochDayOf = (d: Date) => Math.round(dayStartMs(d) / 86_400_000);
export const dayStartOf = (day: number) => new Date(1970, 0, 1 + day);

/**
 * 拉任务，三种口径（对应后端 GET /api/tasks 的互斥分支）：
 * - unscheduled: true → 只拉未排期待办（忽略 start/end）
 * - 给 start（可不给 end = 未来侧开放）→ 窗口内单次任务 ∪ 全部重复系列
 * - 无参数 → 全量（调试口径，前端正常流程不使用）
 */
export function fetchTasks(params?: {
  start?: Date;
  end?: Date;
  unscheduled?: boolean;
}): Promise<ApiTask[]> {
  const q = new URLSearchParams();
  if (params?.unscheduled) q.set("unscheduled", "true");
  if (params?.start) q.set("start", toNaiveLocalISO(params.start));
  if (params?.end) q.set("end", toNaiveLocalISO(params.end));
  const qs = q.toString();
  return getJson<ApiTask[]>(`/api/tasks${qs ? `?${qs}` : ""}`);
}

// ── 统一搜索（2026-11-06 搜索批）：标题/备注关键词搜任务；日程/待办分组由
//    前端按 start_time 有无判定。project_name 后端 join 好直显归属 ──
export interface SearchTaskRow {
  id: string;
  title: string;
  notes: string | null;
  start_time: string | null;
  end_time: string | null;
  recurrence_rule: string | null;
  project_id: string | null;
  project_name: string | null;
  priority: "high" | "medium" | "low";
  done_at: string | null;
}

export interface SearchProjectRow {
  id: string;
  name: string;
}

export interface SearchProposalItemRow {
  proposal_id: string;
  idx: number;
  kind: string; // inbox / schedule
  title: string;
}

export interface SearchRange {
  label: string; // 命中的时间词原文（如"上周"）
  start: string;
  end: string;
}

export interface SearchReply {
  tasks: SearchTaskRow[];
  projects: SearchProjectRow[];
  proposal_items: SearchProposalItemRow[];
  parsed_range: SearchRange | null;
}

export function searchAll(q: string, includeDone = false): Promise<SearchReply> {
  return getJson<SearchReply>(
    `/api/search?q=${encodeURIComponent(q)}${includeDone ? "&include_done=true" : ""}`,
  );
}

// ── 关于页两件套（2026-11-06 批 A）：检查更新 / 备份导出 / 恢复准备 ──

export type LatestRelease = {
  available: boolean;
  tag: string | null;
  html_url: string | null;
  reason: string | null;
};

export type BackupResult = { ok: boolean; size_bytes: number; files: string[] };

export type RestoreResult = { ok: boolean; requires_restart: boolean; message: string };

export const checkLatestRelease = () => getJson<LatestRelease>("/api/about/latest-release");

/** 导出备份到用户选定路径（saveBackupPath 先取路径）；version 来自 getVersion()。 */
export const createBackup = (path: string, version: string | null) =>
  request<BackupResult>("POST", "/api/about/backup", { path, version }, 60_000);

/** 恢复第一阶段（staging 落盘）；成功后走 restartBackend 编排（dev 提示手动）。 */
export const restoreBackup = (path: string) =>
  request<RestoreResult>("POST", "/api/about/restore", { path }, 60_000);

// ── 统计报告（2026-11-06 批 D；2026-11 扩口径：环比/负载率/分列/AI 协作/滞后）──

export type StatsSummary = {
  range: { from: string; to: string };
  overview: {
    todos_done: number;
    todos_created: number;
    events_held: number;
    scheduled_minutes: number;
    todo_digest_rate: number | null;
    inbox_backlog: number;
    capacity_minutes: number;
    load_rate: number;
    delta: { todos_done: number; events_held: number; scheduled_minutes: number };
  };
  daily_done: { date: string; count: number; todos: number; events: number; minutes: number }[];
  by_project: { name: string; minutes: number }[];
  by_priority: { high: number; medium: number; low: number };
  rhythm: {
    planned_count: number;
    done_count: number;
    done_rate: number | null;
    avg_done_mins: number | null;
    top_deferred: { title: string; count: number; priority?: number }[];
  };
  behavior: {
    reschedules: number;
    avg_delta_min: number | null;
    event_count: number;
    done_events?: number;
  };
  ai: {
    adoption: { total: number; accepted_modified: number; rate: number };
    schedule_proposals: number;
  };
  lag: { todos_days: number | null; lead_days: number | null };
  warnings?: {
    severity: "high" | "medium" | "low";
    message: string;
    action: "open_inbox" | "view_week" | null;
  }[];
  prev_avg?: {
    todos_done: number;
    events_held: number;
    todo_digest_rate: number | null;
    load_rate: number | null;
  };
  habits?: {
    best_slot: string | null;
    defer_slot: string | null;
    top_project: string | null;
    top_project_pct: number | null;
    priority_completion: {
      high: { created: number; done: number; rate: number | null };
      medium: { created: number; done: number; rate: number | null };
      low: { created: number; done: number; rate: number | null };
    };
    best_priority_text: string | null;
    heatmap_done?: number[][];
    heatmap_defer?: number[][];
  };
  health?: {
    dimensions: {
      digest: number;
      load: number;
      advance: number;
      rhythm: number;
      ai_collab: number;
    };
    total: number;
    labels: {
      digest: string;
      load: string;
      advance: string;
      rhythm: string;
      ai_collab: string;
    };
  };
};

/** 建议打卡形态（新留档）；旧留档为纯字符串数组（读侧兼容，打卡时后端顺带升级） */
export type StatsAdviceItem = { text: string; status: "proposed" | "completed" | "dropped" };

export type StatsReportRow = {
  id: string;
  period_type: "week" | "month";
  period_start: string;
  content: string;
  payload?: {
    advice_items?: (StatsAdviceItem | string)[];
    keep_habits?: string[];
    health?: { total: number; dimensions: Record<string, number> };
  } | null;
  created_at: string;
};

export const fetchStatsSummary = (days: 7 | 30 | 90) =>
  getJson<StatsSummary>(`/api/stats/summary?days=${days}`);

/** AI 动态洞察（后端一天 TTL 缓存；refresh=true 跳过缓存手动重新生成）。 */
export const fetchStatsInsight = (days: 7 | 30 | 90, refresh = false) =>
  getJson<{ insight: string }>(`/api/stats/insight?days=${days}${refresh ? "&refresh=1" : ""}`)
    .then(d => d.insight);

export const fetchStatsReports = () => getJson<StatsReportRow[]>("/api/stats/reports");

/** 生成周/月报（LLM 秒级，60s 超时兜底）。 */
export const generateStatsReport = (period: "week" | "month") =>
  request<StatsReportRow>("POST", "/api/stats/reports", { period }, 60_000);

/** 导出报告为 .md（path 来自原生保存对话框，后端落盘）。 */
export const exportStatsReport = (id: string, path: string) =>
  request<{ ok: boolean; path: string; size_bytes: number }>(
    "POST", `/api/stats/reports/${id}/export`, { path }, 30_000);

/** 建议打卡：确认某条建议的执行情况（下期报告对照点评的反馈信号）。 */
export const setStatsAdvice = (
  id: string,
  index: number,
  status: "proposed" | "completed" | "dropped",
) =>
  request<StatsReportRow>(
    "POST", `/api/stats/reports/${id}/advice`, { index, status }, 15_000);

/** 沉淀复盘反思结论为情节事实（Tier 2 记忆；同主题同结论印证不堆行）。 */
export const saveStatsFact = (topic: string, fact: string) =>
  request<{ ok: boolean; id: string }>("POST", "/api/stats/facts", { topic, fact }, 15_000);

// —— 统计问答（POST /api/stats/ask，SSE 流式独立轻通道）——

/** 复盘反思沉淀候选（facts 帧载荷；确认后经 saveStatsFact 落库）。 */
export type StatsFactCandidate = {
  topic: string;
  fact: string;
  expires_days?: number;
};

/** /ask 的 data 帧载荷（帧协议对齐 /api/ai/chat 的子集 + 统计域 facts 帧）。 */
export type StatsAskEvent =
  | { type: "delta"; text: string } // 回复正文增量（打字机）
  | { type: "reply"; content: string } // 整段权威文本（终帧，替换 delta 累积）
  | { type: "facts"; candidates: StatsFactCandidate[] } // 复盘总结轮的沉淀候选（确认条消费）
  | { type: "done" }
  | { type: "error"; code: string; message: string };

export type StatsAskTurn = { role: "user" | "assistant"; content: string };

/** 统计问答流：帧解析同 lib/ai.ts streamChat（\n\n 分帧 + data: 前缀过滤 +
 *  坏帧静默丢弃）；signal 透传 AbortController；mode=review 为复盘引导会话。 */
export async function askStats(
  days: 7 | 30 | 90,
  question: string,
  history: StatsAskTurn[],
  onEvent: (ev: StatsAskEvent) => void,
  signal?: AbortSignal,
  mode: "ask" | "review" = "ask",
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/stats/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ days, question, history, mode }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`统计问答请求失败: ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const data = frame
        .split("\n")
        .filter((l) => l.startsWith("data: "))
        .map((l) => l.slice(6))
        .join("\n");
      if (!data) continue; // ": connected" 注释帧
      try {
        onEvent(JSON.parse(data) as StatsAskEvent);
      } catch {
        // 坏帧丢弃
      }
    }
  }
}

/**
 * ApiTask → 前端 Task：
 * - 绝对时间 → dateOffset（相对今天 0 点的天数，round 防精度毛刺）+ start/end 小时小数；
 *   无 end_time → end undefined（"默认一小时卡"规则沿用组件）；start_time 为 null → 三字段全
 *   undefined（isScheduled 谓词自然判为未排期）
 * - project_id → project name（映射不到视同无项目）；duration_mins → minutes + 派生展示文本；
 *   recurrence_rule → parseRrule（解析失败降级为不重复）
 */
export function toFrontendTask(t: ApiTask, projectNameById: Map<string, string>): Task {
  let dateOffset: number | undefined;
  let start: number | undefined;
  let end: number | undefined;
  if (t.start_time !== null) {
    const s = new Date(t.start_time);
    const todayStart = dayStartMs(new Date());
    dateOffset = Math.round((dayStartMs(s) - todayStart) / 86_400_000);
    start = s.getHours() + s.getMinutes() / 60;
    if (t.end_time !== null) {
      const e = new Date(t.end_time);
      // 跨天 end：按钟点 + 24×天差累加（seed 无跨天数据；出现时画布自然裁剪，待后续支持）
      end = e.getHours() + e.getMinutes() / 60 + 24 * Math.round((dayStartMs(e) - dayStartMs(s)) / 86_400_000);
    }
  }
  const minutes = t.duration_mins ?? undefined;
  const project = t.project_id !== null ? projectNameById.get(t.project_id) : undefined;
  return {
    id: t.id,
    title: t.title,
    priority: t.priority,
    project,
    minutes,
    duration: minutes !== undefined ? durationText(minutes) : undefined,
    note: t.notes ?? undefined,
    recurrence: parseRrule(t.recurrence_rule) ?? undefined,
    allDay: t.all_day ? true : undefined,
    checklist: t.subtasks?.length ? t.subtasks : undefined,
    isException: t.parent_id !== null,
    cancelledDates: t.cancelled_dates?.length ? t.cancelled_dates : undefined,
    done: t.done_at !== null ? true : undefined,
    sourceKey: t.source_key ?? undefined,
    dateOffset,
    start,
    end,
  };
}

export function toFrontendProject(p: ApiProject): Project {
  return { id: p.id, name: p.name, color: p.color, starred: p.starred };
}

// —— 写路径（增删改）：请求体构造与发送 ——

// 任务 PATCH 请求体：键不出现 = 不动；显式 null = 清空该字段（与后端 exclude_unset 语义对偶）
export type TaskPatchBody = {
  title?: string;
  notes?: string | null;
  priority?: Task["priority"];
  duration_mins?: number | null;
  start_time?: string; // naive 本地 ISO；null = 取消排期（本期前端不发）
  end_time?: string | null; // null = 清结束时刻
  all_day?: boolean; // 全天/跨天开关（false = 转回普通；后端归一日期区间并豁免 duration）
  recurrence_rule?: string | null;
  project_id?: string | null;
  done?: boolean; // true = 标记完成（done_at=now）/ false = 撤销完成（清 done_at）；系列任务后端 422
  subtasks?: { title: string; done: boolean }[]; // 键出现 = 全量替换（空数组 = 清空）
};

// 任务 POST 请求体（快捷录入新建；priority 后端默认 medium。notes 供 inbox 建议卡
// 收录时携带来源摘要，快捷录入不携带；溯源三键随采集建议采纳透传——教练溯源地基）
export type TaskCreateBody = {
  title: string;
  duration_mins?: number;
  notes?: string;
  start_time?: string; // naive 本地 ISO
  end_time?: string;
  all_day?: boolean; // 全天/跨天（后端归一 0 点/次日 0 点，duration 恒空）
  recurrence_rule?: string;
  project_id?: string;
  priority?: number; // 1 高 2 中 3 低——AI 明示重要信号直判时携带（2026-10-22）
  source_key?: string; // 采集来源源键（2026-10-23）
  source_fingerprint?: string; // 来源条目指纹（2026-10-23）
  source_evidence?: string; // 提炼时引用的素材摘录（2026-10-23）
};

export type ProjectPatchBody = { name?: string; starred?: boolean; color?: string };

export async function request<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
  timeoutMs?: number,
): Promise<T> {
  // timeoutMs 可选超时（AbortController）：超时 fetch 抛 name === "AbortError" 的
  // DOMException 冒泡，调用方按 name 区分提示；不传 = 无超时（CRUD 本地回环毫秒级）
  const controller = new AbortController();
  const timer = timeoutMs !== undefined ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
  if (!res.ok) {
    // 附 status 供调用方区分错误（如模型表单 409 重名提示）；不读时行为同纯 Error
    const err = new Error(`${method} ${path} 失败: ${res.status}`) as Error & {
      status?: number;
      detail?: string;
    };
    err.status = res.status;
    // 后端 detail 文案可直显（如引擎 422「未来 7 天没有可用空档」）——仅 JSON
    // 响应体尝试读取，任何异常不改原抛错路径（全站公共底座，宁缺勿炸）
    try {
      if ((res.headers.get("content-type") || "").includes("application/json")) {
        const body = (await res.json()) as { detail?: unknown };
        if (typeof body?.detail === "string") err.detail = body.detail;
      }
    } catch {
      /* 忽略：detail 拿不到就用默认 message */
    }
    throw err;
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

// 编辑任务：默认直接更新任务本体；重复系列可带 mode（this = 实体化单次例外、
// following = 截断老系列 + 新建主记录，均需 date 指被编辑实例日），响应为受影响的行
//（this/following 时是后端新建的行，id 后端生成、无法纯乐观）
export const updateTask = (
  id: string,
  body: TaskPatchBody,
  opts?: { mode: "this" | "following" | "all"; date: string },
) => {
  const q = new URLSearchParams();
  if (opts) {
    q.set("mode", opts.mode);
    if (opts.date) q.set("date", opts.date);
  }
  const qs = q.toString();
  return request<ApiTask>("PATCH", `/api/tasks/${id}${qs ? `?${qs}` : ""}`, body);
};

// 删除任务：默认整任务删（后端按生命周期 6 场景判定物理删/软删）；重复系列可带
// mode（this = 墓碑挖空单次实例、following = UNTIL 截断删及后续、all = 整系列），
// this/following 必带 date（被删实例日）
export const deleteTask = (id: string, opts?: { mode: "this" | "following" | "all"; date: string }) => {
  const q = new URLSearchParams();
  if (opts) {
    q.set("mode", opts.mode);
    if (opts.date) q.set("date", opts.date);
  }
  const qs = q.toString();
  return request<void>("DELETE", `/api/tasks/${id}${qs ? `?${qs}` : ""}`);
};

// 新建项目必须拿回后端 UUID（后续重命名/星标/删除全靠 id 寻址）
export const createProject = (name: string, color: string) =>
  request<ApiProject>("POST", "/api/projects", { name, color });

// 新建任务：响应为后端生成的行（id 后端生成、无法纯乐观，调用方 await 后入列）
export const createTask = (body: TaskCreateBody) =>
  request<ApiTask>("POST", "/api/tasks", body);

/** 预估校准提示（批 E1）：同类已完成任务近 90 天实际时长均值与样本数。 */
export const fetchEstimateHint = (title: string) =>
  getJson<{ avg_min: number | null; count: number }>(
    `/api/tasks/estimate-hint?title=${encodeURIComponent(title)}`,
  );

// —— 一键智能安排（统一排期引擎，2026-10-20；同日决策：单卡纯规则直落，
//    批量按钮退役——多任务批量排期全走对话草案卡）——

// 单卡智能安排：规则层定时间并落库，返回更新后的任务行；409 已排期 / 422 无空档
//（err.detail 文案可直显）/ 404 不存在
export const autoScheduleTask = (id: string) =>
  request<ApiTask>("POST", `/api/tasks/${id}/auto-schedule`);

export const updateProject = (id: string, body: ProjectPatchBody) =>
  request<ApiProject>("PATCH", `/api/projects/${id}`, body);

export const deleteProject = (id: string) => request<void>("DELETE", `/api/projects/${id}`);

// —— 应用设置（单行资源 /api/settings；形状镜像 schemas.py SettingsOut，只声明消费的字段）——
export type ApiSettings = {
  auto_start: boolean;
  close_action: "hide" | "quit";
  theme_mode: "system" | "light" | "dark";
  default_view: "day" | "week" | "month";
  widget_transparency: number; // 透明度百分比（10–80，越大越透）
  calendar_start_min: number; // 分钟整数（30 粒度）
  calendar_end_min: number;
  lunch_enabled: boolean;
  lunch_start_min: number;
  lunch_end_min: number;
  work_minutes_per_day: number;
  reminder_lead_min: number | null; // null = 不提醒
  notify_style: "system" | "floating" | "corner";
  sound: "chime" | "bowl" | "water" | "off";
  ai_enabled: boolean; // AI 主开关（阶段二）
  morning_brief_min: number; // 晨间简报时刻（分钟整数，默认 510 = 08:30）
  ingest_enabled: boolean; // 每日数据采集开关（默认关——采集打外网 API，显式开启）
  ingest_brief_min: number; // 数据采集时刻（分钟整数，默认 540 = 09:00）
  ingest_runs_per_day: number; // 每日采集轮数（1-4 档，默认 2；后续槽当天顺延）
  ingest_llm_budget: number; // 采集 LLM 调用日上限（0 = 不限，默认 30）
  nightly_start_min: number; // 夜间工作流报点时刻（分钟整数，默认 1200 = 20:00；nightly/prep/hardtask 三流共用）
  // 四流独立开关（2026-10-21 复查批 D1）：与 ai_enabled 正交，关 = 永不认领
  morning_enabled: boolean;
  nightly_enabled: boolean;
  prep_enabled: boolean;
  hardtask_enabled: boolean;
  weekly_report_enabled: boolean; // 周报自动生成（2026-11-07 统计 120 分档，周日晚随夜间报点）
  minutes_enabled: boolean; // 会后纪要邀请（2026-11-10 批 3，重要会议结束后询问补记）
  chat_autocompact_enabled: boolean; // 对话上下文自动压缩（2026-10-26，默认开）
  coach_probe_deep: boolean; // 教练溯源深探（优化批 9，默认关）
  assistant_chime: boolean; // 助手消息轻提示音（2026-11-04 通知分轨）
  weather_city: string | null; // 排期天气对照城市（空 = 停用，Open-Meteo）
  // AI 个性化（2026-10-20）：风格四档 / 哲学预设 id 逗号串 / 自定义哲学文本
  ai_style: "balanced" | "concise" | "detailed" | "rigorous";
  ai_philosophy: string | null;
  ai_philosophy_custom: string | null;
};

export const fetchSettings = () => getJson<ApiSettings>("/api/settings");

// 部分更新：键不出现 = 后端不动；返回全量（含午休自动修复后的真实落库值，调用方回填）
export const updateSettings = (body: Partial<ApiSettings>) =>
  request<ApiSettings>("PATCH", "/api/settings", body);

// 天气城市探测回执（设置页「测试」钮）：命中 → {ok, resolved}；未命中 422 detail
// 可直显。getJson 无 detail 增强（公共底座不动），这里独立解析
export async function testWeatherCity(city: string): Promise<{ ok: boolean; resolved: string }> {
  const res = await fetch(`${API_BASE}/api/settings/weather-test?city=${encodeURIComponent(city)}`);
  if (!res.ok) {
    const err = new Error(`天气城市探测失败: ${res.status}`) as Error & { detail?: string };
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") err.detail = body.detail;
    } catch {
      /* detail 拿不到就用默认 message */
    }
    throw err;
  }
  return res.json() as Promise<{ ok: boolean; resolved: string }>;
}

// 分钟整数 → 小时小数（与时间轴 11.5 口径同源），一次换算全字段
export function toFrontendSettings(s: ApiSettings): Settings {
  return {
    autoStart: s.auto_start,
    closeAction: s.close_action,
    themeMode: s.theme_mode,
    defaultView: s.default_view,
    widgetTransparency: s.widget_transparency,
    dayStart: s.calendar_start_min / 60,
    dayEnd: s.calendar_end_min / 60,
    lunchEnabled: s.lunch_enabled,
    lunchStart: s.lunch_start_min / 60,
    lunchEnd: s.lunch_end_min / 60,
    workHours: s.work_minutes_per_day / 60,
    reminderLeadMin: s.reminder_lead_min,
    notifyStyle: s.notify_style,
    sound: s.sound,
    aiEnabled: s.ai_enabled,
    morningBriefAt: s.morning_brief_min / 60,
    ingestEnabled: s.ingest_enabled,
    ingestAt: s.ingest_brief_min / 60,
    ingestRunsPerDay: s.ingest_runs_per_day,
    ingestLlmBudget: s.ingest_llm_budget,
    nightlyStartAt: s.nightly_start_min / 60,
    morningEnabled: s.morning_enabled,
    nightlyEnabled: s.nightly_enabled,
    prepEnabled: s.prep_enabled,
    hardtaskEnabled: s.hardtask_enabled,
    weeklyReportEnabled: s.weekly_report_enabled,
    minutesEnabled: s.minutes_enabled,
    chatAutocompactEnabled: s.chat_autocompact_enabled,
    coachProbeDeep: s.coach_probe_deep,
    assistantChime: s.assistant_chime,
    weatherCity: s.weather_city ?? null,
    aiStyle: s.ai_style,
    aiPhilosophy: (s.ai_philosophy ?? "").split(",").filter(Boolean),
    aiPhilosophyCustom: s.ai_philosophy_custom ?? null,
  };
}

// 前端 patch（小时小数）→ 请求体（分钟整数）；只用 "key" in patch 判显式设置（口径同 toTaskPatch）
export function toSettingsPatch(patch: Partial<Settings>): Partial<ApiSettings> {
  const body: Partial<ApiSettings> = {};
  if ("autoStart" in patch) body.auto_start = patch.autoStart;
  if ("closeAction" in patch) body.close_action = patch.closeAction;
  if ("themeMode" in patch) body.theme_mode = patch.themeMode;
  if ("defaultView" in patch) body.default_view = patch.defaultView;
  if ("widgetTransparency" in patch) body.widget_transparency = patch.widgetTransparency;
  if ("dayStart" in patch) body.calendar_start_min = Math.round(patch.dayStart! * 60);
  if ("dayEnd" in patch) body.calendar_end_min = Math.round(patch.dayEnd! * 60);
  if ("lunchEnabled" in patch) body.lunch_enabled = patch.lunchEnabled;
  if ("lunchStart" in patch) body.lunch_start_min = Math.round(patch.lunchStart! * 60);
  if ("lunchEnd" in patch) body.lunch_end_min = Math.round(patch.lunchEnd! * 60);
  if ("workHours" in patch) body.work_minutes_per_day = Math.round(patch.workHours! * 60);
  if ("reminderLeadMin" in patch) body.reminder_lead_min = patch.reminderLeadMin;
  if ("notifyStyle" in patch) body.notify_style = patch.notifyStyle;
  if ("sound" in patch) body.sound = patch.sound;
  if ("aiEnabled" in patch) body.ai_enabled = patch.aiEnabled;
  if ("morningBriefAt" in patch) body.morning_brief_min = Math.round(patch.morningBriefAt! * 60);
  if ("ingestEnabled" in patch) body.ingest_enabled = patch.ingestEnabled;
  if ("ingestAt" in patch) body.ingest_brief_min = Math.round(patch.ingestAt! * 60);
  if ("ingestRunsPerDay" in patch) body.ingest_runs_per_day = patch.ingestRunsPerDay!;
  if ("ingestLlmBudget" in patch) body.ingest_llm_budget = patch.ingestLlmBudget;
  if ("nightlyStartAt" in patch) body.nightly_start_min = Math.round(patch.nightlyStartAt! * 60);
  if ("morningEnabled" in patch) body.morning_enabled = patch.morningEnabled;
  if ("nightlyEnabled" in patch) body.nightly_enabled = patch.nightlyEnabled;
  if ("prepEnabled" in patch) body.prep_enabled = patch.prepEnabled;
  if ("hardtaskEnabled" in patch) body.hardtask_enabled = patch.hardtaskEnabled;
  if ("weeklyReportEnabled" in patch) body.weekly_report_enabled = patch.weeklyReportEnabled;
  if ("minutesEnabled" in patch) body.minutes_enabled = patch.minutesEnabled;
  if ("chatAutocompactEnabled" in patch) body.chat_autocompact_enabled = patch.chatAutocompactEnabled;
  if ("coachProbeDeep" in patch) body.coach_probe_deep = patch.coachProbeDeep;
  if ("assistantChime" in patch) body.assistant_chime = patch.assistantChime;
  if ("weatherCity" in patch) body.weather_city = patch.weatherCity || null; // 空串=停用
  if ("aiStyle" in patch) body.ai_style = patch.aiStyle;
  // 哲学数组 → 逗号串；空数组 → null（清空全部）。"in patch" 判显式设置——否则
  // 每次改风格都会把哲学清空
  if ("aiPhilosophy" in patch) body.ai_philosophy = patch.aiPhilosophy!.join(",") || null;
  if ("aiPhilosophyCustom" in patch)
    body.ai_philosophy_custom = patch.aiPhilosophyCustom?.trim() || null; // 空白=未配置
  return body;
}

// 手动立即采集（AgentSettings「立即采集」按钮；/api/ingest/run，M1-⑤a）：202 已
// 并出（结果走 ingest 广播 + refreshKey 重拉）/ 409 在途 / 422 前置未满足（detail
// 为中文引导）。每源 GET/PUT /api/ingest/sources 端点后端保留，但数据接入页已不
// 展示采集状态（2026-10-15 UI 撤除），前端无消费者
export const runIngestNow = () => request<{ started: boolean }>("POST", "/api/ingest/run");

// 等待完成型手动采集（2026-11-04 手动刷新批，数据源页刷新钮）：wait=true 等
// 本轮跑完返回摘要——刷新语义需要完成反馈（"新增 N 条"/"无新数据"）；数秒到
// 十几秒（Map 并发 LLM），调用方持 loading 态。skipped 键：in_flight/ai_off/
// no_model/not_ready/error（前置三闸已在 router 翻 422，skipped 是竞态窗口内
// 的二道防线，前端按通用失败提示；采集熔断闸已移除——采集是数据同步不是排期）
export type IngestRunSummary = {
  skipped: string | null;
  silent: boolean;
  drafts: number;
  inbox: number;
  direct_calendar: number;
  direct_inbox: number;
};

export const runIngestAndWait = () =>
  request<IngestRunSummary>("POST", "/api/ingest/run?wait=true");

// 采集源状态（2026-10-24 高可信直通配套）：数据接入已连接卡消费 key→trusted
// 映射（todo/calendar 域条目免确认直落收件箱/日历；doc 域恒建卡——提取的不准确
// 须确认）。只声明消费的字段，后端 IngestSourceOut 返回更多键
export interface IngestSourceState {
  key: string; // 源稳定键：cli:{adapter_id} / MCP name / Composio 连接 id / local_folder
  trusted: boolean;
}

export const fetchIngestSources = () =>
  getJson<IngestSourceState[]>("/api/ingest/sources");

// 切换高可信直通开关（PUT /api/ingest/sources/{key}，await 回填不乐观——同卡上
// 启停钮口径；停用中的源不在后端枚举内会 404，调用方 catch 静默保持原态）
export const updateIngestSourceTrusted = (key: string, trusted: boolean) =>
  request<IngestSourceState>(
    "PUT", `/api/ingest/sources/${encodeURIComponent(key)}`, { trusted },
  );

// 导入 .ics 日历文件（2026-11-11 批，数据接入页底部入口）：默认信任直落——日程
// 直接建日历、待办进收件箱，不经建议结算；parsed_* 含坏/重/降级，与 created_* 的
// 差值见三个计数。等待型（本地毫秒~亚秒级），30s 超时给大文件留裕量；422 detail
// 为后端中文引导（路径无效 / 非日历文件 / 零条目），可直显
export interface IcsImportSummary {
  ok: boolean;
  calendar: string;
  parsed_events: number;
  parsed_todos: number;
  created_events: number;
  created_todos: number;
  skipped_duplicates: number;
  skipped_invalid: number;
  degraded_recurring: number;
}

export const importIcsFile = (path: string) =>
  request<IcsImportSummary>("POST", "/api/ingest/ics", { path }, 30_000);

/**
 * 前端排期坐标 → 绝对 naive 本地 ISO（写回 start_time/end_time 用）：今天 0 点 +
 * dateOffset 天 + hourFraction 小时。同 toNaiveLocalISO 口径禁用 toISOString（UTC 串会
 * 让日期整体左移）；15 分钟吸附粒度（0.25h）下小时小数无浮点毛刺。
 */
export function scheduleToISO(dateOffset: number, hourFraction: number): string {
  const d = dayStartOf(epochDayOf(new Date()) + dateOffset);
  d.setHours(Math.floor(hourFraction), Math.round((hourFraction % 1) * 60), 0, 0);
  return toNaiveLocalISO(d);
}

/**
 * datetime-local 值（"YYYY-MM-DDTHH:mm"，收件箱「安排时间」手选）→ 前端排期坐标
 * （dateOffset + 小时小数），scheduleToISO 的互逆换算。非法/空值返回 null（调用方
 * 提示先选时间）；日期按本地时区解析（与 dayStartOf 同口径，禁 new Date(str)——
 * 部分浏览器按 UTC 解析无时刻串会让日期偏移）。
 */
export function manualPickToRelative(
  value: string,
): { dateOffset: number; start: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (Number.isNaN(d.getTime())) return null;
  return {
    dateOffset: epochDayOf(d) - epochDayOf(new Date()),
    start: Number(m[4]) + Number(m[5]) / 60,
  };
}

/**
 * 前端编辑 patch → 任务 PATCH 请求体。核心：用 `"key" in patch` 判键是否被显式设置
 * （不是 `!== undefined`）——组件约定"显式 undefined = 清空该字段"（如 note.trim() || undefined），
 * 而 JSON.stringify 会丢弃 undefined 键，必须翻译成 null 后端才认作清空。
 * 不处理：duration（展示文本，库由 duration_mins 派生）、project/dateOffset/start/end
 * （归属走 handleTodoToProject、排期走 handleScheduleChange，各有专属通道）。
 */
export function toTaskPatch(patch: Partial<Task>): TaskPatchBody {
  const body: TaskPatchBody = {};
  if ("title" in patch) body.title = patch.title;
  if ("note" in patch) body.notes = patch.note ?? null;
  if ("minutes" in patch) body.duration_mins = patch.minutes ?? null;
  if ("priority" in patch) body.priority = patch.priority;
  if ("recurrence" in patch) body.recurrence_rule = toRrule(patch.recurrence);
  if ("done" in patch) body.done = patch.done; // 完成标记直译（boolean 不可 undefined 翻 null，语义本就是开关）
  if ("allDay" in patch && patch.allDay !== undefined)
    body.all_day = patch.allDay; // 全天开关（后端按库中 start/end 归一日期区间）
  if ("checklist" in patch)
    // 清单键出现即全量替换（undefined 翻空数组 = 清空）；行内 id 不回传（后端重生成）
    body.subtasks = (patch.checklist ?? []).map((s) => ({ title: s.title, done: !!s.done }));
  return body;
}

// —— 模型接入（/api/models；形状镜像 schemas.py ModelProviderOut）——
// 与 ApiSettings 不同，这里无单位换算字段（上下文长度换算 K 仅在卡片展示层做），
// 因此不做 toFrontend/toPatch 双程适配层，组件直接消费 snake_case 行
export type ApiModel = {
  id: string;
  name: string;
  base_url: string | null; // 空 = LiteLLM 按 provider/ 前缀官方路由
  model_name: string; // LiteLLM 模型标识（provider/模型名）
  api_key: string | null; // null = 本地免鉴权服务
  thinking_mode: boolean;
  context_length: number; // token 数（8192..1048576 档位）
  is_active: boolean; // 全局至多一行 true
  is_aux: boolean; // 轻量助手模型槽（优化批 9）：小调用白名单走此行，未配置回落激活模型
  last_test_ok: boolean | null; // null = 从未测试
  last_test_ms: number | null;
  last_test_at: string | null;
  created_at: string;
  updated_at: string;
};

// 连通性测试结果（探测失败也是 200——失败是数据不是错误）
export type ApiModelTestResult = {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
};

export type ModelCreateBody = {
  name: string;
  base_url?: string | null; // null/空 = LiteLLM 官方路由
  model_name: string;
  api_key?: string | null;
  thinking_mode?: boolean;
  context_length?: number;
};

export type ModelPatchBody = {
  name?: string;
  base_url?: string | null; // 显式 null = 清回官方路由
  model_name?: string;
  api_key?: string | null; // 显式 null = 清空
  thinking_mode?: boolean;
  context_length?: number;
};

export const fetchModels = () => getJson<ApiModel[]>("/api/models");

export const createModel = (body: ModelCreateBody) =>
  request<ApiModel>("POST", "/api/models", body);

export const updateModel = (id: string, body: ModelPatchBody) =>
  request<ApiModel>("PATCH", `/api/models/${id}`, body);

export const deleteModel = (id: string) => request<void>("DELETE", `/api/models/${id}`);

// 激活指定模型（后端同事务把其余激活行置 False——全局至多一行激活）
export const activateModel = (id: string) =>
  request<ApiModel>("POST", `/api/models/${id}/activate`);

// 设/取消轻量助手模型（toggle；路由/分类/反思等小调用走此行——本地小模型亚秒级）
export const setAuxModel = (id: string) =>
  request<ApiModel>("POST", `/api/models/${id}/aux`);

// 对已保存模型测速：结果落库（last_test_* 三列），响应为测试结果本身
export const testModelById = (id: string) =>
  request<ApiModelTestResult>("POST", `/api/models/${id}/test`);

// 草稿探测：表单"测试连通性"按钮用当前填写参数（不读库不落库；base_url 空 = 官方路由）
export const testModelDraft = (body: {
  base_url?: string | null;
  model_name: string;
  api_key?: string | null;
}) => request<ApiModelTestResult>("POST", "/api/models/test", body);

// —— 集成接入（/api/integrations，Composio；形状镜像 schemas.py，snake_case 直消费）——

// Key 配置（api_key 原样回显免重填；key_ok null = 从未验证）
export type ApiComposioKey = {
  api_key: string | null;
  key_ok: boolean | null;
  key_error: string | null;
  key_checked_at: string | null;
};

// 搜索结果中的应用（logo 为远程 URL，img 直载）
export type ApiComposioApp = {
  slug: string;
  name: string;
  description: string | null;
  logo: string | null;
  tools_count: number | null;
};

// POST /connect 响应：redirect_url 交系统浏览器完成 OAuth，connected_account_id 轮询锚
export type ApiComposioConnect = {
  connected_account_id: string;
  redirect_url: string | null;
};

// 连接状态（status 为 Composio 原生值：INITIALIZING/INITIATED/ACTIVE/FAILED/…）
export type ApiComposioConnectStatus = {
  connected_account_id: string;
  status: string;
  toolkit_slug: string | null;
};

// 已连接条目（name/logo/description 后端 enrich，拉不到为 null）。enabled 为本地
// 采集开关（镜像 ingest_sources，key=连接 ID；停用 = 不参与每日采集，云端连接不动）
export type ApiComposioConnection = {
  id: string;
  status: string;
  toolkit_slug: string;
  name: string | null;
  logo: string | null;
  description: string | null;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
};

// 连接「测试」结果（失败也是 200——失败是数据）：与采集同源的动作解析真调云端
// 数条目（不过时间窗，报原始返回数）；未内置采集命令的应用退化为仅验连接状态
export type ApiComposioTest = {
  ok: boolean;
  latency_ms: number | null;
  items_count: number | null;
  note: string | null;
  error: string | null;
};

export const fetchComposioKey = () => getJson<ApiComposioKey>("/api/integrations/key");

// 保存（或清除：null/空串）Key，后端保存后自动验证一次。60s 超时 > 后端最坏
// ~40s（SDK 10s 超时 ×2 次尝试），弱网下按钮必然复位不再无界"验证中…"
export const saveComposioKey = (apiKey: string | null) =>
  request<ApiComposioKey>("PUT", "/api/integrations/key", { api_key: apiKey }, 60_000);

// 对已存 Key 手动重验（2026-10-15 修复：原误发 POST /api/integrations/key——后端
// 只注册 GET/PUT /key 与 POST /key/verify，必 405，"重新验证"自上线从未成功过）
export const verifyComposioKey = () =>
  request<ApiComposioKey>("POST", "/api/integrations/key/verify", undefined, 60_000);

export const searchComposioApps = (search: string) =>
  getJson<ApiComposioApp[]>(`/api/integrations/apps?search=${encodeURIComponent(search)}`);

export const connectComposioApp = (toolkitSlug: string) =>
  request<ApiComposioConnect>("POST", "/api/integrations/connect", { toolkit_slug: toolkitSlug });

export const fetchConnectStatus = (connectedAccountId: string) =>
  getJson<ApiComposioConnectStatus>(`/api/integrations/connect/${connectedAccountId}`);

export const fetchComposioConnections = () =>
  getJson<ApiComposioConnection[]>("/api/integrations/connections");

// 测试超时 60s：后端 get + execute 最坏 10s×2 次×2 调用（SDK 单次超时 + 1 重试）
export const testComposioConnection = (connectedAccountId: string) =>
  request<ApiComposioTest>(
    "POST", `/api/integrations/connections/${connectedAccountId}/test`, undefined, 60_000
  );

export const setComposioEnabled = (connectedAccountId: string, enabled: boolean) =>
  request<void>(
    "PUT", `/api/integrations/connections/${connectedAccountId}/enabled`, { enabled }
  );

export const disconnectComposio = (connectedAccountId: string) =>
  request<void>("DELETE", `/api/integrations/connections/${connectedAccountId}`);

// —— MCP 服务接入（/api/mcp；形状镜像 schemas.py，snake_case 直消费）——

export type McpTransport = "stdio" | "streamable_http" | "sse";

// env/headers 原样回显免重填（本地单用户，同 ApiModel.api_key 口径）
export type ApiMcpServer = {
  id: string;
  name: string;
  description: string | null; // 用户自定义描述；空 = 卡片回落技术串
  transport: McpTransport;
  command: string | null; // null = 非 stdio
  args: string[] | null;
  env: Record<string, string> | null;
  url: string | null; // null = stdio
  headers: Record<string, string> | null;
  enabled: boolean;
  last_test_ok: boolean | null; // null = 从未测试
  last_test_ms: number | null;
  last_test_at: string | null;
  last_test_tools: number | null; // 徽标"连通 · Nms · N 个工具"；失败为 null
  created_at: string;
  updated_at: string;
};

// 连通性测试结果（探测失败也是 200——失败是数据不是错误）
export type ApiMcpTestResult = {
  ok: boolean;
  latency_ms: number | null;
  tools_count: number | null; // ok 时必有：发现的工具数
  error: string | null;
};

// Create/Patch 共用体（Patch 键不出现 = 不动、显式 null = 清空；undefined 键经
// JSON.stringify 丢弃——编辑表单发全量，切换 transport 后对侧字段显式 null 清空）
export type McpServerBody = {
  name?: string;
  description?: string | null; // 显式 null = 清空描述
  transport?: McpTransport;
  command?: string | null;
  args?: string[] | null;
  env?: Record<string, string> | null;
  url?: string | null;
  headers?: Record<string, string> | null;
  enabled?: boolean;
};

// 草稿探测入参（表单现值，不读库不落库）
export type McpDraftBody = {
  transport: McpTransport;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  url?: string | null;
  headers?: Record<string, string>;
};

// 本地文件夹配置（内置 MCP 服务端；path null = 未配置）
// 本地文件夹配置 + 上次卡上「测试」持久化结果（test_ok null = 从未测试或目录变更后清空）
export type ApiLocalFolder = {
  paths: string[]; // 空 = 未配置
  enabled: boolean;
  test_ok: boolean | null;
  test_files: number | null;
};

export type ApiLocalFolderTestResult = {
  ok: boolean;
  files_count: number | null;
  error: string | null;
};

export const fetchMcpServers = () => getJson<ApiMcpServer[]>("/api/mcp");

export const createMcpServer = (body: McpServerBody) =>
  request<ApiMcpServer>("POST", "/api/mcp", body);

export const updateMcpServer = (id: string, body: McpServerBody) =>
  request<ApiMcpServer>("PATCH", `/api/mcp/${id}`, body);

export const deleteMcpServer = (id: string) => request<void>("DELETE", `/api/mcp/${id}`);

// 对已保存服务测试：结果落库（last_test_* 三列）；与 enabled 正交。超时 80s >
// 后端 stdio 档外层硬帽 70s——后端任何挂起下 UI 必然复位（"测试中"不再无界）
export const testMcpServerById = (id: string) =>
  request<ApiMcpTestResult>("POST", `/api/mcp/${id}/test`, undefined, 80_000);

export const testMcpServerDraft = (body: McpDraftBody) =>
  request<ApiMcpTestResult>("POST", "/api/mcp/test", body, 80_000);

export const fetchLocalFolder = () => getJson<ApiLocalFolder>("/api/mcp/local-folder");

// 保存目录列表 + 开关（后端校验：每项须绝对路径且目录存在，坏路径不落库；
// 空列表 = 清除全部，最多 10 个）
export const saveLocalFolder = (paths: string[], enabled: boolean) =>
  request<ApiLocalFolder>("PUT", "/api/mcp/local-folder", { paths, enabled });

// 有效性测试:paths 传值 = 草稿探测（面板当前填写列表，不读库、选完即可测），
// 省略 = 测库中已保存路径（卡上"测试"按钮口径，对齐 MCP 卡测已保存行）。
// 超时兜底：后端全量遍历目录树，大目录可能偏慢
export const testLocalFolder = (paths?: string[]) =>
  request<ApiLocalFolderTestResult>("POST", "/api/mcp/local-folder/test", { paths }, 60_000);

// —— CLI 工具接入（/api/cli，第四源；形状镜像 schemas.py，snake_case 直消费）——

// CLI 工具卡（注册表全量 + DB 行合并；零子进程——探测态只读上次落库结果）。
// custom=true 为窄口自定义只读命令行（探测/授权端点不适用）；fetch_ready=false
// 表示采集命令待真机实测（接入不阻塞，源走 note 清单路径）
export type ApiCliTool = {
  adapter_id: string;
  name: string;
  description: string;
  custom: boolean;
  install_url: string | null; // 官方文档外链（安装命令不内嵌，以文档为准）
  auth_mode: "auto" | "manual" | "none"; // auto=捕获 URL 开系统浏览器 / manual=复制终端命令 / none=免授权直连
  connected: boolean; // DB 行存在 = 已接入
  enabled: boolean;
  bin_ok: boolean | null; // null = 从未探测
  bin_version: string | null;
  bin_path: string | null;
  auth_ok: boolean | null; // null = 从未探测或不适用（auth_mode=none / 自定义行）
  test_ok: boolean | null; // 自定义行上次「测试」结果；null = 从未（精选行走 probe）
  test_items: number | null;
  custom_argv: string[] | null; // 自定义行命令参数（编辑表单回填）
  last_error: string | null;
  fetch_ready: boolean;
  actions: { name: string; description: string }[]; // 只读命令声明清单
};

// 安装+授权探测（失败也是 200——失败是数据；已接入行同时落库）
export type ApiCliProbe = {
  ok: boolean; // bin_ok 且授权探测未明示失败
  bin_ok: boolean;
  bin_version: string | null;
  bin_path: string | null;
  auth_ok: boolean | null;
  error: string | null;
};

// 发起授权：auto 档 ok=true 带 url（开系统浏览器+轮询）；manual 档带 manual_command
export type ApiCliAuthStart = {
  ok: boolean;
  url: string | null;
  manual_command: string | null;
  error: string | null;
};

export type ApiCliAuthStatus = { authenticated: boolean; error: string | null };

// 自定义命令「测试」结果（失败也是 200——失败是数据）：近 24h 窗真跑一遍采集
// 命令 + stdout 启发式条目数；结果同时落库（test_* 徽标展示）
export type ApiCliTest = {
  ok: boolean;
  latency_ms: number | null;
  items_count: number | null;
  error: string | null;
};

// 窄口自定义只读命令入参（argv 全静态 token，占位仅 {since}/{until}）
export type CustomCliBody = { name: string; bin_path: string; argv: string[] };

// 行更新入参：启停 + 自定义行编辑（编辑须 name/bin_path/argv 三字段全量同给，
// 精选行拒绝编辑——后端校验）
export type CliToolUpdateBody = {
  enabled?: boolean;
  name?: string;
  bin_path?: string;
  argv?: string[];
};

export const fetchCliTools = () => getJson<ApiCliTool[]>("/api/cli/tools");

// 探测超时 30s > 后端 version/auth 两段各 10s 之和（探测挂起时按钮必然复位）
export const probeCliTool = (id: string) =>
  request<ApiCliProbe>("POST", `/api/cli/tools/${id}/probe`, undefined, 30_000);

// 自定义行测试超时 70s > 后端 run_cli 60s（伪 spec fetch 默认超时）
export const testCustomCli = (id: string) =>
  request<ApiCliTest>("POST", `/api/cli/tools/${id}/test`, undefined, 70_000);

// 表单态直测（「保存前先试」）：不建行不落库，用表单当前值直接跑
export const testCustomCliDraft = (body: CustomCliBody) =>
  request<ApiCliTest>("POST", "/api/cli/tools/custom/test", body, 70_000);

// 未安装 422（detail 引导安装）；未知适配器 404
export const connectCliTool = (id: string) => request<ApiCliTool>("POST", `/api/cli/tools/${id}/connect`);

// 启停（后端联动镜像 ingest_sources.enabled）+ 自定义行编辑（「配置」）
export const updateCliTool = (id: string, body: CliToolUpdateBody) =>
  request<ApiCliTool>("PUT", `/api/cli/tools/${id}`, body);

// 删除只动 Clara 侧接入行，CLI 自己的凭据留在系统钥匙串（重接入免再授权）
export const deleteCliTool = (id: string) => request<void>("DELETE", `/api/cli/tools/${id}`);

// 发起授权：后端 spawn auth login ≤30s，前端 35s 兜底
export const startCliAuth = (id: string) =>
  request<ApiCliAuthStart>("POST", `/api/cli/tools/${id}/auth/start`, undefined, 35_000);

export const fetchCliAuthStatus = (id: string) =>
  getJson<ApiCliAuthStatus>(`/api/cli/tools/${id}/auth/status`);

export const createCustomCli = (body: CustomCliBody) =>
  request<ApiCliTool>("POST", "/api/cli/tools/custom", body);

// —— 聊天通讯渠道（/api/chat-channels，2026-10-23；形状镜像 schemas.py，snake_case 直消费）——

// 渠道卡（三渠道目录 + DB 行状态合并；app_secret 原样回显供编辑回填——
// Composio key 先例，本地单用户明文定调，前端密码态 + 眼睛查看）
export type ApiChatChannel = {
  provider: string; // feishu / dingtalk / wechat
  name: string;
  desc: string;
  setup_url: string | null; // 官方文档外链（凭据申请指引）
  placeholder: boolean; // true = 建设中占位（微信，恒未配置不可接）
  configured: boolean; // DB 行存在 = 已填凭据
  app_id: string | null; // 编辑表单回填（未配置恒空）
  app_secret: string | null; // 同上；密码态显示 + 眼睛切换查看
  enabled: boolean; // worker 装载开关（false = 不监听消息）
  push_enabled: boolean; // 定时任务推送开关（晨报/会前准备等也发该渠道）
  push_ready: boolean; // 推送目标已缓存（用户与机器人有过对话）
  conn_ok: boolean | null; // 上次凭据探测；null = 从未
  conn_error: string | null;
  conn_at: string | null;
};

// 保存/更新凭据（保存即探测）；编辑形态 app_secret 留空 = 保持旧值
export type ChatChannelUpsertBody = { app_id: string; app_secret: string };

export const fetchChatChannels = () => getJson<ApiChatChannel[]>("/api/chat-channels");

export const saveChatChannel = (provider: string, body: ChatChannelUpsertBody) =>
  request<ApiChatChannel>("PUT", `/api/chat-channels/${provider}`, body);

export const testChatChannel = (provider: string) =>
  request<ApiChatChannel>("POST", `/api/chat-channels/${provider}/test`);

export const toggleChatChannel = (provider: string, enabled: boolean) =>
  request<ApiChatChannel>("PUT", `/api/chat-channels/${provider}/enabled`, { enabled });

// 定时任务推送开关（与监听启停正交：监听关、推送仍可开）
export const toggleChannelPush = (provider: string, pushEnabled: boolean) =>
  request<ApiChatChannel>("PUT", `/api/chat-channels/${provider}/push`, {
    push_enabled: pushEnabled,
  });

export const deleteChatChannel = (provider: string) =>
  request<void>("DELETE", `/api/chat-channels/${provider}`);

// 微信扫码登录（wechat 专用；凭据是扫码形态不收粘贴——qr_url 为二维码载体
// URL 字符串，状态机见 services/chat_channels.py poll_wechat_login docstring）
export type ApiWechatLogin = { ticket: string; qr_url: string };
export type ApiWechatLoginStatus = {
  status: string; // wait / scaned / confirmed / expired / need_verifycode / …
};

export const startWechatLogin = () =>
  request<ApiWechatLogin>("POST", "/api/chat-channels/wechat/login");

export const pollWechatLoginStatus = (ticket: string, verifyCode?: string) =>
  getJson<ApiWechatLoginStatus>(
    `/api/chat-channels/wechat/login/status?ticket=${encodeURIComponent(ticket)}` +
      (verifyCode ? `&verify_code=${encodeURIComponent(verifyCode)}` : "")
  );
