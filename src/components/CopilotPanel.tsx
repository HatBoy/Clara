import { memo, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ArrowUp, Check, ChevronDown, Flag, HelpCircle, PanelRightClose, Pencil, RotateCcw, Sparkles, Timer, Trash2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  clearAiSession,
  compactAiSession,
  fetchAiMessages,
  fetchAiProposals,
  resolveProposal,
  settleProposalItems,
  streamChat,
  submitMinutes,
  type AiProposal,
  type DeletePlanItem,
  type DraftPlanItem,
  type EvidenceEntry,
  type InboxItem,
} from "../lib/ai";
import { fetchModels, type ApiTask } from "../lib/api";
import { onShortcutCmd } from "../lib/shortcuts";
import { PRIORITY_COLORS } from "../data/tasks";
// 草案编辑区与收件箱待办编辑区同形态（2026-10-26 UI 对齐批）：日期药丸与耗时
// 档位复用共享组件 / 常量，优先级圆点复用 inbox-todo-prio-item 类族（CSS 单源）
import WhenPicker from "./WhenPicker";
import { DURATION_PRESETS } from "./InboxPanel";
import "./CopilotPanel.css";

// 面板内消息（后端 AiMessage 的消费子集；发送中的气泡无后端 id 用递增临时键）
type PanelMessage = {
  key: string;
  role: "user" | "assistant";
  content: string;
  proposalId: string | null;
  source?: string | null; // 来源渠道徽标（null = 桌面端；feishu/dingtalk = 通讯工具轮）
  error?: boolean; // 后端 error 帧 / 网络失败 → 警示样式气泡
  streaming?: boolean; // 打字机流式中（纯文本 + 光标；reply 终帧撤除切 md 渲染）
};

// 渠道徽标文案（provider → 展示名；未知 provider 兜底原值——目录变更不炸渲染）
const SOURCE_LABELS: Record<string, string> = {
  feishu: "飞书",
  dingtalk: "钉钉",
  wecom: "企业微信",
  wechat: "微信",
};

// 问号按钮悬停语句（2026-10-20）：覆盖 chat 六意图 + 批量排期，点击直接发送——
// 新用户快速体验的入口（批量排期引导文字已从收件箱 footer 移除，此处统一承接）；
// 末两条是本地指令（纯指令名直发即执行，2026-10-21 按用户要求去说明后缀）
const HINT_PHRASES = [
  "把所有待办排期",
  "记一下，明天上午10点体检",
  "今天有什么安排？",
  "明天的高优任务我要怎么准备？",
  "我现在该做什么？",
  "今天太忙了，帮我理一理",
  "看看接入源里有什么要做的",
  "删掉牙医",
  "讲个冷笑话",
  "/clear",
  "/compact",
  "/stop",
];

// 本地指令表（2026-10-21）：输入 "/" 联想选择，精确匹配即执行——不经 SSE
// 零 LLM 成本零延迟。只收对话框做不到或很绕的操作（清空重来 / 打断生成 /
// 手动整理上下文）；排期、查询、删除本来就是对话强项，不做指令
const COMMANDS = [
  { name: "/clear", desc: "清空会话历史" },
  { name: "/compact", desc: "压缩对话上下文" },
  { name: "/stop", desc: "停止当前回复" },
];

