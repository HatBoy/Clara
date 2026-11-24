import { toRrule } from "../lib/rrule";
import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  CalendarClock,
  Check,
  ChevronDown,
  Clock,
  Database,
  Flag,
  PanelRightClose,
  RefreshCw,
  Repeat,
  Timer,
  Trash2,
} from "lucide-react";
import EmptyState from "./EmptyState";
import {
  entryHasSource,
  fetchAiProposals,
  ingestItemsOf,
  isIngestProposal,
  type ProposalDecision,
  type DecisionResult,
  type AiProposal,
  type DraftPlanItem,
  type EvidenceEntry,
  type InboxItem,
} from "../lib/ai";
import { runIngestAndWait } from "../lib/api";
import { TODO_MIME } from "../lib/dnd";
import ConfirmPopover from "./ConfirmPopover";
import WhenPicker from "./WhenPicker";
import {
  NOTE_MAX_LEN,
  PRIORITY_COLORS,
  RECURRENCE_FREQ_OPTIONS,
  RECURRENCE_WEEKDAY_LABELS,
  recurrenceText,
  setRecurrenceFreq,
  TITLE_MAX_LEN,
  toggleWeekday,
  type Recurrence,
} from "../data/tasks";
// 复用收件箱面板的三段骨架与待办卡体系（.inbox-* 行卡 / 优先级色条 / 编辑区 /
// 悬停操作组 / 提示条）——数据源页与收件箱页同 540px 右栏形态，视觉语言完全一致，
// 自有增量样式（源分组 / 卡头徽标 / 编辑条目块）在 DataSourcePanel.css
import "./InboxPanel.css";
import "./DataSourcePanel.css";

