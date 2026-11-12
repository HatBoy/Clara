// 阶段二 AI 助手数据接入（/api/ai/*；形状镜像后端 schemas.py，snake_case 直消费
// 口径同 api.ts 的 models/mcp 节——无单位换算字段，不做 toFrontend/toPatch 双程适配）。
// 请求底座复用 api.ts 的 getJson/request（同一 API_BASE 与错误口径）；SSE 为 POST
// 流（EventSource 不支持 POST），fetch + ReadableStream 手解析（协议见后端
// routers/ai_coach.py 模块头：": connected" 注释行 + "data: {json}\n\n" 帧）。
import { API_BASE, ApiTask, epochDayOf, getJson, request } from "./api";
import type { Recurrence } from "../data/tasks";

// —— 会话历史（GET /messages，时间正序）——
export type AiMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  proposal_id: string | null; // assistant 消息可关联一条建议行
  source: string | null; // 来源渠道（null = 桌面端；"feishu"/"dingtalk" = 通讯工具轮）
  created_at: string;
};

export type ProposalOutcome = "accepted" | "modified" | "rejected" | "ignored";

// 建议行（GET /proposals）：outcome 空 = 待处理（expired 只由后端夜间整理落）
export type AiProposal = {
  id: string;
  kind:
    | "schedule"
    | "preflight"
    | "energy_tag"
    | "inbox"
    | "delete"
    | "question"
    | "prep" // 会前准备清单卡（2026-10-20）
    | "hardtask" // 高难任务协助卡（2026-10-20）
    | "brief" // 自定义定时简报卡（2026-10-20）
    | "minutes" // 会后纪要行动项卡（2026-11-10）
    | "minutes_invite"; // 会后纪要邀请卡（批 3 tick 流，每日至多一次）
  payload: Record<string, unknown>;
  outcome: ProposalOutcome | "expired" | null;
  created_at: string;
  resolved_at: string | null;
};

// 删除确认卡条目（payload.tasks；确认后经前端既有 DELETE 管线执行）
export type DeletePlanItem = {
  task_id: string;
  title: string;
};

// 排期草案条目（接缝契约，docs/ai-agents.md §六：start 为 naive 本地 ISO，
// duration 分钟；end 由前端换算。origin/explicit_time 为 2026-10-17 增的条目级
// 附加键——agent 草案不带，仅快录/applied 载荷出现，前端不消费只透传类型）
export type DraftPlanItem = {
  task_id: string | null;
  title: string;
  start: string;
  duration: number;
  origin?: "user" | "fuzzy" | "agent";
  explicit_time?: string;
  priority?: number; // 明示重要信号直判高优（2026-10-22）——新建采纳时透传落库
  // 溯源附加键（2026-10-24 直排批）：数据源页 schedule 条目确认 / inbox 条目
  // 手选时间直排时由面板组装——applyDraftItems 采纳落库随溯源三键透传
  source_ref?: { source_key: string; fingerprint?: string } | null;
  evidence?: string | null;
  // 编辑附加键（2026-10-26 收件箱 UI 对齐批）：数据源页编辑态组装（备注 +
  // 重复规则预设），applyDraftItems 落库随 createTask 透传——agent 草案不带
  notes?: string | null;
  recurrence?: Recurrence | null;
};

// 溯源条目（M1-①：schedule 卡 payload.evidence，idx 对齐 draft_plan 序；
// 旧卡无此键不渲染）
export type EvidenceEntry = {
  idx: number;
  source: string;
  source_key: string;
  item_id: string;
  title: string;
  time: string | null;
  fingerprint: string;
};

// 收件箱建议条目（M1-②：inbox 卡 payload.items；source_ref 缺省可空；evidence 为
// 编排侧按指纹回填的素材摘录——采纳时随溯源三键透传落任务行，2026-10-23）
export type InboxItem = {
  title: string;
  duration?: number | null; // 分钟 1-1440 | 未估时
  notes?: string | null;
  priority?: number | null; // 1 高 2 中 3 低（口径同 TaskCreateBody；采集建议不带键 → 默认中）
  evidence?: string | null; // 来源素材摘录（Reduce 后滤时按指纹对候选回填）
  // 重复规则预设（2026-10-26 收件箱 UI 对齐批）：数据源页编辑态组装，未排期
  // 建任务时随 recurrence_rule 落库成系列——Reduce 产物不带
  recurrence?: Recurrence | null;
  source_ref?: {
    source: string;
    source_key: string;
    item_id: string;
    title: string;
    time: string | null;
    fingerprint: string;
  } | null;
};

