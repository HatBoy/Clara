import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  ArrowUpDown,
  Calendar,
  CalendarClock,
  CalendarPlus,
  CalendarSync,
  Check,
  ChevronDown,
  Clock,
  Flag,
  Folder,
  GripVertical,
  Inbox,
  PanelRightClose,
  Repeat,
  Timer,
  Trash2,
} from "lucide-react";
import {
  isScheduled,
  NOTE_MAX_LEN,
  PRIORITY_COLORS,
  RECURRENCE_FREQ_OPTIONS,
  RECURRENCE_WEEKDAY_LABELS,
  recurrenceText,
  seriesAlive,
  setRecurrenceFreq,
  TITLE_MAX_LEN,
  toggleWeekday,
  type Project,
  type ScheduledTask,
  type Task,
  durationText,
} from "../data/tasks";
import { TODO_MIME } from "../lib/dnd";
import { fetchEstimateHint, manualPickToRelative } from "../lib/api";
import ConfirmPopover from "./ConfirmPopover";
import SubtaskList from "./SubtaskList";
import WhenPicker, { WhenTimePicker } from "./WhenPicker";
import "./InboxPanel.css";

// 展开卡属性草稿类型（draft state）：四键全量装载，收起时与原任务对比提交
type EditDraft = Pick<Task, "minutes" | "duration" | "priority" | "recurrence">;

// 耗时快捷档（编辑区胶囊）：点击同写 minutes + duration 展示文本（durationText 统一口径）
// export 供对话框草案卡编辑区同款消费（2026-10-26 草案卡改版）
export const DURATION_PRESETS = [
  { minutes: 30, label: "30m", duration: durationText(30) },
  { minutes: 60, label: "1h", duration: durationText(60) },
  { minutes: 120, label: "2h", duration: durationText(120) },
] as const;

// 优先级排序权重（高 → 低），同优先级保持原序（sort 稳定）
const PRIORITY_ORDER: Record<Task["priority"], number> = {
  high: 0,
  medium: 1,
  low: 2,
};

// 小时（小数）→ "HH:MM"（与 ScheduleCanvas 同口径的本地实现，仅供已排期任务列表展示）
const fmtTime = (hour: number) =>
  `${String(Math.floor(hour)).padStart(2, "0")}:${String(Math.round((hour % 1) * 60)).padStart(2, "0")}`;

// 已排期任务的列表展示文本："今天 14:00 – 15:00"（dateOffset 相对运行当天换算）；
// 重复任务显示规则文本（如 "每天 09:30 – 10:30"）——列表语义是系列规则而非某个单日，
// 基准日多为过去日，显示绝对日期反而误导
const scheduleText = (t: ScheduledTask) => {
  if (t.recurrence)
    return `${recurrenceText(t.recurrence, t.dateOffset)} ${fmtTime(t.start)}${t.end ? ` – ${fmtTime(t.end)}` : ""}`;
  const d = new Date();
  d.setDate(d.getDate() + t.dateOffset);
  const dayLabel =
    t.dateOffset === 0
      ? "今天"
      : t.dateOffset === 1
        ? "明天"
        : t.dateOffset === -1
          ? "昨天"
          : `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${dayLabel} ${fmtTime(t.start)}${t.end ? ` – ${fmtTime(t.end)}` : ""}`;
};

// 已排期任务当前排期 → WhenPicker 值（"YYYY-MM-DDTHH:mm"，manualPickToRelative 互逆）：
// 编辑区日期药丸装载初始值用——已排期卡药丸直接显示当前排期时刻，改选后「改期」提交
const scheduledPickValue = (t: ScheduledTask) => {
  const d = new Date();
  d.setDate(d.getDate() + t.dateOffset);
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return `${iso}T${fmtTime(t.start)}`;
};

// 错误对象上的后端 detail 文案（api.ts request 失败时挂载）——可直显优先（引擎
// 422「未来 7 天没有可用空档」等），拿不到回落 fallback
const errDetail = (e: unknown, fallback: string) => {
  const d = (e as { detail?: unknown } | null)?.detail;
  return typeof d === "string" && d ? d : fallback;
};