// schedule 条目时刻 → "MM-DD HH:MM"（草案多在明天起，带日期防歧义；坏值 → 空串）
const fmtWhen = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const md = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${md} ${hhmm}`;
};

// 估时输入串 → 分钟（空/非法/非正 → null 未估时；上限对齐后端 1440）
const durOf = (s: string): number | null => {
  if (!s.trim()) return null;
  const n = Math.round(Number(s));
  return Number.isFinite(n) && n > 0 ? Math.min(n, 1440) : null;
};

// 后端优先级 number（1 高 2 中 3 低，TaskCreateBody 口径）↔ 前端色表键（收件箱同源）
const prioLabel = (n: number) => (n === 1 ? "high" : n === 3 ? "low" : "medium");
const PRIO_NUM = { high: 1, medium: 2, low: 3 } as const;

// 耗时快捷档（口径同 InboxPanel 编辑区胶囊；点击同写数字草稿）
const DURATION_PRESETS = [
  { minutes: 30, label: "30m" },
  { minutes: 60, label: "1h" },
  { minutes: 120, label: "2h" },
] as const;

// 条目时刻 → 重复规则 monthly/weekly 默认锚（相对今天的天数偏移）：切频率与
// 「每月N日」显示需要基准日；无日期/坏值 → undefined 退化「每月」、weekly 默认周一
const anchorOffsetOf = (start: string | undefined): number | undefined => {
  if (!start) return undefined;
  const d = new Date(`${start.slice(0, 10)}T00:00`);
  if (Number.isNaN(d.getTime())) return undefined;
  const t0 = new Date();
  t0.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - t0.getTime()) / 86400000);
};

// 条目源信息（key 分组用，name 显示用）：inbox 逐条 source_ref、schedule 按
// evidence.idx 对齐溯源。「未知来源」兜底仅为类型完备——无来源条目已在
// groupEntries 过滤不进渲染（每条建议必有明确来源，2026-10-26 用户产品规则）
const entrySource = (
  p: AiProposal,
  idx: number,
): { key: string; name: string; ref: { source: string; title: string } | null } => {
  const items = ingestItemsOf(p);
  if (p.kind === "inbox") {
    const ref = (items[idx] as InboxItem).source_ref ?? null;
    return ref
      ? { key: ref.source_key, name: ref.source, ref }
      : { key: "__none__", name: "未知来源", ref: null };
  }
  const ev =
    ((p.payload.evidence as EvidenceEntry[] | undefined) ?? []).find((e) => e.idx === idx) ?? null;
  return ev
    ? { key: ev.source_key, name: ev.source, ref: ev }
    : { key: "__none__", name: "未知来源", ref: null };
};

// 单条溯源行（口径同 CopilotPanel 建议卡 traceRows）
const traceRowOf = (title: string, ref: { source: string; title: string } | null) =>
  ref ? `「${title}」 来自 ${ref.source} ·「${ref.title}」` : `「${title}」 · 无溯源`;

// 编辑草稿（2026-10-26 收件箱 UI 对齐批，布局复刻收件箱编辑区）：start 为
// datetime-local 值（"YYYY-MM-DDTHH:mm"），确认时拼 ":00" 还原 naive ISO；
// priority 为后端 number 口径（采集建议不带该键，装载默认 2=中）；notes 恒装载
// （schedule 条目也可填备注）；recurrence 为重复规则预设（采集建议不带恒 null）
type EditDraft = {
  title: string;
  duration: string;
  priority: number;
  notes: string;
  start?: string;
  recurrence: Recurrence | null;
};

type DsEntry = { p: AiProposal; idx: number; item: InboxItem | DraftPlanItem };
type DsGroup = { key: string; name: string; entries: DsEntry[] };

// 按条目自身的源分组（跨源卡各条归各源组——2026-10-24 单条操作批：操作粒度落到
// 条目后分组也随条目走，整卡按首源归组的旧口径作废），组名按中文排序；无来源
// 条目（历史旧卡缺溯源键）跳过不渲染——Reduce 升级后新卡每条必带来源
const groupEntries = (proposals: AiProposal[]): DsGroup[] => {
  const groups = new Map<string, DsGroup>();
  for (const p of proposals.filter(isIngestProposal)) {
    ingestItemsOf(p).forEach((item, idx) => {
      if (!entryHasSource(p, idx)) return;
      const src = entrySource(p, idx);
      let g = groups.get(src.key);
      if (!g) {
        g = { key: src.key, name: src.name, entries: [] };
        groups.set(src.key, g);
      }
      g.entries.push({ p, idx, item });
    });
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name, "zh"));
};

// 数据源按来源展示待处理条目。每张卡的稳定 ID 与清单从同一响应读取，
// 编辑、确认、拖拽均由后端单次结算；本地不预先删除或创建任务。
function DataSourcePanel({
  style,
  onCollapse,
  onDecide,
  onOpenSettings,
  refreshKey = 0,
  syncTrigger = 0,
}: {
  style?: CSSProperties;
  onCollapse: () => void;
  onDecide: (id: string, items: ProposalDecision[]) => Promise<DecisionResult>;
  // 空态动作（2026-11-17 批 1）：打开设置 → 数据接入
  onOpenSettings?: () => void;
  // 重拉信号（App 的 aiRefreshKey：工作流事件递增；effect 初跑即挂载拉取）
  refreshKey: number;
  // 全局快捷键 sync-now 触发计数（App 递增；0 = 从未触发）。经 props 驱动而非
  // 事件转派：本面板是 activeView 条件挂载，CustomEvent 可能在挂载 effect 注册
  // 订阅之前发出而丢失（2026-11-05 真机回归——常驻挂载的 QuickAdd/CopilotPanel
  // 先例不适用于此）；依赖驱动的 effect 由 React 保证挂载后必跑，零竞态
  syncTrigger: number;
}) {
  const [proposals, setProposals] = useState<AiProposal[]>([]);
  // 溯源展开 / 编辑态（均为单条，键 `${proposalId}#${idx}`）
  const [traceKey, setTraceKey] = useState<string | null>(null);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  // 重复/日期面板开合态（收件箱 2026-10-26 同款：点开互斥、点外即收——日期面板
  // 已抽 WhenPicker 共享组件内置收起，开合受控留在消费侧管互斥；值在
  // editDraft.start / recurrence 草稿态，保存走「确认」显式提交）
  const [recurrenceOpen, setRecurrenceOpen] = useState(false);
  const [whenOpen, setWhenOpen] = useState(false);
  // 重复面板外点收起（收件箱批次七口径，日期面板抽组件后只管重复）：点药丸
  // 本身走 onClick（toggle + 互斥），点日期药丸不误收（互斥由 onOpenChange
  // 回调显式关）
  useEffect(() => {
    if (!recurrenceOpen) return;
    const onDown = (e: PointerEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest(".inbox-todo-repeat-panel, .inbox-todo-repeat-bar, .inbox-todo-when"))
        return;
      setRecurrenceOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [recurrenceOpen]);
  // 底部瞬时提示条（口径同 InboxPanel：4 秒自清，bad 红字）
  const [notice, setNotice] = useState<{ text: string; bad?: boolean } | null>(null);
  // 手动刷新态（2026-11-04 手动刷新批）：采集在途时按钮转圈禁点；完成按摘要反馈
  const [refreshing, setRefreshing] = useState(false);

  const [loadError, setLoadError] = useState<string | null>(null);
  const readGeneration = useRef(0);
  const refetch = () => {
    const generation = ++readGeneration.current;
    fetchAiProposals(true)
      .then((rows) => {
        if (generation !== readGeneration.current) return;
        setProposals(rows);
        setLoadError(null);
      })
      .catch((e) => {
        if (generation !== readGeneration.current) return;
        console.error("数据源建议拉取失败", e);
        setLoadError(e instanceof Error ? e.message : "拉取失败");
      });
  };

  useEffect(() => {
    refetch();
  }, [refreshKey]);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(t);
  }, [notice]);

  const submit = async (p: AiProposal, decisions: ProposalDecision[]) => {
    const result = await onDecide(p.id, decisions);
    ++readGeneration.current;
    setProposals(prev => prev.flatMap(row => row.id !== p.id || row.settled_count > result.proposal.settled_count
      ? [row] : result.proposal.outcome ? [] : [result.proposal]));
    return result;
  };

  // 手动刷新（2026-11-04）：立即采集一轮并等待完成（feed 类内容页的刷新语义
  // ——定时档 1-4 次/天间隙大，用户在数据源页想看最新数据时在此刷）。等待型
  // 端点返回本轮摘要：静默轮 → 「无新数据」；有产出 → 新增计数 + 直通计数 +
  // refetch 重拉建议列表。422/409 走 JobsSettings 立即采集同款错误口径。
  // 防重入用 ref 而非 refreshing state——StrictMode 挂载 effect 双跑与快捷键
  // 连按是同步连续调用，state 未提交时双读 false 会双发（第二个吃 409 误报）
  const refreshingRef = useRef(false);
  const refreshNow = () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true;
    setRefreshing(true);
    runIngestAndWait()
      .then((s) => {
        if (s.skipped) {
          // 采集熔断闸已移除（2026-11-04 用户拍板：采集是数据同步不是排期，
          // 永不因过载拦截）——skipped 只剩竞态/降级态
          setNotice({ text: "本轮采集未能执行，请稍后重试", bad: true });
        } else if (s.silent) {
          setNotice({ text: "已采集：各源无新数据" });
        } else {
          const parts: string[] = [];
          const cards = s.drafts + s.inbox;
          if (cards > 0) parts.push(`新增建议 ${cards} 条`);
          if (s.direct_calendar > 0) parts.push(`日程直落日历 ${s.direct_calendar} 条`);
          if (s.direct_inbox > 0) parts.push(`待办直落收件箱 ${s.direct_inbox} 条`);
          setNotice({ text: parts.length ? `已采集：${parts.join("，")}` : "已采集：无新建议" });
          refetch();
        }
      })
      .catch((e) => {
        const status = (e as { status?: number }).status;
        if (status === 409) setNotice({ text: "已有一次采集在跑，稍候再试", bad: true });
        else if (status === 422)
          setNotice({ text: (e as { detail?: string }).detail ?? "采集前置条件未满足", bad: true });
        else setNotice({ text: "请求失败：后端服务不可用", bad: true });
      })
      .finally(() => {
        refreshingRef.current = false;
        setRefreshing(false);
      });
  };

  // 全局快捷键 sync-now（Ctrl+Alt+S）：Rust 已唤窗、App 已切本页并递增 syncTrigger，
  // 依赖驱动的 effect 复用手动刷新同款体验（转圈 + 「采集中…」+ 完成摘要）。
  // 依赖只放 syncTrigger——effect 执行用的闭包来自触发渲染（当刻 refreshing 最新，
  // refreshNow 自带防重入闸；连按第二下在采集在途时被正确吞掉）
  useEffect(() => {
    if (syncTrigger > 0) refreshNow();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncTrigger]);

  const decideEntry = async (en: DsEntry, action: ProposalDecision["action"], entry?: Record<string, unknown>) => {
    try {
      const result = await submit(en.p, [{item_id: en.p.item_ids[en.idx], action, entry}]);
      setEditingKey(null);
      setRecurrenceOpen(false);
      setWhenOpen(false);
      setNotice({text: action === "reject" ? "已拒绝" : result.receipts[0].scheduled ? "已写入日历" :
        action === "smart" ? "已收录为待办，未排期" : "已加入收件箱"});
    } catch {
      setNotice({text: "未确认本次结算，正在核对最新建议。", bad: true});
      refetch();
    }
  };
  const confirmItem = (en: DsEntry) => decideEntry(en, "accept");
  const rejectItem = (en: DsEntry) => decideEntry(en, "reject");

  // 装载编辑态（2026-10-26 布局复刻收件箱）：schedule 的 naive ISO 截成
  // datetime-local 值、inbox 留空（留空 = 进收件箱，选值 = 直排进日历）；备注与
  // 重复规则恒装载（schedule 条目也可填备注/设系列预设）；浏览月按已选值定位
  const openEdit = (en: DsEntry) => {
    const it = en.item;
    const start = en.p.kind === "inbox" ? "" : (it as DraftPlanItem).start.slice(0, 16);
    setEditDraft({
      title: it.title,
      duration: it.duration ? String(it.duration) : "",
      priority: it.priority ?? 2,
      notes: (it as InboxItem).notes ?? "",
      start,
      recurrence: (it as { recurrence?: Recurrence | null }).recurrence ?? null,
    });
    setEditingKey(`${en.p.id}#${en.p.item_ids[en.idx]}`);
    setRecurrenceOpen(false);
    setWhenOpen(false);
  };

  // 草稿 → 落库条目组装（确认与智能排期共用）：direct = schedule 恒真 / inbox
  // 选了时间；溯源键两形态都随条目透传不丢来源（schedule 条目 payload 不带
  // source_ref——溯源在 evidence per idx，服务端按稳定 ID 回填溯源）
  const assembleItem = (en: DsEntry, d: EditDraft): InboxItem | DraftPlanItem => {
    const title = d.title.trim() || en.item.title; // 空标题回落原值
    const direct = en.p.kind === "schedule" || !!d.start; // 直排进日历（schedule 恒是）
    return direct
      ? ({
          task_id: null,
          title,
          start: d.start ? `${d.start}:00` : (en.item as DraftPlanItem).start,
          duration: durOf(d.duration) ?? en.item.duration ?? 60, // 草案时长是引擎产物，空值回落不落 0
          priority: d.priority,
          ...(d.notes.trim() ? { notes: d.notes.trim() } : {}),
          ...(d.recurrence ? { recurrence: d.recurrence } : {}),
          ...(en.p.kind === "inbox" ? { evidence: (en.item as InboxItem).evidence ?? null } : {}),
        } as DraftPlanItem)
      : ({
          ...(en.item as InboxItem),
          title,
          duration: durOf(d.duration),
          notes: d.notes.trim() || null,
          priority: d.priority,
          ...(d.recurrence ? { recurrence: d.recurrence } : {}),
        } as InboxItem);
  };

  const editableFields = (item: InboxItem | DraftPlanItem) => ({
    title: item.title, duration: item.duration, notes: item.notes, priority: item.priority,
    recurrence_rule: item.recurrence ? toRrule(item.recurrence) : null,
    ...("start" in item ? { start: item.start } : {}),
  });
  const applyModified = (en: DsEntry) => editDraft && decideEntry(en, "modify", editableFields(assembleItem(en, editDraft)));
  const smartItem = (en: DsEntry, item: InboxItem | DraftPlanItem) =>
    decideEntry(en, "start" in item && item.start ? "schedule" : "smart", editableFields(item));

  const groups = groupEntries(proposals);

  return (
    <section className="inbox" style={style}>
      <header className="inbox-header">
        <div className="inbox-title">
          <div className="inbox-title-icon">
            <Database size={15} strokeWidth={2} />
          </div>
          <span className="inbox-title-text">数据源</span>
        </div>
        <div className="inbox-header-actions">
          {/* 手动刷新（2026-11-04）：立即采集一轮（feed 内容页刷新语义）。采集中
              双反馈——图标转圈 + 「采集中…」文字（真机反馈单转圈不显眼易以为
              卡死，LLM 提炼可达 15s+）；完成/失败经底部 notice 反馈 */}
          {refreshing && <span className="ds-refreshing-hint">采集中…</span>}
          <button
            type="button"
            className="inbox-collapse ds-refresh"
            onClick={refreshNow}
            disabled={refreshing}
            title={refreshing ? "正在采集…" : "立即从数据源采集（约数秒，含 AI 提炼）"}
            aria-label="立即采集"
          >
            <RefreshCw size={16} strokeWidth={2} className={refreshing ? "spin" : undefined} />
          </button>
          <button
            type="button"
            className="inbox-collapse"
            onClick={onCollapse}
            aria-label="折叠数据源面板"
          >
            <PanelRightClose size={18} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="inbox-main">
        <div className="ds-list">
          {groups.length === 0 ? (
            loadError ? (
              <EmptyState
                compact
                tone="error"
                title="数据源建议加载失败"
                description={loadError}
                action={{ label: "重试", onClick: refetch }}
              />
            ) : (
              <EmptyState
                compact
                icon={<Database size={22} strokeWidth={1.6} />}
                title="暂无待确认的数据源建议"
                description="接入邮箱、日历或本地文件夹后，采集到的条目会先到这里等你确认"
                action={
                  onOpenSettings
                    ? { label: "去配置数据源", onClick: onOpenSettings }
                    : undefined
                }
              />
            )
          ) : (
            groups.map((g) => (
              <div key={g.key} className="ds-group">
                <div className="ds-group-head">
                  <span className="ds-group-name">{g.name}</span>
                  <span className="ds-group-count">{g.entries.length}</span>
                  <button
                    type="button"
                    className="ds-group-accept"
                    onClick={() => {
                      const cards = new Map<AiProposal, ProposalDecision[]>();
                      for (const en of g.entries) cards.set(en.p, [...(cards.get(en.p) ?? []),
                        {item_id: en.p.item_ids[en.idx], action: "accept"}]);
                      void (async () => {
                        let n = 0;
                        try {
                          for (const [card, decisions] of cards) n += (await submit(card, decisions)).receipts.length;
                          setNotice({text: `已确认 ${n} 条`});
                        } catch {
                          setNotice({text: `已收到 ${n}/${g.entries.length} 条回执，正在核对剩余条目。`, bad: true});
                          refetch();
                        }
                      })();
                    }}
                  >
                    <Check size={12} strokeWidth={2.5} />
                    确认全部
                  </button>
                </div>
                {g.entries.map((en) => {
                  const key = `${en.p.id}#${en.p.item_ids[en.idx]}`;
                  const editing = editingKey === key;
                  const src = entrySource(en.p, en.idx);
                  const d = editDraft;
                  const prioNum = editing ? (d?.priority ?? 2) : (en.item.priority ?? 2);
                  const notes = (en.item as InboxItem).notes;
                  return (
                    // 卡盒复用收件箱待办卡体系：优先级色条（prio-*，单条即自身优先级、
                    // 编辑态实时跟随草稿）/ 玻璃盒 / 悬停操作组（.inbox-todo-actions
                    // 显隐规则对 .inbox-todo:hover 同样命中）；.ds-card 仅覆盖纵向堆叠
                    // 布局。可拖拽直排（2026-10-24）：不挂 plain（保留 grab 光标），编辑
                    // 态关 draggable；
                    // 放置端只提交原稳定条目标识和候选时间，伪 id（ds-…）
                    // 保证退化路径按 id 查不到任务时静默放弃
                    <div
                      key={key}
                      className={`inbox-todo ds-card prio-${prioLabel(prioNum)}${editing ? " expanded" : ""}`}
                      draggable={!editing}
                      onDragStart={(e) => {
                        e.dataTransfer.setData(
                          TODO_MIME,
                          JSON.stringify({
                            id: `ds-${en.p.id}-${en.idx}`,
                            title: en.item.title,
                            minutes: en.item.duration ?? null,
                            suggest: {
                              proposalId: en.p.id,
                              itemId: en.p.item_ids[en.idx],
                            },
                          }),
                        );
                        e.dataTransfer.effectAllowed = "move";
                      }}
                    >
                      <div className="ds-card-head">
                        <span className="ds-src-badge">
                          <i />
                          {src.name}
                        </span>
                        <span className="ds-kind">
                          {en.p.kind === "inbox" ? "待办建议" : "排期草案"}
                        </span>
                        <button
                          type="button"
                          className={`ds-trace-toggle${traceKey === key ? " open" : ""}`}
                          aria-label={traceKey === key ? "收起源链" : "展开溯源"}
                          onClick={() => setTraceKey((k) => (k === key ? null : key))}
                        >
                          <ChevronDown size={13} strokeWidth={2} />
                        </button>
                      </div>

                      {editing && d ? (
                        <div className="inbox-todo-edit">
                          <input
                            className="inbox-todo-title-input"
                            value={d.title}
                            maxLength={TITLE_MAX_LEN}
                            placeholder="标题"
                            onChange={(ev) => setEditDraft({ ...d, title: ev.target.value })}
                          />
                          {/* 备注（2026-10-26 对齐批恒渲染：schedule 条目也可填；
                              高度自适应同收件箱 fitNoteHeight——挂载按已有内容撑高，
                              输入随内容重算，rows={2} 只是 JS 失效保底） */}
                          <div className="inbox-todo-note-wrap">
                            <textarea
                              className="inbox-todo-note"
                              value={d.notes}
                              maxLength={NOTE_MAX_LEN}
                              rows={2}
                              placeholder="添加执行备注 (如注意点、参考链接)..."
                              ref={(el) => {
                                if (el) {
                                  el.style.height = "auto";
                                  el.style.height = `${el.scrollHeight}px`;
                                }
                              }}
                              onInput={(ev) => {
                                ev.currentTarget.style.height = "auto";
                                ev.currentTarget.style.height = `${ev.currentTarget.scrollHeight}px`;
                              }}
                              onChange={(ev) => setEditDraft({ ...d, notes: ev.target.value })}
                            />
                            <span className="inbox-todo-note-count">
                              {d.notes.length}/{NOTE_MAX_LEN}
                            </span>
                          </div>
                          {/* 任务属性行（收件箱八批定稿同构）：耗时、优先级两药丸
                              Grid 1fr 1fr 双列，列边界轨道定义强制对齐时间行 */}
                          <div className="inbox-todo-edit-row inbox-todo-attr-row">
                            <div className="inbox-pill-group">
                              <div className="inbox-todo-durations">
                                <Timer
                                  size={14}
                                  strokeWidth={2}
                                  className="inbox-pill-icon"
                                  aria-label="耗时"
                                />
                                {DURATION_PRESETS.map((p) => (
                                  <button
                                    key={p.minutes}
                                    type="button"
                                    className={`inbox-todo-duration${Number(d.duration) === p.minutes ? " on" : ""}`}
                                    onClick={() =>
                                      setEditDraft({ ...d, duration: String(p.minutes) })
                                    }
                                  >
                                    {p.label}
                                  </button>
                                ))}
                              </div>
                            </div>
                            <div className="inbox-pill-group">
                              <Flag
                                size={14}
                                strokeWidth={2}
                                className="inbox-pill-icon"
                                aria-label="优先级"
                              />
                              {(Object.keys(PRIORITY_COLORS) as ("high" | "medium" | "low")[]).map(
                                (pl) => (
                                  <button
                                    key={pl}
                                    type="button"
                                    style={{ color: PRIORITY_COLORS[pl] }}
                                    className={`inbox-todo-prio-item${prioLabel(d.priority) === pl ? " on" : ""}`}
                                    aria-label={{ high: "高", medium: "中", low: "低" }[pl]}
                                    onClick={() => setEditDraft({ ...d, priority: PRIO_NUM[pl] })}
                                  >
                                    <i />
                                  </button>
                                ),
                              )}
                            </div>
                          </div>
                          {/* 时间行（收件箱同构）：重复在前、日期在后，两药丸各自
                              整钮开合面板，面板在本行顶层独立轨道占满整行宽 */}
                          <div className="inbox-todo-time-row">
                            <div className="inbox-todo-repeat-setting">
                              <button
                                type="button"
                                className="inbox-todo-repeat-bar"
                                onClick={() => {
                                  setRecurrenceOpen((v) => !v);
                                  setWhenOpen(false);
                                }}
                              >
                                <Repeat
                                  size={14}
                                  strokeWidth={2}
                                  className="inbox-pill-icon"
                                  aria-label="重复"
                                />
                                <span
                                  className={`inbox-todo-repeat-bar-value${d.recurrence ? "" : " muted"}`}
                                >
                                  {d.recurrence
                                    ? // weekly 收起态只显「每周」（收件箱口径），周几见面板圆钮
                                      d.recurrence.freq === "weekly"
                                      ? "每周"
                                      : recurrenceText(d.recurrence, anchorOffsetOf(d.start), "、")
                                    : "不重复"}
                                </span>
                                <ChevronDown
                                  size={13}
                                  strokeWidth={2}
                                  className={`inbox-todo-chevron${recurrenceOpen ? " open" : ""}`}
                                />
                              </button>
                            </div>
                            {/* WhenPicker 选时间写草稿，确认走操作行「确认」钮
                                （2026-11-11 统一批：面板底部文字提交钮退役，
                                确认一个入口——inbox 条目留空确认 = 进收件箱，
                                选了时间确认 = 直排进日历） */}
                            <WhenPicker
                              value={d.start ?? ""}
                              onChange={(v) => setEditDraft({ ...d, start: v })}
                              open={whenOpen}
                              onOpenChange={(o) => {
                                setWhenOpen(o);
                                if (o) setRecurrenceOpen(false); // 与重复面板互斥
                              }}
                            />
                            {/* 重复面板（独立轨道整行宽）：频率四档 + weekly 多选圆钮 */}
                            {recurrenceOpen && (
                              <div className="inbox-todo-repeat-panel">
                                {RECURRENCE_FREQ_OPTIONS.map((opt) => {
                                  const active = d.recurrence
                                    ? d.recurrence.freq === opt.value
                                    : opt.value === "none";
                                  const weeklyRule =
                                    opt.value === "weekly" && d.recurrence?.freq === "weekly"
                                      ? d.recurrence
                                      : undefined;
                                  return (
                                    <div key={opt.value}>
                                      <button
                                        type="button"
                                        className={`inbox-todo-repeat-option${active ? " on" : ""}`}
                                        onClick={() =>
                                          setEditDraft({
                                            ...d,
                                            recurrence:
                                              setRecurrenceFreq(
                                                d.recurrence ?? undefined,
                                                opt.value,
                                                anchorOffsetOf(d.start),
                                              ) ?? null,
                                          })
                                        }
                                      >
                                        <i className="inbox-todo-repeat-dot" />
                                        {opt.label}
                                      </button>
                                      {weeklyRule && (
                                        <div className="inbox-todo-repeat-weekdays">
                                          {RECURRENCE_WEEKDAY_LABELS.map((label, i) => {
                                            const n = i + 1;
                                            return (
                                              <button
                                                key={n}
                                                type="button"
                                                className={`inbox-todo-repeat-weekday${
                                                  weeklyRule.weekdays.includes(n) ? " on" : ""
                                                }`}
                                                onClick={() => {
                                                  // 最后一个星期不可取消（返 null 忽略）
                                                  const next = toggleWeekday(weeklyRule.weekdays, n);
                                                  if (next)
                                                    setEditDraft({
                                                      ...d,
                                                      recurrence: { freq: "weekly", weekdays: next },
                                                    });
                                                }}
                                              >
                                                {label}
                                              </button>
                                            );
                                          })}
                                        </div>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                          {/* 编辑区操作行（收件箱同构底部静态四钮）：确认 = 按草稿
                              落库（modified）/ 智能排期 = 按草稿排期 / 删除 = 拒绝
                              条目 / 收起 = 放弃编辑回卡片态（chevron 恒展开态） */}
                          <div className="inbox-todo-edit-actions">
                            <button
                              type="button"
                              className="inbox-todo-icon ok"
                              aria-label="确认"
                              title="确认"
                              onClick={() => void applyModified(en)}
                            >
                              <Check size={14} strokeWidth={2} />
                            </button>
                            <button
                              type="button"
                              className="inbox-todo-icon"
                              aria-label="智能安排"
                              title="智能安排"
                              onClick={() => void smartItem(en, assembleItem(en, d))}
                            >
                              <CalendarClock size={14} strokeWidth={2} />
                            </button>
                            <ConfirmPopover
                              message="拒绝该条目？同源同类内容将不再提醒"
                              onConfirm={() => void rejectItem(en)}
                              buttonClass="inbox-todo-icon del"
                              buttonTitle="删除"
                              icon={<Trash2 size={14} strokeWidth={2} />}
                            />
                            <button
                              type="button"
                              className="inbox-todo-icon"
                              aria-label="收起"
                              title="收起"
                              onClick={() => setEditingKey(null)}
                            >
                              <ChevronDown
                                size={14}
                                strokeWidth={2}
                                className="inbox-todo-chevron open"
                              />
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="ds-entries">
                            <div className="ds-entry">
                              <p className="inbox-todo-title">{en.item.title}</p>
                              <div className="inbox-todo-meta">
                                <Clock size={13} strokeWidth={2} />
                                <span>
                                  {en.p.kind === "schedule"
                                    ? `${fmtWhen((en.item as DraftPlanItem).start)} · ${en.item.duration} 分钟`
                                    : en.item.duration
                                      ? `${en.item.duration} 分钟`
                                      : "未估时"}
                                </span>
                                {notes ? (
                                  <span className="ds-entry-note" title={notes}>
                                    {notes.length > 22 ? `${notes.slice(0, 22)}…` : notes}
                                  </span>
                                ) : null}
                              </div>
                            </div>
                          </div>
                          {/* 悬停操作组（2026-10-26 对齐批四钮，顺序按用户定稿：确认 /
                              智能排期（App 侧无日期先入收件箱再引擎找空档）/ 删除
                              （Trash2 = 拒绝条目）/ 编辑（ChevronDown 进编辑态）） */}
                          <div className="inbox-todo-actions">
                            <button
                              type="button"
                              className="inbox-todo-icon ok"
                              aria-label="确认"
                              title="确认"
                              onClick={() => void confirmItem(en)}
                            >
                              <Check size={14} strokeWidth={2} />
                            </button>
                            <button
                              type="button"
                              className="inbox-todo-icon"
                              aria-label="智能排期"
                              title="智能排期"
                              onClick={() => void smartItem(en, en.item)}
                            >
                              <CalendarClock size={14} strokeWidth={2} />
                            </button>
                            <ConfirmPopover
                              message="拒绝该条目？同源同类内容将不再提醒"
                              onConfirm={() => void rejectItem(en)}
                              buttonClass="inbox-todo-icon del"
                              buttonTitle="删除"
                              icon={<Trash2 size={14} strokeWidth={2} />}
                            />
                            <button
                              type="button"
                              className="inbox-todo-icon"
                              aria-label="编辑"
                              title="编辑"
                              onClick={() => openEdit(en)}
                            >
                              <ChevronDown size={14} strokeWidth={2} className="inbox-todo-chevron" />
                            </button>
                          </div>
                        </>
                      )}

                      {traceKey === key && (
                        <ul className="ds-trace">
                          <li className="ds-trace-row">{traceRowOf(en.item.title, src.ref)}</li>
                        </ul>
                      )}
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>

      {notice && (
        <div className={`inbox-notice${notice.bad ? " bad" : ""}`} role="status">
          <span>{notice.text}</span>
        </div>
      )}

      <footer className="inbox-footer">
        <Database size={13} strokeWidth={2} />
        <span>采集提炼的建议 · 拖入日历或编辑选时间直接排期</span>
      </footer>
    </section>
  );
}

export default DataSourcePanel;
