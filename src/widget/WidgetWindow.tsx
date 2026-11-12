// 桌面小部件窗口根组件：双 tab（收件箱 / 今日日程）的轻量独立窗口。
// 不跑完整 App——自带最小数据面（四请求并行拉取）+ 双窗同步（lib/sync.ts），
// 渲染层 100% 复用主界面组件与类名：QuickAdd（输入文法逐项一致）、
// ScheduleCanvas 的节拍/泳道/刻度/负载指示器、InboxPanel 的待办卡盒子。
// 写操作 = 快捷录入建任务 / 卡片拖动拉伸调排期 / 待办编辑与删除（复刻 InboxPanel
// 编辑区 + 双击标题）/ 今日浮层三问编辑（复刻 ScheduleCanvas 详情浮层），均乐观更新
// + emit 同步回主界面；主界面写操作经 onPeerSync 推来（upsert/delete/settings/reload）。
// 窗口位置与尺寸自管：localStorage 记忆逻辑坐标与宽高（边缘可拖拽调整大小），
// 无记忆默认工作区右上角 360×540（玻璃 = 纯透明窗口直透壁纸 + CSS tint，
// Rust 仅设 DWM 圆角，见 lib.rs setup）
import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  CalendarDays,
  ChevronDown,
  Clock,
  Home,
  Inbox,
  Pin,
  PinOff,
  Repeat,
  Trash2,
  X,
} from "lucide-react";
import { getCurrentWebviewWindow, WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { availableMonitors, currentMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import {
  dateKeyOfOffset,
  durationText,
  isScheduled,
  NOTE_MAX_LEN,
  occursOn,
  PRIORITY_COLORS,
  PROJECT_COLORS,
  RECURRENCE_FREQ_OPTIONS,
  RECURRENCE_WEEKDAY_LABELS,
  recurrenceText,
  setRecurrenceFreq,
  shiftDateKey,
  TITLE_MAX_LEN,
  toggleWeekday,
  type Project,
  type ScheduledTask,
  type Task,
} from "../data/tasks";
import {
  createProject,
  createTask,
  deleteTask,
  fetchProjects,
  fetchSettings,
  fetchTasks,
  scheduleToISO,
  toFrontendProject,
  toFrontendSettings,
  toFrontendTask,
  toTaskPatch,
  updateTask,
  type TaskCreateBody,
} from "../lib/api";
import { toRrule } from "../lib/rrule";
import { applyTheme, DEFAULT_SETTINGS, type Settings } from "../lib/settings";
import { notifyPeer, onPeerSync } from "../lib/sync";
import type { ParsedInput } from "../lib/syntax";
import { fetchWidgetVisible, hideWidgetPersisted, isTauri } from "../lib/tauri";
import QuickAdd from "../components/QuickAdd";
import ConfirmPopover from "../components/ConfirmPopover";
import {
  assignLanes,
  fmtLoadHours,
  fmtTime,
  HOUR_HEIGHT,
  laneLeft,
  laneRight,
  renderHourMarks,
} from "../components/ScheduleCanvas";

// 窗口尺寸（逻辑像素，与 tauri.conf.json 的 widget 声明同步——360×540 为无记忆
// 兜底）；尺寸下限与 conf 的 minWidth/minHeight 同值（恢复记忆时 clamp 用，改一
// 处须同步另一处）；默认位置贴工作区右上角的边距
const WINDOW_W = 360;
const WINDOW_H = 540;
const MIN_W = 300;
const MIN_H = 420;
const EDGE_MARGIN = 16;
// 位置/尺寸记忆键（localStorage 逻辑坐标——主界面布局走 Tauri layout.json，但
// widget 窗口几何属轻量偏好且需 JS 高频读写防抖，localStorage 更简）
const POS_KEY = "clara:widget-pos";
// 置顶记忆键（纯 JS 读写域：唯一写入口 toggleOnTop、唯一读入口挂载恢复——与显隐
// 不同，Rust 托盘/拦截器不碰置顶，无需进 Tauri 侧配置）
const PIN_KEY = "clara:widget-pinned";

// 置顶记忆读取（坏数据容错返 false；"1"=置顶，其余一律不置顶）
const readSavedPinned = (): boolean => {
  try {
    return localStorage.getItem(PIN_KEY) === "1";
  } catch {
    return false;
  }
};

// 记忆窗口矩形读取（坏数据容错返 null；旧格式只有 {x,y} → 尺寸取默认值）
const readSavedRect = (): { x: number; y: number; w: number; h: number } | null => {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as {
      x?: unknown;
      y?: unknown;
      w?: unknown;
      h?: unknown;
    };
    if (typeof v.x !== "number" || typeof v.y !== "number") return null;
    return {
      x: v.x,
      y: v.y,
      w: typeof v.w === "number" ? v.w : WINDOW_W,
      h: typeof v.h === "number" ? v.h : WINDOW_H,
    };
  } catch {
    return null;
  }
};

// 边缘调整大小（8 向）：页内热区 div 接住 pointerdown 后调 startResizeDragging，
// 交给系统 resize loop（与 header 拖动的 startDragging 同族官方 API）。不用无边框
// 窗原生边缘热区——WebView2 是子窗口会吞鼠标命中测试，tao 宿主收不到；尺寸下限
// 由 conf 的 minWidth/minHeight 声明、系统拖动时自动 clamp。ResizeDirection 未从
// @tauri-apps/api 导出，本地定义同构字符串字面量联合（结构兼容可直接传入）
type ResizeDir =
  | "North"
  | "South"
  | "East"
  | "West"
  | "NorthEast"
  | "NorthWest"
  | "SouthEast"
  | "SouthWest";

const RESIZE_EDGES: { cls: string; dir: ResizeDir }[] = [
  { cls: "n", dir: "North" },
  { cls: "s", dir: "South" },
  { cls: "e", dir: "East" },
  { cls: "w", dir: "West" },
  { cls: "ne", dir: "NorthEast" },
  { cls: "nw", dir: "NorthWest" },
  { cls: "se", dir: "SouthEast" },
  { cls: "sw", dir: "SouthWest" },
];

// 优先级排序权重（高 → 低，同优先级稳定保持原序）——口径同 InboxPanel 私有常量
const PRIORITY_ORDER: Record<Task["priority"], number> = {
  high: 0,
  medium: 1,
  low: 2,
};

// 耗时快捷档（编辑区胶囊）——口径同 InboxPanel.DURATION_PRESETS（点击同写 minutes + duration）
const DURATION_PRESETS = [
  { minutes: 30, label: "30m", duration: durationText(30) },
  { minutes: 60, label: "1h", duration: durationText(60) },
  { minutes: 120, label: "2h", duration: durationText(120) },
] as const;

// 详情浮层定位宽（与 .schedule-detail 的 CSS width:300px 同值，clamp 计算用）
const DETAIL_W = 300;

// 星期名（浮层时间行"今天 周X"用；同 ScheduleCanvas 私有常量口径，getDay() 0=周日）
const WEEKDAY_NAMES = ["日", "一", "二", "三", "四", "五", "六"];

type WidgetTab = "inbox" | "today";

function WidgetWindow() {
  const [tab, setTab] = useState<WidgetTab>("inbox");
  // 置顶态（初值 = localStorage 记忆，坏数据/无记忆回不置顶——与 conf 的
  // alwaysOnTop:false 一致；恢复见挂载 effect，写入口仅 toggleOnTop）
  const [onTop, setOnTop] = useState(readSavedPinned);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  // 当前时刻（红线/降淡/今日窗口，分钟精度）；widget 恒"今天"无翻页
  const [now, setNow] = useState(() => new Date());
  // reload 计数：主界面发 reload 载荷（Clone & Override 删除/项目重命名等派生变化）
  // 时 +1 触发数据 effect 重拉
  const [reloadTick, setReloadTick] = useState(0);

  // —— 收件箱待办编辑（复刻 InboxPanel）：编辑区单开 + 标题内联编辑单开 ——
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  // —— 今日日程详情浮层（复刻 ScheduleCanvas）：锚点 = 任务 id + fixed 定位坐标 ——
  const [detail, setDetail] = useState<{ id: string; left: number; top: number } | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  // —— 编辑草稿（收件箱与浮层共用一份：进入路径各自显式装载，两处互不同时活跃）——
  const [noteDraft, setNoteDraft] = useState("");
  const [titleDraft, setTitleDraft] = useState("");
  // 重复规则面板展开态（换目标/收起/打开浮层时复位）
  const [recurrenceOpen, setRecurrenceOpen] = useState(false);
  // 重复系列待应用编辑三问缓冲（浮层内改备注/标题/优先级先聚合，选范围才提交）
  const [pendingEdit, setPendingEdit] = useState<Partial<Task> | null>(null);
  // Enter/Esc 主动退出标题编辑后，吞掉 input 卸载可能触发的 blur，防二次提交
  const skipTitleBlur = useRef(false);
  // 浮层根节点：document 点外关闭时判断落点是否在浮层内
  const detailRef = useRef<HTMLDivElement>(null);

  // 累积式 upsert：Map 按 id 合并保序（口径同 App，收件箱默认序 = 后端 sort_order 序）
  const upsertTasks = (incoming: Task[]) =>
    setTasks((prev) => {
      if (incoming.length === 0) return prev;
      const byId = new Map(prev.map((t) => [t.id, t]));
      for (const t of incoming) byId.set(t.id, t);
      return [...byId.values()];
    });
  const upsertProjects = (incoming: Project[]) =>
    setProjects((prev) => {
      if (incoming.length === 0) return prev;
      const byId = new Map(prev.map((p) => [p.id ?? p.name, p]));
      for (const p of incoming) byId.set(p.id ?? p.name, p);
      return [...byId.values()];
    });

  // 项目 id → name 映射（建任务时 toFrontendTask 转换用）
  const projectNameById = useMemo(
    () => new Map(projects.flatMap((p) => (p.id ? [[p.id, p.name] as const] : []))),
    [projects],
  );

  // —— 数据拉取（启动 + reload 重跑）：四请求并行——项目 + 未排期待办全量 + 今日
  // 最小有界窗口（今天 0 点 ~ 明天 0 点；重复系列恒全量随窗口返回）+ 设置。
  // StrictMode 双执行 cancelled 标记丢弃首轮；失败保持现状（本地回环毫秒级，
  // 无 loading UI）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const n = new Date();
        const dayStart = new Date(n.getFullYear(), n.getMonth(), n.getDate());
        const dayEnd = new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1);
        const [apiProjects, apiUnscheduled, apiToday, apiSettings] =
          await Promise.all([
            fetchProjects(),
            fetchTasks({ unscheduled: true }),
            fetchTasks({ start: dayStart, end: dayEnd }),
            fetchSettings(),
          ]);
        if (cancelled) return;
        setProjects(apiProjects.map(toFrontendProject));
        setSettings(toFrontendSettings(apiSettings));
        const nameById = new Map(apiProjects.map((p) => [p.id, p.name]));
        upsertTasks(
          [...apiUnscheduled, ...apiToday].map((t) => toFrontendTask(t, nameById)),
        );
      } catch (e) {
        console.error("小部件加载数据失败", e);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadTick]);

  // 主题跟随（口径同 App：system 模式常挂 matchMedia 监听）；settings 载荷到达即变
  useEffect(() => {
    applyTheme(settings.themeMode);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme(settings.themeMode);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [settings.themeMode]);

  // 小部件透明度：把设置注入 CSS 变量（0..1 小数），tokens.css 的 --glass-widget 经
  // calc 按 t 算 tint alpha——亮暗两态自动随 data-theme 切换，JS 零主题感知；
  // 主窗口不注入（无 widget-shell 消费者，fallback 0.2 = 默认观感）
  useEffect(() => {
    document.documentElement.style.setProperty(
      "--widget-transparency",
      String(settings.widgetTransparency / 100),
    );
  }, [settings.widgetTransparency]);

  // now tick：60s 自更新 + 窗口从隐藏转可见时立即校正（WebView2 隐藏态计时器节流，
  // 红线/降淡会滞后一个 tick——emitTo 对隐藏窗口照常投递，数据不滞后）
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") setNow(new Date());
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // —— 双窗同步监听：widget 只发 upsert（建任务/建项目/改排期），但主界面会推来
  // 全部四类载荷——upsert（乐观前端形状零转换入列）/ delete（按 id 移除）/
  // settings（设置与主题跟随）/ reload（派生变化整窗重拉）。接收纯读不回发，
  // 天然无回环；非 Tauri 下 onPeerSync 返回空 unlisten，纯浏览器 dev 恒 no-op
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
      } else if (payload.kind === "settings") {
        setSettings(payload.settings);
      } else {
        setReloadTick((t) => t + 1);
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

  // —— 窗口几何（挂载一次，非 Tauri 跳过）：localStorage 记忆位置与尺寸（物理 ÷
  // scaleFactor 的逻辑值），恢复前校验窗口矩形与任一现存显示器相交（拔显示器回
  // 默认）；无记忆/越界 = 当前显示器工作区右上角 + 默认尺寸（范本
  // ReminderWindow.positionWindow 的换算）。onMoved/onResized 共用 300ms 防抖写回
  // （拖动中只重排计时器，松手一次落盘——事件哪个后到无所谓，落盘读的是窗口现值）
  useEffect(() => {
    if (!isTauri()) return;
    const win = getCurrentWebviewWindow();
    let unlistenMoved: (() => void) | null = null;
    let unlistenResized: (() => void) | null = null;
    let timer: number | undefined;
    let disposed = false;
    (async () => {
      try {
        const mon = await currentMonitor();
        if (!mon) return;
        // 各显示器逻辑矩形（各自按自己的 scaleFactor 换算，混合 DPI 下近似够用）
        const rects = (await availableMonitors()).map((m) => ({
          x: m.position.x / m.scaleFactor,
          y: m.position.y / m.scaleFactor,
          w: m.size.width / m.scaleFactor,
          h: m.size.height / m.scaleFactor,
        }));
        const saved = readSavedRect();
        const fits =
          saved !== null &&
          rects.some(
            (r) =>
              saved.x < r.x + r.w &&
              saved.x + saved.w > r.x &&
              saved.y < r.y + r.h &&
              saved.y + saved.h > r.y,
          );
        const sf = mon.scaleFactor;
        const wx = mon.workArea.position.x / sf;
        const wy = mon.workArea.position.y / sf;
        const ww = mon.workArea.size.width / sf;
        const wh = mon.workArea.size.height / sf;
        const pos = fits
          ? saved
          : {
              x: Math.round(wx + ww - WINDOW_W - EDGE_MARGIN),
              y: Math.round(wy + EDGE_MARGIN),
            };
        // 恢复尺寸 clamp 进 [下限, 当前显示器工作区]（记忆超出已拔显示器时收边）
        const size = fits
          ? {
              w: Math.min(Math.max(saved.w, MIN_W), Math.round(ww)),
              h: Math.min(Math.max(saved.h, MIN_H), Math.round(wh)),
            }
          : { w: WINDOW_W, h: WINDOW_H };
        await win.setPosition(new LogicalPosition(pos.x, pos.y));
        await win.setSize(new LogicalSize(size.w, size.h));
        const save = () => {
          window.clearTimeout(timer);
          timer = window.setTimeout(() => {
            void (async () => {
              try {
                const [p, sz, s] = await Promise.all([
                  win.outerPosition(),
                  win.outerSize(),
                  win.scaleFactor(),
                ]);
                localStorage.setItem(
                  POS_KEY,
                  JSON.stringify({
                    x: Math.round(p.x / s),
                    y: Math.round(p.y / s),
                    w: Math.round(sz.width / s),
                    h: Math.round(sz.height / s),
                  }),
                );
              } catch {
                // 读窗口几何失败静默跳过本次落盘
              }
            })();
          }, 300);
        };
        unlistenMoved = await win.onMoved(save);
        unlistenResized = await win.onResized(save);
        if (disposed) {
          unlistenMoved();
          unlistenResized();
        }
      } catch {
        // 定位失败静默（conf 的默认位置兜底）
      } finally {
        // —— 显隐恢复：几何就绪后才 show（防 conf 默认位置闪现跳动）；finally
        // 覆盖 return（monitor null 放弃）/ catch（定位失败停兜底位）路径——
        // 配置说显示就显示；StrictMode 双跑 show 幂等无害 ——
        try {
          if (await fetchWidgetVisible()) await win.show();
        } catch {
          // show 失败静默（托盘"桌面小部件"仍可唤出）
        }
      }
    })();
    return () => {
      disposed = true;
      unlistenMoved?.();
      unlistenResized?.();
      window.clearTimeout(timer);
    };
  }, []);

  // 置顶记忆恢复（挂载一次；与几何恢复无交互，独立 effect 保 monitor null 放弃
  // 路径也能恢复——闭包捕获的 onTop 即 lazy 初值 readSavedPinned 的结果）
  useEffect(() => {
    if (!isTauri() || !onTop) return;
    void getCurrentWebviewWindow().setAlwaysOnTop(true).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // —— 派生（全部仿 ScheduleCanvas 日视图，查看日恒今天）——
  const START = settings.dayStart;
  const END = settings.dayEnd;
  const WORK_HOURS = settings.workHours;
  const hours = Array.from(
    { length: Math.floor(END) - Math.ceil(START) + 1 },
    (_, i) => Math.ceil(START) + i,
  );
  const y = (hour: number) => (hour - START) * HOUR_HEIGHT;
  const axisHeight = (END - START) * HOUR_HEIGHT;
  const nowTop =
    (now.getHours() + now.getMinutes() / 60 - START) * HOUR_HEIGHT;
  const nowLabel = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const isPastEvent = (endHour: number) =>
    todayStart + endHour * 3_600_000 <= now.getTime();

  // 今日实例（重复任务在匹配日生成 dateOffset=0 副本，同主界面展开语义；
  // lanes 与渲染共用同一次计算——assignLanes 以对象引用为 key）。
  // 全天/跨天行不进小部件时间轴（2026-11-06 批 B：精简视图一期不同步全天条，
  // 防止 0 点全天卡被 clamp 成整轴大块；主窗口已完整呈现）
  const todayEvents: ScheduledTask[] = [];
  for (const t of tasks) {
    if (!isScheduled(t) || t.allDay || !occursOn(t, 0)) continue;
    todayEvents.push(t.dateOffset === 0 ? t : { ...t, dateOffset: 0 });
  }
  const lanes = assignLanes(todayEvents);

  // 负载统计（口径同主界面日视图指示器：耗时按 end ?? start+1，分母 = max(总耗时, 工时)）
  const prioHours = { high: 0, medium: 0, low: 0 } as Record<
    Task["priority"],
    number
  >;
  for (const e of todayEvents)
    prioHours[e.priority] += (e.end ?? e.start + 1) - e.start;
  const loadTotal = prioHours.high + prioHours.medium + prioHours.low;
  const loadRate = loadTotal / WORK_HOURS;
  const loadDenom = Math.max(loadTotal, WORK_HOURS);

  // 收件箱列表（口径 = 主界面收件箱右栏默认列表：未排期且未归属项目、未完成，优先级排序）
  const inboxList = tasks
    .filter((t) => !isScheduled(t) && !t.project && !t.done)
    .sort((a, b) => PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority]);

  // —— 写 handler ——
  // 写请求失败自愈：乐观更新已生效（错值可能已推给主界面），本窗 reloadTick 重拉 +
  // 通知主界面 reload，把两窗都拉回后端真值——不回滚本地（重拉语义等价且能顺带纠偏对端）
  const recoverFromWriteError = (label: string, e: unknown) => {
    console.error(label, e);
    setReloadTick((t) => t + 1);
    notifyPeer("widget", { kind: "reload" });
  };

  // 快捷录入建任务（复刻 App.handleCreateTask）：未知 # 项目顺手创建并归属，新行
  // id 由后端生成须 await 再 upsert；失败向上抛由 QuickAdd 红边提示保留输入
  const handleCreateTask = async (draft: ParsedInput) => {
    let projectId: string | undefined;
    let nameById = projectNameById;
    if (draft.newProject) {
      const color =
        PROJECT_COLORS[Math.floor(Math.random() * PROJECT_COLORS.length)];
      const created = await createProject(draft.newProject, color);
      const frontendProject = toFrontendProject(created);
      setProjects((prev) =>
        prev.some((p) => p.name === frontendProject.name)
          ? prev
          : [...prev, frontendProject],
      );
      notifyPeer("widget", { kind: "upsert", projects: [frontendProject] });
      projectId = created.id;
      nameById = new Map(projectNameById);
      nameById.set(created.id, created.name);
    } else if (draft.project) {
      projectId = projects.find((p) => p.name === draft.project)?.id;
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
      ...(draft.recurrence
        ? { recurrence_rule: toRrule(draft.recurrence) ?? undefined }
        : {}),
      ...(projectId ? { project_id: projectId } : {}),
    };
    const created = await createTask(body);
    const frontend = toFrontendTask(created, nameById);
    upsertTasks([frontend]);
    notifyPeer("widget", { kind: "upsert", tasks: [frontend] });
  };

  // 排期写回（拖动/拉伸松手）：纯调时间保原基准 dateOffset（重复系列基准日多在
  // 过去，写实例日 0 会丢早期实例——主界面 dayShift=0 分支同款）；end undefined ⟺
  // 原无 end 不发 end_time 键；end 齐备时本地重算 minutes/duration（与后端
  // duration 派生不变式同口径）
  const handleScheduleChange = (
    taskId: string,
    start: number,
    end: number | undefined,
  ) => {
    const src = tasks.find((t) => t.id === taskId);
    if (!src || !isScheduled(src)) return;
    const minutes = end !== undefined ? Math.round((end - start) * 60) : undefined;
    const apply = (t: Task): Task => ({
      ...t,
      start,
      end,
      ...(minutes !== undefined
        ? { minutes, duration: durationText(minutes) }
        : {}),
    });
    setTasks((list) => list.map((t) => (t.id === taskId ? apply(t) : t)));
    notifyPeer("widget", { kind: "upsert", tasks: [apply(src)] });
    const body = {
      start_time: scheduleToISO(src.dateOffset, start),
      ...(end !== undefined
        ? { end_time: scheduleToISO(src.dateOffset, end) }
        : {}),
    };
    updateTask(taskId, body).catch((e) => console.error("排期写回后端失败", e));
  };

  // 待办字段编辑（标题/备注/耗时/优先级/重复）——复刻 App.handleUpdateTask：patch
  // 乐观合并 + toTaskPatch 翻译写回；重复系列可带 opts 走 Clone & Override 分流
  //（浮层三问）：this = 乐观挖空该日虚拟实例、following = 乐观给老系列加 until 截断，
  // 两分支新行 id 由后端生成须等响应再 upsert；all/无 opts = 现状乐观合并同步主界面
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
        // 显式清空重复规则时连带清挖空日期——否则已排期任务会被 occursOn 挖空在
        // 唯一实例日（全隐身），口径同 App
        if ("recurrence" in patch && patch.recurrence === undefined) {
          return { ...t, ...patch, cancelledDates: undefined };
        }
        return { ...t, ...patch };
      }),
    );
    if (opts?.mode === "this" || opts?.mode === "following") {
      // 派生变化（挖空/截断 + 新行）让主界面整窗重拉最简，取舍同 App
      notifyPeer("widget", { kind: "reload" });
      updateTask(taskId, toTaskPatch(patch), opts)
        .then((created) => upsertTasks([toFrontendTask(created, projectNameById)]))
        .catch((e) => recoverFromWriteError("任务编辑写回后端失败", e));
      return;
    }
    // all/无 opts：乐观合并后的整任务同步给主界面（清空重复规则连带清挖空，口径同上）
    const src = tasks.find((t) => t.id === taskId);
    if (src) {
      const merged: Task =
        "recurrence" in patch && patch.recurrence === undefined
          ? { ...src, ...patch, cancelledDates: undefined }
          : { ...src, ...patch };
      notifyPeer("widget", { kind: "upsert", tasks: [merged] });
    }
    updateTask(taskId, toTaskPatch(patch)).catch((e) =>
      recoverFromWriteError("任务编辑写回后端失败", e),
    );
  };

  // 删除任务三分支——复刻 App.handleDeleteTask：this = 乐观追加 cancelledDates（系列
  // 行保留）、following = 乐观给 recurrence 加 until（被删日前一天）；无 opts/all =
  // 整行移除。整删按 id 通知主界面移除，this/following 的派生变化交主界面整体重拉
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
    notifyPeer(
      "widget",
      keepRow ? { kind: "reload" } : { kind: "delete", taskIds: [taskId] },
    );
    deleteTask(taskId, opts).catch((e) => recoverFromWriteError("任务删除写回后端失败", e));
  };

  // —— 收件箱待办编辑（复刻 InboxPanel 编辑体系，写回走上方两个 handler）——
  // 备注提交：trim 空归 undefined 抹字段，仅变更才写回（幂等）。收起路径必须显式调用——
  // 收起即卸载 textarea，聚焦元素被移除时 Chromium 不触发 blur，onBlur 提交永不执行
  const finishNote = (todo: Task | undefined) => {
    if (!todo) return;
    const note = noteDraft.trim() || undefined;
    if (note !== todo.note) handleUpdateTask(todo.id, { note });
  };

  // 编辑区开关：同卡收起（先显式落盘）；换目标先落盘旧卡草稿（键盘 Tab+Enter 路径只
  // 产生 click 不触发外点监听，直接装载新卡草稿会静默覆盖旧卡输入）再装载新卡备注
  const toggleExpand = (todo: Task) => {
    if (expandedId === todo.id) {
      finishNote(todo);
      setExpandedId(null);
      setRecurrenceOpen(false);
      return;
    }
    if (expandedId !== null) finishNote(tasks.find((t) => t.id === expandedId));
    setNoteDraft(todo.note ?? "");
    setExpandedId(todo.id);
    setRecurrenceOpen(false);
  };

  // 展开态点卡片外任意处自动收起（closest 守卫卡内交互不触发；依赖含 noteDraft 保
  // 闭包随草稿重建，提交读到的是最新值）
  useEffect(() => {
    if (expandedId === null) return;
    const onPointerDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest(".inbox-todo.expanded")) return;
      finishNote(tasks.find((t) => t.id === expandedId));
      setExpandedId(null);
      setRecurrenceOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedId, noteDraft]);

  // 标题编辑收尾：commit 时 trim 非空且有变更才写回，否则保留原标题
  const finishTodoTitle = (todo: Task, commit: boolean) => {
    if (commit) {
      const title = titleDraft.trim();
      if (title && title !== todo.title) handleUpdateTask(todo.id, { title });
    }
    skipTitleBlur.current = true;
    setEditingId(null);
  };

  // —— 今日日程详情浮层（复刻 ScheduleCanvas 点击浮层；无 offset——widget 恒今天，
  // 实例日恒 dateKeyOfOffset(0)）——任务本体经 id 派生：已删除（浮层开着时别处删了
  // 同一任务）则条件渲染直接收起
  const detailTask = detail
    ? todayEvents.find((ev) => ev.id === detail.id)
    : undefined;
  const detailProject = detailTask?.project
    ? projects.find((p) => p.name === detailTask.project)
    : undefined;
  // 时间行日期文本：主界面显示被点实例日的绝对日期，widget 恒今天
  const detailDateText = `今天 周${WEEKDAY_NAMES[now.getDay()]}`;

  // 打开浮层：定位用 clamp（卡片右侧优先、右缘不足压回视口内——主界面"不足翻左"公式
  // 在 300px 窄窗会算出负 left；垂直 clamp 同主界面，360 为内容高度估算）
  const openDetail = (id: string, rect: DOMRect) => {
    const task = todayEvents.find((ev) => ev.id === id);
    if (!task) return;
    const left = Math.max(8, Math.min(rect.right + 8, window.innerWidth - DETAIL_W - 8));
    const top = Math.min(Math.max(rect.top, 12), Math.max(window.innerHeight - 12 - 360, 12));
    setNoteDraft(task.note ?? "");
    setEditingTitle(false);
    setRecurrenceOpen(false);
    setPendingEdit(null); // 上次浮层遗留的未决编辑随重开丢弃
    setDetail({ id, left, top });
  };

  // 编辑提交入口：重复系列的备注/标题/优先级先进三问缓冲（多字段聚合，等范围选择），
  // 单次日程/例外实体直发——例外实体已是物理行，编辑即改自己，无三问。返回是否进了
  // 缓冲（closeDetail 据此拦截关闭防丢稿）
  const queueEdit = (patch: Partial<Task>): boolean => {
    if (!detailTask) return false;
    if (detailTask.recurrence) {
      setPendingEdit((p) => ({ ...(p ?? {}), ...patch }));
      return true;
    }
    handleUpdateTask(detailTask.id, patch);
    return false;
  };

  // 备注提交：trim 空归 undefined 抹字段，仅变更才写回；重复系列经 queueEdit 进缓冲
  const commitNote = (): boolean => {
    if (!detailTask) return false;
    const note = noteDraft.trim() || undefined;
    if (note === detailTask.note) return false;
    return queueEdit({ note });
  };

  // 标题编辑收尾（口径同收件箱）：commit 时 trim 非空且有变更才写回；重复系列经
  // queueEdit 进缓冲
  const finishDetailTitle = (commit: boolean): boolean => {
    let queued = false;
    if (commit && detailTask) {
      const title = titleDraft.trim();
      if (title && title !== detailTask.title) queued = queueEdit({ title });
    }
    skipTitleBlur.current = true;
    setEditingTitle(false);
    return queued;
  };

  // 统一关闭：提交路径先显式落盘草稿（点外/Esc/滚动路径 textarea 是卸载不走 blur，
  // 不提交必丢稿）；重复系列有未决编辑（本次刚聚合进缓冲或缓冲已有存量）时不关闭
  // 浮层——应用范围气泡常显，由用户显式选范围或取消后再关闭；滚动/切 tab 锚点失效
  // 走 force=true 直接关
  const closeDetail = (commit: boolean, force = false) => {
    if (!detail) return;
    let queued = false;
    if (commit) {
      if (editingTitle) queued = finishDetailTitle(true);
      queued = commitNote() || queued;
    }
    if ((queued || pendingEdit !== null) && !force) return;
    setDetail(null);
  };

  // —— 卡片拖动/拉伸（主界面日视图精简版：无周/月分支；pointerup 位移 <4px = 纯
  // 点击弹详情浮层）——
  const [drag, setDrag] = useState<{
    eventId: string;
    mode: "move" | "resize";
    start: number;
    end: number;
  } | null>(null);
  const dragBaseRef = useRef<{
    mode: "move" | "resize";
    pointerX0: number;
    pointerY0: number;
    origStart: number;
    origEnd: number;
  } | null>(null);

  const onCardPointerDown = (
    e: ReactPointerEvent<HTMLDivElement>,
    event: ScheduledTask,
  ) => {
    if (e.button !== 0) return; // 仅主键
    const origEnd = event.end ?? event.start + 1;
    dragBaseRef.current = {
      mode: "move",
      pointerX0: e.clientX,
      pointerY0: e.clientY,
      origStart: event.start,
      origEnd,
    };
    setDrag({ eventId: event.id, mode: "move", start: event.start, end: origEnd });
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  // 下边框把手：拉伸结束时间（无结束时间的日程由此设置 end），事件冒泡回卡片复用 move/up
  const onResizePointerDown = (
    e: ReactPointerEvent<HTMLDivElement>,
    event: ScheduledTask,
  ) => {
    if (e.button !== 0) return;
    e.stopPropagation(); // 不触发卡片整体拖动
    const origEnd = event.end ?? event.start + 1;
    dragBaseRef.current = {
      mode: "resize",
      pointerX0: e.clientX,
      pointerY0: e.clientY,
      origStart: event.start,
      origEnd,
    };
    setDrag({ eventId: event.id, mode: "resize", start: event.start, end: origEnd });
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onCardPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const base = dragBaseRef.current;
    if (!base) return;
    const deltaHours = (e.clientY - base.pointerY0) / HOUR_HEIGHT;
    if (base.mode === "move") {
      const snapped = Math.round((base.origStart + deltaHours) * 4) / 4; // 15 分钟吸附
      const duration = base.origEnd - base.origStart;
      const start = Math.min(Math.max(snapped, START), END - duration);
      setDrag((d) =>
        d && d.start !== start ? { ...d, start, end: start + duration } : d,
      );
    } else {
      const snapped = Math.round((base.origEnd + deltaHours) * 4) / 4;
      const end = Math.min(Math.max(snapped, base.origStart + 0.25), END); // 最短 15 分钟
      setDrag((d) => (d && d.end !== end ? { ...d, end } : d));
    }
  };

  const onCardPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const base = dragBaseRef.current;
    if (!base) return;
    const current = drag;
    dragBaseRef.current = null;
    setDrag(null);
    if (!current) return;
    // 纯点击（真实 pointerup 且位移 <4px）弹详情浮层（pointercancel 触摸打断不算；
    // 15 分钟吸附粒度 20px 远大于阈值，点击与拖动/拉伸写回天然互斥）
    if (e.type === "pointerup") {
      const moved = Math.hypot(e.clientX - base.pointerX0, e.clientY - base.pointerY0);
      if (moved < 4) {
        openDetail(current.eventId, e.currentTarget.getBoundingClientRect());
        return;
      }
    }
    const src = tasks.find((t) => t.id === current.eventId);
    if (!src || !isScheduled(src)) return;
    const changed =
      base.mode === "move"
        ? current.start !== src.start
        : current.end !== (src.end ?? src.start + 1);
    if (!changed) return;
    handleScheduleChange(
      src.id,
      current.start,
      base.mode === "move" && src.end === undefined ? undefined : current.end,
    );
  };

  // 日程卡片渲染（主界面 renderEventCard 精简版：无紧凑变体，浮层打开
  // 的卡片挂 .detailing 描边；必须是渲染函数而非内嵌组件——拖动中重挂卡片会丢
  // pointer capture）
  const renderEventCard = (
    event: ScheduledTask,
    lane: { column: number; columns: number },
  ) => {
    const dragging = drag?.eventId === event.id;
    const start = dragging ? drag!.start : event.start;
    const end = dragging ? drag!.end : (event.end ?? event.start + 1);
    // 越界日程几何 clamp 收边（口径同主界面，数据不动）
    const top = Math.min(Math.max(y(start), 0), axisHeight);
    const bottom = Math.min(Math.max(y(end), 0), axisHeight);
    const narrow = lane.columns >= 3;
    return (
      <div
        key={event.id}
        className={`timeline-event card prio-${event.priority}${narrow ? " narrow" : ""}${dragging ? " dragging" : ""}${detail?.id === event.id ? " detailing" : ""}${isPastEvent(end) ? " past" : ""}`}
        style={{
          top: top + 4,
          height: Math.max(0, bottom - top - 8),
          left: laneLeft(lane),
          right: laneRight(lane),
        }}
        onPointerDown={(e) => onCardPointerDown(e, event)}
        onPointerMove={onCardPointerMove}
        onPointerUp={onCardPointerUp}
        onPointerCancel={onCardPointerUp}
      >
        <div className="timeline-event-title-row">
          <span className="timeline-event-title">{event.title}</span>
          {event.project && (
            <span className="timeline-event-tag">{event.project}</span>
          )}
        </div>
        <span className="timeline-event-meta">
          <Clock size={12} strokeWidth={2} />
          {event.end || (dragging && drag!.mode === "resize")
            ? `${fmtTime(start)} – ${fmtTime(end)}`
            : fmtTime(start)}
          {event.recurrence && (
            <>
              <Repeat size={12} strokeWidth={2} />
              {recurrenceText(event.recurrence, event.dateOffset)}
            </>
          )}
        </span>
        <div
          className="timeline-event-resize"
          onPointerDown={(e) => onResizePointerDown(e, event)}
        />
      </div>
    );
  };

  const scrollRef = useRef<HTMLDivElement>(null);
  // 时间轴初始滚动：nowTop 居中（挂载 + 每次切入今日 tab 时；nowTop 取当刻值，
  // 不入依赖——60s tick 重跑会把用户滚动位置拽回红线）
  useEffect(() => {
    if (tab !== "today") return;
    const el = scrollRef.current;
    if (el) el.scrollTop = nowTop - el.clientHeight / 2;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // 浮层存活期的全局关闭路由：Esc / 点浮层外 / 时间轴滚动（scroll 不冒泡，须直接挂
  // 滚动容器，force 关——浮层不随滚动移动、锚点漂移必须关）。监听随草稿 state 重建
  // 保 closeDetail 闭包新鲜（口径同 ScheduleCanvas）
  useEffect(() => {
    if (!detail) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDetail(true);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (detailRef.current && !detailRef.current.contains(e.target as Node)) {
        closeDetail(true);
      }
    };
    const onScroll = () => closeDetail(true, true);
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown);
    scrollRef.current?.addEventListener("scroll", onScroll);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown);
      scrollRef.current?.removeEventListener("scroll", onScroll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, noteDraft, titleDraft, editingTitle, pendingEdit]);

  // 切 tab force 关浮层（语境已变，未决编辑强制丢弃——同主界面切视图口径；挂载首跑
  // detail 为 null，closeDetail 内部 no-op）
  useEffect(() => {
    closeDetail(true, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // —— header 三钮 + 拖动 ——
  // 打开主页面：唤回主窗口（恢复 + 显示 + 聚焦三连，口径同 Rust 托盘的 show_main）
  const openMain = async () => {
    if (!isTauri()) return;
    try {
      const main = await WebviewWindow.getByLabel("main");
      if (!main) return;
      await main.show();
      await main.unminimize();
      await main.setFocus();
    } catch {
      // 主窗口不存在（理论不发生）静默
    }
  };

  // 置顶/取消置顶切换（Pin/PinOff 图标与 on 高亮随 state）；记忆写 localStorage
  // （不可用仅丢记忆，不影响本次切换）
  const toggleOnTop = () => {
    if (!isTauri()) return;
    const next = !onTop;
    setOnTop(next);
    try {
      localStorage.setItem(PIN_KEY, next ? "1" : "0");
    } catch {
      // 忽略——置顶记忆写失败
    }
    void getCurrentWebviewWindow().setAlwaysOnTop(next).catch(() => {});
  };

  // 关闭钮：Rust 写 visible=false + hide（不退出，窗口常驻复用；托盘"桌面小部件"
  // 再唤出）；invoke 失败回落 JS 直调 hide——窗口关不掉比状态脏一拍更糟（状态由
  // 下一任一显隐入口自愈）
  const hideWidget = () => {
    if (!isTauri()) return;
    hideWidgetPersisted().catch((e) => {
      console.error("hide_widget 命令失败，回落纯隐藏", e);
      void getCurrentWebviewWindow().hide().catch(() => {});
    });
  };

  // header 拖动：显式 pointerdown → start_dragging（交给系统 move loop，丝滑）。
  // 不用 data-tauri-drag-region——它按事件 target 判定，.widget-tabs 宭代子元素占满
  // header 时点击永不触发（此前"经常拖不动"的根因）；改为落在非按钮区域即拖动，
  // tab/功能按钮本身不受影响
  const onHeaderPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (!isTauri() || e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button")) return;
    void getCurrentWebviewWindow().startDragging().catch(() => {});
  };

  // 边缘调整大小：热区 pointerdown → startResizeDragging 交给系统 resize loop
  // （尺寸下限由 conf 的 minWidth/minHeight 声明，系统拖动时自动 clamp，JS 不算尺寸）
  const onResizeEdgePointerDown = (
    e: ReactPointerEvent<HTMLDivElement>,
    dir: ResizeDir,
  ) => {
    if (!isTauri() || e.button !== 0) return;
    void getCurrentWebviewWindow().startResizeDragging(dir).catch(() => {});
  };

  return (
    <div className="widget-shell">
      {/* header = 拖动区（显式 pointerdown → start_dragging，按钮区域不触发）；
          右侧三钮：打开主页面 / 置顶切换 / 关闭（hide） */}
      <header className="widget-header" onPointerDown={onHeaderPointerDown}>
        <div className="widget-tabs">
          <button
            type="button"
            className={`widget-tab${tab === "inbox" ? " on" : ""}`}
            onClick={() => setTab("inbox")}
          >
            <Inbox size={13} strokeWidth={2} />
            收件箱
            {inboxList.length > 0 && (
              <span className="widget-tab-badge">{inboxList.length}</span>
            )}
          </button>
          <button
            type="button"
            className={`widget-tab${tab === "today" ? " on" : ""}`}
            onClick={() => setTab("today")}
          >
            <CalendarDays size={13} strokeWidth={2} />
            今日日程
            {todayEvents.length > 0 && (
              <span className="widget-tab-badge">{todayEvents.length}</span>
            )}
          </button>
        </div>
        <div className="widget-actions">
          <button
            type="button"
            className="widget-icon-btn"
            aria-label="打开主界面"
            title="打开主界面"
            onClick={openMain}
          >
            <Home size={13} strokeWidth={2} />
          </button>
          <button
            type="button"
            className={`widget-icon-btn${onTop ? " on" : ""}`}
            aria-label={onTop ? "取消置顶" : "置顶小部件"}
            title={onTop ? "取消置顶" : "置顶小部件"}
            onClick={toggleOnTop}
          >
            {onTop ? <PinOff size={13} strokeWidth={2} /> : <Pin size={13} strokeWidth={2} />}
          </button>
          <button
            type="button"
            className="widget-icon-btn"
            aria-label="隐藏小部件"
            title="隐藏小部件"
            onClick={hideWidget}
          >
            <X size={13} strokeWidth={2} />
          </button>
        </div>
      </header>

      {tab === "inbox" ? (
        <div className="widget-page">
          {/* 输入文法与主界面逐项一致（组件 100% 复用）；@今天 建排期后自动切到今日页 */}
          <QuickAdd
            projects={projects}
            onCreateTask={handleCreateTask}
            onJumpDate={(offset) => {
              if (offset === 0) setTab("today");
            }}
            hours={{ start: START, end: END }}
          />
          <div className="inbox-list widget-inbox-list">
            {inboxList.length === 0 ? (
              <p className="inbox-empty">暂无待办</p>
            ) : (
              inboxList.map((todo) => {
                const expanded = expandedId === todo.id;
                return (
                  <div
                    key={todo.id}
                    className={`inbox-todo prio-${todo.priority}${expanded ? " expanded" : ""} widget-todo`}
                  >
                    <div className="inbox-todo-body">
                      {editingId === todo.id ? (
                        <input
                          className="inbox-todo-title-input"
                          value={titleDraft}
                          maxLength={TITLE_MAX_LEN}
                          autoFocus
                          onChange={(e) => setTitleDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") finishTodoTitle(todo, true);
                            else if (e.key === "Escape") finishTodoTitle(todo, false);
                          }}
                          onBlur={() => {
                            // Enter/Esc 已主动退出的卸载 blur 被吞掉，仅失焦路径走到这里（保存）
                            if (skipTitleBlur.current) {
                              skipTitleBlur.current = false;
                              return;
                            }
                            finishTodoTitle(todo, true);
                          }}
                        />
                      ) : (
                        <p
                          className="inbox-todo-title"
                          onDoubleClick={() => {
                            // 重置守卫，防上次残留误吞本次编辑的失焦保存
                            skipTitleBlur.current = false;
                            setTitleDraft(todo.title);
                            setEditingId(todo.id);
                          }}
                        >
                          {todo.title}
                        </p>
                      )}
                      {todo.duration && (
                        <div className="inbox-todo-meta">
                          <Clock size={13} strokeWidth={2} />
                          <span>{todo.duration}</span>
                        </div>
                      )}
                      {expanded && (
                        <div className="inbox-todo-edit">
                          <div className="inbox-todo-note-wrap">
                            <textarea
                              className="inbox-todo-note"
                              value={noteDraft}
                              maxLength={NOTE_MAX_LEN}
                              autoFocus
                              rows={2}
                              placeholder="添加执行备注 (如注意点、参考链接)..."
                              onChange={(e) => setNoteDraft(e.target.value)}
                              onBlur={() => {
                                // 仅变更才写回（口径同 finishNote）：点卡内其他控件触发的
                                // 失焦不发无效 PATCH
                                const note = noteDraft.trim() || undefined;
                                if (note !== todo.note) handleUpdateTask(todo.id, { note });
                              }}
                            />
                            <span className="inbox-todo-note-count">
                              {noteDraft.length}/{NOTE_MAX_LEN}
                            </span>
                          </div>
                          {/* 重复规则：摘要行 + 内嵌展开面板（radio 频率档 + weekly 星期几
                              圆钮多选）——inboxList 恒未排期，此处是排期后即成系列的
                              预设（无基准时 weekly 默认周一） */}
                          <div className="inbox-todo-repeat-setting">
                            <button
                              type="button"
                              className="inbox-todo-repeat-bar"
                              onClick={() => setRecurrenceOpen((v) => !v)}
                            >
                              <span className="inbox-todo-repeat-bar-label">重复</span>
                              <span
                                className={`inbox-todo-repeat-bar-value${todo.recurrence ? "" : " muted"}`}
                              >
                                {todo.recurrence
                                  ? recurrenceText(todo.recurrence, todo.dateOffset, "、")
                                  : "不重复"}
                              </span>
                              <ChevronDown
                                size={13}
                                strokeWidth={2}
                                className={`inbox-todo-chevron${recurrenceOpen ? " open" : ""}`}
                              />
                            </button>
                            {recurrenceOpen && (
                              <div className="inbox-todo-repeat-panel">
                                {RECURRENCE_FREQ_OPTIONS.map((opt) => {
                                  const active = todo.recurrence
                                    ? todo.recurrence.freq === opt.value
                                    : opt.value === "none";
                                  // 当前生效的 weekly 规则（仅在 weekly 档且已选中 weekly 时
                                  // 存在）：提前收窄为 const，圆钮回调闭包里的 weekdays
                                  // 访问才有类型保障
                                  const weeklyRule =
                                    opt.value === "weekly" && todo.recurrence?.freq === "weekly"
                                      ? todo.recurrence
                                      : undefined;
                                  return (
                                    <div key={opt.value}>
                                      <button
                                        type="button"
                                        className={`inbox-todo-repeat-option${active ? " on" : ""}`}
                                        onClick={() =>
                                          handleUpdateTask(todo.id, {
                                            recurrence: setRecurrenceFreq(
                                              todo.recurrence,
                                              opt.value,
                                              todo.dateOffset,
                                            ),
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
                                                  // 最后一个星期不可取消（toggleWeekday 返 null 忽略）
                                                  const next = toggleWeekday(weeklyRule.weekdays, n);
                                                  if (next)
                                                    handleUpdateTask(todo.id, {
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
                          <div className="inbox-todo-edit-row">
                            <div className="inbox-todo-durations">
                              <span className="inbox-todo-durations-label">耗时</span>
                              {DURATION_PRESETS.map((p) => (
                                <button
                                  key={p.minutes}
                                  type="button"
                                  className={`inbox-todo-duration${todo.minutes === p.minutes ? " on" : ""}`}
                                  onClick={() =>
                                    handleUpdateTask(todo.id, {
                                      minutes: p.minutes,
                                      duration: p.duration,
                                    })
                                  }
                                >
                                  {p.label}
                                </button>
                              ))}
                            </div>
                            <div className="inbox-todo-prio">
                              <span className="inbox-todo-prio-label">优先级</span>
                              {(Object.keys(PRIORITY_COLORS) as Task["priority"][]).map((p) => (
                                <button
                                  key={p}
                                  type="button"
                                  style={{ color: PRIORITY_COLORS[p] }}
                                  className={`inbox-todo-prio-item${todo.priority === p ? " on" : ""}`}
                                  aria-label={{ high: "高", medium: "中", low: "低" }[p]}
                                  onClick={() => handleUpdateTask(todo.id, { priority: p })}
                                >
                                  <i />
                                </button>
                              ))}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                    {/* 操作按钮组：悬浮卡片右下角（位置语言同主界面收件箱）。删除走二次
                        确认气泡——inboxList 口径恒未排期，文案恒"确认删除该待办？" */}
                    <div className="inbox-todo-actions">
                      <ConfirmPopover
                        message="确认删除该待办？"
                        onConfirm={() => {
                          handleDeleteTask(todo.id);
                          if (expanded) {
                            setExpandedId(null);
                            setNoteDraft("");
                            setRecurrenceOpen(false);
                          }
                        }}
                        buttonClass="inbox-todo-icon del"
                        buttonTitle="删除"
                        icon={<Trash2 size={14} strokeWidth={2} />}
                      />
                      <button
                        type="button"
                        className="inbox-todo-icon"
                        aria-label="编辑"
                        title="编辑"
                        onClick={() => toggleExpand(todo)}
                      >
                        <ChevronDown
                          size={14}
                          strokeWidth={2}
                          className={`inbox-todo-chevron${expanded ? " open" : ""}`}
                        />
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      ) : (
        <div className="widget-page">
          {/* 负载指示器（逐节点复刻主界面日视图，全套同名类；仅压掉对翻页器的缩进） */}
          <div className="schedule-load widget-load" aria-label="当日负载指示">
            <div className={`schedule-load-bar${loadRate > 0.8 ? " over" : ""}`}>
              {(["high", "medium", "low"] as const).map((p) => (
                <span
                  key={p}
                  className={`schedule-load-seg ${p}`}
                  style={{ width: `${(prioHours[p] / loadDenom) * 100}%` }}
                />
              ))}
              <span
                className="schedule-load-seg free"
                style={{
                  width: `${Math.max(0, 1 - loadTotal / loadDenom) * 100}%`,
                }}
              />
            </div>
            <span
              className={`schedule-load-text${
                loadRate > 0.8 ? " danger" : loadRate > 0.5 ? " warn" : ""
              }`}
            >
              {fmtLoadHours(prioHours.high)}/{fmtLoadHours(loadTotal)}/{fmtLoadHours(WORK_HOURS)}
              {loadRate > 1
                ? `·已超载${fmtLoadHours(loadTotal - WORK_HOURS)}`
                : ""}
            </span>
          </div>
          <div className="schedule-scroll" ref={scrollRef}>
            <div className="timeline" style={{ height: axisHeight + 40 }}>
              {renderHourMarks(hours, START)}
              {todayEvents.map((event) =>
                renderEventCard(event, lanes.get(event)!),
              )}
              {/* 当前时间线（widget 恒今天，恒渲染） */}
              <span className="timeline-now-label" style={{ top: nowTop - 15 }}>
                {nowLabel}
              </span>
              <div className="timeline-now-line" style={{ top: nowTop }} />
              <div className="timeline-now-dot" style={{ top: nowTop - 3.4 }} />
            </div>
          </div>

          {/* 日程详情浮层（fixed 定位；widget 恒今天，实例日恒 dateKeyOfOffset(0)；
              时间只读——改时间走卡片拖动/拉伸；删除走二次确认气泡） */}
          {detail && detailTask && (
            <div
              className="schedule-detail"
              ref={detailRef}
              style={{ left: detail.left, top: detail.top }}
            >
              <button
                type="button"
                className="schedule-detail-close"
                aria-label="关闭详情"
                onClick={() => closeDetail(true)}
              >
                <X size={14} strokeWidth={2} />
              </button>
              {editingTitle ? (
                <input
                  className="schedule-detail-title-input"
                  value={titleDraft}
                  maxLength={TITLE_MAX_LEN}
                  autoFocus
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") finishDetailTitle(true);
                    else if (e.key === "Escape") {
                      // 标题编辑中的 Esc 只取消编辑：阻止冒泡到 document 的关闭监听
                      e.stopPropagation();
                      finishDetailTitle(false);
                    }
                  }}
                  onBlur={() => {
                    // Enter/Esc 已主动退出的卸载 blur 被吞掉，仅失焦路径走到这里（保存）
                    if (skipTitleBlur.current) {
                      skipTitleBlur.current = false;
                      return;
                    }
                    finishDetailTitle(true);
                  }}
                />
              ) : (
                <p
                  className="schedule-detail-title"
                  onDoubleClick={() => {
                    // 重置守卫，防上次残留误吞本次编辑的失焦保存
                    skipTitleBlur.current = false;
                    setTitleDraft(detailTask.title);
                    setEditingTitle(true);
                  }}
                >
                  {detailTask.title}
                </p>
              )}
              <div className="schedule-detail-meta">
                <Clock size={13} strokeWidth={2} />
                <span>
                  {detailDateText} {fmtTime(detailTask.start)}
                  {detailTask.end ? ` – ${fmtTime(detailTask.end)}` : ""}
                </span>
                {detailProject && (
                  <span className="schedule-detail-project">
                    <i style={{ background: detailProject.color }} />
                    {detailProject.name}
                  </span>
                )}
              </div>
              <div className="schedule-detail-note-wrap">
                <textarea
                  className="schedule-detail-note"
                  value={noteDraft}
                  maxLength={NOTE_MAX_LEN}
                  rows={3}
                  placeholder="添加执行备注 (如注意点、参考链接)..."
                  onChange={(e) => setNoteDraft(e.target.value)}
                  onBlur={commitNote}
                />
                <span className="schedule-detail-note-count">
                  {noteDraft.length}/{NOTE_MAX_LEN}
                </span>
              </div>
              {/* 重复规则：摘要行 + 内嵌展开面板（radio 频率档 + weekly 星期几圆钮多选），
                  位于备注下方与收件箱编辑区同序；改的是系列字段（all 直发，规则无单次
                  语义）——"不重复"写 undefined 抹字段，非实例日实例即刻消失/出现；
                  例外行（"仅修改此日程"实体化的单次实体）不支持设置重复规则（后端 422
                  安全闸），入口禁用只读提示 */}
              <div className="schedule-detail-repeat-setting">
                <button
                  type="button"
                  className="schedule-detail-repeat-bar"
                  onClick={() => setRecurrenceOpen((v) => !v)}
                  disabled={detailTask.isException}
                >
                  <span className="schedule-detail-repeat-bar-label">重复</span>
                  <span
                    className={`schedule-detail-repeat-bar-value${detailTask.recurrence ? "" : " muted"}`}
                  >
                    {detailTask.isException
                      ? "重复系列的单次例外"
                      : detailTask.recurrence
                        ? recurrenceText(detailTask.recurrence, detailTask.dateOffset, "、")
                        : "不重复"}
                  </span>
                  {!detailTask.isException && (
                    <ChevronDown
                      size={13}
                      strokeWidth={2}
                      className={`schedule-detail-repeat-chevron${recurrenceOpen ? " open" : ""}`}
                    />
                  )}
                </button>
                {recurrenceOpen && !detailTask.isException && (
                  <div className="schedule-detail-repeat-panel">
                    {RECURRENCE_FREQ_OPTIONS.map((opt) => {
                      const active = detailTask.recurrence
                        ? detailTask.recurrence.freq === opt.value
                        : opt.value === "none";
                      // 当前生效的 weekly 规则（仅在 weekly 档且已选中 weekly 时存在）：
                      // 提前收窄为 const，圆钮回调闭包里的 weekdays 访问才有类型保障
                      const weeklyRule =
                        opt.value === "weekly" && detailTask.recurrence?.freq === "weekly"
                          ? detailTask.recurrence
                          : undefined;
                      return (
                        <div key={opt.value}>
                          <button
                            type="button"
                            className={`schedule-detail-repeat-option${active ? " on" : ""}`}
                            onClick={() =>
                              handleUpdateTask(detailTask.id, {
                                recurrence: setRecurrenceFreq(
                                  detailTask.recurrence,
                                  opt.value,
                                  detailTask.dateOffset,
                                ),
                              })
                            }
                          >
                            <i className="schedule-detail-repeat-dot" />
                            {opt.label}
                          </button>
                          {weeklyRule && (
                            <div className="schedule-detail-repeat-weekdays">
                              {RECURRENCE_WEEKDAY_LABELS.map((label, i) => {
                                const n = i + 1;
                                return (
                                  <button
                                    key={n}
                                    type="button"
                                    className={`schedule-detail-repeat-weekday${
                                      weeklyRule.weekdays.includes(n) ? " on" : ""
                                    }`}
                                    onClick={() => {
                                      // 最后一个星期不可取消（toggleWeekday 返 null 忽略）
                                      const next = toggleWeekday(weeklyRule.weekdays, n);
                                      if (next)
                                        handleUpdateTask(detailTask.id, {
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
              <div className="schedule-detail-footer">
                <div className="schedule-detail-prio">
                  <span className="schedule-detail-prio-label">优先级</span>
                  {(Object.keys(PRIORITY_COLORS) as Task["priority"][]).map((p) => (
                    <button
                      key={p}
                      type="button"
                      style={{ color: PRIORITY_COLORS[p] }}
                      className={`schedule-detail-prio-item${detailTask.priority === p ? " on" : ""}`}
                      aria-label={{ high: "高", medium: "中", low: "低" }[p]}
                      onClick={() => {
                        // 点已激活优先级不进三问缓冲（口径同 commitNote 的变更守卫）——
                        // 否则"仅修改此日程"会实体化一条零变更例外行
                        if (p !== detailTask.priority) queueEdit({ priority: p });
                      }}
                    >
                      <i />
                    </button>
                  ))}
                </div>
                {/* 删除日程：二次确认气泡（任务已删草稿无意义，确认后不走 closeDetail 提交
                    路径）；重复系列纵向三选——仅删本次 = 后端墓碑挖空该日实例（系列行保
                    留）、删及后续 = UNTIL 截断到被删日前一天、删除所有 = 整任务删 */}
                <ConfirmPopover
                  message={detailTask.recurrence ? "该日程属于重复系列" : "确认删除该日程？"}
                  actions={
                    detailTask.recurrence
                      ? [
                          {
                            text: "仅删除本次",
                            tone: "danger-outline",
                            onClick: () => {
                              handleDeleteTask(detailTask.id, {
                                mode: "this",
                                date: dateKeyOfOffset(0),
                              });
                              setDetail(null);
                            },
                          },
                          {
                            text: "删除此日程及后续",
                            tone: "danger-outline",
                            onClick: () => {
                              handleDeleteTask(detailTask.id, {
                                mode: "following",
                                date: dateKeyOfOffset(0),
                              });
                              setDetail(null);
                            },
                          },
                          {
                            text: "删除所有",
                            onClick: () => {
                              handleDeleteTask(detailTask.id);
                              setDetail(null);
                            },
                          },
                        ]
                      : undefined
                  }
                  onConfirm={() => {
                    handleDeleteTask(detailTask.id);
                    setDetail(null);
                  }}
                  buttonClass="schedule-detail-delete"
                  buttonTitle="删除"
                  icon={<Trash2 size={13} strokeWidth={2} />}
                  label="删除"
                />
              </div>
              {/* 应用范围气泡（重复系列编辑三问）：pendingEdit 聚合了待应用的编辑，选范围
                  后统一提交——this/following 后端已换成新行（例外实体/新主记录），关浮层
                  （锚点失效）；all 更新主记录本体、浮层保持打开即时联动；取消丢弃编辑并
                  回滚备注草稿 */}
              {pendingEdit && (
                <div className="schedule-detail-apply" role="dialog" aria-label="选择修改应用范围">
                  <p className="schedule-detail-apply-message">修改应用于？</p>
                  <div className="schedule-detail-apply-actions">
                    <button
                      type="button"
                      className="schedule-detail-apply-action"
                      onClick={() => {
                        handleUpdateTask(detailTask.id, pendingEdit, {
                          mode: "this",
                          date: dateKeyOfOffset(0),
                        });
                        setPendingEdit(null);
                        setDetail(null);
                      }}
                    >
                      仅修改此日程
                    </button>
                    <button
                      type="button"
                      className="schedule-detail-apply-action"
                      onClick={() => {
                        handleUpdateTask(detailTask.id, pendingEdit, {
                          mode: "following",
                          date: dateKeyOfOffset(0),
                        });
                        setPendingEdit(null);
                        setDetail(null);
                      }}
                    >
                      修改此日程及后续
                    </button>
                    <button
                      type="button"
                      className="schedule-detail-apply-action"
                      onClick={() => {
                        handleUpdateTask(detailTask.id, pendingEdit);
                        setPendingEdit(null);
                      }}
                    >
                      修改所有日程
                    </button>
                    <button
                      type="button"
                      className="schedule-detail-apply-cancel"
                      onClick={() => {
                        setPendingEdit(null);
                        setNoteDraft(detailTask.note ?? "");
                      }}
                    >
                      取消修改
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 边缘调整大小热区（8 向；无边框窗 WebView 覆盖全窗，原生热区不可达） */}
      {RESIZE_EDGES.map(({ cls, dir }) => (
        <div
          key={cls}
          className={`widget-resize ${cls}`}
          onPointerDown={(e) => onResizeEdgePointerDown(e, dir)}
        />
      ))}
    </div>
  );
}

export default WidgetWindow;