// 助手气泡的 Markdown 渲染（2026-10-16）：react-markdown + GFM（表格 / 删除线 /
// 任务列表）；默认不渲染原始 HTML（LLM 输出按不可信内容处理，无 XSS 面）。
// memo：流式 rAF 高频刷新时未变气泡保持引用稳定，跳过 md 重解析。
const CopilotMarkdown = memo(function CopilotMarkdown({ content }: { content: string }) {
  return (
    <div className="copilot-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
});

// 报告类卡（2026-10-25 问题 1 修复）：preflight 日程简报 / brief 自定义简报 /
// prep 会前准备是纯信息输出，采纳/拒绝无任何落库副作用——按钮无意义，直接
// 展示内容即可（行保持 pending 由过期清理收，不污染采纳率统计的分子）
const REPORT_KINDS = new Set(["preflight", "brief", "prep"]);

// 条目确认卡（2026-11-11 三件套统一批）：有真实采纳语义且条目可逐条编辑/删除
// 的四类——schedule（排期草案）/ inbox（收件箱清单）/ hardtask（任务协助）/
// minutes（会后纪要）。delete 卡是单条删除确认（编辑无对象）、REPORT_KINDS 是
// 纯信息卡、minutes_invite 的补记/不用即是其采纳/拒绝形态——均不在此列
const EDITABLE_KINDS = new Set(["schedule", "inbox", "hardtask", "minutes"]);

// 建议卡（assistant 气泡下结构化卡片）：待处理时按 kind 出按钮——schedule 草案
// 采纳 / 修改 / 拒绝 + 条件「转收件箱」，inbox/hardtask/delete 两钮（采纳有
// 落库副作用：收件箱条目落库 / 步骤入收件箱 / 确认删除），报告类无按钮；
// 已决渲染徽标。「修改」进卡内逐条编辑（2026-10-25 问题 3 修复：旧"修改后
// 采纳"无编辑入口点击即回写，modified 口径失真），编辑后条目经 onModify 走
// App 落库 + modified 回写；「转收件箱」承接"不排期但保留为待办"心智
// （onSendToInbox），仅含新建条目（无 task_id）的卡显示——条目本就来自收件箱
// 时（夜间重要整理预排 / 批量排期）该按钮无意义，隐藏（2026-10-26）。
// 回写统一经 onSettle 上抛父组件（乐观改行 + 异步落库都在一处）
function ProposalCard({
  proposal,
  onAccept,
  onSettle,
  onModify,
  onModifyInbox,
  onModifyMinutes,
  onModifySteps,
  onRemoveEntries,
  onSendToInbox,
  onAcceptMinutes,
  onInviteSubmit,
}: {
  proposal: AiProposal;
  onAccept: () => void;
  onSettle: (outcome: "modified" | "rejected", reason?: string) => void;
  // 编辑后条目上抛（schedule 专属）：App applyDraftItems 落库 + modified 回写
  onModify: (items: DraftPlanItem[]) => void;
  // 编辑后条目上抛（2026-11-11 确认卡三件套统一批）：inbox / minutes 走
  // applyInboxItems、hardtask 走 applyHardtaskSteps，均 modified 回写
  onModifyInbox?: (items: InboxItem[]) => void;
  onModifyMinutes?: (items: InboxItem[]) => void;
  onModifySteps?: (steps: string[]) => void;
  // 编辑态删除条目上抛（四类条目卡通用，2026-11-11）：settle-items(rejected)
  // 收缩卡 payload（有 task_id 的任务本体不动、自然回未排期收件箱；agent 新建
  // 条目不落库；hardtask 步骤不建）；卡清空由后端自动回写整卡 rejected
  onRemoveEntries?: (indexes: number[]) => Promise<boolean>;
  // 条目剥时间转收件箱（schedule 专属）：App applyInboxItems + accepted 回写
  onSendToInbox: () => void;
  // 会后纪要采纳（minutes 专属，2026-11-10）：全量条目上抛，App 溯源组装后
  // applyInboxItems 入收件箱 + accepted 回写；2026-11-11 三件套对齐——勾选
  // 与「采纳并安排」直排退役，选择性取舍统一走编辑态逐条删除
  onAcceptMinutes?: (items: InboxItem[]) => void;
  // 邀请卡补记提交（minutes_invite 专属，批 3）：纪要原文直提 → 新 minutes 卡；
  // 返回错误文案或 null（成功），卡内显示
  onInviteSubmit?: (text: string) => Promise<string | null>;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  // 溯源展开（M2-④：evidence/source_ref 白盒可审计；旧卡无键不显示展开钮）
  const [traceOpen, setTraceOpen] = useState(false);
  // 草案编辑态（2026-10-25；2026-10-26 收件箱编辑区 UI 对齐）：edits 与
  // draftItems 按下标对位；duration 改档位数字（0 = 未选，回写回落原值）、
  // start 与 WhenPicker 值同格式（naive ISO 截 "YYYY-MM-DDTHH:mm"）
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<{ title: string; minutes: number; priority: number; start: string }[]>([]);
  // 编辑态删除标记（2026-11-11）：标删行灰化划线可恢复（钮变 RotateCcw），确认
  // 时走 onRemoveEntries（settle rejected）+ 保留条目照常 modified；取消编辑全弃
  const [removed, setRemoved] = useState<Set<number>>(new Set());
  // 多条目时日期月历单开（同时至多一条的 WhenPicker 展开，互斥下标记录）
  const [whenIdx, setWhenIdx] = useState<number | null>(null);
  // 邀请卡补记态（批 3）：收起 = 「补记纪要 / 本次不用」两钮；展开 = textarea
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteText, setInviteText] = useState("");
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const pending = proposal.outcome === null;

  // 问询行不渲染卡（2026-10-17）：排期问询只留 assistant 文字气泡，回复即确认
  // （"可以"/新时间）——出卡会把确认引导成按钮点击，破坏对话流；重拉历史时
  // question 行经消息 proposalId 关联到这里被跳过
  if (proposal.kind === "question") return null;

  const draftItems =
    proposal.kind === "schedule" ? ((proposal.payload.draft_plan as DraftPlanItem[]) ?? []) : [];
  const inboxItems =
    proposal.kind === "inbox" ? ((proposal.payload.items as InboxItem[]) ?? []) : [];
  const minutesItems =
    proposal.kind === "minutes" ? ((proposal.payload.items as InboxItem[]) ?? []) : [];
  const hardtaskSteps =
    proposal.kind === "hardtask" ? ((proposal.payload.steps as string[]) ?? []) : [];
  const deleteItems =
    proposal.kind === "delete" ? ((proposal.payload.tasks as DeletePlanItem[]) ?? []) : [];
  const evidence =
    proposal.kind === "schedule" ? ((proposal.payload.evidence as EvidenceEntry[]) ?? []) : [];
  // 溯源行：schedule 按 evidence.idx 对齐 draft_plan 序，inbox 逐条 source_ref；缺失 →「无溯源」
  const traceRows =
    proposal.kind === "schedule"
      ? draftItems.map((item, i) => {
          const ev = evidence.find((e) => e.idx === i);
          return ev
            ? `「${item.title}」 来自 ${ev.source} ·「${ev.title}」${fmtTraceDate(ev.time)}`
            : `「${item.title}」 · 无溯源`;
        })
      : inboxItems.map((item) => {
          const ref = item.source_ref ?? null;
          return ref
            ? `「${item.title}」 来自 ${ref.source} ·「${ref.title}」${fmtTraceDate(ref.time)}`
            : `「${item.title}」 · 无溯源`;
        });
  const hasTrace =
    proposal.kind === "schedule" ? evidence.length > 0 : inboxItems.some((it) => it.source_ref);

  // 编辑条目源（四类条目卡统一装载形态：hardtask steps 是纯标题无耗时/优先级，
  // 装载 0/2 不渲染对应控件；schedule 另带 start）
  const editSource: { title: string; duration?: number | null; priority?: number | null; start?: string }[] =
    proposal.kind === "schedule" ? draftItems
      : proposal.kind === "hardtask" ? hardtaskSteps.map((s) => ({ title: s }))
      : proposal.kind === "minutes" ? minutesItems
      : inboxItems;

  // 进入编辑（2026-11-11 三件套统一批扩到四卡）：条目装载为可编辑形态
  //（start naive ISO 截成 WhenPicker 值，优先级缺省 2=中；minutes 0 = 非档位值
  // 档位全不亮，回写回落原值）
  const openEdit = () => {
    setEdits(editSource.map((it) => ({
      title: it.title,
      minutes: it.duration ?? 0,
      priority: it.priority ?? 2,
      start: (it.start ?? "").slice(0, 16),
    })));
    setRemoved(new Set());
    setWhenIdx(null);
    setEditing(true);
  };

  // 确认编辑：标删条目先走 onRemoveEntries（settle rejected 收缩卡——先收缩再
  // modified 回写，applied_entries 与收缩后快照对位反而更准），保留条目按卡分流
  // 上抛：schedule 走 onModify（applyDraftItems 带时间落库）、inbox/minutes 走
  // applyInboxItems、hardtask 走 applyHardtaskSteps。编辑值统一规则：标题空串
  // 回落原值、耗时空档位回落原值（不落 0）、start 空/清除回落原值（拼秒还原），
  // 原条目展开保留 task_id / source_ref（改期路径与溯源透传不断）。
  // 全删光 = 卡清空由后端自动回写 rejected，不再上抛保留条目
  const confirmEdit = async () => {
    const kept = editSource
      .map((it, i) => ({ it, d: edits[i], i }))
      .filter(({ i }) => !removed.has(i));
    const delIndexes = [...removed].sort((a, b) => a - b);
    setEditing(false);
    setWhenIdx(null);
    if (delIndexes.length) await onRemoveEntries?.(delIndexes);
    if (!kept.length) return;
    if (proposal.kind === "schedule") {
      onModify(kept.map(({ it, d }) => ({
        ...it,
        title: d.title.trim() || it.title,
        duration: d.minutes || it.duration,
        priority: d.priority,
        start: d.start ? `${d.start}:00` : it.start,
      })) as DraftPlanItem[]);
    } else if (proposal.kind === "hardtask") {
      onModifySteps?.(kept.map(({ it, d }) => d.title.trim() || it.title));
    } else {
      const items = kept.map(({ it, d }) => ({
        ...it,
        title: d.title.trim() || it.title,
        duration: d.minutes || it.duration,
        priority: d.priority,
      })) as InboxItem[];
      if (proposal.kind === "minutes") onModifyMinutes?.(items);
      else onModifyInbox?.(items);
    }
  };

  // 「转收件箱」条件（2026-10-26 删除批）：条目全带 task_id = 本就来自收件箱
  // （夜间重要整理预排 / 批量排期），剥时间转回无意义，隐藏；含新建条目的卡保留
  const inboxNative = draftItems.length > 0 && draftItems.every((it) => it.task_id);

  // 邀请卡补记提交（批 3）：纪要原文 → onInviteSubmit（面板调 submitMinutes +
  // accepted 结算 + 重拉）；错误文案留在卡内，成功由面板收卡
  const submitInvite = async () => {
    if (inviteBusy || !onInviteSubmit) return;
    const text = inviteText.trim();
    if (text.length < 10) {
      setInviteError("纪要原文至少 10 字");
      return;
    }
    setInviteBusy(true);
    setInviteError(null);
    const err = await onInviteSubmit(text);
    if (err) setInviteError(err);
    setInviteBusy(false);
  };

  return (
    <div className={`copilot-proposal${pending ? "" : " settled"}`}>
      <div className="copilot-proposal-head">
        <span className={`copilot-proposal-kind${proposal.kind === "delete" ? " danger" : ""}`}>
          {proposal.kind === "schedule"
            ? "排期草案"
            : proposal.kind === "preflight"
              ? "日程简报"
              : proposal.kind === "inbox"
                ? "收件箱清单"
                : proposal.kind === "delete"
                  ? "删除确认"
                  : proposal.kind === "prep"
                    ? "会前准备"
                    : proposal.kind === "hardtask"
                      ? "任务协助"
                      : proposal.kind === "brief"
                        ? "定时简报"
                        : proposal.kind === "minutes"
                          ? "会后纪要"
                          : proposal.kind === "minutes_invite"
                            ? "纪要邀请"
                            : "建议"}
        </span>
        {hasTrace && (
          <button
            type="button"
            className={`copilot-proposal-trace-toggle${traceOpen ? " open" : ""}`}
            aria-label={traceOpen ? "收起源链" : "展开溯源"}
            onClick={() => setTraceOpen((v) => !v)}
          >
            <ChevronDown size={13} strokeWidth={2} />
          </button>
        )}
        {!pending && (
          <span className={`copilot-badge ${proposal.outcome}`}>{badgeText(proposal.outcome)}</span>
        )}
      </div>

      {proposal.kind === "schedule" && !editing && (
        <ul className="copilot-proposal-items">
          {draftItems.map((item, i) => (
            <li key={i} className="copilot-proposal-item">
              <span className="copilot-proposal-item-title">{item.title}</span>
              <span className="copilot-proposal-item-meta">{fmtDraftItem(item)}</span>
            </li>
          ))}
        </ul>
      )}

      {/* 条目逐条编辑区（2026-10-25「修改」真编辑入口；2026-10-26 收件箱编辑区
          UI 对齐；2026-11-11 三件套统一批扩到四类条目卡——schedule 全字段
          （标题/WhenPicker/耗时/优先级）、inbox 与 minutes 无时间（标题/耗时/
          优先级）、hardtask 纯标题（标题）；统一条目删除：标删行灰化划线可恢复，
          确认时先 settle 收缩被删条目再走保留条目 modified */}
      {editing && (
        <div className="copilot-edit">
          {edits.map((d, i) => {
            const off = removed.has(i);
            return (
            <div key={i} className={`copilot-edit-row${off ? " removed" : ""}`}>
              <div className="copilot-edit-titleline">
                <input
                  className="copilot-edit-title"
                  value={d.title}
                  maxLength={60}
                  placeholder="标题"
                  onChange={(e) => setEdits((prev) => prev.map((p, j) => (j === i ? { ...p, title: e.target.value } : p)))}
                />
                <button
                  type="button"
                  className="copilot-edit-del"
                  aria-label={off ? "恢复此条" : "删除此条"}
                  title={off ? "恢复此条" : "删除此条（确认后从清单移除，不建任务）"}
                  onClick={() =>
                    setRemoved((prev) => {
                      const next = new Set(prev);
                      if (next.has(i)) next.delete(i);
                      else next.add(i);
                      return next;
                    })
                  }
                >
                  {off ? <RotateCcw size={13} strokeWidth={2} /> : <Trash2 size={13} strokeWidth={2} />}
                </button>
              </div>
              {proposal.kind === "schedule" && (
                <WhenPicker
                  value={d.start}
                  onChange={(v) => setEdits((prev) => prev.map((p, j) => (j === i ? { ...p, start: v } : p)))}
                  open={whenIdx === i}
                  onOpenChange={(o) => setWhenIdx(o ? i : null)}
                />
              )}
              {proposal.kind !== "hardtask" && (
              <div className="copilot-edit-attr">
                <div className="inbox-pill-group">
                  <Timer size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="耗时" />
                  {DURATION_PRESETS.map((p) => (
                    <button
                      key={p.minutes}
                      type="button"
                      className={`inbox-todo-duration${d.minutes === p.minutes ? " on" : ""}`}
                      onClick={() =>
                        setEdits((prev) => prev.map((q, j) => (j === i ? { ...q, minutes: p.minutes } : q)))
                      }
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <div className="inbox-pill-group">
                  <Flag size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="优先级" />
                  {(["high", "medium", "low"] as const).map((pl, pi) => (
                    <button
                      key={pl}
                      type="button"
                      style={{ color: PRIORITY_COLORS[pl] }}
                      className={`inbox-todo-prio-item${d.priority === pi + 1 ? " on" : ""}`}
                      aria-label={{ high: "高", medium: "中", low: "低" }[pl]}
                      title={`设为${{ high: "高", medium: "中", low: "低" }[pl]}优先级`}
                      onClick={() =>
                        setEdits((prev) => prev.map((q, j) => (j === i ? { ...q, priority: pi + 1 } : q)))
                      }
                    >
                      <i />
                    </button>
                  ))}
                </div>
              </div>
              )}
            </div>
            );
          })}
          <div className="copilot-edit-actions">
            <button type="button" className="copilot-prop-btn ghost" onClick={() => setEditing(false)}>
              取消
            </button>
            <button type="button" className="copilot-prop-btn primary" onClick={() => void confirmEdit()}>
              {removed.size === 0
                ? "确认采纳"
                : editSource.length - removed.size > 0
                  ? `确认采纳 ${editSource.length - removed.size} 条`
                  : "删除全部"}
            </button>
          </div>
        </div>
      )}

      {proposal.kind === "inbox" && !editing && (
        <ul className="copilot-proposal-items">
          {inboxItems.map((item, i) => (
            <li key={i} className="copilot-proposal-item">
              <span className="copilot-proposal-item-title">{item.title}</span>
              <span className="copilot-proposal-item-meta">
                {item.duration ? `${item.duration} 分钟` : "未估时"}
              </span>
            </li>
          ))}
        </ul>
      )}

      {proposal.kind === "delete" && (
        <ul className="copilot-proposal-items">
          {deleteItems.map((item, i) => (
            <li key={i} className="copilot-proposal-item">
              <span className="copilot-proposal-item-title">{item.title}</span>
              <span className="copilot-proposal-item-meta">确认后删除</span>
            </li>
          ))}
        </ul>
      )}

      {traceOpen && hasTrace && (
        <ul className="copilot-proposal-trace">
          {traceRows.map((row, i) => (
            <li key={i} className="copilot-proposal-trace-row">
              {row}
            </li>
          ))}
        </ul>
      )}

      {proposal.kind === "preflight" && (
        <div className="copilot-proposal-preflight">
          {typeof proposal.payload.headline === "string" && (
            <p className="copilot-proposal-headline">{proposal.payload.headline}</p>
          )}
          {Array.isArray(proposal.payload.checklist) && (
            <ul className="copilot-proposal-items">
              {(proposal.payload.checklist as string[]).map((line, i) => (
                <li key={i} className="copilot-proposal-item">
                  <span className="copilot-proposal-item-title">{line}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 会前准备卡（2026-10-20）：逐会议分组 = 时刻·会议名（主句）+ 准备重点（灰句）
          + 清单条目；多会议一次 LLM 合并产出，组间留白分隔 */}
      {proposal.kind === "prep" && (
        <div className="copilot-proposal-preflight">
          {(proposal.payload.meetings as PrepMeeting[] | undefined)?.map((m, i) => (
            <div key={m.task_id || i} className="copilot-proposal-prep-group">
              <p className="copilot-proposal-headline">
                {fmtPrepWhen(m.start)} · {m.title}
              </p>
              <p className="copilot-proposal-note">{m.headline}</p>
              <ul className="copilot-proposal-items">
                {m.checklist.map((line, j) => (
                  <li key={j} className="copilot-proposal-item">
                    <span className="copilot-proposal-item-title">{line}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {/* 高难协助卡（2026-10-20）：headline + 步骤清单 + 建议；采纳 = 各步骤
          逐条入收件箱（App 层落库），卡片本身不改原任务 */}
      {proposal.kind === "hardtask" && !editing && (
        <div className="copilot-proposal-preflight">
          {typeof proposal.payload.headline === "string" && (
            <p className="copilot-proposal-headline">{proposal.payload.headline}</p>
          )}
          {Array.isArray(proposal.payload.steps) && (
            <ul className="copilot-proposal-items">
              {(proposal.payload.steps as string[]).map((line, i) => (
                <li key={i} className="copilot-proposal-item">
                  <span className="copilot-proposal-item-title">{line}</span>
                </li>
              ))}
            </ul>
          )}
          {typeof proposal.payload.advice === "string" && proposal.payload.advice && (
            <p className="copilot-proposal-note">{proposal.payload.advice}</p>
          )}
          <p className="copilot-proposal-note">采纳后各步骤将加入收件箱</p>
        </div>
      )}

      {/* 定时简报卡（2026-10-20）：headline + Markdown 正文（指令驱动的汇总内容） */}
      {proposal.kind === "brief" && (
        <div className="copilot-proposal-preflight">
          {typeof proposal.payload.headline === "string" && (
            <p className="copilot-proposal-headline">{proposal.payload.headline}</p>
          )}
          {typeof proposal.payload.body === "string" && (
            <CopilotMarkdown content={proposal.payload.body} />
          )}
        </div>
      )}

      {/* 会后纪要卡（2026-11-10；2026-11-11 三件套对齐）：会议名·时刻头（时刻
          缺失省「 · 」分隔，未关联会议退占位）+ headline + 条目清单——与 inbox
          卡同构（标题左/时长右），取舍统一走编辑态逐条删除，勾选交互退役；
          零行动项卡仅总结回执 */}
      {proposal.kind === "minutes" && !editing && (
        <div className="copilot-proposal-preflight">
          <p className="copilot-proposal-headline">
            {typeof proposal.payload.meeting_title === "string" && proposal.payload.meeting_title
              ? [fmtPrepWhen(String(proposal.payload.meeting_start ?? "")), proposal.payload.meeting_title]
                  .filter(Boolean)
                  .join(" · ")
              : "未关联会议"}
          </p>
          {typeof proposal.payload.headline === "string" && proposal.payload.headline && (
            <p className="copilot-proposal-note">{proposal.payload.headline}</p>
          )}
          {minutesItems.length > 0 ? (
            <ul className="copilot-proposal-items">
              {minutesItems.map((item, i) => (
                <li key={i} className="copilot-proposal-item">
                  <span className="copilot-proposal-item-title">{item.title}</span>
                  <span className="copilot-proposal-item-meta">
                    {item.duration ? `${item.duration} 分钟` : "未估时"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="copilot-proposal-note">未识别出行动项</p>
          )}
        </div>
      )}

      {/* 会后纪要邀请卡（2026-11-10 批 3，tick 流每日至多一次）：纯规则零 LLM
          的轻询问——「补记纪要」卡内展开 textarea 直提（成功即出新 minutes 卡，
          本卡 accepted 收场）；「本次不用」= 拒绝（后端查重口径：该会议不再邀） */}
      {proposal.kind === "minutes_invite" && (
        <div className="copilot-proposal-preflight">
          <p className="copilot-proposal-headline">
            {typeof proposal.payload.meeting_title === "string" && proposal.payload.meeting_title
              ? `《${proposal.payload.meeting_title}》刚结束`
              : "会议刚结束"}
          </p>
          <p className="copilot-proposal-note">要补记纪要吗？粘贴原文即提炼行动项</p>
          {pending && onInviteSubmit && (
            inviteOpen ? (
              <>
                <div className="copilot-invite-wrap">
                  <textarea
                    className="copilot-invite-input"
                    placeholder="粘贴纪要原文…（Ctrl+Enter 提炼）"
                    maxLength={8000}
                    value={inviteText}
                    autoFocus
                    onChange={(e) => {
                      setInviteText(e.target.value);
                      if (inviteError) setInviteError(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing)
                        void submitInvite();
                    }}
                  />
                  <span className="copilot-invite-count">
                    {inviteText.length}/8000
                  </span>
                </div>
                <div className="copilot-edit-actions">
                  <button
                    type="button"
                    className="copilot-prop-btn ghost"
                    disabled={inviteBusy}
                    onClick={() => {
                      setInviteOpen(false);
                      setInviteError(null);
                    }}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="copilot-prop-btn primary"
                    disabled={inviteBusy}
                    onClick={() => void submitInvite()}
                  >
                    {inviteBusy ? "提炼中…" : "提炼行动项"}
                  </button>
                </div>
                {inviteError && <p className="copilot-invite-error">{inviteError}</p>}
              </>
            ) : (
              <div className="copilot-prop-actions">
                <button type="button" className="copilot-prop-btn primary" onClick={() => setInviteOpen(true)}>
                  <Check size={13} strokeWidth={2} />
                  补记纪要
                </button>
                <button type="button" className="copilot-prop-btn ghost" onClick={() => onSettle("rejected")}>
                  本次不用
                </button>
              </div>
            )
          )}
        </div>
      )}

      {pending &&
        !editing &&
        !REPORT_KINDS.has(proposal.kind) &&
        proposal.kind !== "minutes_invite" &&
        (rejecting ? (
          <div className="copilot-reject-row">
            <input
              className="copilot-reject-input"
              placeholder="拒绝理由（选填，供 AI 学习你的习惯）"
              maxLength={200}
              value={reason}
              autoFocus
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing)
                  onSettle("rejected", reason.trim() || undefined);
              }}
            />
            <button
              type="button"
              className="copilot-prop-btn danger"
              onClick={() => onSettle("rejected", reason.trim() || undefined)}
            >
              确认拒绝
            </button>
            <button
              type="button"
              className="copilot-prop-btn ghost"
              aria-label="取消拒绝"
              onClick={() => setRejecting(false)}
            >
              <X size={13} strokeWidth={2} />
            </button>
          </div>
        ) : (
          <div className="copilot-prop-actions">
            {proposal.kind === "minutes" && onAcceptMinutes ? (
              // 会后纪要采纳（2026-11-10；2026-11-11 三件套对齐）：全量采纳
              // 入收件箱，选择性取舍统一走编辑态；零行动项卡退「知道了」纯回写
              <button
                type="button"
                className="copilot-prop-btn primary"
                onClick={() => onAcceptMinutes(minutesItems)}
              >
                <Check size={13} strokeWidth={2} />
                {minutesItems.length === 0 ? "知道了" : "采纳"}
              </button>
            ) : (
              <button type="button" className="copilot-prop-btn primary" onClick={onAccept}>
                <Check size={13} strokeWidth={2} />
                {proposal.kind === "delete" ? "删除" : "采纳"}
              </button>
            )}
            {EDITABLE_KINDS.has(proposal.kind) && editSource.length > 0 && (
              <button type="button" className="copilot-prop-btn secondary" onClick={openEdit}>
                <Pencil size={13} strokeWidth={2} />
                编辑
              </button>
            )}
            {proposal.kind === "schedule" && !inboxNative && (
              <button type="button" className="copilot-prop-btn ghost" onClick={onSendToInbox}>
                转收件箱
              </button>
            )}
            <button type="button" className="copilot-prop-btn danger" onClick={() => setRejecting(true)}>
              拒绝
            </button>
          </div>
        ))}
    </div>
  );
}

const badgeText = (outcome: string | null) =>
  outcome === "accepted"
    ? "已采纳"
    : outcome === "modified"
      ? "已修改采纳"
      : outcome === "rejected"
        ? "已拒绝"
        : outcome === "ignored"
          ? "已忽略"
          : "已过期";

// 会前准备卡条目（payload.meetings，2026-10-20 增）：后端 LLM 只回 task_id/
// headline/checklist，title/start 由 graph 入口回填展示字段
type PrepMeeting = {
  task_id: string;
  title: string;
  start: string;
  headline: string;
  checklist: string[];
};

// 会议实例时刻 → "MM-DD HH:MM"（明日会议带日期防跨月歧义；naive ISO 按本地解析）
function fmtPrepWhen(start: string) {
  const d = new Date(start);
  if (Number.isNaN(d.getTime())) return "";
  const md = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${md} ${hhmm}`;
}

// draft 条目 → "HH:MM · N 分钟"（start 为 naive 本地 ISO，按本地解析同 toFrontendTask 口径）
function fmtDraftItem(item: DraftPlanItem) {
  const s = new Date(item.start);
  const hhmm = `${String(s.getHours()).padStart(2, "0")}:${String(s.getMinutes()).padStart(2, "0")}`;
  return `${hhmm} · ${item.duration} 分钟`;
}

// 溯源时间 → " MM-dd" 后缀（缺省/坏值 → 空串，行内其余部分照常显示）
function fmtTraceDate(time: string | null) {
  if (!time) return "";
  const d = new Date(time);
  if (Number.isNaN(d.getTime())) return "";
  return ` ${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// 右栏 · 日程助手（阶段二激活）：消息流 + 建议卡 + SSE 对话。header 视觉沿用阶段一；
// 发送流 = 乐观 user 气泡 + streamChat 逐帧入列（message 回显帧跳过防重复），finally
// 重拉真值纠偏。手写区未就绪时 error 帧（not_ready）显示为警示气泡（过渡期常态）；
// 零激活显示置顶引导卡（no_model 错误码的前端口径）。
function CopilotPanel({
  style,
  onCollapse,
  onOpenSettings,
  // AI 总闸（2026-10-21 五点修缮）：关闭时引导态——「AI 助手已关闭」引导卡 +
  // 输入禁用；总闸比零激活更高层，两者同关时本卡优先（开了总闸才轮到配模型）
  aiEnabled,
  draft,
  onDiscardDraft,
  onDraft,
  onAccept,
  onModify,
  onModifyInbox,
  onModifyMinutes,
  onModifySteps,
  onSendToInbox,
  onAcceptMinutes,
  onApplied,
  onIngestDone,
  onBulk,
  refreshKey = 0,
}: {
  style?: CSSProperties;
  onCollapse: () => void;
  // 引导卡直达设置页（menu 可指定初始分区，如 "model"）
  onOpenSettings: (menu?: string) => void;
  aiEnabled: boolean;
  // 当前幽灵方块草案（App 持有；null = 无）
  draft: { proposalId: string; items: DraftPlanItem[] } | null;
  onDiscardDraft: () => void;
  // draft 事件上抛（App 置幽灵方块）
  onDraft: (items: DraftPlanItem[], proposalId: string) => void;
  // 采纳建议（schedule 从 payload 重建草案应用；其余仅回写）——App 统一处理
  onAccept: (proposal: AiProposal) => void;
  // 草案编辑后采纳（2026-10-25 问题 3 修复）：面板逐条编辑的条目上抛，App
  // applyDraftItems 落库 + modified 回写（modified 从「假修改」变真）
  onModify: (proposal: AiProposal, items: DraftPlanItem[]) => void;
  // 条目卡编辑后采纳（2026-11-11 三件套统一批）：inbox/minutes 走
  // applyInboxItems、hardtask 走 applyHardtaskSteps，均 modified 回写
  onModifyInbox?: (proposal: AiProposal, items: InboxItem[]) => void;
  onModifyMinutes?: (proposal: AiProposal, items: InboxItem[]) => void;
  onModifySteps?: (proposal: AiProposal, steps: string[]) => void;
  // 草案转收件箱（2026-10-25）：条目剥时间转待办入收件箱，App applyInboxItems
  // + accepted 回写（承接「不排期但保留为待办」心智）
  onSendToInbox: (proposal: AiProposal) => void;
  // 会后纪要采纳（2026-11-10）：卡内勾选后的条目上抛，App applyInboxItems
  // 入收件箱 + accepted 回写（勾选项可能为空 = 零行动项卡纯回写）
  onAcceptMinutes?: (proposal: AiProposal, items: InboxItem[]) => void;
  // 快录直落（applied 帧，2026-10-17）：origin=user 已由后端写库任务直通 App 刷画布——
  // 不入面板本地列表（finally 重拉纠偏），面板自身零卡片零按钮
  onApplied: (tasks: ApiTask[]) => void;
  // 采集轮完成（ingest_done 帧，2026-10-25 功能边界）：建议卡只落数据源页，
  // 对话框零卡片——App 递增 aiRefreshKey 刷数据源徽标/面板（不弹通知）
  onIngestDone?: () => void;
  // 批量排期草案（bulk 帧，2026-10-25 功能边界）：App 开弹窗清单确认——
  // 对话框只沟通，批量确认收口弹窗（引擎时间建议经 applyDraftItems 落库）
  onBulk?: (items: DraftPlanItem[], proposalId: string) => void;
  // 工作流广播递增键（晨/夜/采集 SSE 完成后 App 递增 → 重拉纠偏；overload 除外）
  refreshKey?: number;
}) {
  const [messages, setMessages] = useState<PanelMessage[]>([]);
  const [proposals, setProposals] = useState<AiProposal[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  // 阶段反馈（2026-10-26 提速批 2）：status 帧文案替换静态「思考中…」；
  // 帧频极低（每重节点一帧）直接 setState 无渲染压力，轮始/轮末清空
  const [statusText, setStatusText] = useState("");
  // 零激活引导（挂载拉一次模型表判定；拉取失败不误报——保持 true 不显引导卡）
  const [modelReady, setModelReady] = useState(true);
  const [guideDismissed, setGuideDismissed] = useState(false);
  // 问号常用语句弹层（hover 开合；120ms 延迟关兜底按钮→弹层间隙的 mouseleave）
  const [hintsOpen, setHintsOpen] = useState(false);
  // 指令联想（2026-10-21）：输入以 / 开头时前缀过滤弹层；Esc 关闭当次，输入变化重开
  const [cmdIdx, setCmdIdx] = useState(0);
  const [cmdDismissed, setCmdDismissed] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 用户 /stop 主动中止标志：中止轮 finally 跳过消息重拉（保留本地停止反馈气泡）
  const stopFlagRef = useRef(false);
  const keySeqRef = useRef(0);
  const hintsTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(hintsTimerRef.current), []);

  // 全局快捷键「进入日程助手」（经 App 转派）：唤窗与展开右栏已由 App 完成，此处只
  // 把光标落到对话框。面板常驻渲染（display:none 切换），隐藏态下 focus 是 no-op——
  // 故再等一帧（rAF）确保右栏由隐藏转显示的布局已生效。AI 关闭时输入框 disabled，
  // focus 自然落空（此时面板显引导卡，无对话可聚焦）。
  useEffect(
    () =>
      onShortcutCmd("open-assistant", () =>
        requestAnimationFrame(() => inputRef.current?.focus()),
      ),
    [],
  );

  const openHints = () => {
    window.clearTimeout(hintsTimerRef.current);
    setHintsOpen(true);
  };

  const scheduleCloseHints = () => {
    hintsTimerRef.current = window.setTimeout(() => setHintsOpen(false), 120);
  };

  const toPanelMessages = (rows: Awaited<ReturnType<typeof fetchAiMessages>>): PanelMessage[] =>
    rows.map((m) => ({
      key: m.id,
      role: m.role,
      content: m.content,
      proposalId: m.proposal_id,
      source: m.source,
    }));

  const refreshModelReady = () =>
    void fetchModels()
      .then((rows) => setModelReady(rows.some((m) => m.is_active)))
      .catch(() => {});

  // 重拉会话历史 / 建议列表（挂载、发送收尾、工作流广播递增 refreshKey 三处复用）
  const refreshAll = () => {
    void fetchAiMessages().then((rows) => setMessages(toPanelMessages(rows))).catch(() => {});
    void fetchAiProposals().then(setProposals).catch(() => {});
  };

  // 挂载拉取会话历史 / 建议列表 / 模型激活态；卸载中断在途流
  useEffect(() => {
    refreshAll();
    refreshModelReady();
    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 工作流广播（晨/夜/采集）完成 → App 递增 refreshKey → 重拉真值（新建议卡/自动收录纠偏）；
  // 模型配置变更（设置页保存/激活/删除）也走同一把钥匙 → 顺带重查激活态，零激活
  // 引导卡随配置即时消失/出现（2026-10-22：曾只在挂载时查一次，配完模型提示
  // 仍滞留到下一轮问答才消失）
  useEffect(() => {
    if (refreshKey) {
      void refreshAll();
      refreshModelReady();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  // 消息流自动滚底（新消息与发送指示都跟随；单面板低频，用户上翻被打断可接受）
  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  // —— 本地指令（2026-10-21）：联想面板保证输入正确，sendText 只认全等执行 ——
  // 指令按正常用户输入回显（user 气泡右侧）——操作在消息流有迹可查；
  // /clear：后端清消息与滚动摘要游标，面板重建为指令气泡 + 确认气泡两条
  // （建议卡是待办建议保留）；
  // /compact：后端强制把未摘要消息并入滚动摘要（消息与近期原文不动，仅追加
  // 一条确认气泡——与 /clear 不同不重建消息流）；
  // /stop：中止在途流（abort → catch AbortError 静默；置 stopFlag 让 finally
  // 跳过消息重拉——重拉整组替换会抹掉本地停止反馈。user 消息已在后端、
  // assistant 未落即丢弃，这轮作废）
  const runCommand = (name: string) => {
    setInput("");
    setMessages((prev) => [
      ...prev,
      { key: `local-${++keySeqRef.current}`, role: "user", content: name, proposalId: null },
    ]);
    if (name === "/clear") {
      void clearAiSession()
        .then(() => {
          setMessages([
            { key: `local-${++keySeqRef.current}`, role: "user", content: name, proposalId: null },
            { key: `local-${++keySeqRef.current}`, role: "assistant", content: "已清空会话历史", proposalId: null },
          ]);
          void fetchAiProposals().then(setProposals).catch(() => {});
        })
        .catch(() => {
          setMessages((prev) => [
            ...prev,
            {
              key: `local-${++keySeqRef.current}`,
              role: "assistant",
              content: "清空失败，请稍后重试",
              proposalId: null,
              error: true,
            },
          ]);
        });
    } else if (name === "/compact") {
      void compactAiSession()
        .then((r) => {
          const content =
            r.compacted > 0
              ? `已把 ${r.compacted} 条较早对话并入滚动摘要`
              : r.reason === "no_model"
                ? "尚未激活模型，请先到 设置 → 模型接入 激活"
                : r.reason === "disabled"
                  ? "AI 功能已在设置中关闭，无法压缩"
                  : r.reason === "partial" || r.reason === "error"
                    ? "压缩未完成（部分成果已保留），请稍后重试"
                    : "当前没有需要压缩的对话";
          setMessages((prev) => [
            ...prev,
            { key: `local-${++keySeqRef.current}`, role: "assistant", content, proposalId: null },
          ]);
        })
        .catch(() => {
          setMessages((prev) => [
            ...prev,
            {
              key: `local-${++keySeqRef.current}`,
              role: "assistant",
              content: "压缩失败，请稍后重试",
              proposalId: null,
              error: true,
            },
          ]);
        });
    } else if (name === "/stop") {
      if (sending && abortRef.current) {
        setMessages((prev) => [
          ...prev,
          { key: `local-${++keySeqRef.current}`, role: "assistant", content: "已停止回复", proposalId: null },
        ]);
        stopFlagRef.current = true;
        abortRef.current.abort();
      } else {
        setMessages((prev) => [
          ...prev,
          {
            key: `local-${++keySeqRef.current}`,
            role: "assistant",
            content: "当前没有正在生成的回复",
            proposalId: null,
          },
        ]);
      }
    }
  };

  // 发送任意文本：问号语句点击与输入框 send 共用管线（语句点击不清输入框——
  // 清空是"从输入框发送"的语义，用户草稿与所点语句无关不应被丢弃）
  const sendText = (raw: string) => {
    const text = raw.trim();
    if (!text || sending) return;
    const cmd = COMMANDS.find((c) => c.name === text);
    if (cmd) {
      runCommand(cmd.name);
      return;
    }
    stopFlagRef.current = false;
    setSending(true);
    setStatusText(""); // 轮始清残留（上轮 status 短路后可能滞留）
    // 乐观 user 气泡（后端 message 回显帧跳过防重复；finally 重拉以真值纠偏）
    setMessages((prev) => [
      ...prev,
      { key: `local-${++keySeqRef.current}`, role: "user", content: text, proposalId: null },
    ]);
    const controller = new AbortController();
    abortRef.current = controller;
    const push = (m: Omit<PanelMessage, "key">) =>
      setMessages((prev) => [...prev, { ...m, key: `local-${++keySeqRef.current}` }]);
    // —— 打字机流式（2026-10-16）：delta 累积进闭包（不进 state），rAF 合并刷进
    // 固定 key 的流式气泡（高频 token 只触发每帧一次 re-render）；reply 终帧以
    // 权威文本原地转正（streaming 撤除切 md 渲染）；error / finally 清残留气泡，
    // 防半截文本与警示气泡并列。json_mode 分支无 delta，reply 直接整段。 ——
    const streamKey = `local-${++keySeqRef.current}`;
    let streamText = "";
    let streaming = false;
    let rafId: number | null = null;
    const cancelFlush = () => {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    };
    const dropStreamBubble = () => {
      cancelFlush();
      if (streaming) {
        streaming = false;
        setMessages((prev) => prev.filter((m) => m.key !== streamKey));
      }
    };
    streamChat(
      text,
      (ev) => {
        if (ev.type === "status") {
          setStatusText(ev.text);
        } else if (ev.type === "delta") {
          streamText += ev.text;
          if (!streaming) {
            streaming = true;
            setMessages((prev) => [
              ...prev,
              { key: streamKey, role: "assistant", content: "", proposalId: null, streaming: true },
            ]);
          }
          if (rafId === null) {
            rafId = requestAnimationFrame(() => {
              rafId = null;
              setMessages((prev) =>
                prev.map((m) => (m.key === streamKey ? { ...m, content: streamText } : m)),
              );
            });
          }
        } else if (ev.type === "reply") {
          cancelFlush();
          if (streaming) {
            // 权威整段原地转正：content 以 reply 为准（strip 等权威口径），撤流式标记
            streaming = false;
            setMessages((prev) =>
              prev.map((m) =>
                m.key === streamKey ? { ...m, content: ev.content, streaming: undefined } : m,
              ),
            );
          } else {
            push({ role: "assistant", content: ev.content, proposalId: null });
          }
        } else if (ev.type === "draft") {
          onDraft(ev.draft_plan, ev.proposal_id);
          setProposals((prev) => [
            {
              id: ev.proposal_id,
              kind: "schedule",
              payload: { draft_plan: ev.draft_plan },
              outcome: null,
              created_at: "",
              resolved_at: null,
            },
            ...prev,
          ]);
        } else if (ev.type === "proposal") {
          setProposals((prev) => [
            {
              id: ev.proposal_id,
              kind: ev.kind as AiProposal["kind"],
              payload: ev.payload,
              outcome: null,
              created_at: "",
              resolved_at: null,
            },
            ...prev,
          ]);
        } else if (ev.type === "applied") {
          // 快录直落：任务已由壳写库，直通 App 刷新画布（不能依赖 refreshKey——它只重拉面板）
          onApplied(ev.tasks);
        } else if (ev.type === "ingest_done") {
          // 采集轮收尾：卡在数据源页（面板零卡片），上抛 App 刷数据源徽标/面板
          onIngestDone?.();
        } else if (ev.type === "bulk") {
          // 批量排期草案：上抛 App 开弹窗（面板零卡片零幽灵方块）
          onBulk?.(ev.draft_plan, ev.proposal_id);
        } else if (ev.type === "error") {
          dropStreamBubble();
          push({ role: "assistant", content: ev.message, proposalId: null, error: true });
          if (ev.code === "no_model") setModelReady(false);
        }
      },
      controller.signal,
    )
      .catch((e) => {
        // fetch 网络层失败（后端未起等）——显示为警示气泡；卸载中断不算错
        if (e instanceof DOMException && e.name === "AbortError") return;
        dropStreamBubble();
        push({ role: "assistant", content: "连接助手服务失败，请稍后重试", proposalId: null, error: true });
      })
      .finally(() => {
        // 正常路径 reply 已转正（streaming=false 此处零动作）；异常/中断路径兜底清残留
        dropStreamBubble();
        setSending(false);
        setStatusText(""); // 轮末清阶段文案（下一轮从「思考中…」重新开始）
        abortRef.current = null;
        // 用户 /stop 中止：本轮 assistant 已丢弃，消息重拉会整组替换抹掉本地
        // 停止反馈——只重拉建议纠偏（半途 draft/proposal 本地行的真值对账）
        if (stopFlagRef.current) {
          void fetchAiProposals().then(setProposals).catch(() => {});
        } else {
          // 重拉真值纠偏（id 关联与时间戳落定、乐观临时键替换、模型态刷新）
          refreshAll();
        }
        refreshModelReady();
      });
  };

  const send = () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    sendText(text);
  };

  // 建议回写（本地乐观改行 + 异步落库；当前草案被决则同步撤幽灵方块）
  const settleProposal = (id: string, outcome: "modified" | "rejected", reason?: string) => {
    setProposals((prev) => prev.map((p) => (p.id === id ? { ...p, outcome } : p)));
    void resolveProposal(id, outcome, reason).catch(() => {});
    if (draft?.proposalId === id) onDiscardDraft();
  };

  // 采纳：乐观置徽标，应用与落库由 App 统一处理（schedule 应用失败时下次重拉自愈回待处理）
  const acceptProposal = (p: AiProposal) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "accepted" } : pp)));
    onAccept(p);
  };

  // 编辑后采纳 / 转收件箱（2026-10-25）：乐观置徽标 + 撤幽灵方块（当前草案被决），
  // 落库与回写由 App 统一处理（失败重拉自愈回待处理，同采纳口径）
  const modifyProposal = (p: AiProposal, items: DraftPlanItem[]) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "modified" } : pp)));
    if (draft?.proposalId === p.id) onDiscardDraft();
    onModify(p, items);
  };

  const sendToInboxProposal = (p: AiProposal) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "accepted" } : pp)));
    if (draft?.proposalId === p.id) onDiscardDraft();
    onSendToInbox(p);
  };

  // 会后纪要采纳（2026-11-10）：乐观置徽标，落库由 App 统一处理（口径同采纳）
  const acceptMinutesProposal = (p: AiProposal, items: InboxItem[]) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "accepted" } : pp)));
    onAcceptMinutes?.(p, items);
  };

  // 条目编辑态删除（2026-11-11 三件套统一批，四类条目卡通用）：settle-items
  // (rejected) 收缩卡 payload（响应行回填本地——条目少一条/outcome 或已回写）；
  // 全删光（卡清空自动回写 rejected）时撤当前幽灵方块（schedule 的 modify 链
  // 不再被调，撤除责任在此；非 schedule 卡的 id 不会命中幽灵锚点，天然安全）
  const removeEntries = async (p: AiProposal, indexes: number[]) => {
    try {
      const updated = await settleProposalItems(p.id, indexes, "rejected");
      setProposals((prev) => prev.map((pp) => (pp.id === p.id ? updated : pp)));
      if (draft?.proposalId === p.id && !((updated.payload.draft_plan as unknown[] | undefined)?.length)) {
        onDiscardDraft();
      }
      return true;
    } catch (e) {
      console.error("条目删除失败", e);
      return false;
    }
  };

  // 编辑后采纳（2026-11-11 三件套统一批）：inbox / minutes / hardtask 三类条目
  // 卡与 schedule 的 modifyProposal 同款（乐观置 modified + 落库回写由 App 统一）
  const modifyInboxProposal = (p: AiProposal, items: InboxItem[]) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "modified" } : pp)));
    onModifyInbox?.(p, items);
  };

  const modifyMinutesProposal = (p: AiProposal, items: InboxItem[]) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "modified" } : pp)));
    onModifyMinutes?.(p, items);
  };

  const modifyStepsProposal = (p: AiProposal, steps: string[]) => {
    setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "modified" } : pp)));
    onModifySteps?.(p, steps);
  };

  // 邀请卡补记提交（批 3）：调 submitMinutes 出新 minutes 卡 → 本卡 accepted
  // 结算（邀请达成目的）+ 重拉面板；错误文案返给卡内显示
  const submitInviteMinutes = async (p: AiProposal, text: string): Promise<string | null> => {
    try {
      const tid = typeof p.payload.meeting_task_id === "string"
        ? p.payload.meeting_task_id : null;
      await submitMinutes(tid, text);
      setProposals((prev) => prev.map((pp) => (pp.id === p.id ? { ...pp, outcome: "accepted" } : pp)));
      void resolveProposal(p.id, "accepted").catch(() => {});
      void fetchAiProposals().then(setProposals).catch(() => {});
      return null;
    } catch (e) {
      return (e as { detail?: string }).detail || "提炼失败，请稍后重试";
    }
  };

  // 指令联想派生：以 / 开头时按输入前缀过滤；无匹配或已 Esc 关闭则不弹
  const cmdInput = input.trim();
  const cmdMatches = cmdInput.startsWith("/")
    ? COMMANDS.filter((c) => c.name.startsWith(cmdInput))
    : [];
  const cmdOpen = cmdMatches.length > 0 && !cmdDismissed;

  return (
    <section className="copilot" style={style}>
      <header className="copilot-header">
        <div className="copilot-title">
          <Sparkles size={16} strokeWidth={2} className="copilot-title-icon" />
          <span className="copilot-title-text">日程助手</span>
        </div>
        <div className="copilot-header-actions">
          <div className="copilot-hints" onMouseEnter={openHints} onMouseLeave={scheduleCloseHints}>
            <button
              type="button"
              className="copilot-collapse"
              aria-label="常用语句"
              onClick={() => setHintsOpen((v) => !v)}
            >
              <HelpCircle size={18} strokeWidth={2} />
            </button>
            {hintsOpen && (
              <div className="copilot-hints-pop">
                <span className="copilot-hints-title">试试这样说</span>
                {HINT_PHRASES.map((phrase) => (
                  <button
                    key={phrase}
                    type="button"
                    className="copilot-hints-item"
                    onClick={() => {
                      setHintsOpen(false);
                      window.clearTimeout(hintsTimerRef.current);
                      sendText(phrase);
                    }}
                  >
                    {phrase}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button className="copilot-collapse" onClick={onCollapse} aria-label="折叠日程助手面板">
            <PanelRightClose size={18} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="copilot-feed" ref={feedRef}>
        {/* AI 总闸关闭引导卡：对话与定时增强全停，一键直达设置-智能体（复刻零激活
            引导卡；点击灰化入口按钮展开面板即见此卡——"点击提示开启"的落地） */}
        {!aiEnabled && !guideDismissed && (
          <div className="copilot-guide">
            <div className="copilot-guide-text">
              <span className="copilot-guide-title">AI 助手已关闭</span>
              <span className="copilot-guide-sub">
                对话与晨间、夜间、定时增强已全部停用，开启后恢复
              </span>
            </div>
            <div className="copilot-guide-actions">
              <button type="button" className="copilot-prop-btn primary" onClick={() => onOpenSettings("agent")}>
                去开启
              </button>
              <button type="button" className="copilot-prop-btn ghost" onClick={() => setGuideDismissed(true)}>
                暂不
              </button>
            </div>
          </div>
        )}

        {/* 零激活引导卡：配置模型后解锁对话（总闸关闭时不重复显示，先开总闸） */}
        {modelReady === false && aiEnabled && !guideDismissed && (
          <div className="copilot-guide">
            <div className="copilot-guide-text">
              <span className="copilot-guide-title">配置模型后解锁 AI 对话</span>
              <span className="copilot-guide-sub">日程助手需要一个大模型来理解你的安排</span>
            </div>
            <div className="copilot-guide-actions">
              <button type="button" className="copilot-prop-btn primary" onClick={() => onOpenSettings("model")}>
                去配置
              </button>
              <button type="button" className="copilot-prop-btn ghost" onClick={() => setGuideDismissed(true)}>
                暂不
              </button>
            </div>
          </div>
        )}

        {messages.map((m) => {
          const linked = m.proposalId ? proposals.find((p) => p.id === m.proposalId) : undefined;
          return (
            <div key={m.key} className={`copilot-msg ${m.role}${m.error ? " error" : ""}`}>
              {m.source && (
                <span className="copilot-source">{SOURCE_LABELS[m.source] ?? m.source}</span>
              )}
              <div className={`copilot-bubble${m.streaming ? " streaming" : ""}`}>
                {m.role === "assistant" && !m.error && !m.streaming ? (
                  <CopilotMarkdown content={m.content} />
                ) : (
                  m.content
                )}
              </div>
              {m.role === "assistant" && linked && (
                <ProposalCard
                  proposal={linked}
                  onAccept={() => acceptProposal(linked)}
                  onSettle={(outcome, reason) => settleProposal(linked.id, outcome, reason)}
                  onModify={(items) => modifyProposal(linked, items)}
                  onModifyInbox={onModifyInbox ? (items) => modifyInboxProposal(linked, items) : undefined}
                  onModifyMinutes={onModifyMinutes ? (items) => modifyMinutesProposal(linked, items) : undefined}
                  onModifySteps={onModifySteps ? (steps) => modifyStepsProposal(linked, steps) : undefined}
                  onRemoveEntries={(indexes) => removeEntries(linked, indexes)}
                  onSendToInbox={() => sendToInboxProposal(linked)}
                  onAcceptMinutes={
                    onAcceptMinutes ? (items) => acceptMinutesProposal(linked, items) : undefined
                  }
                  onInviteSubmit={(text) => submitInviteMinutes(linked, text)}
                />
              )}
            </div>
          );
        })}

        {/* 流式气泡已在打字时不再叠加「思考中」（流式气泡恒为末位消息）；
            status 帧到达后替换为当前阶段文案（非流式分支过程反馈，提速批 2） */}
        {sending && !messages[messages.length - 1]?.streaming && (
          <div className="copilot-typing">{statusText || "思考中…"}</div>
        )}
        {messages.length === 0 && modelReady !== false && aiEnabled && (
          <div className="copilot-empty">和助手聊聊今天的安排，或让它帮你排期待办</div>
        )}
      </div>

      <footer className="copilot-footer">
        {/* 指令联想弹层：输入 / 前缀过滤；↑↓ 移高亮、Enter 执行、Esc 关闭当次 */}
        {cmdOpen && (
          <div className="copilot-cmd-pop">
            {cmdMatches.map((c, i) => (
              <button
                key={c.name}
                type="button"
                className={`copilot-cmd-item${i === cmdIdx ? " active" : ""}`}
                onMouseEnter={() => setCmdIdx(i)}
                onClick={() => runCommand(c.name)}
              >
                <span className="copilot-cmd-name">{c.name}</span>
                <span className="copilot-cmd-desc">{c.desc}</span>
              </button>
            ))}
          </div>
        )}
        <div className="copilot-input">
          <input
            className="copilot-input-text"
            ref={inputRef}
            placeholder={aiEnabled ? "对话或追加要求...（/ 查看指令）" : "AI 助手已关闭，到 设置-智能体 开启"}
            maxLength={2000}
            value={input}
            disabled={!aiEnabled}
            onChange={(e) => {
              setInput(e.target.value);
              setCmdIdx(0);
              setCmdDismissed(false);
            }}
            onKeyDown={(e) => {
              const composing = e.nativeEvent.isComposing;
              if (e.key === "Escape" && cmdOpen) {
                setCmdDismissed(true);
                return;
              }
              if (cmdOpen && !composing) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setCmdIdx((i) => Math.min(i + 1, cmdMatches.length - 1));
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setCmdIdx((i) => Math.max(i - 1, 0));
                  return;
                }
                // 联想激活时 Enter 执行高亮指令（防 "/cl" 半截指令误发成对话）
                if (e.key === "Enter") {
                  e.preventDefault();
                  runCommand(cmdMatches[cmdIdx].name);
                  return;
                }
              }
              if (e.key === "Enter" && !composing) send();
            }}
          />
          <button
            type="button"
            className="copilot-send"
            aria-label="发送"
            disabled={!input.trim() || sending || !aiEnabled}
            onClick={send}
          >
            <ArrowUp size={15} strokeWidth={2} className="copilot-send-icon" />
          </button>
        </div>
      </footer>
    </section>
  );
}

export default CopilotPanel;