export const fetchAiMessages = () => getJson<AiMessage[]>("/api/ai/messages");

export const fetchAiProposals = (unresolved = false) =>
  getJson<AiProposal[]>(`/api/ai/proposals${unresolved ? "?unresolved=true" : ""}`);

// 建议回写（采纳率统计信号）；拒绝可选填理由（夜间学习信号）；
// appliedEntries（优化批 4 modified 回流）= 修改后采纳时编辑过的条目清单——
// 后端与 payload 快照比对生成 modified_diff，夜间反思链学习「用户改成了什么」
export const resolveProposal = (
  id: string,
  outcome: ProposalOutcome,
  reason?: string,
  appliedEntries?: unknown,
) =>
  request<AiProposal>("POST", `/api/ai/proposals/${id}/outcome`, {
    outcome,
    reason,
    applied_entries: appliedEntries ?? undefined,
  });

// 条目级部分结算（2026-10-24 数据源页单条操作）：从待处理卡移除 indexes 条目
// （后端 payload 收缩，卡清空才回写 outcome）；拒绝只静默被拒条目的指纹；
// appliedEntries 同上（条目级 modified 的偏离比对）
export const settleProposalItems = (
  id: string,
  indexes: number[],
  outcome: "accepted" | "modified" | "rejected",
  appliedEntries?: unknown[],
) =>
  request<AiProposal>("POST", `/api/ai/proposals/${id}/settle-items`, {
    indexes,
    outcome,
    applied_entries: appliedEntries ?? undefined,
  });

// 会后纪要提炼（2026-11-10）：纪要原文 → LLM 提炼行动项 → minutes 建议卡
// （提取不生成，README 反定位红线）。taskId 可空（聊天纯文本入口无关联会议）；
// 同步秒级返回建好的建议行，面板消息/广播由后端落
export const submitMinutes = (taskId: string | null, text: string) =>
  request<AiProposal>("POST", "/api/ai/meeting-minutes", {
    task_id: taskId ?? undefined,
    text,
  });

// 数据源页过滤与条目解析（面板渲染与 App 徽标共源）：from_ingest 标记的待处理
// inbox/schedule 卡才是采集建议第二视图；条目数（而非卡数）是徽标口径
export const isIngestProposal = (p: AiProposal) =>
  p.outcome === null &&
  (p.kind === "inbox" || p.kind === "schedule") &&
  p.payload?.from_ingest === true;

export const ingestItemsOf = (p: AiProposal): (InboxItem | DraftPlanItem)[] =>
  p.kind === "inbox"
    ? ((p.payload.items as InboxItem[]) ?? [])
    : ((p.payload.draft_plan as DraftPlanItem[]) ?? []);

// 条目来源判定（2026-10-26 无来源批）：inbox 看 source_ref、schedule 看
// evidence per idx 覆盖。Reduce 白名单后滤已升级为无来源条目整条丢弃（见
// agents/graph.py），此判定兜历史旧卡——升级前建的卡可能缺溯源键，按「数据
// 源页每条建议必有明确来源」的产品规则不显示也不计徽标（两处消费共源）
export const entryHasSource = (p: AiProposal, idx: number): boolean =>
  p.kind === "inbox"
    ? Boolean((p.payload.items as InboxItem[] | undefined)?.[idx]?.source_ref)
    : ((p.payload.evidence as { idx: number }[] | undefined) ?? []).some(
        (e) => e.idx === idx,
      );