// 收件箱/项目两页共用的任务清单面板：
// inbox 模式 = 未排期任务（开关开时含已归属项目的），project 模式 = 该项目任务（开关开时含已排期的）
function InboxPanel({
  style,
  onCollapse,
  tasks,
  mode,
  project,
  projects,
  onUpdateTask,
  onDeleteTask,
  onAutoSchedule,
  onScheduleChange,
  projectToggleOn,
  onProjectToggleChange,
  focusTask,
}: {
  style?: CSSProperties;
  onCollapse: () => void;
  // 全量任务（含已排期）：项目页"已排期"开关需要看到排期中的任务，过滤在面板内做
  tasks: Task[];
  mode: "inbox" | "project";
  // project 模式下当前项目（必有）
  project?: Project;
  // 项目列表：卡片项目标识的色点取色用
  projects: Project[];
  // 待办字段编辑（patch 合并：title/note/minutes+duration/priority）
  onUpdateTask: (taskId: string, patch: Partial<Task>) => void;
  // 删除待办
  onDeleteTask: (taskId: string) => void;
  // 一键智能安排（统一排期引擎，纯规则直落）：失败抛错（err.detail 文案可直显），
  // 面板 catch 提示；多任务批量排期不走按钮——对日程助手说"把所有待办排期"
  onAutoSchedule: (taskId: string) => Promise<void>;
  // 手动选时间排期（2026-10-24）：编辑区「安排时间」行提交，与拖拽同一写回链路
  onScheduleChange: (
    taskId: string,
    dateOffset: number,
    start: number,
    end: number | undefined,
  ) => void;
  // 项目页"已排期"开关受控（2026-11-10 项目筛选联动）：成对传入时 project 模式
  // 开关走 App 态（打开 = 中栏日历过滤为该项目）；inbox 模式开关恒本地态不受影响
  projectToggleOn?: boolean;
  onProjectToggleChange?: (on: boolean) => void;
  // 搜索定位请求（2026-11-06 搜索批，App 递增 seq 触发）：展开目标卡 + 滚入
  // 视口 + 短暂高亮。props 驱动（seq 依赖）——挂载后必达且同 id 可重复触发
  focusTask?: { id: string; seq: number } | null;
}) {
  // 头部开关（默认关）：inbox=只看未归属项目的，project=只看未排期的；
  // project 模式受控化（2026-11-10 项目筛选联动）——App 传入 projectToggleOn 时
  // 开关值走 App 态（打开同步触发中栏日历过滤），本地 state 仅 inbox 模式使用
  const [toggleOn, setToggleOn] = useState(false);
  const toggleValue =
    mode === "project" && projectToggleOn !== undefined ? projectToggleOn : toggleOn;
  // 列表排序（默认按优先级高→低，仅作用于未排期组；再点头部按钮切回原序）
  const [byPriority, setByPriority] = useState(true);
  // 编辑区单开（同时只展开一张卡）；noteDraft 为展开卡的备注草稿（onBlur 提交，展开时装载）
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  // 重复规则面板展开态（换目标/收起/删除即收起）
  const [recurrenceOpen, setRecurrenceOpen] = useState(false);
  // 「安排时间」手选草稿（datetime-local 格式串 "YYYY-MM-DDTHH:mm"；换目标/收起即
  // 清空——手选是即时动作不落任务，无需像 noteDraft 那样显式落盘）
  const [whenDraft, setWhenDraft] = useState("");
  // 重复系列执行时刻手选草稿（"HH:mm"；展开时装载当前执行时刻——改期钮确认提交，
  // 同 whenDraft 草稿生命周期）。2026-11-11 统一批：时刻面板点时/分写草稿不提交
  const [timeDraft, setTimeDraft] = useState("");
  // 日期面板开合态（2026-10-26 抽 WhenPicker 共享组件：月历/时间列/滚动定位/
  // 点外即收全部内置，开合受控留在消费侧——管与重复面板的互斥）
  const [whenOpen, setWhenOpen] = useState(false);
  // 重复系列执行时刻面板开合态（2026-11-10 问题 2：WhenTimePicker 时刻版，同互斥管理）
  const [timeOpen, setTimeOpen] = useState(false);
  // 标题内联编辑单开（双击进入）；titleDraft 为草稿
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  // 展开卡属性草稿（2026-10-26 四点完善第 4 点）：耗时/优先级/重复编辑中不实时
  // PATCH——优先级直发会立即触发列表重排、卡片跳位打断编辑；收起编辑区时统一
  // 对比提交（备注 noteDraft 与手选 whenDraft 本就是草稿态，各走各的提交点）
  const [draft, setDraft] = useState<EditDraft | null>(null);
  // Enter/Esc 主动退出标题编辑后，吞掉 input 卸载可能触发的 blur，防二次提交
  const skipTitleBlur = useRef(false);
  // 智能安排在途态（单卡 id）：防连点，按钮转忙碌
  const [autoId, setAutoId] = useState<string | null>(null);
  // 底部瞬时提示条（智能安排结果；4 秒自动清除，同 AgentSettings runHint 先例）。
  // undo = 勾选完成后 4 秒内的撤销锚（误点可救：PATCH done:false 卡片即回）
  const [notice, setNotice] = useState<{
    text: string;
    bad?: boolean;
    undo?: { taskId: string; title: string };
  } | null>(null);
  const noticeTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(noticeTimer.current), []);

  const showNotice = (
    text: string,
    bad = false,
    undo?: { taskId: string; title: string },
  ) => {
    setNotice({ text, bad, undo });
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 4000);
  };

  // 勾选完成：乐观标 done（列表过滤即消失）+ 提示条挂撤销锚；系列任务入口已
  // 隐藏，此处仅余单次行（与后端 422 闸互为双保险）
  const completeTodo = (todo: Task) => {
    onUpdateTask(todo.id, { done: true });
    showNotice(`已完成「${todo.title}」`, false, { taskId: todo.id, title: todo.title });
  };

  // 单卡智能安排：成功行由 App 层 upsert（画布即现），此处只管提示与在途态
  const runAutoSchedule = async (todo: Task) => {
    if (autoId !== null) return;
    setAutoId(todo.id);
    try {
      await onAutoSchedule(todo.id);
      showNotice(`已安排「${todo.title}」`);
    } catch (e) {
      showNotice(errDetail(e, "智能安排失败，请稍后重试"), true);
    } finally {
      setAutoId(null);
    }
  };

  // 手动选时间排期（2026-10-24 需求：拖拽之外补日期手选）：datetime-local 值换算
  // 前端排期坐标走 onScheduleChange——与拖拽同一条写回链路（乐观更新 + 持久化 +
  // widget 同步全继承）；耗时已知给 end，无耗时只有开始时刻（一小时卡展示规则）
  const applyManualWhen = (todo: Task) => {
    const rel = manualPickToRelative(whenDraft);
    if (!rel) {
      showNotice("先选择日期和时间再排期", true);
      return;
    }
    onScheduleChange(
      todo.id,
      rel.dateOffset,
      rel.start,
      todo.minutes ? rel.start + todo.minutes / 60 : undefined,
    );
    showNotice(`已安排「${todo.title}」到 ${whenDraft.replace("T", " ")}`);
  };

  // 备注高度自适应（2026-10-26 布局优化）：挂载时按已有内容撑高（展开装草稿的
  // ref 回调），输入时随内容重算——rows={2} 只是 JS 失效时的保底
  const fitNoteHeight = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  };

  // 备注提交：trim 空归 undefined 抹字段，仅变更才写回（幂等）。收起路径必须显式调用——
  // 收起即卸载 textarea，聚焦元素被移除时 Chromium 不触发 blur，onBlur 提交永不执行
  //（此前假设"blur 先于收起"是错的，收起即丢稿的根因）
  const finishNote = (todo: Task | undefined) => {
    if (!todo) return;
    const note = noteDraft.trim() || undefined;
    if (note !== todo.note) onUpdateTask(todo.id, { note });
  };

  // 属性草稿统一提交：与原任务逐键对比，变更键组装单次 patch 合并 PATCH（含
  // 「不重复」→ recurrence: undefined——toTaskPatch 按 in 判键翻译 null 清空）；
  // recurrence 深比较（setRecurrenceFreq 每次产新对象，浅比较必误判）。
  // 已排期任务跳过 minutes/duration 键——后端不变式 duration_mins ≡ end-start
  // 强制重算，此通道发了也会被覆盖（陈旧草稿回写反致前后端漂移）；改时长走
  // 耗时档位直发 onScheduleChange（见 attr-row），与卡片拉伸同链路
  const flushDraft = (todo: Task | undefined) => {
    if (!todo || !draft) return;
    const patch: Partial<Task> = {};
    if (
      !isScheduled(todo) &&
      (draft.minutes !== todo.minutes || draft.duration !== todo.duration)
    ) {
      patch.minutes = draft.minutes;
      patch.duration = draft.duration;
    }
    if (draft.priority !== todo.priority) patch.priority = draft.priority;
    if (JSON.stringify(draft.recurrence ?? null) !== JSON.stringify(todo.recurrence ?? null))
      patch.recurrence = draft.recurrence;
    if (Object.keys(patch).length) onUpdateTask(todo.id, patch);
  };

  // 已排期日程改期（2026-11-10 编辑区补齐）：日期药丸改选后经操作行「改期」提交，
  // 平移语义——保时长（end-start 原样平移，与日历拖拽同式）走 onScheduleChange
  // 同一条写回链路（乐观更新 + PATCH + widget 同步全继承）；改时长走耗时档位另路
  const applyReschedule = (todo: Task) => {
    if (!isScheduled(todo)) return;
    const rel = manualPickToRelative(whenDraft);
    if (!rel) {
      showNotice("先选择日期和时间再改期", true);
      return;
    }
    const dur = todo.end !== undefined ? todo.end - todo.start : undefined;
    if (rel.dateOffset === todo.dateOffset && rel.start === todo.start) {
      showNotice("未修改排期时间");
      return;
    }
    onScheduleChange(
      todo.id,
      rel.dateOffset,
      rel.start,
      dur !== undefined ? rel.start + dur : undefined,
    );
    showNotice(`已将「${todo.title}」改期到 ${whenDraft.replace("T", " ")}`);
  };

  // 重复系列执行时刻改点（2026-11-10 问题 2）：只改时刻——dateOffset 基准日与
  // 星期规则不动（改天走重复面板圆钮、整体挪移走日历实例拖拽），end 随时长平移；
  // 全系列生效（列表本就是系列语义，同重复药丸 all 直发口径）
  const applySeriesTime = (todo: Task, hhmm: string) => {
    if (!isScheduled(todo)) return;
    const [h, m] = hhmm.split(":").map(Number);
    const newStart = h + m / 60;
    if (newStart === todo.start) {
      showNotice("未修改执行时间");
      return;
    }
    const dur = todo.end !== undefined ? todo.end - todo.start : undefined;
    onScheduleChange(
      todo.id,
      todo.dateOffset,
      newStart,
      dur !== undefined ? newStart + dur : undefined,
    );
    showNotice(`已将「${todo.title}」执行时间改到 ${hhmm}`);
  };

  // 编辑区开关：点已展开卡收起（先显式落盘备注与属性草稿——卸载不走 blur/无
  // 提交点），点新卡换目标并装载该卡草稿
  const toggleExpand = (todo: Task) => {
    if (expandedId === todo.id) {
      finishNote(todo);
      flushDraft(todo);
      setDraft(null);
      setExpandedId(null);
      setRecurrenceOpen(false);
      setWhenOpen(false);
      setTimeOpen(false);
      setWhenDraft("");
      setTimeDraft("");
      return;
    }
    // 换目标前显式落盘旧卡草稿：键盘路径（Tab 聚焦编辑按钮后 Enter）只产生 click
    // 不产生 pointerdown，外点收起监听不触发，直接装载新卡草稿会静默覆盖旧卡输入
    if (expandedId !== null) {
      const prev = tasks.find((t) => t.id === expandedId);
      finishNote(prev);
      flushDraft(prev);
    }
    setNoteDraft(todo.note ?? "");
    setDraft({
      minutes: todo.minutes,
      duration: todo.duration,
      priority: todo.priority,
      recurrence: todo.recurrence,
    });
    setExpandedId(todo.id);
    setRecurrenceOpen(false);
    setWhenOpen(false);
    setTimeOpen(false);
    // 已排期非重复卡（含例外实体行，recurrence_rule=NULL 天然单次）：日期药丸装载
    // 当前排期时刻作改期基准；重复系列时刻药丸装载当前执行时刻作改时间基准
    setWhenDraft(
      isScheduled(todo) && !todo.recurrence ? scheduledPickValue(todo) : "",
    );
    setTimeDraft(
      isScheduled(todo) && todo.recurrence ? fmtTime(todo.start) : "",
    );
  };

  // 展开态点卡片外任意处自动收起：落点仍在当前展开卡内的交互（圆点/胶囊/双击标题）
  // 不触发；点另一张卡的编辑按钮 = 先收起旧卡（此处先落盘备注与属性草稿）、再由
  // click 展开新卡，与单开逻辑天然衔接。收起即卸载 textarea 无 blur——handler 里
  // 必须显式提交草稿；依赖含 noteDraft/draft 使闭包随草稿重建（提交读到最新值）
  useEffect(() => {
    if (expandedId === null) return;
    const onPointerDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest(".inbox-todo.expanded")) return;
      const todo = tasks.find((t) => t.id === expandedId);
      finishNote(todo);
      flushDraft(todo);
      setDraft(null);
      setExpandedId(null);
      setRecurrenceOpen(false);
      setWhenOpen(false);
      setTimeOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [expandedId, noteDraft, draft]);

  // 搜索定位（2026-11-06 搜索批）：App 递增 seq 触发——展开目标卡、滚入视口、
  // 短暂高亮。scrollIntoView 走 data-task-id 直查（切页刚挂载时任务已在 App 内存
  // state，首渲染即含目标卡，effect 时 DOM 已提交）；目标不可见（如收件箱页开关
  // 过滤）时静默无操作，用户再点一次同结果——可接受的小缝隙
  const [focusFlash, setFocusFlash] = useState<string | null>(null);
  // 预估校准提示（2026-11-06 批 E1）：展开时按任务标题拉同类历史时长；已填估时
  // 与均值偏差 >25% 且样本 ≥2 才展示「同类均值 X · 采纳」——采纳写入属性草稿
  // （收起时 flushDraft 统一提交，与估时药丸同一条链路）
  const [estHint, setEstHint] = useState<{ avg_min: number | null; count: number } | null>(null);
  useEffect(() => {
    setEstHint(null);
    if (expandedId === null) return;
    const todo = tasks.find((t) => t.id === expandedId);
    if (!todo) return;
    let alive = true;
    fetchEstimateHint(todo.title)
      .then((h) => alive && setEstHint(h))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [expandedId]); // title 变更不重拉（编辑中提示跳变扰人）；下次展开自然刷新
  useEffect(() => {
    if (!focusTask) return;
    setExpandedId(focusTask.id);
    setFocusFlash(focusTask.id);
    document
      .querySelector(`[data-task-id="${focusTask.id}"]`)
      ?.scrollIntoView({ block: "center" });
    const t = window.setTimeout(() => setFocusFlash(null), 1600);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTask?.seq]);

  // 重复面板外点收起（2026-10-26 七次修订，日期面板抽 WhenPicker 后只管重复）：
  // 面板开着时点卡内其他区域或卡外任意处即折叠；点药丸本身走 onClick（toggle +
  // 互斥），面板内交互豁免，点日期药丸不误收（互斥由 WhenPicker onOpenChange
  // 回调显式关）。值已在草稿态（draft.recurrence / whenDraft），保存仍走卡收起
  // 时 flushDraft 统一提交——「折叠后统一保存」原则不变
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

  // 标题编辑收尾：commit 时 trim 非空且有变更才写回，否则保留原标题
  const finishTitle = (todo: Task, commit: boolean) => {
    if (commit) {
      const title = titleDraft.trim();
      if (title && title !== todo.title) onUpdateTask(todo.id, { title });
    }
    skipTitleBlur.current = true;
    setEditingId(null);
  };

  let list: Task[];
  if (mode === "project") {
    // "已排期"开关只放行今天及以后的排期（dateOffset 相对当天）：过期任务去日历翻过去日期回看，列表长度天然有界；
    // 重复任务例外放行——基准日多在过去但系列今天起仍发生，一条规则一条记录不破坏长度有界，
    // 但 UNTIL 截断后的死系列不放行（seriesAlive，日历 occursOn 已天然隐藏，此处补列表侧）
    const mine = tasks.filter(
      (t) =>
        t.project === project?.name &&
        // 未排期且已完成：从列表隐藏（完成 ≠ 删除，数据仍在——撤销/日历历史不受影响）
        !(t.done && !isScheduled(t)) &&
        (!isScheduled(t) ||
          (toggleValue &&
            (t.recurrence !== undefined ? seriesAlive(t) : t.dateOffset >= 0))),
    );
    // 未排期在前（需行动优先），已排期按 dateOffset+start 升序垫后（近 → 远）——
    // 已排期是时间承诺，两种排序下恒保持时间序；未排期组默认按优先级高→低（可切回原序）
    list = mine.sort((a, b) => {
      if (isScheduled(a) !== isScheduled(b)) return isScheduled(a) ? 1 : -1;
      if (isScheduled(a) && isScheduled(b))
        return a.dateOffset - b.dateOffset || a.start - b.start;
      return byPriority
        ? PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority]
        : 0;
    });
  } else {
    // filter 返回新数组，原地排序无副作用；已完成行隐藏（口径同 project 分支注释）
    list = tasks.filter((t) => !isScheduled(t) && !t.done && (toggleOn || !t.project));
    if (byPriority)
      list = list.sort(
        (a, b) => PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority],
      );
  }

  return (
    <section className="inbox" style={style}>
      <header className="inbox-header">
        <div className="inbox-title">
          <div
            className="inbox-title-icon"
            style={mode === "project" ? { background: project?.color } : undefined}
          >
            {mode === "project" ? (
              <Folder size={15} strokeWidth={2} />
            ) : (
              <Inbox size={15} strokeWidth={2} />
            )}
          </div>
          <span className="inbox-title-text">
            {mode === "project" ? project?.name : "收件箱"}
          </span>
        </div>

        <div className="inbox-header-actions">
          <button
            type="button"
            className={`inbox-sort${byPriority ? " on" : ""}`}
            title={byPriority ? "按优先级排序" : "按默认排序"}
            aria-label="切换优先级排序"
            aria-pressed={byPriority}
            onClick={() => setByPriority((v) => !v)}
          >
            <ArrowUpDown size={16} strokeWidth={2} />
          </button>
          <div className="inbox-toggle-group">
            <span className="inbox-toggle-label">
              {mode === "inbox" ? "所有待办" : "已排期"}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={toggleValue}
              aria-label={mode === "inbox" ? "所有待办" : "已排期"}
              className={`inbox-toggle${toggleValue ? " on" : ""}`}
              onClick={() =>
                mode === "project" && onProjectToggleChange
                  ? onProjectToggleChange(!toggleValue)
                  : setToggleOn((v) => !v)
              }
            />
          </div>
          <span className="inbox-header-divider" />
          <button
            type="button"
            className="inbox-collapse"
            onClick={onCollapse}
            aria-label="折叠收件箱面板"
          >
            <PanelRightClose size={18} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="inbox-main">
        <div className="inbox-list">
          {list.length === 0 ? (
            <p className="inbox-empty">暂无待办</p>
          ) : (
            list.map((todo) => {
              const scheduled = isScheduled(todo);
              const expanded = expandedId === todo.id;
              // 编辑态（展开编辑区或标题编辑中）整卡禁拖：卡内选文本/输入不再误触发原生拖拽
              const editActive = expanded || editingId === todo.id;
              // 项目标识仅在收件箱页显示（"所有待办"混排时区分归属；项目页整列同项目，头部已标识）
              const proj =
                mode === "inbox" && todo.project
                  ? projects.find((p) => p.name === todo.project)
                  : undefined;
              // 操作按钮组双形态（2026-10-26 三次修订）：非展开卡 = 卡右下角悬浮
              //（hover/focus 显隐）；展开卡 = 编辑区底部静态行——原悬浮态四钮
              //（宽约 104px）叠压日期+重复行尾部、且行宽让位参差，行内化后内容
              // 行全宽统一（第 3 点）、按钮恒可见。手动排期仅行内版渲染（悬浮版
              // 无展开态可选日期，灰钮无意义）
              const renderActions = (inline: boolean) => (
                <>
                  {/* 手动排期（行内版第一位，四次修订排序：手动 → 智能 → 删除
                      → 展开；已排期卡为 改期 → 删除 → 展开）：选了日期（行内版即
                      展开态）才可用；提交走 applyManualWhen——与拖拽/日期行
                      Enter 同链路 */}
                  {inline && !scheduled && (
                    <button
                      type="button"
                      className="inbox-todo-icon"
                      aria-label="手动排期"
                      title={whenDraft ? "按所选时间排期" : "先在上方选择日期时间"}
                      disabled={!whenDraft}
                      onClick={() => applyManualWhen(todo)}
                    >
                      <CalendarPlus size={14} strokeWidth={2} />
                    </button>
                  )}
                  {/* 已排期改期（2026-11-10 编辑区补齐）：非重复卡 = 日期药丸改选后
                      提交平移（例外实体行后端 recurrence_rule=NULL 天然单次，照常
                      获得）；保时长，提交走 applyReschedule（与拖拽同链路）；改时长
                      走 attr-row 耗时档位。重复系列（2026-11-11 统一批补，同确认
                      语义）= 时刻药丸选新时刻（timeDraft 草稿）后点钮提交
                      applySeriesTime（全系列执行时刻生效，不动锚点与星期规则）
                      ——重锚定整系列刻意不做（误伤面大），实例级改期走日历拖拽/
                      详情浮层既有能力 */}
                  {inline && scheduled && !todo.recurrence && (
                    <button
                      type="button"
                      className="inbox-todo-icon"
                      aria-label="改期"
                      title={whenDraft ? "按所选时间改期" : "先在上方选择新的日期时间"}
                      disabled={!whenDraft}
                      onClick={() => applyReschedule(todo)}
                    >
                      <CalendarSync size={14} strokeWidth={2} />
                    </button>
                  )}
                  {inline && scheduled && todo.recurrence && (
                    <button
                      type="button"
                      className="inbox-todo-icon"
                      aria-label="改时间"
                      title="按所选时间改时间（全系列执行时刻）"
                      disabled={!timeDraft}
                      onClick={() => applySeriesTime(todo, timeDraft)}
                    >
                      <CalendarSync size={14} strokeWidth={2} />
                    </button>
                  )}
                  {/* 一键智能安排（统一排期引擎，收件箱/项目页未排期卡同享——
                      2026-11-11 统一批放开项目页）：规则层找空档直落日历（毫秒级）；
                      无空档 422 / 已排期 409 文案进底部提示条。多任务批量排期走对话
                      （见 footer 引导） */}
                  {!scheduled && (
                    <button
                      type="button"
                      className="inbox-todo-icon"
                      aria-label="智能安排"
                      title="智能安排"
                      disabled={autoId !== null}
                      onClick={() => void runAutoSchedule(todo)}
                    >
                      <CalendarClock
                        size={14}
                        strokeWidth={2}
                        className={autoId === todo.id ? "inbox-spin" : undefined}
                      />
                    </button>
                  )}
                  {/* 删除待办：二次确认气泡（原删除逻辑移入 onConfirm）。列表是系列语义、
                      无具体实例，已排期的重复系列不提供"仅删本次"（那在日历实例卡上），
                      但文案提醒删的是整个系列；任务即删，属性草稿随之丢弃 */}
                  <ConfirmPopover
                    message={
                      isScheduled(todo) && todo.recurrence
                        ? "该待办是重复系列，确认删除所有？"
                        : "确认删除该待办？"
                    }
                    onConfirm={() => {
                      onDeleteTask(todo.id);
                      if (expanded) {
                        setExpandedId(null);
                        setNoteDraft("");
                        setDraft(null);
                        setRecurrenceOpen(false);
                        setWhenOpen(false);
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
                </>
              );
              return (
                <div
                  key={todo.id}
                  className={`inbox-todo prio-${todo.priority}${scheduled && todo.recurrence ? " plain" : ""}${todo.done ? " done" : ""}${expanded ? " expanded" : ""}${!(todo.duration || scheduled || proj) && !expanded ? " slim" : ""}${focusFlash === todo.id ? " focus-flash" : ""}`}
                  data-task-id={todo.id}
                  /* 拖拽（2026-11-10 已排期单次行放开，与待办一致）：未排期全可拖
                     （含系列——拖入日历 = 首次排期，规则成系列预设）；已排期仅单次
                     可拖（拖入 = 保时长平移改期，时长靠不变式 minutes ≡ end-start
                     在落点换算保真）；已排期重复系列禁拖——列表行是系列语义，拖一下
                     = 重锚定整系列基准日（所有实例跟着挪），误伤面太大，实例级改期
                     走日历拖拽既有能力 */
                  draggable={!editActive && (!scheduled || !todo.recurrence)}
                  onDragStart={
                    scheduled && todo.recurrence
                      ? undefined
                      : (e) => {
                          e.dataTransfer.effectAllowed = "move";
                          e.dataTransfer.setData(
                            TODO_MIME,
                            JSON.stringify({
                              id: todo.id,
                              title: todo.title,
                              // 已排期单次行：minutes 即时长（后端不变式 ≡ end-start），
                              // 落点 end = start + minutes/60 = 保时长平移改期
                              minutes: todo.minutes ?? null,
                            }),
                          );
                        }
                  }
                >
                  {/* 行首完成钮（2026-11-10 已排期单次行补齐，与未排期一致）：
                      未完成单次行（未排期/已排期，含例外实体行——后端 recurrence
                      NULL 天然单次）= 空心圆钮点击完成；重复系列行（两种）= 同宽
                      占位（列表是系列语义无"标哪天"语境，后端整系列 done 422，实例
                      级完成走日历详情浮层 mode=this；直接消失会整卡左移，占位保
                      标题列对齐）；已完成已排期行 = 绿勾钮可点撤销（卡片保留在
                      列表，作 4 秒撤销窗外的恢复入口）；已完成未排期行被过滤
                      不可达，静态绿勾分支保守保留 */}
                  {todo.done ? (
                    scheduled ? (
                      <button
                        type="button"
                        className="inbox-todo-check on"
                        aria-label="撤销完成"
                        title="撤销完成"
                        onClick={() => onUpdateTask(todo.id, { done: false })}
                      >
                        <Check size={13} strokeWidth={2.5} />
                      </button>
                    ) : (
                      <Check size={13} strokeWidth={2.5} className="inbox-todo-done-mark" />
                    )
                  ) : todo.recurrence ? (
                    <span className="inbox-todo-check-spacer" aria-hidden="true" />
                  ) : (
                    <button
                      type="button"
                      className="inbox-todo-check"
                      aria-label="标记完成"
                      title="标记完成"
                      onClick={() => completeTodo(todo)}
                    >
                      <Check size={11} strokeWidth={2.5} />
                    </button>
                  )}
                  {/* 手柄与 draggable 同条件（单次已排期行也有手柄，系列已排期无——
                      视觉区分可拖性） */}
                  {scheduled && todo.recurrence ? null : (
                    <GripVertical size={14} strokeWidth={2} className="inbox-todo-grip" />
                  )}
                  <div className="inbox-todo-body">
                    {editingId === todo.id ? (
                      <input
                        className="inbox-todo-title-input"
                        value={titleDraft}
                        maxLength={TITLE_MAX_LEN}
                        autoFocus
                        onChange={(e) => setTitleDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") finishTitle(todo, true);
                          else if (e.key === "Escape") finishTitle(todo, false);
                        }}
                        onBlur={() => {
                          // Enter/Esc 已主动退出的卸载 blur 被吞掉，仅失焦路径走到这里（保存）
                          if (skipTitleBlur.current) {
                            skipTitleBlur.current = false;
                            return;
                          }
                          finishTitle(todo, true);
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
                    {todo.duration || scheduled || proj ? (
                      <div className="inbox-todo-meta">
                        {scheduled || todo.duration ? (
                          <>
                            <Clock size={13} strokeWidth={2} />
                            <span>{scheduled ? scheduleText(todo) : todo.duration}</span>
                          </>
                        ) : null}
                        {todo.checklist && todo.checklist.length > 0 ? (
                          <span className="inbox-todo-checklist-badge">
                            子任务 {todo.checklist.filter((c) => c.done).length}/{todo.checklist.length}
                          </span>
                        ) : null}
                        {proj ? (
                          <span className="inbox-todo-project">
                            <i style={{ background: proj.color }} />
                            {proj.name}
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    {expanded && draft ? (
                      <div className="inbox-todo-edit">
                        <div className="inbox-todo-note-wrap">
                          <textarea
                            className="inbox-todo-note"
                            value={noteDraft}
                            maxLength={NOTE_MAX_LEN}
                            autoFocus
                            rows={2}
                            ref={fitNoteHeight}
                            placeholder="添加执行备注 (如注意点、参考链接)..."
                            onChange={(e) => setNoteDraft(e.target.value)}
                            onInput={(e) => {
                              e.currentTarget.style.height = "auto";
                              e.currentTarget.style.height = `${e.currentTarget.scrollHeight}px`;
                            }}
                            onBlur={() => {
                              // 仅变更才写回（口径同 finishNote）：点卡内其他控件触发的
                              // 失焦不发无效 PATCH（空值翻译成 notes: null 会真发 UPDATE）
                              const note = noteDraft.trim() || undefined;
                              if (note !== todo.note) onUpdateTask(todo.id, { note });
                            }}
                          />
                          <span className="inbox-todo-note-count">
                            {noteDraft.length}/{NOTE_MAX_LEN}
                          </span>
                        </div>
                        {/* 子任务清单（批 C）：勾选/删行/添加即时提交——全量替换语义，
                            收件箱编辑区无三问缓冲直发 */}
                        <SubtaskList
                          items={todo.checklist ?? []}
                          onChange={(rows) => onUpdateTask(todo.id, { checklist: rows })}
                        />
                        {/* 任务属性行（2026-10-26 四次修订）：耗时、优先级两药丸
                            并列、空格间隔（竖线退役），flex:1 平分与时间行两药丸
                            列对齐（⏱耗时↔🔁重复、🚩优先级↔📅日期）。未排期：控件
                            读写属性草稿——编辑中不实时 PATCH，收起时 flushDraft 统一
                            提交；已排期（2026-11-10 补齐）：耗时档位直发改时长（耗
                            时 ≡ end-start 后端不变式，保日保起点改 end，与卡片拉伸
                            同链路、列表序不变），优先级仍走草稿 */}
                        <div className="inbox-todo-edit-row inbox-todo-attr-row">
                          <div className="inbox-pill-group">
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
                                className={`inbox-todo-duration${
                                  (scheduled
                                    ? todo.end !== undefined
                                      ? Math.round((todo.end - todo.start) * 60)
                                      : todo.minutes
                                    : draft.minutes) === p.minutes
                                    ? " on"
                                    : ""
                                }`}
                                onClick={() => {
                                  // 已排期：档位直发改时长（无 end 卡点击即补时长），
                                  // 闭包内 TS 不随 aliased condition 收窄故显式守卫
                                  if (isScheduled(todo)) {
                                    onScheduleChange(
                                      todo.id,
                                      todo.dateOffset,
                                      todo.start,
                                      todo.start + p.minutes / 60,
                                    );
                                    return;
                                  }
                                  setDraft({ ...draft, minutes: p.minutes, duration: p.duration });
                                }}
                              >
                                {p.label}
                              </button>
                            ))}
                          </div>
                          <div className="inbox-pill-group">
                            <Flag
                              size={14}
                              strokeWidth={2}
                              className="inbox-pill-icon"
                              aria-label="优先级"
                            />
                            {(Object.keys(PRIORITY_COLORS) as Task["priority"][]).map((p) => (
                              <button
                                key={p}
                                type="button"
                                style={{ color: PRIORITY_COLORS[p] }}
                                className={`inbox-todo-prio-item${draft.priority === p ? " on" : ""}`}
                                aria-label={{ high: "高", medium: "中", low: "低" }[p]}
                                title={`设为${{ high: "高", medium: "中", low: "低" }[p]}优先级`}
                                onClick={() => setDraft({ ...draft, priority: p })}
                              >
                                <i />
                              </button>
                            ))}
                          </div>
                        </div>
                        {/* 预估校准提示（批 E1）：已填估时与同类历史均值偏差 >25% 且
                            样本 ≥2 时显示；采纳写入属性草稿（flushDraft 统一提交） */}
                        {!scheduled &&
                          draft.minutes !== undefined &&
                          estHint !== null &&
                          estHint.count >= 2 &&
                          estHint.avg_min !== null &&
                          Math.abs(draft.minutes - estHint.avg_min) / estHint.avg_min > 0.25 && (
                            <div className="inbox-est-hint">
                              <span>
                                同类任务近 90 天均值 {durationText(estHint.avg_min)}（{estHint.count} 次）
                              </span>
                              <button
                                type="button"
                                onClick={() =>
                                  setDraft({
                                    ...draft,
                                    minutes: estHint.avg_min!,
                                    duration: durationText(estHint.avg_min!),
                                  })
                                }
                              >
                                采纳
                              </button>
                            </div>
                          )}
                        {/* 时间行（2026-10-26 五次修订）：重复在前、日期在后，CSS Grid
                            1fr 1fr 双列与属性行同一轨道模板——列边界数学强制对齐
                            （flex 平分受内容/wrap 干扰，无结构保证）；两药丸各自整钮
                            开合面板，面板移至 time-row 顶层独立轨道占满整行——曾嵌
                            列内：7×26px 星期圆钮 + 22px 缩进 ≈228px 超列宽 ~211px，
                            flex 默认 shrink 把圆钮压成椭圆（「变形 + 超右界」根因） */}
                        <div className="inbox-todo-time-row">
                          {/* 重复规则（时间行左药丸）：摘要条整钮开合面板。已排期 =
                              改系列规则，未排期 = 排期后即成系列的预设（无基准时
                              weekly 默认周一）；例外行（项目页"已排期"开关下可见）
                              不支持设置重复规则（后端 422 安全闸），入口禁用只读
                              提示。控件读写属性草稿（第 4 点，收起统一提交） */}
                          <div className="inbox-todo-repeat-setting">
                            <button
                              type="button"
                              className="inbox-todo-repeat-bar"
                              onClick={() => {
                                setRecurrenceOpen((v) => !v);
                                setWhenOpen(false);
                                setTimeOpen(false);
                              }}
                              disabled={todo.isException}
                            >
                              <Repeat
                                size={14}
                                strokeWidth={2}
                                className="inbox-pill-icon"
                                aria-label="重复"
                              />
                              <span
                                className={`inbox-todo-repeat-bar-value${draft.recurrence ? "" : " muted"}`}
                              >
                                {todo.isException
                                  ? "重复系列的单次例外"
                                  : draft.recurrence
                                    ? // weekly 收起态不列周几（列了显示不下拉伸药丸），
                                      // 只显「每周」；选了哪些天见面板圆钮（2026-10-26）
                                      draft.recurrence.freq === "weekly"
                                      ? "每周"
                                      : recurrenceText(draft.recurrence, todo.dateOffset, "、")
                                    : "不重复"}
                              </span>
                              {!todo.isException && (
                                <ChevronDown
                                  size={13}
                                  strokeWidth={2}
                                  className={`inbox-todo-chevron${recurrenceOpen ? " open" : ""}`}
                                />
                              )}
                            </button>
                          </div>
                          {/* 时间行右位药丸（2026-11-10 三分支）：未排期 / 已排期
                              非重复 = WhenPicker 日期药丸（自绘月历 + 时间双列，
                              选中实时写 whenDraft）；已排期重复系列 = WhenTimePicker
                              时刻药丸（纯时/分双列，选中写 timeDraft 草稿——不动
                              锚点与星期规则，applySeriesTime 全系列生效）。面板底部
                              文字提交主钮 2026-11-11 统一批退役——确认统一走操作行
                              图标钮（CalendarPlus/CalendarSync，hover 提示即
                              「按所选时间排期/改期/改时间」）；开面板时关重复面板（互斥） */}
                          {!scheduled ? (
                            <WhenPicker
                              value={whenDraft}
                              onChange={setWhenDraft}
                              open={whenOpen}
                              onOpenChange={(o) => {
                                setWhenOpen(o);
                                if (o) setRecurrenceOpen(false);
                              }}
                            />
                          ) : !todo.recurrence ? (
                            <WhenPicker
                              value={whenDraft}
                              onChange={setWhenDraft}
                              open={whenOpen}
                              onOpenChange={(o) => {
                                setWhenOpen(o);
                                if (o) setRecurrenceOpen(false);
                              }}
                            />
                          ) : (
                            <WhenTimePicker
                              value={timeDraft}
                              open={timeOpen}
                              onOpenChange={(o) => {
                                setTimeOpen(o);
                                if (o) setRecurrenceOpen(false);
                              }}
                              onChange={setTimeDraft}
                            />
                          )}
                          {/* 重复面板（独立轨道，占满整行宽——grid-column 1/-1） */}
                          {recurrenceOpen && !todo.isException && (
                            <div className="inbox-todo-repeat-panel">
                              {RECURRENCE_FREQ_OPTIONS.map((opt) => {
                                const active = draft.recurrence
                                  ? draft.recurrence.freq === opt.value
                                  : opt.value === "none";
                                // 当前生效的 weekly 规则（仅在 weekly 档且已选中 weekly 时存在）：
                                // 提前收窄为 const，圆钮回调闭包里的 weekdays 访问才有类型保障
                                const weeklyRule =
                                  opt.value === "weekly" && draft.recurrence?.freq === "weekly"
                                    ? draft.recurrence
                                    : undefined;
                                return (
                                  <div key={opt.value}>
                                    <button
                                      type="button"
                                      className={`inbox-todo-repeat-option${active ? " on" : ""}`}
                                      onClick={() =>
                                        setDraft({
                                          ...draft,
                                          recurrence: setRecurrenceFreq(
                                            draft.recurrence,
                                            opt.value,
                                            todo.dateOffset,
                                          ),
                                        })
                                      }
                                    >
                                      <i className="inbox-todo-repeat-dot" />
                                      {opt.label}
                                    </button>
                                    {/* weekly 多选圆钮保留（多天重复语义不变）；「显示
                                        不下」只发生在收起态药丸列周几——已在药丸处收
                                        口为只显「每周」，面板整行宽圆钮行无溢出 */}
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
                                                  setDraft({
                                                    ...draft,
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
                        {/* 展开卡操作行（编辑区底部静态，替代悬浮组——行宽统一
                            且不与内容行叠压） */}
                        <div className="inbox-todo-edit-actions">{renderActions(true)}</div>
                      </div>
                    ) : null}
                  </div>
                  {/* 操作按钮组：非展开卡悬浮卡片右下角（展开卡由编辑区底部静态
                      操作行接替，按钮内容由 renderActions 单源供给） */}
                  {!expanded && <div className="inbox-todo-actions">{renderActions(false)}</div>}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* 智能安排/完成结果瞬时提示（4 秒自动清除；bad 红字；undo = 完成撤销） */}
      {notice && (
        <div className={`inbox-notice${notice.bad ? " bad" : ""}`} role="status">
          <span>{notice.text}</span>
          {notice.undo && (
            <button
              type="button"
              className="inbox-notice-undo"
              onClick={() => {
                onUpdateTask(notice.undo!.taskId, { done: false });
                setNotice(null);
              }}
            >
              撤销
            </button>
          )}
        </div>
      )}

      <footer className="inbox-footer">
        <Calendar size={13} strokeWidth={2} />
        <span>拖拽任务至日历完成排期</span>
      </footer>
    </section>
  );
}

export default InboxPanel;
