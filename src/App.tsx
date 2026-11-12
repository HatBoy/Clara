import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { Database, Folder, Inbox } from "lucide-react";
import "./App.css";
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
  toastActionPatch,
  type ReminderToastItem,
} from "./components/ReminderToast";
import {
  PROJECT_COLORS,
  durationText,
  isScheduled,
  shiftDateKey,
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
  fetchTasks,
  scheduleToISO,
  toFrontendProject,
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
  resolveProposal,
  settleProposalItems,
  type AiProposal,
  type DraftPlanItem,
  type EvidenceEntry,
  type InboxItem,
} from "./lib/ai";
import { applyTheme, DEFAULT_SETTINGS, type Settings } from "./lib/settings";
import { playSound } from "./lib/sound";
import { notifyPeer, onPeerSync } from "./lib/sync";
import { dispatchShortcutCmd, onCanvasJump } from "./lib/shortcuts";
import {
  forwardReminderEvent,
  onAppShortcut,
  panelWidths,
  savePanelWidths,
  sendSystemNotification,
  setCloseBehavior,
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
const INITIAL_LOOKBACK_DAYS = 30;
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
  const [tasks, setTasks] = useState<Task[]>([]);
  // 已加载历史窗口的左边界（epoch day 绝对日序号，跨午夜不失准；未来侧开放无右边界）。
  // 经 useState 初始值同步置位——若等 fetch 完成才置位，StrictMode 挂载双跑的在途
  // 间隙里日历的首次范围上报会误判未覆盖而触发冗余扩展请求
  const [loadedFromDay, setLoadedFromDay] = useState(
    () => epochDayOf(new Date()) - INITIAL_LOOKBACK_DAYS,
  );
  // 项目列表（Sidebar 受控；任务 project 字段以 name 引用项目）
  const [projects, setProjects] = useState<Project[]>([]);
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
  // AI 排期草案（幽灵方块，阶段二）：chat 的 draft 事件驱动，采纳/拒绝后清空。
  // 仅主窗口内存态（不经 notifyPeer——widget 无画布消费方）
  const [aiDraft, setAiDraft] = useState<{ proposalId: string; items: DraftPlanItem[] } | null>(null);
  // 批量排期弹窗（2026-10-25 功能边界批）：bulk 帧载荷（引擎草案 + 卡行 id），
  // 确认走 applyDraftItems 改期路径 + 卡行结清；null = 无弹窗
  const [bulkDraft, setBulkDraft] = useState<{ proposalId: string; items: DraftPlanItem[] } | null>(null);
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  // AI 面板刷新信号：工作流事件流收 morning/ingest/nightly 即递增——
  // 建议卡与消息是后端真值，SSE 载荷不携带全量，递增 key 让 CopilotPanel 重拉
  const [aiRefreshKey, setAiRefreshKey] = useState(0);
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

  // 累积式缓存 upsert：Map 按 id 合并（旧保位、新追加）。保序是收件箱"默认排序"
  //（后端 sort_order 序）的前提——该组只由 unscheduled 批构成，窗口批（start 非空）
  // 不混入；不淘汰旧数据（翻回已加载窗口秒显，内存增长有界于翻过的范围）
  const upsertTasks = (incoming: Task[]) =>
    setTasks((prev) => {
      if (incoming.length === 0) return prev;
      const byId = new Map(prev.map((t) => [t.id, t]));
      for (const t of incoming) byId.set(t.id, t);
      return [...byId.values()];
    });

  // 项目累积式 upsert（口径同 upsertTasks）：Map 按标识合并保序（id 缺失回退 name，
  // 项目均来自后端 id 正常恒在）；桌面小部件同步（widget 侧建项目）入列用
  const upsertProjects = (incoming: Project[]) =>
    setProjects((prev) => {
      if (incoming.length === 0) return prev;
      const byId = new Map(prev.map((p) => [p.id ?? p.name, p]));
      for (const p of incoming) byId.set(p.id ?? p.name, p);
      return [...byId.values()];
    });

  // 项目 id → name 映射（窗口扩展批的任务转换用）；启动批直接用当次请求的 apiProjects 建
  const projectNameById = useMemo(
    () => new Map(projects.flatMap((p) => (p.id ? [[p.id, p.name] as const] : []))),
    [projects],
  );

  // 启动四请求并行：项目 + 未排期待办（全量）+ 已排期任务（历史窗口起点今天-30，未来侧
  // 开放）+ 应用设置。StrictMode 双执行：GET 无副作用幂等，cancelled 标记丢弃首轮迟到结果，
  // 二轮覆盖即终态；失败保持空列表/默认设置（本地回环毫秒级，不加 loading UI）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [apiProjects, apiUnscheduled, apiWindow, apiSettings] = await Promise.all([
          fetchProjects(),
          fetchTasks({ unscheduled: true }),
          fetchTasks({ start: dayStartOf(epochDayOf(new Date()) - INITIAL_LOOKBACK_DAYS) }),
          fetchSettings(),
        ]);
        if (cancelled) return;
        setProjects(apiProjects.map(toFrontendProject));
        // 设置落位 + 启动副作用对齐（DB 为真源单向同步）：关闭行为同步 Rust（托盘拦截读它）、
        // 开机自启同步注册表；主题由下方 themeMode effect 驱动，无需在此重复调用
        const frontendSettings = toFrontendSettings(apiSettings);
        setSettings(frontendSettings);
        setCloseBehavior(frontendSettings.closeAction);
        void syncAutostart(frontendSettings.autoStart);
        // 先拿 projects 建 name 映射再转 tasks（映射依赖）；两批任务合并 upsert
        const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
        upsertTasks([...apiUnscheduled, ...apiWindow].map((t) => toFrontendTask(t, nameById)));
      } catch (e) {
        console.error("从后端加载数据失败", e);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        void sendSystemNotification("Clara 日程提醒", `${ev.title} ${timeText} ${prepNote}`);
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
      if (ev.type === "ingest") reloadTasks();
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
        window.localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString());
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
  }, []);

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
      if (payload.kind === "upsert") {
        if (payload.tasks) upsertTasks(payload.tasks);
        if (payload.projects) upsertProjects(payload.projects);
      } else if (payload.kind === "delete") {
        const ids = new Set(payload.taskIds);
        setTasks((list) => list.filter((t) => !ids.has(t.id)));
      } else if (payload.kind === "reload") {
        reloadTasks();
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
  }, []);

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
  }, []);

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
        upsertTasks(api.map((t) => toFrontendTask(t, projectNameById)));
        setLoadedFromDay(fetchFrom);
      })
      .catch((e) => console.error("扩展历史窗口失败", e));
  };

  // 整窗重拉任务纠偏（自包含：项目名映射用当次请求结果，不依赖组件 state——同步监听
  // effect 挂载一次，闭包里的 projectNameById 若取组件 state 会陈旧为首帧空映射，把任务
  // 归属抹成无项目）：unscheduled 全量 + 已加载窗口起点的已排期批，upsert 浅替换整行
  // （乐观删掉的行也会被后端行复活）。两处消费：widget 推来的 reload 载荷（Clone &
  // Override 编辑/删除牵连派生变化）、本窗写请求失败自愈。挂载监听路径的 loadedFromDay
  // 闭包陈旧为首帧初始值——纠偏至少覆盖初始窗口，足够。自愈请求失败静默（后端不在时
  // 无数据可纠，下次写失败再试）
  const reloadTasks = () => {
    fetchProjects()
      .then((apiProjects) => {
        const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
        return Promise.all([
          fetchTasks({ unscheduled: true }),
          fetchTasks({ start: dayStartOf(loadedFromDay) }),
        ]).then((batches) => batches.flat().map((t) => toFrontendTask(t, nameById)));
      })
      .then(upsertTasks)
      .catch(() => {});
  };

  // 写请求失败自愈：乐观更新已生效（错值可能已推给 widget），本地重拉 + 通知对端 reload
  // 把两窗都拉回后端真值——不回滚本地（无快照机制，重拉语义等价且能顺带纠偏对端）
  const recoverFromWriteError = (label: string, e: unknown) => {
    console.error(label, e);
    reloadTasks();
    notifyPeer("main", { kind: "reload" });
  };

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
  const inboxCount = tasks.filter((t) => !isScheduled(t) && !t.project).length;
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
    const minutes = end !== undefined ? Math.round((end - start) * 60) : undefined;
    const apply = (t: Task): Task => ({
      ...t,
      dateOffset,
      start,
      end,
      ...(minutes !== undefined
        ? { minutes, duration: durationText(minutes) }
        : {}),
    });
    const body = { start_time: scheduleToISO(dateOffset, start), ...(end !== undefined ? { end_time: scheduleToISO(dateOffset, end) } : {}) };
    if (opts) {
      // 三问 this/following 两步（乐观模式同 handleUpdateTask）：先乐观挖空/
      // 截断主行，PATCH 响应的例外行/新系列行 upsert 回来；widget 整体重拉
      // 最简（同 handleUpdateTask 同分支取舍）
      setTasks((list) =>
        list.map((t) => {
          if (t.id !== taskId) return t;
          if (opts.mode === "this") {
            return { ...t, cancelledDates: [...(t.cancelledDates ?? []), opts.date] };
          }
          return t.recurrence
            ? { ...t, recurrence: { ...t.recurrence, until: shiftDateKey(opts.date, -1) } }
            : t;
        }),
      );
      notifyPeer("main", { kind: "reload" });
      updateTask(taskId, body, opts)
        .then((created) => upsertTasks([toFrontendTask(created, projectNameById)]))
        .catch((e) => recoverFromWriteError("重复日程排期写回失败", e));
      return;
    }
    setTasks((list) => list.map((t) => (t.id === taskId ? apply(t) : t)));
    // 排期变化同步给桌面小部件（乐观前端形状整任务，widget 按 id upsert）
    const src = tasks.find((t) => t.id === taskId);
    if (src) notifyPeer("main", { kind: "upsert", tasks: [apply(src)] });
    updateTask(taskId, body).catch((e) => console.error("排期写回后端失败", e));
  };

  // 待办拖入侧栏项目：写入/转移项目归属（排期字段不动）。前端以 name 引用项目，
  // 持久化按 id 寻址——name → id 经 projects state 解析
  const handleTodoToProject = (projectName: string, taskId: string) => {
    setTasks((list) =>
      list.map((t) => (t.id === taskId ? { ...t, project: projectName } : t)),
    );
    // 归属变化同步给桌面小部件（该任务即从其收件箱默认列表消失）
    const src = tasks.find((t) => t.id === taskId);
    if (src) notifyPeer("main", { kind: "upsert", tasks: [{ ...src, project: projectName }] });
    const project = projects.find((p) => p.name === projectName);
    if (project?.id) {
      updateTask(taskId, { project_id: project.id }).catch((e) =>
        console.error("任务归属项目写回后端失败", e),
      );
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
    setTasks((list) =>
      list.map((t) => {
        if (t.id !== taskId) return t;
        if (opts?.mode === "this") {
          return { ...t, cancelledDates: [...(t.cancelledDates ?? []), opts.date] };
        }
        if (opts?.mode === "following") {
          return t.recurrence
            ? { ...t, recurrence: { ...t.recurrence, until: shiftDateKey(opts.date, -1) } }
            : t;
        }
        // 显式清空重复规则时连带清挖空日期——后端聚合只对带规则的行生成
        // cancelled_dates，保留会让已排期任务被 occursOn 挖空在唯一实例日（全隐身）
        if ("recurrence" in patch && patch.recurrence === undefined) {
          return { ...t, ...patch, cancelledDates: undefined };
        }
        return { ...t, ...patch };
      }),
    );
    if (opts?.mode === "this" || opts?.mode === "following") {
      // Clone & Override 分支牵连主行挖空/截断 + 新行两处派生变化，widget 整体重拉
      // 最简（只 upsert 新行会让它上面的系列主行副本缺 cancelledDates/until，该日
      // 虚拟实例与例外实体双渲染——取舍同下方删除 this/following 分支）
      notifyPeer("main", { kind: "reload" });
      updateTask(taskId, toTaskPatch(patch), opts)
        .then((created) => upsertTasks([toFrontendTask(created, projectNameById)]))
        .catch((e) => recoverFromWriteError("任务编辑写回后端失败", e));
      return;
    }
    // all/无 opts：乐观合并后的整任务同步给 widget（清空重复规则连带清挖空，口径同上面 map 内）
    const src = tasks.find((t) => t.id === taskId);
    if (src) {
      const merged: Task =
        "recurrence" in patch && patch.recurrence === undefined
          ? { ...src, ...patch, cancelledDates: undefined }
          : { ...src, ...patch };
      notifyPeer("main", { kind: "upsert", tasks: [merged] });
    }
    updateTask(taskId, toTaskPatch(patch)).catch((e) =>
      recoverFromWriteError("任务编辑写回后端失败", e),
    );
  };

  // 删除任务三分支（与后端 DELETE mode 对齐）：this = 墓碑挖空单次（追加 cancelledDates，
  // 系列行保留）；following = UNTIL 截断（recurrence 加 until = 被删日前一天，行保留）；
  // 无 opts / all = 整任务删（行移除，后端按生命周期判定物理删/软删）
  const handleDeleteTask = (
    taskId: string,
    opts?: { mode: "this" | "following" | "all"; date: string },
  ) => {
    const keepRow = opts?.mode === "this" || opts?.mode === "following";
    setTasks((list) =>
      keepRow
        ? list.map((t) => {
            if (t.id !== taskId || !opts) return t;
            if (opts.mode === "this") {
              return { ...t, cancelledDates: [...(t.cancelledDates ?? []), opts.date] };
            }
            return t.recurrence
              ? { ...t, recurrence: { ...t.recurrence, until: shiftDateKey(opts.date, -1) } }
              : t;
          })
        : list.filter((t) => t.id !== taskId),
    );
    // 整删按 id 通知 widget 移除；this/following 保系列行的派生变化（挖空/截断）
    // 交给 widget 整体重拉
    notifyPeer("main", keepRow ? { kind: "reload" } : { kind: "delete", taskIds: [taskId] });
    deleteTask(taskId, opts).catch((e) => recoverFromWriteError("任务删除写回后端失败", e));
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
    setProjects((prev) =>
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

  // 草案条目批量落库（当前草案采纳与历史 schedule 提案采纳共用）：有 task_id 且任务
  // 仍在 = 已有待办改排期（handleScheduleChange 乐观 + 写回 + widget 同步一条龙）；
  // 否则（无 task_id 或任务已被删）= 新建任务（id 后端生成，await 后 upsert + widget
  // 同步，模式同 handleCreateTask 尾段）。任一条目失败即中止返回 false——已应用条目
  // 保留（后端真值已在，刷新自愈），未应用条目随草案重试
  const applyDraftItems = async (items: DraftPlanItem[]): Promise<boolean> => {
    for (const item of items) {
      try {
        const g = draftToGhost(item);
        if (item.task_id && tasks.some((t) => t.id === item.task_id)) {
          handleScheduleChange(item.task_id, g.dateOffset, g.start, g.end);
        } else {
          const created = await createTask({
            title: item.title,
            start_time: item.start,
            end_time: scheduleToISO(g.dateOffset, g.end),
            priority: item.priority, // 明示重要 → 高优直判随采纳落库
            // 溯源三键透传（2026-10-24 直排批）：数据源页 schedule 条目确认与
            // inbox 条目手选时间直排不丢来源——教练咨询回放素材的地基
            ...(item.source_ref?.source_key
              ? { source_key: item.source_ref.source_key }
              : {}),
            // 编辑附加键透传（2026-10-26 收件箱 UI 对齐批）：数据源页编辑态组装的
            // 备注与重复规则预设（草稿条目量小，非重复建议不影响原通路）
            ...(item.notes ? { notes: item.notes } : {}),
            ...(item.recurrence
              ? { recurrence_rule: toRrule(item.recurrence) ?? undefined }
              : {}),
            ...(item.source_ref?.fingerprint
              ? { source_fingerprint: item.source_ref.fingerprint }
              : {}),
            ...(item.evidence ? { source_evidence: item.evidence } : {}),
          });
          const frontend = toFrontendTask(created, projectNameById);
          upsertTasks([frontend]);
          notifyPeer("main", { kind: "upsert", tasks: [frontend] });
        }
      } catch (e) {
        console.error("应用草案条目失败", e);
        return false;
      }
    }
    return true;
  };

  // 当前幽灵方块草案采纳已并入 handleAcceptProposal（2026-10-17 双击重复日程 bug
  // 修复）：曾存在 isCurrentDraft 分叉——首击走本函数只落库不回写面板卡片状态（按钮
  // 区不消失），二击换走 handleAcceptProposal 重复 applyDraftItems，createTask 非幂等
  // 产生两条重复日程。统一单路径 + 下方防重闸后删除本分叉。

  // inbox 建议条目批量落库（M1-②）：不带 start_time 建任务即入收件箱（sort_order
  // 排末 / ever_scheduled=False 后端自动）。语义与 applyDraftItems 逐字对齐：任一条
  // 失败返回 false（建议整卡按 rejected 回写——部分成功条目保留，刷新自愈）
  const applyInboxItems = async (items: InboxItem[]): Promise<boolean> => {
    for (const item of items) {
      try {
        const created = await createTask({
          title: item.title,
          ...(item.duration ? { duration_mins: item.duration } : {}),
          ...(item.notes ? { notes: item.notes } : {}),
          // 优先级透传（2026-10-24 数据源页样式对齐批）：编辑态可改，null/越界回落库默认 2
          ...(item.priority ? { priority: item.priority } : {}),
          // 溯源三键透传（2026-10-23）：采集建议采纳不丢来源——教练咨询该任务时
          // 按此回放素材摘录
          ...(item.source_ref?.source_key ? { source_key: item.source_ref.source_key } : {}),
          ...(item.source_ref?.fingerprint
            ? { source_fingerprint: item.source_ref.fingerprint } : {}),
          ...(item.evidence ? { source_evidence: item.evidence } : {}),
          // 重复规则预设透传（2026-10-26 收件箱 UI 对齐批）：未排期建任务随
          // recurrence_rule 落库成系列；Reduce 产物不带该键，原通路零影响
          ...(item.recurrence
            ? { recurrence_rule: toRrule(item.recurrence) ?? undefined }
            : {}),
        });
        const frontend = toFrontendTask(created, projectNameById);
        upsertTasks([frontend]);
        notifyPeer("main", { kind: "upsert", tasks: [frontend] });
      } catch (e) {
        console.error("应用收件箱条目失败", e);
        return false;
      }
    }
    return true;
  };

  // 高难协助步骤批量落库（2026-10-20）：镜像 applyInboxItems——steps 是纯标题
  // （拆解步骤，不带估时/备注、不占用 parent_id 墓碑语义），逐条入收件箱；
  // 任一失败返回 false（整卡 rejected 回写——部分成功条目保留，刷新自愈）
  const applyHardtaskSteps = async (steps: string[]): Promise<boolean> => {
    for (const step of steps) {
      try {
        const created = await createTask({ title: step });
        const frontend = toFrontendTask(created, projectNameById);
        upsertTasks([frontend]);
        notifyPeer("main", { kind: "upsert", tasks: [frontend] });
      } catch (e) {
        console.error("应用协助步骤失败", e);
        return false;
      }
    }
    return true;
  };

  // 采纳防重闸（2026-10-17）：面板乐观置徽标卸载按钮已挡住绝大多数重复点击，此闸
  // 兜底两类窗口——双击竞态（乐观态刷新前二次触发）与"应用失败重拉回待处理后再点"。
  // 带副作用的分支进入即记 id；应用失败移除放行重试；无副作用分支不占闸（回写幂等）
  const settledInFlightRef = useRef<Set<string>>(new Set());

  // 建议卡采纳（App 统一处理，2026-10-17 起含当前幽灵方块草案——单路径）：schedule =
  // 从 payload 重建草案应用；inbox = 逐条建任务入收件箱；delete = 逐条走既有整删
  // 管线（无 opts；非重复任务安全）；hardtask = 拆解步骤逐条入收件箱（2026-10-20）；
  // 其余仅回写。面板已乐观置徽标，失败下次重拉自愈回待处理
  const handleAcceptProposal = (proposal: AiProposal) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    if (proposal.kind === "schedule") {
      const items = (proposal.payload.draft_plan as DraftPlanItem[]) ?? [];
      if (aiDraft?.proposalId === proposal.id) setAiDraft(null);
      void applyDraftItems(items).then((ok) => {
        if (!ok) settledInFlightRef.current.delete(proposal.id);
        void resolveProposal(proposal.id, ok ? "accepted" : "rejected").catch((e) =>
          console.error("建议回写失败", e),
        );
      });
    } else if (proposal.kind === "delete") {
      const items = (proposal.payload.tasks as { task_id: string }[]) ?? [];
      items.forEach((it) => it.task_id && handleDeleteTask(it.task_id));
      void resolveProposal(proposal.id, "accepted").catch((e) => console.error("建议回写失败", e));
    } else if (proposal.kind === "inbox") {
      const items = (proposal.payload.items as InboxItem[]) ?? [];
      void applyInboxItems(items).then((ok) => {
        if (!ok) settledInFlightRef.current.delete(proposal.id);
        void resolveProposal(proposal.id, ok ? "accepted" : "rejected").catch((e) =>
          console.error("建议回写失败", e),
        );
      });
    } else if (proposal.kind === "hardtask") {
      // 拆解步骤逐条入收件箱（语义同 inbox：原任务不动，采纳只建步骤待办）
      const steps = (proposal.payload.steps as string[]) ?? [];
      void applyHardtaskSteps(steps).then((ok) => {
        if (!ok) settledInFlightRef.current.delete(proposal.id);
        void resolveProposal(proposal.id, ok ? "accepted" : "rejected").catch((e) =>
          console.error("建议回写失败", e),
        );
      });
    } else {
      settledInFlightRef.current.delete(proposal.id); // 仅回写无副作用，不占闸
      void resolveProposal(proposal.id, "accepted").catch((e) => console.error("建议回写失败", e));
    }
  };

  // 草案卡编辑后采纳（2026-10-25 问题 3 修复）：面板逐条编辑（标题/时间/耗时/
  // 优先级）的条目走 applyDraftItems 落库 + modified 回写——modified 从旧版
  // 「点击即回写的假修改」变真（统计口径不再失真）。防重闸同 handleAcceptProposal
  const handleModifyProposal = (proposal: AiProposal, items: DraftPlanItem[]) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    if (aiDraft?.proposalId === proposal.id) setAiDraft(null);
    void applyDraftItems(items).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      // appliedEntries（优化批 4）：编辑后条目随结算回传——后端比对快照产
      // modified_diff，夜间反思链学习「用户把建议改成了什么」（改时间=时段
      // 偏好、改时长=估时修正的最强信号）
      void resolveProposal(proposal.id, ok ? "modified" : "rejected", undefined,
        ok ? items : undefined).catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  // 条目卡编辑后采纳（2026-11-11 三件套统一批）：inbox 卡 / minutes 卡（溯源
  // 组装同采纳路）/ hardtask 卡（步骤纯标题）——落库通路各复用既有 apply 函数，
  // modified 回写带 appliedEntries（反思链学习「用户把建议改成了什么」）。防重
  // 闸同 handleModifyProposal
  const handleModifyInbox = (proposal: AiProposal, items: InboxItem[]) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    void applyInboxItems(items).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      void resolveProposal(proposal.id, ok ? "modified" : "rejected", undefined,
        ok ? items : undefined).catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  const handleModifyMinutes = (proposal: AiProposal, items: InboxItem[]) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    const srcItems = assembleMinutesItems(proposal, items);
    void applyInboxItems(srcItems).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      void resolveProposal(proposal.id, ok ? "modified" : "rejected", undefined,
        ok ? srcItems : undefined).catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  const handleModifySteps = (proposal: AiProposal, steps: string[]) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    const applied = steps.map((s) => ({ title: s })); // applied_entries 条目形态（title 可比）
    void applyHardtaskSteps(steps).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      void resolveProposal(proposal.id, ok ? "modified" : "rejected", undefined,
        ok ? applied : undefined).catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  // 草案卡转收件箱（2026-10-25）：承接「不排期但保留为待办」心智——条目剥时间
  // 转 InboxItem 形态入收件箱（优先级/溯源透传，source_ref 缺失时从 evidence 按
  // idx 补——同数据源页口径），整卡按 accepted 结清（建议内容有效只是不排期）
  const handleSendToInbox = (proposal: AiProposal) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    if (aiDraft?.proposalId === proposal.id) setAiDraft(null);
    const drafts = (proposal.payload.draft_plan as DraftPlanItem[]) ?? [];
    const evidence = (proposal.payload.evidence as EvidenceEntry[] | undefined) ?? [];
    const items: InboxItem[] = drafts.map((it, i) => {
      // 溯源：evidence 全键组装 InboxItem.source_ref 形态（条目自带缩形
      // source_ref 时其 source_key/fingerprint 优先）；chat 轮草案无源 → null
      const ev = evidence.find((e) => e.idx === i);
      const ref: InboxItem["source_ref"] = ev
        ? {
            source: ev.source,
            source_key: it.source_ref?.source_key || ev.source_key,
            item_id: ev.item_id,
            title: ev.title,
            time: ev.time,
            fingerprint: it.source_ref?.fingerprint || ev.fingerprint,
          }
        : null;
      return {
        title: it.title,
        ...(it.duration ? { duration: it.duration } : {}),
        ...(it.priority ? { priority: it.priority } : {}),
        ...(it.evidence ? { evidence: it.evidence } : {}),
        source_ref: ref,
      };
    });
    void applyInboxItems(items).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      void resolveProposal(proposal.id, ok ? "accepted" : "rejected").catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  // 会后纪要条目溯源组装（采纳与编辑确认两路共用，2026-11-11 提取）：
  // source_key="minutes:<会议id>"（浮层回看按此前缀匹配）+ notes 来源前缀
  const assembleMinutesItems = (proposal: AiProposal, items: InboxItem[]): InboxItem[] => {
    const mtId = typeof proposal.payload.meeting_task_id === "string"
      ? proposal.payload.meeting_task_id : null;
    const mtTitle = typeof proposal.payload.meeting_title === "string"
      ? proposal.payload.meeting_title : null;
    const originNote = mtTitle ? `来自《${mtTitle}》纪要` : "来自会议纪要";
    return items.map((it) => ({
      ...it,
      notes: it.notes ? `${it.notes}（${originNote}）` : originNote,
      source_ref: mtId
        ? {
            source: "minutes",
            source_key: `minutes:${mtId}`,
            item_id: mtId,
            title: mtTitle ?? "会议",
            time: null,
            fingerprint: "", // 无指纹语义（纪要非采集源，无防重诉求），空串落库时被 falsy 跳过
          }
        : null,
    }));
  };

  // 会后纪要采纳（2026-11-10）：全量条目入收件箱（applyInboxItems 同款通路，
  // 溯源组装 assembleMinutesItems）；零条目（零行动项卡）= 纯回写 accepted。
  // 2026-11-11 三件套对齐：卡面勾选与「采纳并安排」直排退役——选择性取舍走
  // 卡编辑态逐条删除，直排走收件箱单卡智能安排。防重闸同上
  const handleAcceptMinutes = (proposal: AiProposal, items: InboxItem[]) => {
    if (settledInFlightRef.current.has(proposal.id)) return;
    settledInFlightRef.current.add(proposal.id);
    if (items.length === 0) {
      settledInFlightRef.current.delete(proposal.id); // 无副作用不占闸（回写幂等）
      void resolveProposal(proposal.id, "accepted").catch((e) =>
        console.error("建议回写失败", e),
      );
      return;
    }
    void applyInboxItems(assembleMinutesItems(proposal, items)).then((ok) => {
      if (!ok) settledInFlightRef.current.delete(proposal.id);
      void resolveProposal(proposal.id, ok ? "accepted" : "rejected").catch((e) =>
        console.error("建议回写失败", e),
      );
    });
  };

  // 数据源页单条结算（2026-10-24）：直接确认（accepted）与编辑后确认（modified）
  // 共一条通路——先落库该条（既有 apply 通路，溯源三键/优先级随条目透传），成功后
  // 调 settle-items 收缩卡（失败仅日志：条目已入库，返回仍 true 防用户重试造成
  // 重复落库——外部重拉若复活该条目属低概率窗口，登记 tech-debt）。防重闸键 =
  // `${id}#${idx}` 条目级（组头批量与单点并发安全）
  const handleSettleItem = async (
    proposal: AiProposal,
    index: number,
    item: InboxItem | DraftPlanItem,
    outcome: "accepted" | "modified",
  ) => {
    const gate = `${proposal.id}#${index}`;
    if (settledInFlightRef.current.has(gate)) return false;
    settledInFlightRef.current.add(gate);
    // 路由按条目形态而非 proposal.kind（2026-10-24 直排批）：inbox 条目手选了
    // 排期时间时面板组装 DraftPlanItem 形态（start 非空）→ 走 applyDraftItems
    // 直接带 start 建任务进日历；未选时间 → applyInboxItems 入收件箱
    const ok =
      proposal.kind === "schedule" || (item as DraftPlanItem).start
        ? await applyDraftItems([item as DraftPlanItem])
        : await applyInboxItems([item as InboxItem]);
    if (!ok) {
      settledInFlightRef.current.delete(gate);
      return false;
    }
    try {
      // appliedEntries（优化批 4）：条目级编辑后确认同享 modified 回流
      await settleProposalItems(proposal.id, [index], outcome,
        outcome === "modified" ? [item] : undefined);
    } catch (e) {
      console.error("条目结算收缩失败（条目已落库）", e);
    }
    return true;
  };

  // 数据源条目智能排期（2026-10-26 收件箱 UI 对齐批）：有日期（schedule 条目 /
  // inbox 手选时间）与确认同路径直落日历；无日期 = 先建任务入收件箱（溯源三键 /
  // 优先级 / 备注与重复规则预设随条目透传）再调统一引擎 auto-schedule——引擎无
  // 空档（422）或异常时任务留在收件箱不丢，条目照常结清不复活，面板按 scheduled
  // 区分提示。防重闸键与 handleSettleItem 同口径（条目级 `${id}#${idx}`）
  const handleSmartScheduleItem = async (
    proposal: AiProposal,
    index: number,
    item: InboxItem | DraftPlanItem,
  ): Promise<{ ok: boolean; scheduled: boolean }> => {
    const gate = `${proposal.id}#${index}`;
    if (settledInFlightRef.current.has(gate)) return { ok: false, scheduled: false };
    settledInFlightRef.current.add(gate);
    // 有日期：直落日历（applyDraftItems + settle，镜像 handleSettleItem 的
    // schedule 分支——不改 outcome 语义，智能排期对已排期条目按原时间落库）
    if (proposal.kind === "schedule" || (item as DraftPlanItem).start) {
      const ok = await applyDraftItems([item as DraftPlanItem]);
      if (!ok) {
        settledInFlightRef.current.delete(gate);
        return { ok: false, scheduled: false };
      }
      try {
        await settleProposalItems(proposal.id, [index], "accepted");
      } catch (e) {
        console.error("条目结算收缩失败（条目已落库）", e);
      }
      return { ok: true, scheduled: true };
    }
    // 无日期：建任务 → 引擎排期（失败留收件箱）
    try {
      const it = item as InboxItem;
      const created = await createTask({
        title: it.title,
        ...(it.duration ? { duration_mins: it.duration } : {}),
        ...(it.notes ? { notes: it.notes } : {}),
        ...(it.priority ? { priority: it.priority } : {}),
        ...(it.recurrence
          ? { recurrence_rule: toRrule(it.recurrence) ?? undefined }
          : {}),
        ...(it.source_ref?.source_key ? { source_key: it.source_ref.source_key } : {}),
        ...(it.source_ref?.fingerprint
          ? { source_fingerprint: it.source_ref.fingerprint }
          : {}),
        ...(it.evidence ? { source_evidence: it.evidence } : {}),
      });
      const base = toFrontendTask(created, projectNameById);
      upsertTasks([base]);
      notifyPeer("main", { kind: "upsert", tasks: [base] });
      let scheduled = false;
      try {
        const updated = await autoScheduleTask(created.id);
        const frontend = toFrontendTask(updated, projectNameById);
        upsertTasks([frontend]);
        notifyPeer("main", { kind: "upsert", tasks: [frontend] });
        scheduled = true;
      } catch (e) {
        // 引擎无空档/异常：任务已留收件箱（不丢），返回降级口径由面板提示
        console.error("智能排期失败（任务已留收件箱）", e);
      }
      try {
        await settleProposalItems(proposal.id, [index], "accepted");
      } catch (e) {
        console.error("条目结算收缩失败（条目已落库）", e);
      }
      return { ok: true, scheduled };
    } catch (e) {
      console.error("智能排望建任务失败", e);
      settledInFlightRef.current.delete(gate);
      return { ok: false, scheduled: false };
    }
  };

  // 数据源条目拖入日历直排（2026-10-24 需求 2）：落点即排期时间——建任务（溯源
  // 三键/优先级随 suggest 载荷透传，无耗时只有开始时刻一小时卡）+ upsert 画布即
  // 现 + settle-items 结算一步完成，不经收件箱中转。结算 await 完成后才递增
  // aiRefreshKey 触发面板/徽标重拉——顺序执行避开「重拉早于 outcome 回写卡复活」
  // 的既有竞态（结算路径不触发重拉的纪律不破：这里的重拉在写库之后）
  const handleSuggestDrop = async (
    todo: DraggedTodoWithSuggest,
    dateOffset: number,
    start: number,
  ) => {
    const s = todo.suggest;
    if (!s) return;
    const end = s.duration ? start + s.duration / 60 : undefined;
    try {
      const created = await createTask({
        title: todo.title,
        start_time: scheduleToISO(dateOffset, start),
        ...(end !== undefined ? { end_time: scheduleToISO(dateOffset, end) } : {}),
        ...(s.priority ? { priority: s.priority } : {}),
        ...(s.notes ? { notes: s.notes } : {}),
        ...(s.source?.source_key ? { source_key: s.source.source_key } : {}),
        ...(s.source?.fingerprint ? { source_fingerprint: s.source.fingerprint } : {}),
      });
      const frontend = toFrontendTask(created, projectNameById);
      upsertTasks([frontend]);
      notifyPeer("main", { kind: "upsert", tasks: [frontend] });
    } catch (e) {
      console.error("数据源条目拖拽落库失败", e);
      setAiRefreshKey((k) => k + 1); // 建库失败：条目仍在面板，重拉恢复可重试
      return;
    }
    try {
      await settleProposalItems(s.proposalId, [s.index], "accepted");
    } catch (e) {
      console.error("条目结算收缩失败（条目已落库）", e);
    }
    setAiRefreshKey((k) => k + 1);
  };

  // 数据源页徽标（2026-10-24 改条目数口径——一卡多条拆条渲染后卡数与页面视觉
  // 对不上）：挂载初拉 + 工作流事件重拉 + 切页重拉（activeView——面板自拉只在
  // 打开时刷新面板 state，徽标若不随切页重拉会出现「页里 6 条徽标 0」的错位）；
  // 无来源条目不计（entryHasSource，与面板分组过滤同口径）；面板结算走乐观减
  // （onItemsSettled）不经此通道。失败静默——徽标非关键路径
  useEffect(() => {
    fetchAiProposals(true)
      .then((rows) =>
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

  // 批量排期弹窗确认（2026-10-25 功能边界批）：勾选条目经 applyDraftItems
  //（task_id 命中即改期）落库后结清建议行；防重闸同 handleAcceptProposal——
  // 部分勾选也整卡 accepted（未勾任务不丢，留在收件箱未排期，可再触发批量）
  const handleBulkConfirm = async (items: DraftPlanItem[]) => {
    if (!bulkDraft || settledInFlightRef.current.has(bulkDraft.proposalId)) return;
    const id = bulkDraft.proposalId;
    settledInFlightRef.current.add(id);
    setBulkSubmitting(true);
    const ok = await applyDraftItems(items);
    if (!ok) settledInFlightRef.current.delete(id);
    void resolveProposal(id, ok ? "accepted" : "rejected").catch((e) =>
      console.error("批量建议回写失败", e),
    );
    setBulkSubmitting(false);
    setBulkDraft(null);
  };

  // 批量排期弹窗取消：建议行按 rejected 结清（采纳率统计信号），任务不动
  const handleBulkCancel = () => {
    if (bulkDraft) {
      void resolveProposal(bulkDraft.proposalId, "rejected").catch((e) =>
        console.error("批量建议回写失败", e),
      );
    }
    setBulkDraft(null);
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
      updateProject(project.id, { name: newName }).catch((e) =>
        console.error("项目重命名写回后端失败", e),
      );
    }
    setProjects((prev) =>
      prev.map((p) => (p.name === oldName ? { ...p, name: newName } : p)),
    );
    setTasks((prev) =>
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
      deleteProject(project.id).catch((e) => console.error("项目删除写回后端失败", e));
    }
    setProjects((prev) => prev.filter((p) => p.name !== name));
    setTasks((prev) =>
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
      updateProject(project.id, { starred: !project.starred }).catch((e) =>
        console.error("项目星标写回后端失败", e),
      );
    }
    setProjects((prev) =>
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
        // 仅撤幽灵方块（内存态）；建议行回写由面板 settleProposal 负责
        onDiscardDraft={() => setAiDraft(null)}
        onDraft={handleDraftEvent}
        onAccept={handleAcceptProposal}
        onModify={handleModifyProposal}
        onModifyInbox={handleModifyInbox}
        onModifyMinutes={handleModifyMinutes}
        onModifySteps={handleModifySteps}
        onSendToInbox={handleSendToInbox}
        onAcceptMinutes={handleAcceptMinutes}
        onApplied={handleAppliedEvent}
        // 采集轮收尾（ingest_done 帧）：卡在数据源页对话框零卡片，递增钥匙刷
        // 数据源徽标/面板——chat 轮出卡不递增的既有纪律只对对话框内可见卡成立
        onIngestDone={() => setAiRefreshKey((k) => k + 1)}
        // 批量排期草案（bulk 帧）→ 开弹窗清单确认
        onBulk={(items, proposalId) => setBulkDraft({ items, proposalId })}
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
            />
          ) : activeView === "datasource" ? (
            <DataSourcePanel
              style={{ width: rightWidth, flexShrink: 0 }}
              onCollapse={() => setRightCollapsed(true)}
              onSettleItem={handleSettleItem}
              onSmartSchedule={handleSmartScheduleItem}
              // 乐观减徽标（2026-10-24 竞态根修）：结算不再触发 aiRefreshKey 重拉——
              // 重拉与后端回写竞态曾致「点两次才消失」；权威对账留给外部事件/切页
              onItemsSettled={(count) => setDsPendingCount((c) => Math.max(0, c - count))}
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
            onOpenInbox={() => {
              setActiveView("inbox");
              setActiveProject(null);
            }}
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
          onCancel={handleBulkCancel}
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

      {/* 日程提醒页内兜底（纯浏览器 dev 无独立悬浮窗时呈现，inline 居中偏上；
           Tauri 下悬浮框由独立置顶窗口承载，此队列恒空不渲染）。操作按钮走
           直接写库 + 整体重拉（此形态无 widget/提醒窗对端，notifyPeer 无意义） */}
      <ReminderToast
        inline
        items={reminderToasts}
        onClose={(key) => beginToastExit(key, setReminderToasts)}
        onAction={(it, action) => {
          if (!it.taskId) return;
          const { body, opts } = toastActionPatch(it, action);
          updateTask(it.taskId, body, opts)
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
