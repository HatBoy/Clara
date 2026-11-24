import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { Database, Folder, Inbox } from "lucide-react";
import "./App.css";
import * as taskStore from "./lib/taskStore";
import { INITIAL_LOOKBACK_DAYS } from "./lib/taskStore";
import { applyScheduleTime, carveSeriesRow, dropOrCarveTask, mergeTaskPatch } from "./lib/taskWrites";
import Sidebar from "./components/Sidebar";
import ScheduleCanvas from "./components/ScheduleCanvas";
import SearchModal from "./components/SearchModal";
import CopilotPanel from "./components/CopilotPanel";
import { BulkScheduleModal } from "./components/BulkScheduleModal";
import InboxPanel from "./components/InboxPanel";
import DataSourcePanel from "./components/DataSourcePanel";
import ReportsPanel from "./components/ReportsPanel";
import StatsChatPanel from "./components/StatsChatPanel";
import SettingsDialog from "./components/SettingsDialog";
import ReminderToast, {
  beginToastExit,
  scheduleToastExit,
  toToastItem,
  type ReminderToastItem,
} from "./components/ReminderToast";
import UndoBar from "./components/UndoBar";
import OnboardingWizard from "./components/OnboardingWizard";
import {
  PROJECT_COLORS,
  isScheduled,
  type Project,
  type Task,
} from "./data/tasks";
import {
  API_BASE,
  autoScheduleTask,
  createProject,
  createTask,
  dayStartOf,
  deleteProject,
  deleteTask,
  epochDayOf,
  fetchProjects,
  fetchSettings,
  fetchSyncConfig,
  fetchTasks,
  applyUndo,
  fetchUndoState,
  scheduleToISO,
  snoozeReminder,
  toFrontendProject,
  toNaiveLocalISO,
  toFrontendSettings,
  toFrontendTask,
  toSettingsPatch,
  toTaskPatch,
  updateProject,
  updateSettings,
  updateTask,
  type ApiTask,
  type ReminderEvent,
  type SearchTaskRow,
  type TaskCreateBody,
} from "./lib/api";
import { toRrule } from "./lib/rrule";
import {
  draftToGhost,
  fetchAiProposals,
  entryHasSource,
  ingestItemsOf,
  isIngestProposal,
  decideProposal,
  type ProposalDecision,
  type DraftPlanItem,
} from "./lib/ai";
import { applyTheme, DEFAULT_SETTINGS, type Settings } from "./lib/settings";
import { playSound } from "./lib/sound";
import { notifyPeer, onPeerSync } from "./lib/sync";
import { dispatchShortcutCmd, onCanvasJump } from "./lib/shortcuts";
import {
  forwardReminderEvent,
  onAppShortcut,
  onReminderToastAction,
  panelWidths,
  savePanelWidths,
  sendSystemNotification,
  setCloseBehavior,
  showActionableReminderToast,
  syncAutostart,
} from "./lib/tauri";
import type { ParsedInput } from "./lib/syntax";
import type { DraggedTodoWithSuggest } from "./lib/dnd";

// 左右栏拖拽宽度约束（设计稿默认：左 200 / 右 540）
const LEFT_RANGE = [160, 320] as const;
const RIGHT_RANGE = [420, 720] as const;
// 中栏最小宽：中栏无横向滚动、内容完全自适应，须保证不被左右栏挤到无法展示
const CENTER_MIN = 480;

// 栏宽恢复 clamp：静态范围 + 动态上限（窗口宽不足时收窄——显示器变更等场景防
// 恢复值组合爆宽），与拖拽 onMove 同公式
const clampPanelWidth = (
  width: number,
  range: readonly [number, number],
  dynamicMax: number,
) => Math.min(range[1], Math.max(range[0], Math.min(width, dynamicMax)));

// 已排期任务的历史窗口：初始回看天数（覆盖月视图近期翻页）与每次向过去扩展的步长（天）
const EXTEND_CHUNK_DAYS = 90;

// 当前页面：日程 / 收件箱 / 数据源 / 统计报告 / 项目——左栏与中栏不变，仅右栏内容随页面切换
type ViewId = "schedule" | "inbox" | "datasource" | "reports" | "project";

// 快捷键动作 id → 页面（动作 id 带 view- 前缀是 Rust 侧单表口径，ViewId 不带）
const SHORTCUT_VIEW_ID = {
  "view-schedule": "schedule",
  "view-inbox": "inbox",
  "view-datasource": "datasource",
} as const;

// 助手已读位点（2026-11-04 通知分轨配套）：localStorage 记「上次面板可见时刻」，
// 启动恢复未读徽标初值用（GET /api/ai/messages?after=位点 计 assistant 消息数）
const LAST_SEEN_KEY = "clara.lastSeenMsgAt";