// —— 记忆画像（/memory-rules；白盒界面 CRUD，手写入口恒 user_manual/1.0）——
export type AiMemoryRule = {
  id: string;
  category: string;
  content: string;
  // 固定槽位（2026-11-03 记忆重构）：rhythm/meeting/boundary/other——设置页分组
  // 展示；null = 旧行待夜间回填（渲染归 other 组）
  slot: string | null;
  source: "ai_reflect" | "user_manual" | "chat"; // chat = 对话设置的偏好（2026-10-22 批 3，与手动同信任级）
  confidence: number; // 0–1（注入序：user_manual 优先 → chat 次之 → confidence 降序）
  evidence: string | null; // ai_reflect 必带：结论的证据出处（chat 带"对话明示：原话"）
  // 高置信规则的结构化避让窗锚（非空 = 已生效为排期硬约束，行显徽标）
  structured_anchor: {
    weekday?: number | null;
    start_min: number;
    end_min: number;
    rule?: string;
  } | null;
  last_confirmed_at: string | null;
  observation_count: number; // 同义印证计数（≥3 自动升验证级——置信演化白盒面）
  created_at: string;
  updated_at: string;
};

export type MemoryRuleBody = {
  category?: string;
  content?: string;
  evidence?: string | null; // 显式 null = 清空
};

export const fetchMemoryRules = () => getJson<AiMemoryRule[]>("/api/ai/memory-rules");

export const createMemoryRule = (body: { category: string; content: string; evidence?: string | null }) =>
  request<AiMemoryRule>("POST", "/api/ai/memory-rules", body);

export const updateMemoryRule = (id: string, body: MemoryRuleBody) =>
  request<AiMemoryRule>("PATCH", `/api/ai/memory-rules/${id}`, body);

export const deleteMemoryRule = (id: string) =>
  request<void>("DELETE", `/api/ai/memory-rules/${id}`);

// 冲突预检（2026-11-03 记忆重构，用户拍板「冲突提示用户确认」）：保存前纯规则
// difflib 相似度初筛（零 LLM 秒回），有疑似冲突由用户裁决「合并覆盖 / 两者并存」
export type RuleConflict = {
  id: string;
  category: string;
  content: string;
  source: AiMemoryRule["source"];
  similarity: number;
};

export const checkRuleConflicts = (body: {
  category: string;
  content: string;
  exclude_id?: string;
}) =>
  request<{ conflicts: RuleConflict[] }>(
    "POST",
    "/api/ai/memory-rules/conflict-check",
    body,
  );

// —— 情节事实（Tier 2，2026-11-03 记忆重构）：带 TTL 的临时状态白盒清单 ————
export type AiEpisodicFact = {
  id: string;
  topic: string;
  fact: string;
  source: string; // chat = 对话同跳提取 / reflect = 夜间兜底
  confidence: number;
  expires_at: string | null; // null = 永久
  created_at: string;
};

export const fetchEpisodicFacts = () =>
  getJson<AiEpisodicFact[]>("/api/ai/episodic-facts");

export const deleteEpisodicFact = (id: string) =>
  request<void>("DELETE", `/api/ai/episodic-facts/${id}`);

// 内置管理哲学预设（个性化设置页卡片；text 不出——提示词只留后端单源）
export type Philosophy = {
  id: string;
  name: string;
  desc: string;
  // 与之理念冲突的其他预设 id（前端拦截互选）
  conflicts: string[];
};

export const fetchPhilosophies = () => getJson<Philosophy[]>("/api/ai/philosophies");

// —— 定时任务（/api/jobs，2026-10-20 增；文案与识别口径后端单源）——
export type Job = {
  key: string; // morning / ingest / nightly / prep / hardtask / heartbeat / brief:<uuid>
  title: string;
  desc: string;
  enabled: boolean;
  requires_ai: boolean; // true = 受 AI 总闸（关时页面盖「已随 AI 关闭」角标）
  time_min: number | null; // 自 0 点起的分钟数；null = 非时刻驱动（心跳）
  time_note: string | null; // 时刻之外的补充说明（如"空闲时"）
  last_run_at: string | null; // 最近认领时刻（无记录 null，不谎报成功）
};

export type CustomBrief = {
  id: string;
  brief_min: number; // 自 0 点起的分钟数（30 粒度）
  instruction: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export const fetchJobs = () => getJson<{ jobs: Job[] }>("/api/jobs");

export const fetchBriefs = () => getJson<CustomBrief[]>("/api/jobs/briefs");

export const createBrief = (body: { brief_min: number; instruction: string }) =>
  request<CustomBrief>("POST", "/api/jobs/briefs", body);

export const updateBrief = (
  id: string,
  body: { brief_min?: number; instruction?: string; enabled?: boolean },
) => request<CustomBrief>("PATCH", `/api/jobs/briefs/${id}`, body);

export const deleteBrief = (id: string) => request<void>("DELETE", `/api/jobs/briefs/${id}`);

// 清空会话（/clear 命令直调，不经 SSE 零 LLM 成本——消息全删 + 滚动摘要游标重置）
export const clearAiSession = () => request<void>("POST", "/api/ai/session/clear");

// 手动压缩（/compact 命令直调，2026-10-26 增）：强制把未摘要消息并入滚动摘要
// （近期原文不动）；compacted = 并入条数，reason 见 SessionCompactOut 词汇表
export const compactAiSession = () =>
  request<{ compacted: number; reason: string | null }>("POST", "/api/ai/session/compact");

// —— POST /chat 的 SSE 事件（data 帧的 type 判别联合）——
export type ChatEvent =
  | { type: "message"; role: string; content: string } // 用户输入回显（已持久化）
  | { type: "delta"; text: string } // 回复正文增量（打字机；仅 qa/smalltalk 流式分支产生）
  | { type: "status"; text: string } // 阶段反馈（2026-10-26 提速批 2）：非流式分支重节点入口一帧，替换静态「思考中…」
  | { type: "reply"; content: string } // final_reply 整段（权威文本，替换 delta 累积）
  | { type: "draft"; proposal_id: string; draft_plan: DraftPlanItem[] } // 幽灵方块草案
  | { type: "proposal"; proposal_id: string; kind: string; payload: Record<string, unknown> }
  | { type: "applied"; proposal_id: string; tasks: ApiTask[] } // 快录直落（origin=user 已写库，App 侧直通画布）
  | { type: "ingest_done"; count: number } // 采集轮收尾（2026-10-25 功能边界）：卡只落数据源页，App 刷徽标
  | { type: "bulk"; proposal_id: string; draft_plan: DraftPlanItem[] } // 批量排期草案（功能边界批）：App 开弹窗清单确认，对话框零草案卡
  | { type: "done" }
  | { type: "error"; code: string; message: string }; // no_model / not_ready / disabled / agent_error

/**
 * 对话流：POST /api/ai/chat 逐帧回调（注释行 / 坏帧静默丢弃——提示与纠偏以
 * finally 重拉真值为准）。fetch 流读到 EOF 即返回（done 帧后服务端收流）；
 * signal 透传 AbortController（挂载卸载时中断在途流）。
 */
export async function streamChat(
  message: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/ai/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`AI 对话请求失败: ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 帧以空行（\n\n）分隔；帧内取 "data: " 前缀行拼载荷（本协议恒单行）
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const data = frame
        .split("\n")
        .filter((l) => l.startsWith("data: "))
        .map((l) => l.slice(6))
        .join("\n");
      if (!data) continue; // ": connected" / ": ping" 注释帧
      try {
        onEvent(JSON.parse(data) as ChatEvent);
      } catch {
        // 坏帧丢弃
      }
    }
  }
}

// —— 幽灵方块（draft 条目 → 中栏时间线几何；口径同 toFrontendTask）——
export type GhostBlock = {
  dateOffset: number; // 相对今天 0 点的天数
  start: number; // 小时小数
  end: number; // start + duration/60（可越 24 = 跨天，画布自然裁剪）
  title: string;
};

export function draftToGhost(item: DraftPlanItem): GhostBlock {
  // naive 本地 ISO → new Date 按本地解析（与后端存-naive-本地对齐）
  const s = new Date(item.start);
  const hour = s.getHours() + s.getMinutes() / 60;
  return {
    dateOffset: epochDayOf(s) - epochDayOf(new Date()),
    start: hour,
    end: hour + item.duration / 60,
    title: item.title,
  };
}