function App() {
  // 栏宽：main.tsx render 前已从 layout.json 填充 panelWidths（无记忆/纯浏览器
  // 为 null → 默认 200/540）；恢复值 clamp 静态范围 + 动态上限后使用
  const [leftWidth, setLeftWidth] = useState(() =>
    clampPanelWidth(
      panelWidths?.leftWidth ?? 200,
      LEFT_RANGE,
      window.innerWidth - (panelWidths?.rightWidth ?? 540) - CENTER_MIN,
    ),
  );
  const [rightWidth, setRightWidth] = useState(() =>
    clampPanelWidth(
      panelWidths?.rightWidth ?? 540,
      RIGHT_RANGE,
      window.innerWidth - leftWidth - CENTER_MIN,
    ),
  );
  // 右栏折叠态（日程/收件箱两页共享）：折叠时右栏与右拖拽条不渲染、中栏占满，展开按钮出现在中栏头部"日/周/月"右侧；rightWidth 保留以记忆拖拽宽度
  const [rightCollapsed, setRightCollapsed] = useState(false);
  // 统计报告页范围（App 层持有：ReportsPanel 切换，右侧统计问答面板共享同一
  // 范围——其上下文注入跟页面一致）
  const [statsDays, setStatsDays] = useState<7 | 30 | 90>(30);
  // 统一任务数据（从 FastAPI 后端拉取，累积式缓存）：未排期任务显示在收件箱/项目右栏，
  // 已排期任务显示在中栏日历；拖拽流转（归入项目 / 排期 / 改期）已接后端持久化——
  // 本地 setState 先行（乐观更新），API 异步写库，失败仅日志不回滚（刷新自愈）。
  // 加载策略（与增长模型对应）：未排期待办与重复系列天然有界恒全量；已排期单次任务仅
  // 历史方向无限增长 → 历史侧滚动窗口按需拉取、未来侧开放（启动只传 start）——未来侧
  // 开放同时保证项目页"已排期"开关能看到全部未来任务
  // 任务数据单源（2026-11-17 架构批 6）：tasks/projects/loadedFromDay 收进
  // lib/taskStore（模块级置位早于任何渲染，StrictMode 双跑窗口左边界恒就绪；
  // 挂载一次的监听闭包经 store 读最新边界——loadedFromDayRef 防陈旧补丁退役）
  const { tasks, projects, loadedFromDay } = taskStore.useTaskStore();
  // 项目列表（Sidebar 受控；任务 project 字段以 name 引用项目）——store 同源
  // 项目页当前查看的项目名（activeView === "project" 时有效）
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<ViewId>("schedule");
  // 设置悬浮页开关（侧栏底部入口触发）
  const [settingsOpen, setSettingsOpen] = useState(false);
  // 搜索弹层开关（侧栏搜索框 / Ctrl+K 触发，2026-11-06 搜索批）
  const [searchOpen, setSearchOpen] = useState(false);
  // 搜索待办定位请求（SearchModal 上报 → 切页后 InboxPanel 消费）：seq 计数器
  // 保证同一任务可重复触发（props 驱动零竞态——sync-now 回归教训的稳妥形态）
  const [searchFocus, setSearchFocus] = useState<{ id: string; seq: number } | null>(null);
  // 应用设置（"通用偏好"9 项；初始 DEFAULT_SETTINGS 兜底，启动从后端拉取覆盖——数据归 Python，
  // 不引入 localStorage）。时间轴参数化消费在 ScheduleCanvas（dayStart/dayEnd/workHours/午休）
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  // 设置引用：提醒 SSE 回调读最新通知形式/提示音（effect 只挂一次，闭包不随设置变化重建）
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  // 日程提醒页内兜底队列（仅纯浏览器 dev——无独立悬浮窗可弹时才用；key 用递增
  // 序号防同刻多事件撞键）
  const [reminderToasts, setReminderToasts] = useState<ReminderToastItem[]>([]);
  const toastKeyRef = useRef(0);
  // 全局撤销条（2026-11-17 批 1）：只展示后端栈顶（最近一次编辑/删除）
  const [undoHint, setUndoHint] = useState<{ label: string } | null>(null);
  const undoTimerRef = useRef<number | null>(null);
  // AI 排期草案（幽灵方块，阶段二）：chat 的 draft 事件驱动，采纳/拒绝后清空。
  // 仅主窗口内存态（不经 notifyPeer——widget 无画布消费方）
  const [aiDraft, setAiDraft] = useState<{ proposalId: string; items: DraftPlanItem[] } | null>(null);
  // 批量排期弹窗（2026-10-25 功能边界批）：bulk 帧载荷（引擎草案 + 卡行 id），
  // 草案条目标识随快照保存，重试沿用同一份选择。
  const [bulkDraft, setBulkDraft] = useState<{ proposalId: string; items: DraftPlanItem[]; ids: string[] } | null>(null);
  const [decisionError, setDecisionError] = useState("");
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  // AI 面板刷新信号：工作流事件流收 morning/ingest/nightly 即递增——
  // 建议卡与消息是后端真值，SSE 载荷不携带全量，递增 key 让 CopilotPanel 重拉
  const [aiRefreshKey, setAiRefreshKey] = useState(0);
  // 启动首拉提示（2026-11-16 真机批）：新装机配好 WebDAV 后本地空、后台正在
  // 同步——期间主界面显示「正在从云端同步数据」条，SSE sync 完成事件清除
  const [syncBootHint, setSyncBootHint] = useState(false);
  // 启动失败提示与重试（2026-11-17 批 1）：后端未起/正在启动时不再只 console
  const [bootError, setBootError] = useState<string | null>(null);
  const [bootSeq, setBootSeq] = useState(0);
  const retryBoot = () => setBootSeq((n) => n + 1);
  // 首启访谈（2026-11-17 批 1）：设置加载完成 + 未访谈 + 非云端同步中 → 弹向导
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [onboardingDismissed, setOnboardingDismissed] = useState(false);
  // sync-now 快捷键触发计数：DataSourcePanel 经 props 消费（条件挂载面板收
  // 一次性 CustomEvent 有订阅竞态，2026-11-05 真机回归的修复——见该面板注释）
  const [syncNowTick, setSyncNowTick] = useState(0);
  // 助手未读计数（2026-11-04 通知分轨）：AI 定时任务产物与远程轮消息走收件箱轨
  // （聊天框 + 徽标，不再弹打断式提醒）——面板不可见时新消息累计，展开清零。
  // copilotVisibleRef 供 SSE onmessage 闭包读实时可见性（state 闭包会滞后）
  const [aiUnread, setAiUnread] = useState(0);
  const copilotVisibleRef = useRef(false);
  // 数据源页徽标（2026-10-23）：待确认采集建议卡数（from_ingest 未决行）。工作流
  // 事件递增与面板结算操作时重拉——chat 轮的对话框内可见卡仍不触发（CopilotPanel
  // 自身重拉即纠偏），但采集轮经 onIngestDone 递增（2026-10-25 功能边界：卡只落
  // 数据源页，对话框零卡片，徽标必须及时），取舍登记 docs/tech-debt.md
  const [dsPendingCount, setDsPendingCount] = useState(0);
  // 设置页初始分区（助手面板引导卡直达 "model" 用；打开时消费，关闭回落默认第一项）
  const [settingsMenu, setSettingsMenu] = useState<string | null>(null);

  // 数据原语直连 taskStore（原与 WidgetWindow 逐字重复的本地实现退役，调用点零改动；
  // upsertProjects 的 App 侧调用点已全部收敛 setProjects/updateProjects/applyPeer）
  const upsertTasks = taskStore.upsertTasks;

  // 项目 id → name 映射（窗口扩展批的任务转换用）；启动批直接用当次请求的 apiProjects 建
  const projectNameById = useMemo(
    () => new Map(projects.flatMap((p) => (p.id ? [[p.id, p.name] as const] : []))),
    [projects],
  );

  // 启动四请求并行：项目 + 未排期待办（全量）+ 已排期任务（历史窗口起点今天-30，未来侧
  // 开放）+ 应用设置。StrictMode 双执行：GET 无副作用幂等，cancelled 标记丢弃首轮迟到结果，
  // 二轮覆盖即终态；失败保持空列表/默认设置（底座 withStartupRetry 已等待后端就绪）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [apiProjects, apiUnscheduled, apiWindow, apiSettings, apiSync] = await Promise.all([
          fetchProjects(),
          fetchTasks({ unscheduled: true }),
          fetchTasks({ start: dayStartOf(epochDayOf(new Date()) - INITIAL_LOOKBACK_DAYS) }),
          fetchSettings(),
          fetchSyncConfig().catch(() => null), // 同步状态独立失败不拖垮启动
        ]);
        if (cancelled) return;
        taskStore.setProjects(apiProjects.map(toFrontendProject));
        // 设置落位 + 启动副作用对齐（DB 为真源单向同步）：关闭行为同步 Rust（托盘拦截读它）、
        // 开机自启同步注册表；主题由下方 themeMode effect 驱动，无需在此重复调用
        const frontendSettings = toFrontendSettings(apiSettings);
        setSettings(frontendSettings);
        setSettingsLoaded(true);
        setCloseBehavior(frontendSettings.closeAction);
        void syncAutostart(frontendSettings.autoStart);
        // 先拿 projects 建 name 映射再转 tasks（映射依赖）；两批任务合并 upsert
        const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
        const merged = [...apiUnscheduled, ...apiWindow];
        upsertTasks(merged.map((t) => toFrontendTask(t, nameById)));
        // 新装机首拉场景：本地空 + 后台正在同步 → 显示「正在从云端同步数据」
        // 提示条（2026-11-16 真机批：此前空白近一分钟无任何反馈）；SSE sync
        // 事件（同步完成）到达时清除，数据经 reloadTasks 自然到达。
        // 60s 兜底自清（二轮批 1）：完成事件若在 SSE 订阅建立前发出（建连毫秒
        // 级竞态）提示条会永卡——数据本身经 reloadTasks/切页照常到达
        if (apiSync?.syncing && merged.length === 0) {
          setSyncBootHint(true);
          window.setTimeout(() => setSyncBootHint(false), 60_000);
        }
        setBootError(null);
      } catch (e) {
        console.error("从后端加载数据失败", e);
        setBootError(e instanceof Error ? e.message : "无法连接后端，请确认服务已启动");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootSeq]);

  // 主题驱动：themeMode 变化时应用（dark 或 system+系统深色 → html[data-theme="dark"]）；
  // system 模式下常挂 matchMedia 监听实时跟随系统切换（清理函数移除，防泄漏）
  useEffect(() => {
    applyTheme(settings.themeMode);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme(settings.themeMode);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [settings.themeMode]);

  // 日程提醒事件流（SSE 长链接，后端调度循环精确到点推送）：主窗口是唯一 SSE
  // 消费者与提示音播放者（WebView2 后台音频允许），事件到达按设置分发——提示音
  // 播放 + 通知形式分发（互斥，跟随设置）；"不提醒"档的仅声音事件（lead_min
  // 为 null）只播提示音、无视觉呈现。"居中/右下角悬浮框" = 转发给独立置顶提醒窗
  // 口（主窗口最小化/被遮挡也浮在屏幕上——位置随"居中/右下角"设置由窗口侧
  // 定位；转发失败回落页内兜底——纯浏览器 dev
  // 无独立窗口）；"系统通知" = Tauri 原生通知。断线 EventSource 自动重连（后端
  // 不在时静默失败重试，恢复即续上）；断线期间错过的提醒后端不回放（无可提醒对象）
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/reminders/stream`);
    es.onmessage = (e) => {
      let ev: ReminderEvent;
      try {
        ev = JSON.parse(e.data) as ReminderEvent;
      } catch {
        return; // 坏载荷丢弃（后端口径内不该发生）
      }
      const s = settingsRef.current;
      if (s.sound !== "off") playSound(s.sound);
      // "不提醒"档（lead_min = null）= 仅提示音通道：只播声音，无任何视觉呈现
      if (ev.lead_min === null) return;
      if (s.notifyStyle === "system") {
        const startAt = new Date(ev.start_at);
        const timeText = `${String(startAt.getHours()).padStart(2, "0")}:${String(startAt.getMinutes()).padStart(2, "0")}`;
        // 会前准备已生成的会议附注记，引导去助手面板看清单（2026-10-20）
        const prepNote = ev.prep_proposal_id ? "（准备清单已生成，见助手面板）" : "";
        const body = `${ev.title} ${timeText} ${prepNote}`;
        // 可操作 toast（2026-11-17 批 4）：带「完成 / 稍后」按钮；非 Windows
        // 或显示失败回落插件通知（无按钮，动作走提醒窗/页内 toast）
        void showActionableReminderToast({
          taskId: ev.task_id,
          title: "Clara 日程提醒",
          body,
          canDone: !ev.recurrence, // 系列 V1 不支持完成
          refStart: ev.ref_start ?? ev.start_at,
        }).then((ok) => {
          if (!ok) void sendSystemNotification("Clara 日程提醒", body);
        });
        return;
      }
      void forwardReminderEvent(ev).then((ok) => {
        if (ok) return;
        const key = ++toastKeyRef.current;
        setReminderToasts((prev) => [...prev, toToastItem(ev, key)]);
        scheduleToastExit(key, setReminderToasts);
      });
    };
    return () => es.close();
  }, []);

  // 系统 toast 按钮回传（2026-11-17 批 4）：完成 = 勾选任务；稍后 = 只推迟提醒
  // 不移动日程；"open"（点通知本体）= 无动作。写回后整体重拉 + 通知对端窗口。
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void onReminderToastAction(({ task_id: taskId, action, ref_start: refStart }) => {
      if (!taskId) return;
      const req =
        action === "done"
          ? updateTask(taskId, { done: true })
          : action === "snooze10" || action === "snooze60"
            ? snoozeReminder(taskId, action === "snooze10" ? 10 : 60, refStart)
            : null;
      if (!req) return;
      req
        .then(() => {
          reloadTasks();
          notifyPeer("main", { kind: "reload" });
        })
        .catch((e) => console.error("提醒通知按钮写回失败", action, e));
    }).then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reloadTasks 定义在本 effect 之后（挂载一次即可）
  }, []);

  // AI 工作流事件流（SSE，后端 asyncio 调度推送）。2026-11-04 通知分轨（用户拍板，
  // 对标主流 Time-sensitive vs Scheduled Summary）：AI 定时任务产物（晨报/夜报/
  // 过载/清单/简报/采集）一律走收件箱轨——落聊天框（后端 append_message）+ 入口
  // 未读徽标 + 可选轻提示音（assistant_chime，默认关），**不再弹打断式提醒**
  // （旧版借提醒管线弹提醒窗/toast/系统通知 + 声音，是"零新 UI"时代的权宜，
  // 信息性内容打断注意力属通知疲劳反模式）；日程提醒与会前重推仍走打断轨
  // （提醒流管线，notify_style/sound 管辖——设置页文案已分轨说明）。断线自动重连同上
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/ai/stream`);
    es.onmessage = (e) => {
      let ev: { type?: string; headline?: string };
      try {
        ev = JSON.parse(e.data);
      } catch {
        return; // 坏载荷丢弃
      }
      // 直写库信号（ics 导入等）：只重拉任务，无助手消息——不进未读/刷新/提示音轨
      if (ev.type === "tasks_changed") {
        reloadTasks();
        notifyPeer("main", { kind: "reload" });
        return;
      }
      // WebDAV 同步完成（2026-11-14 同步备份）：拉入的数据横跨任务/消息/项目等
      // 全部域——重拉任务 + 递增刷新信号（助手面板/设置页状态行即时更新）；
      // 同属直写库轨：不进未读、不响提示音
      if (ev.type === "sync") {
        reloadTasks();
        notifyPeer("main", { kind: "reload" });
        setAiRefreshKey((k) => k + 1);
        setSyncBootHint(false);
        return;
      }
      if (ev.type !== "morning" && ev.type !== "ingest" && ev.type !== "nightly" && ev.type !== "overload" &&
          ev.type !== "prep" && ev.type !== "hardtask" && ev.type !== "brief" && ev.type !== "chat_sync" &&
          ev.type !== "weekly_report" && ev.type !== "minutes")
        return;
      // 收件箱轨统一动作：刷新信号（CopilotPanel 重拉真值）+ 未读累计（面板
      // 不可见时）+ 可选轻提示音（chat_sync 除外——用户正在手机上对话，桌面
      // 端任何提示都是噪音，2026-10-23 口径不变）
      setAiRefreshKey((k) => k + 1);
      if (!copilotVisibleRef.current) setAiUnread((n) => n + 1);
      // 采集轮直落收件箱的任务实时可见（2026-11-04 真机批）：trusted 直通建的
      // 任务此前只靠用户切页/写操作触发重拉才出现——ingest 事件显式重拉任务
      if (ev.type === "ingest") {
        reloadTasks();
        notifyPeer("main", { kind: "reload" });
      }
      const s = settingsRef.current;
      if (ev.type !== "chat_sync" && s.assistantChime && s.sound !== "off") playSound(s.sound);
    };
    return () => es.close();
  }, []);

  // 助手面板可见性（常驻渲染 display 切换口径同 CopilotPanel 挂载）：可见 = 已读
  // （清零未读 + 推进已读位点）；ref 供 SSE onmessage 闭包读实时值（state 闭包滞后
  // 一轮事件）。位点持久化（2026-11-04 二次修订）：localStorage 记「上次面板可见
  // 时刻」——重启后启动恢复 effect 取此后到达的 assistant 消息数为未读初值，
  // 早上没看的晨报中午重启仍有徽标（主流微信/Slack 的未读是持久状态，同构）
  const copilotVisible = !rightCollapsed && activeView === "schedule";
  useEffect(() => {
    copilotVisibleRef.current = copilotVisible;
    if (copilotVisible) {
      setAiUnread(0);
      try {
        // 后端 Message.created_at 是本地 naive 时间；toISOString() 是 UTC 串，
        // 直接比较会偏一个时区（重启后未读计数虚高，2026-11-16 优化批 1）。
        window.localStorage.setItem(LAST_SEEN_KEY, toNaiveLocalISO(new Date()));
      } catch {
        /* 隐私模式等 localStorage 不可用：仅本轮会话内未读，可接受降级 */
      }
    }
  }, [copilotVisible]);

  // 未读位点恢复（挂载一次）：面板初始不可见时，按位点拉「此后到达」消息计
  // 未读初值——SSE 只覆盖运行期，跨重启的未读由这一查补上；无位点（首装/清库）
  // 从零起算
  useEffect(() => {
    if (!rightCollapsed && activeView === "schedule") return; // 初始即见面：无需恢复
    let last: string | null = null;
    try {
      last = window.localStorage.getItem(LAST_SEEN_KEY);
    } catch {
      return;
    }
    if (!last) return;
    fetch(`${API_BASE}/api/ai/messages?after=${encodeURIComponent(last)}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((msgs: { role: string }[]) => {
        const n = msgs.filter((m) => m.role === "assistant").length;
        if (n > 0 && !copilotVisibleRef.current) setAiUnread(n);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootSeq]);

  // 桌面小部件同步监听：widget 只发 upsert（快捷录入建任务/拖动拉伸改排期，载荷为
  // 前端形状零转换直接入列）、整删与 reload 三类。接收处理纯读不回发，双窗写操作各发
  // 各的，天然无回环；reload = widget 的 Clone & Override 编辑/删除（this/following）与
  // 写失败自愈——挖空/截断 + 新行等派生变化只 upsert 新行会让本窗系列副本缺
  // cancelledDates/until（该日虚拟实例与例外实体双渲染），整窗重拉最简；settings 载荷
  // 仅 main → widget 方向，本窗无需处理。非 Tauri 下 onPeerSync 返回空 unlisten，
  // 纯浏览器 dev 此 effect 恒 no-op
  useEffect(() => {
    let unlisten: () => void = () => {};
    let disposed = false;
    onPeerSync((payload) => {
      // 数据半边收 taskStore.applyPeer（两窗同构分派单源）；reload 是窗口编排
      if (payload.kind === "reload") {
        reloadTasks();
        return;
      }
      taskStore.applyPeer(payload);
    }).then((fn) => {
      if (disposed) fn(); // 等待订阅期间组件已卸载：立即取消本次订阅
      else unlisten = fn;
    });
    return () => {
      disposed = true;
      unlisten();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootSeq]);

  // 全局快捷键落地（键串注册与按下识别全在 Rust，此处只收 action id）：视图切换与
  // 右栏折叠是 App 自持 state 直接做；日历档位/翻页/录入聚焦/助手聚焦属组件内部
  // 状态，经 dispatchShortcutCmd 进程内总线转派给 ScheduleCanvas / QuickAdd /
  // CopilotPanel。组件级动作延一拍（setTimeout 0）再 dispatch——本回调里的
  // setActiveView / setRightCollapsed 要先完成重渲染，目标组件才挂载到可交互的
  // DOM 上（否则 CopilotPanel 由隐藏转显示那一帧前 focus 必然落空）。
  // sync-now 与手动点击刷新完全同款（2026-11-05 推翻早前"不唤窗走系统通知"口径：
  // 零可见反馈让用户误以为没生效）：Rust 已唤窗，此处切到数据源页并递增
  // syncNowTick——DataSourcePanel 依赖驱动 effect 复用 refreshNow 全套（转圈 +
  // 「采集中…」+ 完成摘要）。不走事件转派：面板条件挂载有订阅竞态（同日回归修复）。
  useEffect(() => {
    let unlisten: () => void = () => {};
    let disposed = false;
    onAppShortcut((action) => {
      switch (action) {
        case "view-schedule":
        case "view-inbox":
        case "view-datasource":
          // 口径同左栏导航：切页同时清项目选中（项目页高亮与 activeProject 绑定）
          setActiveView(SHORTCUT_VIEW_ID[action]);
          setActiveProject(null);
          break;
        case "toggle-right":
          setRightCollapsed((v) => !v);
          break;
        case "open-assistant":
          // copilotVisible 是派生值（!rightCollapsed && activeView === "schedule"），
          // 无独立开关：切到日程页 + 展开右栏即展开助手
          setActiveView("schedule");
          setActiveProject(null);
          setRightCollapsed(false);
          setTimeout(() => dispatchShortcutCmd(action), 0);
          break;
        case "open-search":
          // 弹层覆盖当前页（不切页）；显式分支不走事件转派——SearchModal 条件
          // 挂载，一次性 CustomEvent 有订阅竞态（sync-now 回归教训）。
          // toggle 语义（真机批二修）：再按同键关闭，与 Ctrl+K 一致
          setSearchOpen((v) => !v);
          break;
        case "sync-now":
          // 切数据源页 + 递增触发计数（props 驱动，挂载后 effect 必达——事件转派
          // 在条件挂载面板上有订阅竞态，2026-11-05 真机回归修复）
          setActiveView("datasource");
          setActiveProject(null);
          setSyncNowTick((t) => t + 1);
          break;
        default:
          // view-day / view-week / view-month / go-today / prev-period /
          // next-period / quick-add —— 组件内部状态，转派
          setTimeout(() => dispatchShortcutCmd(action), 0);
      }
    }).then((fn) => {
      if (disposed) fn(); // 等待订阅期间组件已卸载：立即取消本次订阅
      else unlisten = fn;
    });
    return () => {
      disposed = true;
      unlisten();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootSeq]);

  // Ctrl+K 开搜索（2026-11-06 搜索批）：window capture，任何上下文（含输入框
  // 内）都唤起——组合键不经 IME，无 isComposing 顾虑
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  // 搜索跳日程从任意页面切回 schedule（2026-11 统计页全宽改造配套）：画布订阅
  //（ScheduleCanvas 内）只改画布内 state，页面态归 App——统计页画布隐藏时若不
  // 切回，跳转对用户不可见；schedule 下 setState 同值 no-op
  useEffect(
    () => onCanvasJump(() => {
      setActiveView("schedule");
      setActiveProject(null);
    }),
    [],
  );

  // 日历翻页进入未加载的历史区间时向左扩展一批（事件式回调，StrictMode 不双调）。
  // 扩边界与 upsert 同批落（触发条件清除，无循环）；失败不扩边界，下次导航自然重试自愈
  const handleVisibleRangeChange = (fromDay: number) => {
    if (fromDay >= loadedFromDay) return; // 已覆盖（未来侧开放恒覆盖，只需判左边界）
    const fetchFrom = fromDay - EXTEND_CHUNK_DAYS; // 左侧余量：月视图翻约 3 页才再一次请求
    fetchTasks({ start: dayStartOf(fetchFrom), end: dayStartOf(loadedFromDay) })
      .then((api) => {
        taskStore.extendWindow(fetchFrom, api.map((t) => toFrontendTask(t, projectNameById)));
      })
      .catch((e) => console.error("扩展历史窗口失败", e));
  };

  // 整窗重拉任务纠偏：收编 taskStore.reloadPruned（剪枝谓词/项目同批回填/迟到
  // 结果丢弃/in-flight 守卫全在 store；窗口起点读 store 当前值——闭包零陈旧）
  const reloadTasks = taskStore.reloadPruned;

  // 写请求失败自愈：乐观更新已生效（错值可能已推给 widget），本地重拉 + 通知对端 reload
  // 把两窗都拉回后端真值——不回滚本地（无快照机制，重拉语义等价且能顺带纠偏对端）
  const recoverFromWriteError = (label: string, e: unknown) => {
    console.error(label, e);
    setUndoHint(null); // 写失败已重拉真值，旧快照不可再撤销
    reloadTasks();
    notifyPeer("main", { kind: "reload" });
  };

  // —— 撤销最近一次操作（2026-11-17 批 1）：写成功后刷新栈顶，6 秒自动收 ——
  const refreshUndo = () => {
    fetchUndoState()
      .then((s) => {
        if (undoTimerRef.current) window.clearTimeout(undoTimerRef.current);
        if (!s.available) {
          setUndoHint(null);
          return;
        }
        setUndoHint({ label: s.label ?? "上一步操作" });
        undoTimerRef.current = window.setTimeout(() => setUndoHint(null), 6000);
      })
      .catch(() => {});
  };

  const handleUndo = () => {
    if (undoTimerRef.current) window.clearTimeout(undoTimerRef.current);
    applyUndo()
      .then(() => {
        setUndoHint(null);
        reloadTasks();
        notifyPeer("main", { kind: "reload" });
      })
      .catch((e) => {
        console.error("撤销失败", e);
        // 失败可见（二轮批 1：旧版静默收条，用户不知为何没撤销）：条上直显
        // 后端 detail（如「项目名已存在」），按钮仍可重试
        const detail = (e as { detail?: string })?.detail;
        setUndoHint({
          label: detail ? `撤销失败：${detail}` : "撤销失败，请稍后重试",
        });
        undoTimerRef.current = window.setTimeout(() => setUndoHint(null), 6000);
        reloadTasks();
      });
  };

  // Ctrl+Z 撤销：输入框/可编辑区内不拦截（保留文字撤销语义）
  useEffect(() => {
    if (!undoHint) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.key.toLowerCase() !== "z") return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || t?.isContentEditable) return;
      e.preventDefault();
      handleUndo();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // 设置项更新（乐观 + 响应回填，口径同任务写回惯例）：本地 setState 先行；系统级副作用
  // 即时分发（关闭行为同步 Rust、开机自启同步注册表——不等网络，托盘拦截要读它；
  // 主题由 themeMode effect 驱动）；PATCH 成功后用后端返回的全量回填（吃午休越界自动
  // 修复等纠偏值，UI 跳变毫秒级纠正）；失败重拉 GET 回填后端权威态（2026-10-21 复查
  // 批 D2 修：自动收录未达标等受控 422 后，乐观值曾是假开态直到手动刷新）
  const handleUpdateSettings = (patch: Partial<Settings>) => {
    setSettings((prev) => ({ ...prev, ...patch }));
    if ("closeAction" in patch && patch.closeAction) setCloseBehavior(patch.closeAction);
    if ("autoStart" in patch && patch.autoStart !== undefined) void syncAutostart(patch.autoStart);
    updateSettings(toSettingsPatch(patch))
      .then((s) => {
        const frontend = toFrontendSettings(s);
        setSettings(frontend);
        // 设置同步给桌面小部件（主题/时间轴区间/负载基准等全部跟随）
        notifyPeer("main", { kind: "settings", settings: frontend });
      })
      .catch((e) => {
        console.error("设置写回后端失败", e);
        fetchSettings()
          .then((raw) => {
            const frontend = toFrontendSettings(raw);
            setSettings(frontend);
            notifyPeer("main", { kind: "settings", settings: frontend });
          })
          .catch(() => {}); // 后端彻底不可达：保留乐观值（下次成功写回自然纠正）
      });
  };

  // 派生：已排期任务 → 中栏日历；徽标口径 = 未排期且未归属项目的任务数（默认收件箱列表长度）
  const scheduledEvents = tasks.filter(isScheduled);
  // 已完成待办不再计入（2026-11-16 优化批 1）：勾选完成后后端 unscheduled 不再
  // 返回该行，但本地乐观值仍在——不排除 done 会让徽标永久多算。
  const inboxCount = tasks.filter((t) => !isScheduled(t) && !t.project && !t.done).length;
  // 项目页"已排期"开关受控值（2026-11-10 项目筛选联动）：开 = 中栏日历过滤为当前
  // 项目；中栏日历筛选态（null = 全量）——chip ✕ 直接清，effect 依赖不含 filter，
  // ✕ 后不重设（本次停留不再自动），开关/项目/页面任一变化才重新同步
  const [projScheduledOn, setProjScheduledOn] = useState(false);
  const [calendarFilter, setCalendarFilter] = useState<string | null>(null);
  useEffect(() => {
    if (activeView !== "project" || !projScheduledOn) setCalendarFilter(null);
    else setCalendarFilter(activeProject ?? null);
  }, [activeView, activeProject, projScheduledOn]);
  // 各项目任务计数（2026-11-10 徽标改「待排期/总数」）：unscheduled = 未排期且已归属
  // 该项目（口径同项目页默认列表），total = 该项目全部任务（含已排期日程，含完成态
  // ——「项目全貌」口径，与列表长度本就不同）；侧栏项目项尾部显示「x/y」
  const projectCounts = useMemo(() => {
    const m = new Map<string, { unscheduled: number; total: number }>();
    for (const t of tasks) {
      if (!t.project) continue;
      const e = m.get(t.project) ?? { unscheduled: 0, total: 0 };
      e.total += 1;
      if (!isScheduled(t)) e.unscheduled += 1;
      m.set(t.project, e);
    }
    return m;
  }, [tasks]);

  // 统一排期写回（待办拖入日历与日历卡片拖动/拉伸共用）：显式赋三字段，end 传 undefined 即"无结束时间"。
  // end === undefined ⟺ 原本就无结束时刻（调用点保证）→ 不发 end_time 键，后端不动该列；
  // recurrence_rule 键永不出现——重复系列跨天拖动只重锚定基准（start_time），规则字符串不变。
  // 三字段一致性（与后端不变式同口径）：start/end 齐备时 duration_mins ≡ end-start，本地
  // 同步重算 minutes 与展示文本（拉伸改时长后收件箱时长展示即刻跟上，不等刷新）；
  // 无 end 时 duration 不动（规则只约束三值齐备态）
  const handleScheduleChange = (
    taskId: string,
    dateOffset: number,
    start: number,
    end: number | undefined,
    // 重复系列范围三问（2026-11-12 拖拽三问批）：this = 例外实体化到新时间（原
    // 实例日挖空）、following = UNTIL 截断 + 新系列承接新时间；缺省 = 既有乐观
    // 合并直发（非重复卡与「所有」分支，行为零变化）
    opts?: { mode: "this" | "following"; date: string },
  ) => {
    const apply = (t: Task): Task => applyScheduleTime(t, start, end, dateOffset);
    const body = { start_time: scheduleToISO(dateOffset, start), ...(end !== undefined ? { end_time: scheduleToISO(dateOffset, end) } : {}) };
    if (opts) {
      // 三问 this/following 两步（乐观模式同 handleUpdateTask）：先乐观挖空/
      // 截断主行，PATCH 响应的例外行/新系列行 upsert 回来；widget 整体重拉
      // 最简（同 handleUpdateTask 同分支取舍）
      taskStore.mapTask(taskId, (t) => carveSeriesRow(t, opts));
      notifyPeer("main", { kind: "reload" });
      updateTask(taskId, body, opts)
        .then((created) => {
          upsertTasks([toFrontendTask(created, projectNameById)]);
          refreshUndo();
        })
        .catch((e) => recoverFromWriteError("重复日程排期写回失败", e));
      return;
    }
    taskStore.mapTask(taskId, apply);
    // 排期变化同步给桌面小部件（乐观前端形状整任务，widget 按 id upsert）
    const src = tasks.find((t) => t.id === taskId);
    if (src) notifyPeer("main", { kind: "upsert", tasks: [apply(src)] });
    updateTask(taskId, body)
      .then(() => refreshUndo())
      .catch((e) => recoverFromWriteError("排期写回后端失败", e));
  };

  // 待办拖入侧栏项目：写入/转移项目归属（排期字段不动）。前端以 name 引用项目，
  // 持久化按 id 寻址——name → id 经 projects state 解析
  const handleTodoToProject = (projectName: string, taskId: string) => {
    taskStore.mapTask(taskId, (t) => ({ ...t, project: projectName }));
    // 归属变化同步给桌面小部件（该任务即从其收件箱默认列表消失）
    const src = tasks.find((t) => t.id === taskId);
    if (src) notifyPeer("main", { kind: "upsert", tasks: [{ ...src, project: projectName }] });
    const project = projects.find((p) => p.name === projectName);
    if (project?.id) {
      updateTask(taskId, { project_id: project.id })
        .then(() => refreshUndo())
        .catch((e) => recoverFromWriteError("任务归属项目写回后端失败", e));
    } else {
      console.warn("项目无后端 id，跳过归属持久化", projectName);
    }
  };

  // 待办字段编辑（标题/备注/耗时/优先级/重复）：patch 合并写回（收件箱/项目右栏编辑区
  // 与日历详情浮层共用），toTaskPatch 负责前端形状 → 请求体（显式 undefined 翻 null 表清空）。
  // 重复系列可带 opts 走 Clone & Override 分流（日历浮层三问）：this = 乐观挖空该日虚拟
  // 实例（追加 cancelledDates）、following = 乐观给老系列加 until 截断，两分支的新行 id 由
  // 后端生成，须等响应再 upsert（挖空到 upsert 之间的毫秒空窗该日无卡，本地回环可忽略）；
  // all / 无 opts = 现状乐观合并
  const handleUpdateTask = (
    taskId: string,
    patch: Partial<Task>,
    opts?: { mode: "this" | "following" | "all"; date: string },
  ) => {
    taskStore.mapTask(
      taskId,
      (t) => (opts?.mode === "this" || opts?.mode === "following")
        ? carveSeriesRow(t, opts)
        : mergeTaskPatch(t, patch),
    );
    if (opts?.mode === "this" || opts?.mode === "following") {
      // Clone & Override 分支牵连主行挖空/截断 + 新行两处派生变化，widget 整体重拉
      // 最简（只 upsert 新行会让它上面的系列主行副本缺 cancelledDates/until，该日
      // 虚拟实例与例外实体双渲染——取舍同下方删除 this/following 分支）
      notifyPeer("main", { kind: "reload" });
      updateTask(taskId, toTaskPatch(patch), opts)
        .then((created) => {
          upsertTasks([toFrontendTask(created, projectNameById)]);
          refreshUndo();
        })
        .catch((e) => recoverFromWriteError("任务编辑写回后端失败", e));
      return;
    }
    // all/无 opts：乐观合并后的整任务同步给 widget（清空重复规则连带清挖空，口径同上面 map 内）
    const src = tasks.find((t) => t.id === taskId);
    if (src) {
      notifyPeer("main", { kind: "upsert", tasks: [mergeTaskPatch(src, patch)] });
    }
    // 纯 done 勾选不刷新撤销条（二轮批 1 回归批 1 原设计）：后端同口径不建撤销
    // 条目——刷条会把上一条旧操作的撤销入口当新操作误弹（收件箱 4 秒局部撤销
    // 是勾选反转的唯一入口）
    const doneOnly = Object.keys(patch).length === 1 && "done" in patch;
    updateTask(taskId, toTaskPatch(patch))
      .then(() => {
        if (!doneOnly) refreshUndo();
      })
      .catch((e) => recoverFromWriteError("任务编辑写回后端失败", e));
  };

  // 删除任务三分支（与后端 DELETE mode 对齐）：this = 墓碑挖空单次（追加 cancelledDates，
  // 系列行保留）；following = UNTIL 截断（recurrence 加 until = 被删日前一天，行保留）；
  // 无 opts / all = 整任务删（行移除，后端按生命周期判定物理删/软删）
  const handleDeleteTask = (
    taskId: string,
    opts?: { mode: "this" | "following" | "all"; date: string },
  ) => {
    const keepRow = opts?.mode === "this" || opts?.mode === "following";
    taskStore.mapTasks((list) => dropOrCarveTask(list, taskId, opts));
    // 整删按 id 通知 widget 移除；this/following 保系列行的派生变化（挖空/截断）
    // 交给 widget 整体重拉
    notifyPeer("main", keepRow ? { kind: "reload" } : { kind: "delete", taskIds: [taskId] });
    deleteTask(taskId, opts)
      .then(() => refreshUndo())
      .catch((e) => recoverFromWriteError("任务删除写回后端失败", e));
  };

  // 一键智能安排（统一排期引擎，2026-10-20）：规则层定时间并落库，成功行转前端对象
  // upsert + 同步 widget（口径同任务写回）；错误向上抛——收件箱面板 catch 后用
  // err.detail 显示瞬时提示（后端 422 无空档 / 409 已排期文案可直显）
  const handleAutoSchedule = async (taskId: string) => {
    const updated = await autoScheduleTask(taskId);
    const frontend = toFrontendTask(updated, projectNameById);
    upsertTasks([frontend]);
    notifyPeer("main", { kind: "upsert", tasks: [frontend] });
  };

  // 新建项目（色点从十色板随机取一；同名守卫——侧栏 key 与任务 project 字段都以 name 为准）。
  // 唯一非乐观更新的写操作：必须 await 后端拿回 UUID 再入 state，否则后续重命名/星标/删除无法寻址。
  // 返回新建/已存在的项目；创建失败向上抛——快捷录入经 handleCreateTask 抛给 QuickAdd
  // 红边提示保留输入（# 归属意图不再静默丢失），侧栏调用点自行 catch 保留输入
  const handleAddProject = async (name: string): Promise<Project> => {
    const existing = projects.find((p) => p.name === name);
    if (existing) return existing;
    const color = PROJECT_COLORS[Math.floor(Math.random() * PROJECT_COLORS.length)];
    const created = await createProject(name, color);
    const frontend = toFrontendProject(created);
    // 入列前再查一次：await 期间快速双 Enter 的第二发同请求已被后端 409 拒掉
    taskStore.updateProjects((prev) =>
      prev.some((p) => p.name === name) ? prev : [...prev, frontend],
    );
    // 新建项目同步给桌面小部件（其快捷录入 # 气泡与任务归属解析依赖项目列表）
    notifyPeer("main", { kind: "upsert", projects: [frontend] });
    return frontend;
  };

  // 快捷录入建任务（不乐观：新行 id 由后端生成，await 后 upsert——模式同 this/following
  // 编辑分支与新建项目）。未知 # 项目名（决策 2026-09-14）顺手创建并归属；失败向上抛，
  // 由 QuickAdd 捕获红边提示并保留输入文本
  const handleCreateTask = async (draft: ParsedInput) => {
    // 项目解析：已有项目按 name 找 id；未知项目先建再拿 id（新建的 projects state 本帧
    // 不可见，id/name 直接用返回值，id→name 映射局部补一份给 toFrontendTask）
    let projectId: string | undefined;
    let nameById = projectNameById;
    if (draft.newProject) {
      const created = await handleAddProject(draft.newProject); // 失败向上抛：QuickAdd 红边保留输入
      projectId = created.id;
      if (created.id) {
        nameById = new Map(projectNameById);
        nameById.set(created.id, created.name);
      }
    } else if (draft.project) {
      projectId = projects.find((p) => p.name === draft.project)?.id;
      if (!projectId) console.warn("项目无后端 id，跳过归属持久化", draft.project);
    }
    const body: TaskCreateBody = {
      title: draft.title,
      ...(draft.minutes !== undefined ? { duration_mins: draft.minutes } : {}),
      ...(draft.dateOffset !== undefined && draft.startHour !== undefined
        ? {
            start_time: scheduleToISO(draft.dateOffset, draft.startHour),
            ...(draft.endHour !== undefined
              ? { end_time: scheduleToISO(draft.dateOffset, draft.endHour) }
              : {}),
          }
        : {}),
      ...(draft.allDay ? { all_day: true } : {}), // 全天/跨天（批 B）：钟点 0 + 后端归一
      ...(draft.recurrence ? { recurrence_rule: toRrule(draft.recurrence) ?? undefined } : {}),
      ...(projectId ? { project_id: projectId } : {}),
    };
    const created = await createTask(body);
    const frontend = toFrontendTask(created, nameById);
    upsertTasks([frontend]);
    // 新任务同步给桌面小部件（未排期进其收件箱列表，排期今日进其时间线）
    notifyPeer("main", { kind: "upsert", tasks: [frontend] });
  };

  // —— AI 排期草案（阶段二）：幽灵方块驱动与采纳落库 ——

  // 建任务、改期、删除和结算都由同一个后端事务负责。响应丢失也重新拉取两端真值。
  const handleDecide = async (id: string, items: ProposalDecision[]) => {
    if (!items.length || items.some(it => !it.item_id)) throw new Error("建议已变更，请刷新");
    try {
      return await decideProposal(id, items);
    } finally {
      reloadTasks();
      setAiRefreshKey(k => k + 1);
    }
  };

  const handleSuggestDrop = async (todo: DraggedTodoWithSuggest, dateOffset: number, start: number) => {
    const s = todo.suggest;
    if (!s) return;
    setDecisionError("");
    try {
      await handleDecide(s.proposalId, [{ item_id: s.itemId, action: "schedule", entry: {
        start: scheduleToISO(dateOffset, start), duration: todo.minutes ?? 60,
      } }]);
    } catch {
      setDecisionError("未确认写入，请按刷新后的建议重试。已结算条目不会重复创建。");
    }
  };

  // 数据源页徽标（2026-10-24 改条目数口径——一卡多条拆条渲染后卡数与页面视觉
  // 对不上）：挂载初拉 + 工作流事件重拉 + 切页重拉（activeView——面板自拉只在
  // 打开时刷新面板 state，徽标若不随切页重拉会出现「页里 6 条徽标 0」的错位）；
  // 徽标只按重新读取的待处理条目计数。
  useEffect(() => {
    let current = true;
    fetchAiProposals(true)
      .then((rows) => current &&
        setDsPendingCount(
          rows
            .filter(isIngestProposal)
            .reduce(
              (n, p) => n + ingestItemsOf(p).filter((_, i) => entryHasSource(p, i)).length,
              0,
            ),
        ),
      )
      .catch(() => {});
    return () => { current = false; };
  }, [aiRefreshKey, activeView]);

  // chat 的 draft 事件：置幽灵方块（叠画中栏日视图，仅查看日命中项渲染）
  const handleDraftEvent = (items: DraftPlanItem[], proposalId: string) => {
    setAiDraft({ proposalId, items });
  };

  // chat 的 applied 事件（2026-10-17 快录直落）：任务已由后端壳写库，这里只做
  // 前端落位——转换 + upsert + widget 同步（模式同 applyDraftItems 新建尾段；
  // 不依赖 refreshKey——那只会重拉面板不刷画布，快录必须立即出现在中栏）
  const handleAppliedEvent = (apiTasks: ApiTask[]) => {
    const frontend = apiTasks.map((t) => toFrontendTask(t, projectNameById));
    if (!frontend.length) return;
    upsertTasks(frontend);
    notifyPeer("main", { kind: "upsert", tasks: frontend });
  };

  // 一次提交整份选择；未勾选是跳过，不是拒绝数据源。
  const handleBulkConfirm = async (selected: number[] | null) => {
    if (!bulkDraft || bulkSubmitting) return;
    setBulkSubmitting(true);
    setDecisionError("");
    try {
      await handleDecide(bulkDraft.proposalId, bulkDraft.ids.map((item_id, i) => ({
        item_id, action: selected === null ? "reject" : selected.includes(i) ? "accept" : "skip",
      })));
      setBulkDraft(null);
    } catch (e) {
      const conflict = (e as {status?: number}).status === 409;
      setDecisionError(conflict ? "建议已被其他操作更改，请查看最新状态。" : "未收到写入回执，请重试。");
      if (conflict) setBulkDraft(null);
    } finally { setBulkSubmitting(false); }
  };

  // 设置页打开（助手面板引导卡可指定初始分区直达）
  const handleOpenSettings = (menu?: string) => {
    setSettingsMenu(menu ?? null);
    setSettingsOpen(true);
  };

  // 重命名项目：name 是任务归属与项目页定位的唯一标识，须三处联动
  // （项目列表、所有引用该项目的任务、当前打开的项目页）；后端按 id 寻址，
  // id 须在 setProjects 改名前解析（改名后旧 name 查不到）
  const handleRenameProject = (oldName: string, newName: string) => {
    const project = projects.find((p) => p.name === oldName);
    if (project?.id) {
      updateProject(project.id, { name: newName })
        .then(() => refreshUndo())
        .catch((e) => recoverFromWriteError("项目重命名写回后端失败", e));
    }
    taskStore.updateProjects((prev) =>
      prev.map((p) => (p.name === oldName ? { ...p, name: newName } : p)),
    );
    taskStore.mapTasks((prev) =>
      prev.map((t) => (t.project === oldName ? { ...t, project: newName } : t)),
    );
    setActiveProject((n) => (n === oldName ? newName : n));
    // 重命名牵连项目列表与全部归属任务的 project 字段，widget 整体重拉最简
    notifyPeer("main", { kind: "reload" });
  };

  // 删除项目：该项目下所有任务的归属置空（回到默认收件箱列表）；
  // 若正打开该项目页则切回收件箱（项目页失去主体）。后端同事务置空归属任务并软删项目
  const handleDeleteProject = (name: string) => {
    const project = projects.find((p) => p.name === name);
    if (project?.id) {
      deleteProject(project.id)
        .then(() => refreshUndo())
        .catch((e) => recoverFromWriteError("项目删除写回后端失败", e));
    }
    taskStore.updateProjects((prev) => prev.filter((p) => p.name !== name));
    taskStore.mapTasks((prev) =>
      prev.map((t) => (t.project === name ? { ...t, project: undefined } : t)),
    );
    if (activeView === "project" && activeProject === name) {
      setActiveView("inbox");
      setActiveProject(null);
    }
    // 删除牵连归属置空等派生变化，widget 整体重拉最简
    notifyPeer("main", { kind: "reload" });
  };

  // 标记/取消重点项目（侧栏排序靠前的开关）
  const handleToggleProjectStar = (name: string) => {
    const project = projects.find((p) => p.name === name);
    if (project?.id) {
      updateProject(project.id, { starred: !project.starred })
        .then(() => refreshUndo())
        .catch((e) => recoverFromWriteError("项目星标写回后端失败", e));
    }
    taskStore.updateProjects((prev) =>
      prev.map((p) => (p.name === name ? { ...p, starred: !p.starred } : p)),
    );
  };

  const handleSelectProject = (name: string) => {
    setActiveView("project");
    setActiveProject(name);
  };

  // 搜索待办定位（2026-11-06 搜索批）：未排期未完成待办 → 有归属切项目页、
  // 否则收件箱页；递增 seq 供 InboxPanel effect 消费（展开+滚动+高亮）
  const handleLocateTodo = (row: SearchTaskRow) => {
    if (row.project_name) {
      setActiveView("project");
      setActiveProject(row.project_name);
    } else {
      setActiveView("inbox");
      setActiveProject(null);
    }
    setSearchFocus((f) => ({ id: row.id, seq: (f?.seq ?? 0) + 1 }));
  };

  // 搜索快速创建（120 分档，滴答同款行为）：直接建未排期待办 → 切收件箱定位
  const handleCreateFromSearch = async (title: string) => {
    const api = await createTask({ title });
    upsertTasks([toFrontendTask(api, projectNameById)]);
    setActiveView("inbox");
    setActiveProject(null);
    setSearchFocus((f) => ({ id: api.id, seq: (f?.seq ?? 0) + 1 }));
  };

  const startResize = (e: ReactMouseEvent, side: "left" | "right") => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = side === "left" ? leftWidth : rightWidth;
    const [min, max] = side === "left" ? LEFT_RANGE : RIGHT_RANGE;
    const setWidth = side === "left" ? setLeftWidth : setRightWidth;

    const onMove = (ev: MouseEvent) => {
      // 左栏随鼠标右移变宽，右栏随鼠标左移变宽
      const delta = side === "left" ? ev.clientX - startX : startX - ev.clientX;
      // 本栏拖宽不得把中栏挤到 CENTER_MIN 以下
      const otherWidth = side === "left" ? rightWidth : leftWidth;
      const dynamicMax = window.innerWidth - otherWidth - CENTER_MIN;
      setWidth(
        Math.min(max, Math.max(min, Math.min(startWidth + delta, dynamicMax))),
      );
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.classList.remove("resizing");
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    document.body.classList.add("resizing");
  };

  // 栏宽持久化：变更后 300ms 防抖写回 Tauri 侧 layout.json（非 Tauri no-op）——
  // 拖拽中每次 move 重排计时器、松手后一次落盘，覆盖拖拽等全部修改源
  useEffect(() => {
    const timer = setTimeout(() => savePanelWidths(leftWidth, rightWidth), 300);
    return () => clearTimeout(timer);
  }, [leftWidth, rightWidth]);

  return (
    <div className="app">
      {/* 首拉反馈：新装机后台同步中（数据到达经 SSE reloadTasks 自然接管） */}
      {decisionError && !bulkDraft && <div className="sync-boot-hint" role="alert">{decisionError} <button onClick={() => setDecisionError("")}>关闭</button></div>}
      {syncBootHint && (
        <div className="sync-boot-hint">正在从云端同步数据，稍候…</div>
      )}
      {bootError && (
        <div className="sync-boot-hint error">
          无法加载数据：{bootError}
          <button type="button" className="sync-boot-retry" onClick={retryBoot}>
            重试
          </button>
        </div>
      )}
      <Sidebar
        style={{ width: leftWidth, flexShrink: 0 }}
        activeId={activeView}
        onNavigate={(id) => {
          // NAV_ITEMS 三项 id 均为合法 ViewId，直接落（项目页另经 onSelectProject 进入）
          setActiveView(id as ViewId);
          setActiveProject(null);
        }}
        inboxCount={inboxCount}
        datasourceCount={dsPendingCount}
        projects={projects}
        projectCounts={projectCounts}
        activeProject={activeView === "project" ? activeProject : null}
        onSelectProject={handleSelectProject}
        onTodoToProject={handleTodoToProject}
        onAddProject={handleAddProject}
        onRenameProject={handleRenameProject}
        onDeleteProject={handleDeleteProject}
        onToggleProjectStar={handleToggleProjectStar}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenSearch={() => setSearchOpen(true)}
      />
      <div
        className="resizer"
        style={{ left: leftWidth }}
        onMouseDown={(e) => startResize(e, "left")}
      />
      <ScheduleCanvas
        // 统计页隐藏画布（2026-11 全宽改造）：display:none 常驻保视图态与跳转
        // 订阅（照 CopilotPanel 先例；undefined 时走 CSS 原样式）
        style={{ display: activeView === "reports" ? "none" : undefined }}
        onExpandCopilot={
          rightCollapsed ? () => setRightCollapsed(false) : undefined
        }
        // 助手未读徽标（2026-11-04 通知分轨；同日修订仅日程页）：仅日程页的展开
        // 按钮是助手面板入口（收件箱/项目页展开的是收件箱面板——挂上徽标既语义
        // 错位又点了消不掉，非日程页消息感知由切回日程页后的面板/徽标承接）
        expandBadge={activeView === "schedule" ? aiUnread : undefined}
        // AI 总闸关闭时日程页入口按钮灰化（收件箱/项目页按钮与 AI 无关不灰）；
        // 点击仍展开——面板渲染「AI 助手已关闭」引导卡直达设置页
        expandDisabled={activeView === "schedule" && !settings.aiEnabled}
        // 展开按钮图标按页面区分：日程页 Sparkles（Copilot 入口），收件箱页 Inbox，
        // 项目页 Folder（统计页画布隐藏，按钮不存在——reports 分支已撤）
        expandIcon={
          activeView === "inbox" ? (
            <Inbox size={15} strokeWidth={2} />
          ) : activeView === "datasource" ? (
            <Database size={15} strokeWidth={2} />
          ) : activeView === "project" ? (
            <Folder size={15} strokeWidth={2} />
          ) : undefined
        }
        // 已排期任务（统一数据源的子集）与排期写回回调；详情浮层的项目取色与字段编辑/删除回调；
        // 设置（时间轴区间/每日工时/午休块）驱动刻度、拖拽 clamp、负载基准与建议块
        events={scheduledEvents}
        settings={settings}
        onScheduleChange={handleScheduleChange}
        // 数据源条目拖入日历直排（2026-10-24）：落点即排期时间，建任务 + 结算一步完成
        onSuggestDrop={handleSuggestDrop}
        onCreateTask={handleCreateTask}
        onVisibleRangeChange={handleVisibleRangeChange}
        projects={projects}
        onUpdateTask={handleUpdateTask}
        onDeleteTask={handleDeleteTask}
        ghosts={aiDraft ? aiDraft.items.map(draftToGhost) : undefined}
        // 全量任务（2026-11-10 批 2）：详情浮层纪要回看区块筛 "minutes:<会议id>"
        // 溯源行动项（未排期待办不在 events 已排期子集里）
        tasks={tasks}
        // 项目筛选（2026-11-10）：项目页"已排期"开关联动——渲染层只画该项目日程，
        // 头部 chip 常显可 ✕ 回全量（状态机见 App 顶部 effect）
        projectFilter={calendarFilter}
        onClearProjectFilter={() => setCalendarFilter(null)}
      />
      {/* 日程助手常驻渲染（2026-10-21）：切页面/折叠右栏只切 display 不卸载——
          卸载会掐断 SSE 流（后端断连后 _finalize_turn 不执行，assistant 回复生成了
          也丢弃不落库）且丢失打字机/草稿/建议卡状态；display:none 下组件照常运行，
          只有程序退出才销毁，回来时会话与输出都在继续 */}
      {/* 日程助手右栏：仅日程页显示（其他页用 display:none 保持状态） */}
      {!rightCollapsed && activeView === "schedule" && (
        <div
          className="resizer"
          style={{ right: rightWidth }}
          onMouseDown={(e) => startResize(e, "right")}
        />
      )}
      <CopilotPanel
        style={{
          width: rightWidth,
          flexShrink: 0,
          display: !rightCollapsed && activeView === "schedule" ? undefined : "none",
        }}
        onCollapse={() => setRightCollapsed(true)}
        onOpenSettings={handleOpenSettings}
        // AI 总闸状态（2026-10-21 五点修缮）：关闭时面板进入引导态——
        // 「AI 助手已关闭」引导卡 + 输入禁用，总闸效果前端可见
        aiEnabled={settings.aiEnabled}
        draft={aiDraft}
        // 收到结算回执后再撤幽灵方块。
        onDiscardDraft={() => setAiDraft(null)}
        onDraft={handleDraftEvent}
        onDecide={handleDecide}
        onApplied={handleAppliedEvent}
        // 采集轮收尾（ingest_done 帧）：卡在数据源页对话框零卡片，递增钥匙刷
        // 数据源徽标/面板——chat 轮出卡不递增的既有纪律只对对话框内可见卡成立
        onIngestDone={() => setAiRefreshKey((k) => k + 1)}
        // 批量排期草案（bulk 帧）→ 开弹窗清单确认
        onBulk={(items, proposalId, ids) => { setDecisionError(""); setBulkDraft({ items, proposalId, ids }); }}
        // 工作流事件后的重拉信号（morning/ingest/nightly 递增）
        refreshKey={aiRefreshKey}
      />
      {/* 右栏与拖拽条：日程/收件箱/项目/数据源页显示（统计页单独处理） */}
      {!rightCollapsed && activeView !== "reports" && activeView !== "schedule" && (
        <>
          <div
            className="resizer"
            style={{ right: rightWidth }}
            onMouseDown={(e) => startResize(e, "right")}
          />
          {activeView === "inbox" || activeView === "project" ? (
            <InboxPanel
              style={{ width: rightWidth, flexShrink: 0 }}
              onCollapse={() => setRightCollapsed(true)}
              // 传全量任务：项目页"已排期"开关需要看到排期中的任务
              tasks={tasks}
              mode={activeView === "inbox" ? "inbox" : "project"}
              project={
                activeView === "project"
                  ? projects.find((p) => p.name === activeProject)
                  : undefined
              }
              projects={projects}
              onUpdateTask={handleUpdateTask}
              onDeleteTask={handleDeleteTask}
              onAutoSchedule={handleAutoSchedule}
              // 手动选时间排期（2026-10-24）：编辑区「安排时间」行提交，与拖拽
              // （日历 drop → 同一 handler）同一条写回链路
              onScheduleChange={handleScheduleChange}
              // 项目页"已排期"开关受控（2026-11-10 筛选联动）：App 态驱动中栏
              // 日历项目过滤（见上方 effect），inbox 模式开关不受影响
              projectToggleOn={projScheduledOn}
              onProjectToggleChange={setProjScheduledOn}
              focusTask={searchFocus}
              onQuickAdd={() => {
                setActiveView("schedule");
                window.setTimeout(() => dispatchShortcutCmd("quick-add"), 60);
              }}
              onOpenData={() => setActiveView("datasource")}
              loadError={bootError}
              onRetry={retryBoot}
            />
          ) : activeView === "datasource" ? (
            <DataSourcePanel
              style={{ width: rightWidth, flexShrink: 0 }}
              onCollapse={() => setRightCollapsed(true)}
              onDecide={handleDecide}
              // 乐观减徽标（2026-10-24 竞态根修）：结算不再触发 aiRefreshKey 重拉——
              // 重拉与后端回写竞态曾致「点两次才消失」；权威对账留给外部事件/切页
              onOpenSettings={() => handleOpenSettings("data")}
              refreshKey={aiRefreshKey}
              syncTrigger={syncNowTick}
            />
          ) : null}
        </>
      )}

      {/* 统计报告页：中栏 ReportsPanel + 右栏统计问答面板（StatsChatPanel 常驻
          渲染见下，display:none 切换——折叠不丢对话、SSE 流不断）；画布
          display:none 常驻保状态 */}
      {activeView === "reports" && (
        <>
          <ReportsPanel
            style={{ flex: 1, minWidth: 0 }}
            days={statsDays}
            onDaysChange={setStatsDays}
            onOpenProject={handleSelectProject}
            onOpenMemory={() => handleOpenSettings("memory")}
            dsPending={dsPendingCount}
            refreshKey={aiRefreshKey}
            chatOpen={!rightCollapsed}
            onToggleChat={() => setRightCollapsed(!rightCollapsed)}
          />
          {!rightCollapsed && (
            <div
              className="resizer"
              style={{ right: rightWidth }}
              onMouseDown={(e) => startResize(e, "right")}
            />
          )}
        </>
      )}
      {/* 统计问答面板常驻渲染（照 CopilotPanel 先例）：仅统计页且未折叠时可见，
          切页/折叠只切 display 不卸载——问答历史与在途 SSE 流都保留 */}
      <StatsChatPanel
        style={{
          width: rightWidth,
          flexShrink: 0,
          display: !rightCollapsed && activeView === "reports" ? undefined : "none",
        }}
        onCollapse={() => setRightCollapsed(true)}
        days={statsDays}
      />

      {/* 批量排期确认弹窗（bulk 帧驱动，2026-10-25 功能边界）：骨架复用
          FormModal（Esc/遮罩关闭走取消回写） */}
      {bulkDraft && (
        <BulkScheduleModal
          items={bulkDraft.items}
          submitting={bulkSubmitting}
          onConfirm={handleBulkConfirm}
          error={decisionError}
          onCancel={() => void handleBulkConfirm(null)}
        />
      )}

      {/* 设置悬浮页（fixed 遮罩 + 居中浮层，压过一切层级）；设置数据受控下发 */}
      {settingsOpen && (
        <SettingsDialog
          onClose={() => setSettingsOpen(false)}
          settings={settings}
          onUpdateSettings={handleUpdateSettings}
          initialMenu={settingsMenu ?? undefined}
          onModelChange={() => setAiRefreshKey((k) => k + 1)}
          memoryRefreshKey={aiRefreshKey}
        />
      )}

      {/* 搜索弹层（2026-11-06 搜索批）：Ctrl+K / 侧栏搜索框唤起，z-60 压过
           设置页（50）；日程跳转走 dispatchCanvasJump 在弹层内自理 */}
      {searchOpen && (
        <SearchModal
          onClose={() => setSearchOpen(false)}
          onLocateTodo={handleLocateTodo}
          onLocateProject={(name) => {
            handleSelectProject(name);
            setSearchOpen(false);
          }}
          onGotoDatasource={() => {
            setActiveView("datasource");
            setActiveProject(null);
            setSearchOpen(false);
          }}
          onCreateTodo={(title) => {
            setSearchOpen(false);
            void handleCreateFromSearch(title);
          }}
        />
      )}

      {/* 首启入职访谈（2026-11-17 批 1）：仅未访谈且非云端同步中弹出一次 */}
      {settingsLoaded && settings.onboardedAt === null && !syncBootHint
        && !onboardingDismissed && (
        <OnboardingWizard
          settings={settings}
          onClose={() => setOnboardingDismissed(true)}
          onDone={(next) => {
            setSettings(next);
            setOnboardingDismissed(true);
            notifyPeer("main", { kind: "settings", settings: next });
          }}
          onOpenModel={() => {
            setOnboardingDismissed(true);
            handleOpenSettings("model");
          }}
        />
      )}

      {/* 全局撤销条（2026-11-17 批 1）：主窗底部居中，6 秒自动消失 */}
      {undoHint && (
        <UndoBar
          label={undoHint.label}
          onUndo={handleUndo}
          onClose={() => setUndoHint(null)}
        />
      )}

      {/* 日程提醒页内兜底（纯浏览器 dev 无独立悬浮窗时呈现，inline 居中偏上；
           Tauri 下悬浮框由独立置顶窗口承载，此队列恒空不渲染）。操作按钮走
           直接写库 + 整体重拉（此形态无 widget/提醒窗对端，notifyPeer 无意义） */}
      <ReminderToast
        inline
        items={reminderToasts}
        onClose={(key) => beginToastExit(key, setReminderToasts)}
        onAction={(it, action) => {
          if (!it.taskId) return;
          const req = action === "done"
            ? updateTask(it.taskId, { done: true })
            : snoozeReminder(it.taskId, action === "snooze10" ? 10 : 60, it.refStartIso);
          req
            .then(() => {
              reloadTasks();
              beginToastExit(it.key, setReminderToasts);
            })
            .catch((e) => console.error("提醒操作写回失败", action, e));
        }}
      />
    </div>
  );
}

export default App;
