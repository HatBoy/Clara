import { Fragment, useEffect, useRef, useState } from "react";
import type { CSSProperties, DragEvent as ReactDragEvent, ReactNode } from "react";
import {
  CalendarSync,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Coffee,
  Flag,
  NotebookPen,
  Repeat,
  Sparkles,
  Timer,
  Trash2,
  X,
} from "lucide-react";
import { HolidayUtil, Solar } from "lunar-typescript";
import {
  dateKeyOfOffset,
  NOTE_MAX_LEN,
  PRIORITY_COLORS,
  RECURRENCE_FREQ_OPTIONS,
  RECURRENCE_WEEKDAY_LABELS,
  occursOn,
  recurrenceText,
  setRecurrenceFreq,
  TITLE_MAX_LEN,
  toggleWeekday,
  type Project,
  type ScheduledTask,
  type Task,
} from "../data/tasks";
import { epochDayOf, manualPickToRelative } from "../lib/api";
import {
  isTodoDrag,
  parseTodoDrag,
  TODO_MIME,
  type DraggedTodo,
  type DraggedTodoWithSuggest,
} from "../lib/dnd";
import { onCanvasJump, onShortcutCmd } from "../lib/shortcuts";
import { submitMinutes, type GhostBlock } from "../lib/ai";
import type { Settings } from "../lib/settings";
import type { ParsedInput } from "../lib/syntax";
import ConfirmPopover from "./ConfirmPopover";
import { DURATION_PRESETS } from "./InboxPanel";
import QuickAdd from "./QuickAdd";
import SeriesScopeAsk from "./SeriesScopeAsk";
import SubtaskList from "./SubtaskList";
import WhenPicker from "./WhenPicker";
import "./ScheduleCanvas.css";

// 时间轴节拍：每小时 80px（固定）。区间（START/END）、每日工时（WORK_HOURS）与午休块
// 由设置驱动（settings prop 注入，组件顶部派生），小时刻度取区间内整数点。
// export 供桌面小部件（widget 窗口）复用同一节拍与下面几个纯函数/算法
export const HOUR_HEIGHT = 80;

// 小时（小数）→ "HH:MM"
export const fmtTime = (hour: number) =>
  `${String(Math.floor(hour)).padStart(2, "0")}:${String(Math.round((hour % 1) * 60)).padStart(2, "0")}`;

// 小时（小数）→ 负载指示器耗时文本："4h" / "4.5h"（一位小数四舍五入、去尾零——
// 两位小数过细无决策价值，2026-11-10 用户要求收窄；主窗口与小部件共用单源）
export const fmtLoadHours = (h: number) => `${Number(h.toFixed(1))}h`;

// 浮层实例当前排期 → WhenPicker 值（"YYYY-MM-DDTHH:mm"，manualPickToRelative
// 互逆；同 InboxPanel scheduledPickValue 口径）——打开浮层时装载改期草稿基准
const detailPickValue = (t: ScheduledTask) => {
  const d = new Date();
  d.setDate(d.getDate() + t.dateOffset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}T${fmtTime(t.start)}`;
};

// 某日日程负载档位（周/月视图每日指示灯圆点）：所有日程总耗时 / 一天总工作时间，
// 口径与分档同日视图负载指示器（≤50% 绿、50–80% 橙、>80% 红；无日程 = 0% → 绿）；
// 耗时按 end ?? start+1（与卡片几何一致）；全天/跨天行不占时段预算（占天语义在
// busy 避让与全天条呈现，不计小时负载——2026-11-06 批 B）
export const dayLoadTone = (
  events: ScheduledTask[],
  workHours: number,
): "green" | "orange" | "red" => {
  let total = 0;
  for (const e of events) {
    if (e.allDay) continue;
    total += (e.end ?? e.start + 1) - e.start;
  }
  const rate = total / workHours;
  return rate > 0.8 ? "red" : rate > 0.5 ? "orange" : "green";
};

// 全天/跨天行的覆盖天数：end 为跨整天数×24（后端归一），空 = 单日
const allDaySpan = (e: ScheduledTask) =>
  e.end ? Math.max(1, Math.round(e.end / 24)) : 1;

// 月视图紧凑卡几何（px）：格内日期数字区高度 / 卡高 / 卡间纵距（格窄单行卡，纵排无泳道）
const MONTH_DATE_H = 22;
const MONTH_CARD_H = 22;
const MONTH_CARD_GAP = 3;

// 议程视图滚动窗口天数（批 C）：viewedDate 起 [0, N) 按日分组单列，翻页按天平移窗口
const AGENDA_DAYS = 14;

// 日视图全天条行高步进（px，四次视觉批）：条高 36（步进含 4px 行距）——约为普通
// 一小时卡（80px）的一半弱，保持细条区分感的同时不与普通卡失衡（26px 曾过细）
const ALLDAY_ROW_H = 40;

// 周视图列内全天条行高步进（px，二次根修）：单行紧凑条纵排步进（条高 = 步进-4）
const WEEK_ALLDAY_H = 30;

// 日程详情浮层宽度（px）：定位翻转与右缘判定用
const DETAIL_W = 300;

// 重叠日程泳道布局：相互连通的重叠日程划为一个簇，簇内按到达顺序贪心分列
// （每个日程进最左一个"该列尾部与本日程不重叠"的列），渲染时按 列号/簇列数 平分宽度并排。
// 恰好首尾相接（A.end === B.start）不算重叠，可复用同一列
export function assignLanes<T extends { start: number; end?: number }>(
  events: T[],
): Map<T, { column: number; columns: number }> {
  const lanes = new Map<T, { column: number; columns: number }>();
  if (events.length === 0) return lanes;

  const sorted = [...events].sort((a, b) => a.start - b.start);
  const eventEnd = (event: T) => event.end ?? event.start + 1; // 无结束默认占 1 小时
  let cluster: { event: T; column: number }[] = [];
  let clusterEnd = -Infinity;
  let columnEnds: number[] = []; // 每列当前占用尾部，索引即列号

  const flushCluster = () => {
    for (const { event, column } of cluster) {
      lanes.set(event, { column, columns: columnEnds.length });
    }
    cluster = [];
    columnEnds = [];
  };

  for (const event of sorted) {
    const end = eventEnd(event);
    if (cluster.length > 0 && event.start >= clusterEnd) {
      flushCluster();
      clusterEnd = end;
    } else {
      clusterEnd = Math.max(clusterEnd, end);
    }
    let column = columnEnds.findIndex((tail) => event.start >= tail);
    if (column === -1) {
      column = columnEnds.length;
      columnEnds.push(end);
    } else {
      columnEnds[column] = end;
    }
    cluster.push({ event, column });
  }
  flushCluster();
  return lanes;
}

// 泳道列 → 事件区（left:64 + right:20 双锚定）内的横向定位：按列平分宽度，列间留 2px 缝，随中栏伸缩
export const laneLeft = ({ column, columns }: { column: number; columns: number }) =>
  `calc(64px + (100% - 84px) * ${column} / ${columns}${column > 0 ? " + 2px" : ""})`;
export const laneRight = ({ column, columns }: { column: number; columns: number }) =>
  `calc(20px + (100% - 84px) * ${columns - 1 - column} / ${columns}${column < columns - 1 ? " + 2px" : ""})`;

// 周视图泳道列 → 天列内（0 到 100% 双锚定）的横向定位：同 laneLeft 规则，锚定改为列内百分比
const weekLaneLeft = ({ column, columns }: { column: number; columns: number }) =>
  `calc(100% * ${column} / ${columns}${column > 0 ? " + 2px" : ""})`;
const weekLaneRight = ({ column, columns }: { column: number; columns: number }) =>
  `calc(100% * ${columns - 1 - column} / ${columns}${column < columns - 1 ? " + 2px" : ""})`;

// 小时刻度与横向网格线（日/周视图共用：刻度居左，网格线跨事件区）；
// hours 为区间内整数钟点列，startHour 为区间起点（y 公式的零点）
export function renderHourMarks(hours: number[], startHour: number) {
  return hours.map((hour) => (
    <Fragment key={hour}>
      <span
        className="timeline-tick"
        style={{ top: (hour - startHour) * HOUR_HEIGHT }}
      >
        {String(hour).padStart(2, "0")}:00
      </span>
      <div
        className="timeline-grid"
        style={{ top: (hour - startHour) * HOUR_HEIGHT }}
      />
    </Fragment>
  ));
}

// 星期与 ISO 周工具（查看日期是组件 state，文本在组件内派生）
const WEEKDAY_NAMES = ["日", "一", "二", "三", "四", "五", "六"];

function getISOWeek(date: Date): number {
  const d = new Date(
    Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()),
  );
  const dayNum = d.getUTCDay() || 7; // 周日记为 7，周四对齐 ISO 周首日
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

// 两日期是否同一天（Date 不可用 === 比较，比 y/m/d 三元组）
function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

// 公历 → 月格日期行农历小字与休/班角标：
// 标签优先级 = 农历节日（除夕/春节/中秋）→ 假期首日的公历节日名（元旦节/国庆节…，day===target
// 仅首日显示，避免长假每天都挂节日名）→ 节气（白露/秋分…）→ 初一显农历月名（八月/闰六月）→ 农历日名（初十/廿九）；
// 角标 = 调休补班"班" / 法定假或周末"休" / 普通工作日无（内置数据至 2026，之后年份退化为纯周末判定）
function lunarDayMeta(date: Date): { label: string; mark: "" | "休" | "班" } {
  const y = date.getFullYear();
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const lunar = Solar.fromYmd(y, m, d).getLunar();
  const festival = lunar.getFestivals()[0];
  const holiday = HolidayUtil.getHoliday(y, m, d);
  const label =
    festival ??
    (holiday && !holiday.isWork() && holiday.getDay() === holiday.getTarget()
      ? holiday.getName()
      : undefined) ??
    lunar.getCurrentJieQi()?.getName() ??
    (lunar.getDayInChinese() === "初一"
      ? `${lunar.getMonthInChinese()}月`
      : lunar.getDayInChinese());
  const mark: "" | "休" | "班" = holiday?.isWork()
    ? "班"
    : holiday || date.getDay() === 0 || date.getDay() === 6
      ? "休"
      : "";
  return { label, mark };
}

// onExpandCopilot：右栏折叠时由 App 传入，存在即在头部"日/周/月"右侧渲染展开按钮；expandIcon 换图标（默认 Sparkles，收件箱页传 Inbox、项目页传 Folder）；expandDisabled 置灰（AI 总闸关闭，仅日程页）；
// events / onScheduleChange：已排期任务与统一排期写回回调都由 App 持有（统一数据源），组件只渲染与计算落点；
// settings：应用设置（时间轴区间 / 每日工时 / 午休块）——刻度、网格、拖拽 clamp、负载基准、
//   午休建议块与 QuickAdd 钟点合法域全部由它派生；
// projects / onUpdateTask / onDeleteTask：详情浮层的项目行取色与字段编辑/删除写回（回调同为 App 持有）；
// onVisibleRangeChange：视图实际覆盖的日期范围（绝对日序号）变化时上报，App 据此按需扩展历史任务窗口
// ghosts：AI 排期草案的幽灵方块（阶段二；仅日视图渲染查看日命中项，参与泳道分列防与真实卡重叠）
function ScheduleCanvas({
  // 外层 style 注入（2026-11 统计页全宽改造）：统计页隐藏画布走 display:none
  //（照 CopilotPanel 常驻先例，保视图态与跳转订阅），undefined 时走 CSS 原样式
  style,
  onExpandCopilot,
  // 展开按钮置灰（2026-10-21 五点修缮）：日程页 AI 总闸关闭时按钮灰化——点击仍展开
  // 面板（面板渲染「AI 助手已关闭」引导卡 + 输入禁用，即"点击提示开启"的落地）；
  // 收件箱/项目页展开的是收件箱面板，与 AI 无关，不置灰
  expandDisabled,
  expandIcon,
  // 助手未读徽标（2026-11-04 通知分轨）：AI 产物走收件箱轨，入口按钮计数提示
  //（>0 渲染角标，展开面板由 App 清零）
  expandBadge,
  events,
  settings,
  onScheduleChange,
  onSuggestDrop,
  projects,
  onUpdateTask,
  onDeleteTask,
  onVisibleRangeChange,
  onCreateTask,
  ghosts,
  // 全量任务（2026-11-10 批 2 纪要回看）：详情浮层按 sourceKey 前缀筛该会议的
  // 纪要行动项（未排期待办不在 events 已排期子集里）；widget 精简口径不传则区块不显示
  tasks,
  // 项目筛选（2026-11-10）：项目页"已排期"开关联动——四视图渲染层只画该项目
  // 日程（occurrencesOn/allDayOn 单点过滤，负载指示随之显示项目负载）；拖拽落点
  // 的智能放置仍用全量数据（monthDropHour 直读 events），防"看似空实则已占"的
  // 假空档。头部 chip 由本组件渲染，✕ 走 onClearProjectFilter
  projectFilter,
  onClearProjectFilter,
}: {
  style?: CSSProperties;
  onExpandCopilot?: () => void;
  expandDisabled?: boolean;
  expandIcon?: ReactNode;
  expandBadge?: number;
  events: ScheduledTask[];
  settings: Settings;
  onScheduleChange: (
    taskId: string,
    dateOffset: number,
    start: number,
    end: number | undefined,
    // 重复系列范围三问（2026-11-12）：this/following 分支的实例日与模式
    opts?: { mode: "this" | "following"; date: string },
  ) => void;
  // 数据源建议条目拖入直排（2026-10-24）：拖拽载荷带 suggest 键时分流到这——
  // 落点即排期时间建任务 + 结算，不查任务表（App 持有）
  onSuggestDrop?: (
    todo: DraggedTodoWithSuggest,
    dateOffset: number,
    start: number,
  ) => Promise<void>;
  // 快捷录入框建任务（App 负责持久化与未知项目顺手创建；失败 reject 由输入框保留文本）
  onCreateTask: (draft: ParsedInput) => Promise<unknown>;
  // 项目筛选（null = 全量）：App 态，触发 = 项目页"已排期"开关打开（受控开关
  // 同步 effect 见 App）；解除 = 关开关 / 离开项目页 / chip ✕
  projectFilter?: string | null;
  onClearProjectFilter?: () => void;
  // 项目列表：详情浮层项目行的色点取色用
  projects: Project[];
  // 日程字段编辑（patch 合并：title/note/priority），与收件箱编辑区共用口径
  onUpdateTask: (
    taskId: string,
    patch: Partial<Task>,
    // 重复系列编辑三问（Clone & Override）：this = 实体化单次例外、following = 拆分系列
    //（均需 date = 被编辑实例日）、all/缺省 = 直接更新任务本体
    opts?: { mode: "this" | "following" | "all"; date: string },
  ) => void;
  // 删除日程写回：重复系列可带 mode（this = 墓碑挖空单次实例 / following = UNTIL 截断
  // 删及后续，均须 date = 被删实例日）；不带 opts = 整任务删（后端按生命周期判定物理删/软删）
  onDeleteTask: (
    taskId: string,
    opts?: { mode: "this" | "following" | "all"; date: string },
  ) => void;
  // 当前视图覆盖的日期范围上报（fromDay/toDay 为 epoch day）：日=单天、周=周一~周日、月=42 格首尾
  onVisibleRangeChange?: (fromDay: number, toDay: number) => void;
  // AI 草案幽灵方块（dateOffset + start/end 小时小数；仅日视图查看日命中项渲染）
  ghosts?: GhostBlock[];
  // 全量任务（含未排期）：纪要回看区块筛 "minutes:<会议id>" 溯源行动项用
  tasks?: Task[];
}) {
  // 当前查看的日期（初始为启动当天；日程内容仍是 mock，切换日期不换内容）
  const [viewedDate, setViewedDate] = useState(() => new Date());
  // 视图模式（初始 day；启动后设置到达时经下方 effect 对齐到"启动默认视图"）
  const [viewMode, setViewMode] = useState<"day" | "week" | "month" | "agenda">("day");
  // 启动默认视图：设置到达后对齐一次（挂载首跑为默认 day 的 no-op）。此后仅当
  // 用户在设置页改默认视图时跟随跳转（即时预览），手动切换视图不受影响——
  // 依赖只在 defaultView 变化时重跑，用户切视图不动该设置
  useEffect(() => {
    setViewMode(settings.defaultView);
  }, [settings.defaultView]);
  // 当前时刻（驱动"现在"红线，分钟精度，每分钟自更新）
  const [now, setNow] = useState(() => new Date());
  const scrollRef = useRef<HTMLDivElement>(null);

  // —— 时间轴参数（设置驱动，小时小数口径）——
  const START = settings.dayStart;
  const END = settings.dayEnd;
  const WORK_HOURS = settings.workHours;
  // 区间内整数钟点列（30 分钟粒度区间自动对齐：8.5–22.5 → 9..22）
  const hours = Array.from(
    { length: Math.floor(END) - Math.ceil(START) + 1 },
    (_, i) => Math.ceil(START) + i,
  );
  // 时间 → 时间轴 y 坐标（px）；时间轴内容总高（.timeline 的 height = 内容高 + 底部余量 40）
  const y = (hour: number) => (hour - START) * HOUR_HEIGHT;
  const axisHeight = (END - START) * HOUR_HEIGHT;
  // 午休建议块（设置驱动；关闭时 null——不渲染、不参与泳道分列）
  const lunch = settings.lunchEnabled
    ? { start: settings.lunchStart, end: settings.lunchEnd }
    : null;

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  const isToday = isSameDay(viewedDate, now);
  const dateText = `${viewedDate.getFullYear()}年${viewedDate.getMonth() + 1}月${viewedDate.getDate()}日`;
  // 日视图头部：农历/节气/节日追加在副行末（方案 1：星期 · 周数 · 农历，口径同周/月视图 lunarDayMeta）
  const dayMeta = lunarDayMeta(viewedDate);
  const weekText = `星期${WEEKDAY_NAMES[viewedDate.getDay()]} · 第 ${getISOWeek(viewedDate)} 周 · ${dayMeta.label}`;
  // "现在"红线：当前时刻 → y 坐标与 HH:MM 文本（时间轴区间外为负/越界值，自然被滚动区剪裁）
  const nowTop =
    (now.getHours() + now.getMinutes() / 60 - START) * HOUR_HEIGHT;
  const nowLabel = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  // 事件结束时刻 = 查看日 0 点 + end 小时，与 now 比较即覆盖三种查看日：过去日期全天已过、未来日期全未过、今天按时段
  const viewedDayStart = new Date(
    viewedDate.getFullYear(),
    viewedDate.getMonth(),
    viewedDate.getDate(),
  ).getTime();
  const isPastEvent = (endHour: number) =>
    viewedDayStart + endHour * 3_600_000 <= now.getTime();
  // 查看日相对当天的偏移（天），用于过滤已排期任务（events prop）
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const viewedOffset = Math.round((viewedDayStart - todayStart) / 86_400_000);
  // 快捷录入创建已排期任务后跳转查看该日：日/周/月三视图都从 viewedDate 派生，
  // 一次设置全适配（原生构造器跨月/跨年自动进位）
  const jumpToDate = (dateOffset: number) => {
    const base = new Date(now.getFullYear(), now.getMonth(), now.getDate() + dateOffset);
    setViewedDate(base);
  };
  // 项目筛选渲染源（2026-11-10）：过滤只作用于"画哪些卡"；**计算恒全量**——
  // 负载指示器/负载灯/智能放置（monthDropHour 直读 events）不随筛选变（筛选≠
  // 删除，容量信号须反映真实总负载，否则筛后大片"空闲"误导排期）
  const viewEvents = projectFilter
    ? events.filter((e) => e.project === projectFilter)
    : events;
  // offset 日的实例收集：普通任务原样引用；重复任务在匹配日生成实例副本（id 保持
  // 系列 id，dateOffset 覆写为实例日——drag/浮层对同系列多实例的定位都依赖它）
  const collectOn = (src: ScheduledTask[], offset: number): ScheduledTask[] => {
    const out: ScheduledTask[] = [];
    for (const e of src) {
      if (!occursOn(e, offset)) continue;
      out.push(e.dateOffset === offset ? e : { ...e, dateOffset: offset });
    }
    return out;
  };
  // 全量口径（负载指示器/负载灯消费）：不受项目筛选影响
  const allOccurrencesOn = (offset: number) => collectOn(events, offset);
  // 渲染口径（四视图卡片消费）：项目筛选态下只含筛选项目
  const occurrencesOn = (offset: number) => collectOn(viewEvents, offset);
  // 查看日的全部时间轴块（午休建议块 + 日程）与泳道分列结果，重叠块并排显示；
  // 午休关闭时不参与分列（列宽即时回收给日程）。
  // 全天/跨天行不进时间轴（dayTimeline 过滤），单独渲染在轴上方的全天条区
  const dayEvents = occurrencesOn(viewedOffset);
  const dayTimeline = dayEvents.filter((e) => !e.allDay);
  // AI 幽灵方块（查看日命中项；参与泳道分列，防与真实卡重叠——同午休建议块口径）
  const dayGhosts = ghosts?.filter((g) => g.dateOffset === viewedOffset) ?? [];
  const lanes = assignLanes([...(lunch ? [lunch] : []), ...dayTimeline, ...dayGhosts]);
  const lunchLane = lunch ? lanes.get(lunch) : undefined;

  // 全天/跨天条在 offset 日的覆盖判定：系列实例（occursOn 命中）+ 跨天单次条
  // 中段日（occurrencesOn 只在起始日命中，中间日由此补齐；副本 dateOffset 覆写
  // 为实例日——浮层定位与拖拽锚定同口径）
  const allDayOn = (offset: number): ScheduledTask[] => {
    const out: ScheduledTask[] = [];
    for (const e of viewEvents) {
      if (!e.allDay) continue;
      if (
        e.recurrence
          ? occursOn(e, offset)
          : e.dateOffset <= offset && offset < e.dateOffset + allDaySpan(e)
      ) {
        out.push(e.dateOffset === offset ? e : { ...e, dateOffset: offset });
      }
    }
    return out;
  };
  const dayBars = allDayOn(viewedOffset);

  // 日视图负载统计（"今天"下方的负载指示器）：该日已排期日程按优先级累加耗时
  //（无 end 按占 1 小时，与卡片几何口径一致；不含午休建议块——非日程）；
  // 负载率 = 所有日程总耗时 / 一天总工作时间（>80% 进度条淡红描边、文字变红）；
  // 进度条比例分母 = max(总耗时, 8h)：不足 8h 以 8h 为分母留灰段、超 8h 以总耗时
  // 归一化恒填满（各优先级相对占比不失真，低优先级不被溢出裁掉）。
  // **全量口径**（2026-11-10 修订）：负载是容量信号非视图内容统计——项目筛选
  // 只改画哪些卡，被滤掉的日程负载照算，防筛后大片"空闲"误导排期
  const loadDayEvents = allOccurrencesOn(viewedOffset);
  const loadTimeline = loadDayEvents.filter((e) => !e.allDay);
  const prioHours = { high: 0, medium: 0, low: 0 } as Record<
    Task["priority"],
    number
  >;
  for (const e of loadTimeline) prioHours[e.priority] += (e.end ?? e.start + 1) - e.start;
  const loadTotal = prioHours.high + prioHours.medium + prioHours.low;
  const loadRate = loadTotal / WORK_HOURS;
  const loadDenom = Math.max(loadTotal, WORK_HOURS);

  // 周视图派生：所在周的周一为起点（getDay 周日=0，(day+6)%7 即"周一列"所需的回退天数）
  const weekStart = new Date(
    viewedDate.getFullYear(),
    viewedDate.getMonth(),
    viewedDate.getDate() - ((viewedDate.getDay() + 6) % 7),
  );
  const weekEndDate = new Date(
    weekStart.getFullYear(),
    weekStart.getMonth(),
    weekStart.getDate() + 6,
  );
  // 周标题：同月省略第二个月份（如 9月7日 – 13日），跨月/跨年则写全
  const weekTitle =
    weekStart.getMonth() === weekEndDate.getMonth()
      ? `${weekStart.getMonth() + 1}月${weekStart.getDate()}日 – ${weekEndDate.getDate()}日`
      : `${weekStart.getMonth() + 1}月${weekStart.getDate()}日 – ${weekEndDate.getMonth() + 1}月${weekEndDate.getDate()}日`;
  const weekSub = `第 ${getISOWeek(weekStart)} 周`;
  // 7 列（周一→周日）各自相对当天的偏移与泳道分列（周视图不含午休块）
  const weekOffsets = Array.from({ length: 7 }, (_, i) =>
    Math.round(
      (new Date(
        weekStart.getFullYear(),
        weekStart.getMonth(),
        weekStart.getDate() + i,
      ).getTime() -
        todayStart) /
        86_400_000,
    ),
  );
  // 展开实例一次计算共用：assignLanes 的 Map 以对象引用为 key，派生与渲染
  // 各自调用 occurrencesOn 会生成不同副本，weekLanes[i].get(event) 将取不到。
  // 全天/跨天条不进列内时间轴（weekDayBars 单独渲染在周头下方的全天行）
  const weekDayEvents = weekOffsets.map((offset) =>
    occurrencesOn(offset).filter((e) => !e.allDay),
  );
  const weekDayBars = weekOffsets.map((offset) => allDayOn(offset));
  const weekLanes = weekDayEvents.map((dayEvents) => assignLanes(dayEvents));
  const weekContainsToday = weekOffsets.includes(0);
  // 周视图降淡：按各列自己的日期判定（今天 0 点 + 列偏移 + end 小时 ≤ now）
  const isPastOnDay = (offset: number, endHour: number) =>
    todayStart + offset * 86_400_000 + endHour * 3_600_000 <= now.getTime();

  // 月视图派生：首格 = 当月 1 号所在周的周一（1 号当天或之前最近的周一，回退规则同周视图）；
  // 标题为年月，副标题为当月覆盖的 ISO 周范围（首尾同周退化为单值）
  const monthFirst = new Date(viewedDate.getFullYear(), viewedDate.getMonth(), 1);
  const monthGridStart = new Date(
    monthFirst.getFullYear(),
    monthFirst.getMonth(),
    monthFirst.getDate() - ((monthFirst.getDay() + 6) % 7),
  );
  const monthTitle = `${viewedDate.getFullYear()}年${viewedDate.getMonth() + 1}月`;
  const firstWeek = getISOWeek(monthFirst);
  const lastWeek = getISOWeek(
    new Date(viewedDate.getFullYear(), viewedDate.getMonth() + 1, 0),
  );
  const monthSub =
    firstWeek === lastWeek
      ? `第 ${firstWeek} 周`
      : `第 ${firstWeek} – ${lastWeek} 周`;
  // 议程视图派生（批 C）：viewedDate 起滚动 14 天窗口；标题同周视图跨月口径
  const agendaEndDate = new Date(
    viewedDate.getFullYear(),
    viewedDate.getMonth(),
    viewedDate.getDate() + AGENDA_DAYS - 1,
  );
  const agendaTitle =
    viewedDate.getMonth() === agendaEndDate.getMonth()
      ? `${viewedDate.getMonth() + 1}月${viewedDate.getDate()}日 – ${agendaEndDate.getDate()}日`
      : `${viewedDate.getMonth() + 1}月${viewedDate.getDate()}日 – ${agendaEndDate.getMonth() + 1}月${agendaEndDate.getDate()}日`;
  const agendaSub = `日程 · ${AGENDA_DAYS} 天`;
  const agendaDays = Array.from({ length: AGENDA_DAYS }, (_, i) => {
    const date = new Date(
      viewedDate.getFullYear(),
      viewedDate.getMonth(),
      viewedDate.getDate() + i,
    );
    return {
      date,
      offset: Math.round((date.getTime() - todayStart) / 86_400_000),
    };
  });
  // 42 格（7 列 × 6 行，周一→周日）：格日期、相对当天偏移（同 weekOffsets 口径）、
  // 当月/今天标记、日期行标签与休/班角标、格内日程（按开始时刻纵排，格窄无泳道并排）
  const monthCells = Array.from({ length: 42 }, (_, i) => {
    const date = new Date(
      monthGridStart.getFullYear(),
      monthGridStart.getMonth(),
      monthGridStart.getDate() + i,
    );
    const offset = Math.round((date.getTime() - todayStart) / 86_400_000);
    const { label, mark } = lunarDayMeta(date);
    return {
      date,
      offset,
      inMonth:
        date.getMonth() === viewedDate.getMonth() &&
        date.getFullYear() === viewedDate.getFullYear(),
      isToday: date.getTime() === todayStart,
      lunar: label,
      mark,
      // 全天/跨天条置顶（占天事件优先于钟点事件；跨天中段由 allDayOn 补齐覆盖）
      events: [...allDayOn(offset), ...occurrencesOn(offset).filter((e) => !e.allDay)].sort(
        (a, b) => Number(!!b.allDay) - Number(!!a.allDay) || a.start - b.start,
      ),
    };
  });

  // 视图覆盖范围上报（epoch day）：App 据此判断是否需要扩展历史任务窗口。
  // 依赖只放 primitive——todayEpochDay 只在跨午夜时变化（60 秒的 now tick 不触发
  // 重跑）；onVisibleRangeChange 不入依赖（App 的回调为纯判定、重跑无副作用，
  // 引用变化不应触发上报——省略惯例同下方 closeDetail effect）
  const todayEpochDay = epochDayOf(now);
  useEffect(() => {
    if (!onVisibleRangeChange) return;
    const [fromDay, toDay] =
      viewMode === "day"
        ? [epochDayOf(viewedDate), epochDayOf(viewedDate)]
        : viewMode === "week"
          ? [epochDayOf(weekStart), epochDayOf(weekEndDate)]
          : viewMode === "agenda"
            ? [epochDayOf(viewedDate), epochDayOf(viewedDate) + AGENDA_DAYS - 1]
            : [epochDayOf(monthGridStart), epochDayOf(monthGridStart) + 41];
    onVisibleRangeChange(fromDay, toDay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, viewedDate, todayEpochDay]);

  // 日程拖动/拉伸：pointer capture 后 move/up 都路由回原卡片；
  // 拖动=临时时刻垂直移动（15 分钟吸附，时长不变），周视图内还叠加水平位移 dayShift（跨天），
  // 月视图拖动=水平+垂直位移换算跨格天数（无时间轴，不改时刻）；拉伸=下边框把手调结束时间（仅垂直）；
  // 泳道列仍按原时刻，松手写回新时刻/新天序才触发整体重排
  const [drag, setDrag] = useState<{
    eventId: string; // 按 id 定位被拖任务（events 为 App 派生数组，存对象引用会失效）
    sourceOffset: number; // 拖动起始实例所在日（重复系列多实例并存时按 id + 实例日定位）
    mode: "move" | "resize";
    start: number;
    end: number;
    dayShift: number; // 跨天写入的天数偏移：周视图=跨天列数、月视图=列 + 7×行，日视图/拉伸恒 0
  } | null>(null);
  const dragBaseRef = useRef<{
    mode: "move" | "resize";
    pointerY0: number;
    pointerX0: number; // 点击判定（pointerup 位移 <4px）用，所有模式都记录；跨天/跨格换算仅周/月视图走
    origStart: number;
    origEnd: number;
    allDay?: boolean; // 全天条（周视图可拖）：只算天级 dayShift，小时吸附/clamp 不适用
    colWidth?: number; // 以下两项仅周视图填充（跨天拖动基准）
    origColIndex?: number; // 周视图：源天列 0..6
    cellHeight?: number; // 以下两项仅月视图填充（跨格拖动基准）
    origCellIndex?: number; // 月视图：源格 0..41
  } | null>(null);

  // —— 日程详情浮层 ——
  // detail 为浮层锚点（任务 id + 被点实例日 + fixed 定位坐标，点击卡片时算出）；任务本体经 id 从 events
  // 派生（已删则不渲染；重复系列的编辑/删除作用于整个系列，浮层日期显示的是被点实例日）
  const [detail, setDetail] = useState<{ id: string; offset: number; left: number; top: number } | null>(null);
  // 备注草稿（打开时装载，失焦/关闭时提交）
  const [noteDraft, setNoteDraft] = useState("");
  // 标题内联编辑（双击进入）；titleDraft 为草稿
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // Enter/Esc 主动退出标题编辑后，吞掉 input 卸载可能触发的 blur，防二次提交（口径同收件箱）
  const skipTitleBlur = useRef(false);
  // 浮层根节点：document 点外关闭时判断落点是否在浮层内
  const detailRef = useRef<HTMLDivElement>(null);
  // 重复规则面板展开态（每次打开浮层默认收起）
  const [recurrenceOpen, setRecurrenceOpen] = useState(false);
  // 时间编辑行（2026-11-11 四处统一批）：WhenPicker 药丸开合 + 改期草稿
  //（"YYYY-MM-DDTHH:mm"，打开浮层时装载当前排期时刻）——与重复面板互斥
  const [whenOpen, setWhenOpen] = useState(false);
  const [whenDraft, setWhenDraft] = useState("");
  // 重复系列范围三问（2026-11-12 拖拽三问批）：拖拽/拉伸/agenda 拖动重复实例
  // 落定后暂存拖拽结果并在落点弹气泡（绝不静默改全系列）。
  // move 语义：fromOffset = 原实例日（this 挖空/截断锚）；newOffset = 仅此/及后续
  // 的目标日（例外行/新系列落哪天）；allOffset（缺省 = newOffset）=「所有」分支的
  // 重锚定基准日——纯调时刻/改耗时场景两者不同（this/following 用实例日、all 保
  // 主行基准），跨天拖动/浮层改期两者相同（都是新日）
  const [scopeAsk, setScopeAsk] = useState<{
    x: number;
    y: number;
    move: {
      id: string;
      fromOffset: number;
      newOffset: number;
      allOffset?: number;
      start: number;
      end: number | undefined;
    };
  } | null>(null);
  // 重复系列待应用编辑（三问缓冲）：浮层内改备注/标题/优先级先聚合到此，用户在应用
  // 范围气泡选"仅此 / 及后续 / 所有"才真正提交；有未决编辑时点外/Esc/X 被 closeDetail
  // 拦截不关浮层（防丢稿），仅滚动/切视图（锚点失效）才真正丢弃；单次日程与例外实体
  //（无 recurrence）不经此缓冲直发；重复规则编辑也无单次语义直发
  const [pendingEdit, setPendingEdit] = useState<Partial<Task> | null>(null);
  // 会后纪要录入态（2026-11-10）：已结束日程的浮层内联入口——粘贴纪要原文提交
  // 提炼（minutes 卡落助手面板）；浮层重开即复位（同 noteDraft 惯例）
  const [minutesOpen, setMinutesOpen] = useState(false);
  const [minutesText, setMinutesText] = useState("");
  const [minutesBusy, setMinutesBusy] = useState(false);
  const [minutesError, setMinutesError] = useState<string | null>(null);

  const onCardPointerDown = (e: React.PointerEvent<HTMLDivElement>, event: ScheduledTask) => {
    if (e.button !== 0) return; // 仅主键
    const origEnd = event.end ?? event.start + 1;
    // 周/月视图：卡片父级即天列/月格，记录尺寸与源索引供跨天（水平）/跨格（水平+垂直）换算（日视图无此上下文）；
    // 周视图全天条的父级是 .week-allday-cell——同按周列口径供 dayShift（真机批：周全天可拖，与月视图对齐）
    const cell = e.currentTarget.parentElement as HTMLElement | null;
    const inWeekCol = cell?.classList.contains("week-col") ?? false;
    const inWeekAllDay = cell?.classList.contains("week-allday-cell") ?? false;
    const inMonthCell = cell?.classList.contains("month-cell") ?? false;
    const cross = (inWeekCol || inWeekAllDay || inMonthCell) && cell; // 三分支互斥：同一卡片父级只属一种
    const rect = cross ? cell.getBoundingClientRect() : undefined;
    const cellIndex = cross
      ? Array.from(cell.parentElement!.children).indexOf(cell)
      : undefined;
    dragBaseRef.current = {
      mode: "move",
      pointerY0: e.clientY,
      pointerX0: e.clientX,
      origStart: event.start,
      origEnd,
      allDay: event.allDay || undefined,
      colWidth: rect?.width,
      origColIndex: inWeekCol || inWeekAllDay ? cellIndex : undefined,
      cellHeight: inMonthCell ? rect?.height : undefined,
      origCellIndex: inMonthCell ? cellIndex : undefined,
    };
    setDrag({ eventId: event.id, sourceOffset: event.dateOffset, mode: "move", start: event.start, end: origEnd, dayShift: 0 });
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  // 下边框把手：拉伸结束时间（无结束时间的日程由此设置 end），事件冒泡回卡片复用 move/up
  const onResizePointerDown = (e: React.PointerEvent<HTMLDivElement>, event: ScheduledTask) => {
    if (e.button !== 0) return; // 仅主键
    e.stopPropagation(); // 不触发卡片整体拖动
    const origEnd = event.end ?? event.start + 1;
    dragBaseRef.current = {
      mode: "resize",
      pointerY0: e.clientY,
      pointerX0: e.clientX,
      origStart: event.start,
      origEnd,
    };
    setDrag({ eventId: event.id, sourceOffset: event.dateOffset, mode: "resize", start: event.start, end: origEnd, dayShift: 0 });
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onCardPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const base = dragBaseRef.current;
    if (!base) return;
    // 月视图跨格拖动：水平+垂直位移换算列/行偏移（四舍五入吸附格，clamp 在 7×6 网格内），
    // dayShift = 列偏移 + 7 × 行偏移；无时间轴，start/end 原样保留，不进下方按小时换算的分支
    if (
      base.mode === "move" &&
      base.cellHeight !== undefined &&
      base.origCellIndex !== undefined
    ) {
      const origCol = base.origCellIndex % 7;
      const origRow = Math.floor(base.origCellIndex / 7);
      const colShift = Math.min(
        Math.max(Math.round((e.clientX - base.pointerX0) / base.colWidth!), -origCol),
        6 - origCol,
      );
      const rowShift = Math.min(
        Math.max(Math.round((e.clientY - base.pointerY0) / base.cellHeight), -origRow),
        5 - origRow,
      );
      const dayShift = colShift + 7 * rowShift;
      setDrag((d) => (d && d.dayShift !== dayShift ? { ...d, dayShift } : d));
      return;
    }
    // 全天条跨天拖动（周视图批修）：占天不占时段——只按列宽换算天级 dayShift，
    // 不进小时吸附/clamp 分支（start 恒 0、end 原样，写回后端归一维持全天语义）
    if (
      base.mode === "move" &&
      base.allDay &&
      base.colWidth !== undefined &&
      base.origColIndex !== undefined
    ) {
      const dayShift = Math.min(
        Math.max(Math.round((e.clientX - base.pointerX0) / base.colWidth), -base.origColIndex),
        6 - base.origColIndex,
      );
      setDrag((d) => (d && d.dayShift !== dayShift ? { ...d, dayShift } : d));
      return;
    }
    const deltaHours = (e.clientY - base.pointerY0) / HOUR_HEIGHT;
    if (base.mode === "move") {
      const snapped = Math.round((base.origStart + deltaHours) * 4) / 4; // 吸附 15 分钟刻度
      const duration = base.origEnd - base.origStart;
      const start = Math.min(Math.max(snapped, START), END - duration);
      // 周视图跨天：水平位移按列宽换算天列数（四舍五入吸附），clamp 在周一~周日
      const dayShift =
        base.colWidth !== undefined && base.origColIndex !== undefined
          ? Math.min(
              Math.max(
                Math.round((e.clientX - base.pointerX0) / base.colWidth),
                -base.origColIndex,
              ),
              6 - base.origColIndex,
            )
          : 0;
      setDrag((d) =>
        d && (d.start !== start || d.dayShift !== dayShift)
          ? { ...d, start, end: start + duration, dayShift }
          : d,
      );
    } else {
      const snapped = Math.round((base.origEnd + deltaHours) * 4) / 4;
      const end = Math.min(Math.max(snapped, base.origStart + 0.25), END); // 最短 15 分钟
      setDrag((d) => (d && d.end !== end ? { ...d, end } : d));
    }
  };

  // 拖拽冲突预览（2026-11-06 批 E2，提示性不阻止——保持「拖拽自由」现状语义）：
  // 拖动态实时判定被拖卡目标区间与目标日（sourceOffset+dayShift）既有日程的相交。
  // 半开区间、首尾相接不算重叠（assignLanes 同口径）；全天条不参与（占天不占时段）；
  // 同 id 系列实例跳过（拖到自家另一实例日不算冲突）。派生而非入 drag state——
  // move 高频 setDrag 不膨胀，松手自然消失
  const dragConflict =
    drag !== null &&
    (() => {
      const targetOffset = drag.sourceOffset + drag.dayShift;
      for (const t of occurrencesOn(targetOffset)) {
        if (t.allDay || t.id === drag.eventId) continue;
        const tEnd = t.end ?? t.start + 1;
        if (drag.start < tEnd && t.start < drag.end) return true;
      }
      return false;
    })();

  const onCardPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const base = dragBaseRef.current;
    if (!base) return;
    const current = drag;
    dragBaseRef.current = null;
    setDrag(null);
    // 纯点击（真实 pointerup 且总位移 <4px；pointercancel 触摸打断不算）→ 打开详情浮层。
    // 位移阈值远小于 15 分钟吸附粒度（20px/刻度），点击必然无写回，直接返回；
    // rect 在 DOM 更新前同步读取，卡片仍在一格未动的原位
    if (current && e.type === "pointerup") {
      const moved = Math.hypot(e.clientX - base.pointerX0, e.clientY - base.pointerY0);
      if (moved < 4) {
        openDetail(current.eventId, current.sourceOffset, e.currentTarget.getBoundingClientRect());
        return;
      }
    }
    const src = current
      ? events.find((e) => e.id === current.eventId)
      : undefined;
    const changed =
      current !== null &&
      src !== undefined &&
      (base.mode === "move"
        ? current.start !== src.start || current.dayShift !== 0
        : current.end !== (src.end ?? src.start + 1));
    if (current && src && changed) {
      const { mode, start, end, dayShift, sourceOffset } = current;
      const newEnd = mode === "move" && src.end === undefined ? undefined : end;
      // 重复系列拖拽/拉伸 → 范围三问（2026-11-12 拖拽三问批，Google/Apple/Outlook
      // 共识）：绝不静默改全系列——暂存拖拽结果，落点弹「仅此/及后续/所有」气泡，
      // 选范围后分流提交（all = 重锚定；this/following = 例外实体化/系列拆分，
      // 后端 _materialize_exception/_split_series 支持时间字段覆盖）。非重复任务
      // 照旧直发
      if (src.recurrence) {
        setScopeAsk({
          x: e.clientX + 12,
          y: e.clientY + 12,
          move: {
            id: src.id,
            fromOffset: sourceOffset,
            // this/following 目标日：跨天 = 实例日+位移；纯调时刻 = 被拖实例日
            newOffset: dayShift !== 0 ? sourceOffset + dayShift : sourceOffset,
            // all 分支基准日：跨天 = 重锚到新日；纯调时刻/拉伸 = 保主行原基准
            //（weekly 非基准实例误写实例日会丢早期实例）
            allOffset: dayShift !== 0 ? sourceOffset + dayShift : src.dateOffset,
            start,
            end: newEnd,
          },
        });
        return;
      }
      // 跨天拖动 = 实例日 + 位移重锚定系列起点；纯调时间（dayShift 0）保原基准——
      // weekly 在非基准实例调时间若误写实例日，基准日后移会丢早期实例；
      // 非重复任务 sourceOffset === dateOffset，两分支等价旧公式零回归
      onScheduleChange(
        src.id,
        dayShift !== 0 ? sourceOffset + dayShift : src.dateOffset,
        start,
        newEnd,
      );
    }
  };

  // 三问气泡选范围后的统一分流（拖拽/拉伸/agenda drop/浮层时间行共用）：
  // all = 重锚定直发（allOffset 基准）；this/following = 实例日 + 模式走 opts
  const applyScopePick = (
    pick: "this" | "following" | "all",
    move: {
      id: string;
      fromOffset: number;
      newOffset: number;
      allOffset?: number;
      start: number;
      end: number | undefined;
    },
  ) => {
    setScopeAsk(null);
    onScheduleChange(
      move.id,
      pick === "all" ? (move.allOffset ?? move.newOffset) : move.newOffset,
      move.start,
      move.end,
      pick === "all"
        ? undefined
        : { mode: pick, date: dateKeyOfOffset(move.fromOffset) },
    );
  };
  // —— 日程详情浮层（点击卡片弹出，三视图统一；时间只读，改时间走拖动/拉伸）——
  // 任务本体经 id 派生：已删除（浮层开着时别处删了同一任务）则条件渲染直接收起
  const detailTask = detail ? events.find((ev) => ev.id === detail.id) : undefined;
  const detailProject = detailTask?.project
    ? projects.find((p) => p.name === detailTask.project)
    : undefined;

  // 浮层时间行用绝对日期 + 周几（周/月视图点开时"哪天"比"今天/明天"信息量大）
  const detailDate = (offset: number) => {
    const d = new Date();
    d.setDate(d.getDate() + offset);
    return `${d.getMonth() + 1}月${d.getDate()}日 周${WEEKDAY_NAMES[d.getDay()]}`;
  };

  // 改期确认钮可用态（时间属性行日期药丸 → footer 钮确认）：草稿换算与**被点
  // 实例**（detail.offset）不同才可确认——重复系列主行 dateOffset 是基准日，
  // 用它对比会误判（点未来实例选未来另一天 = 与基准同 ≠ 未修改）
  const whenRel =
    detailTask && !detailTask.allDay && whenDraft
      ? manualPickToRelative(whenDraft)
      : null;
  const whenChanged =
    !!whenRel &&
    !!detailTask &&
    !!detail &&
    (whenRel.dateOffset !== detail.offset ||
      whenRel.start !== detailTask.start);

  // 打开浮层：宽卡（卡片本身容得下浮层——独占整列的单日程、双列泳道卡）水平居中于卡片
  // 中部，即日历区中央，不甩视口边缘、不盖侧栏；窄卡（周/月列卡、3 列以上泳道）卡片窄、
  // 居中会盖大片相邻区域，保持右侧优先、视口右缘不足翻左；末位视口 clamp 兜底（12px
  // 边距，与垂直 clamp 同式；360 为内容高度估算，CSS max-height 内滚兜底）
  const openDetail = (id: string, offset: number, rect: DOMRect) => {
    const task = events.find((ev) => ev.id === id);
    if (!task) return;
    const wide = rect.width >= DETAIL_W + 24;
    let left = wide
      ? rect.left + (rect.width - DETAIL_W) / 2 // 宽卡：居中于卡片
      : rect.right + DETAIL_W + 8 > window.innerWidth - 12
        ? rect.left - DETAIL_W - 8 // 窄卡：右侧放不下翻左
        : rect.right + 8; // 窄卡：右侧优先
    left = Math.min(Math.max(left, 12), Math.max(window.innerWidth - DETAIL_W - 12, 12)); // 视口 clamp 兜底
    const top = Math.min(Math.max(rect.top, 12), Math.max(window.innerHeight - 12 - 360, 12));
    setNoteDraft(task.note ?? "");
    setEditingTitle(false);
    setRecurrenceOpen(false);
    setWhenOpen(false);
    // 药丸装载被点实例日（offset 参数）而非主行基准日——重复系列点未来实例时
    // 药丸须显示该实例的日期（同 detailEnded 实例日纠偏口径）
    setWhenDraft(task.allDay ? "" : detailPickValue({ ...task, dateOffset: offset }));
    setPendingEdit(null); // 上次浮层遗留的未决编辑随重开丢弃
    setMinutesOpen(false);
    setMinutesText("");
    setMinutesError(null); // busy 不重置：在途提交 finally 自复位
    setDetail({ id, offset, left, top });
  };

  // 编辑提交入口：重复系列的备注/标题/优先级先进三问缓冲（多字段聚合，等范围选择），
  // 单次日程/例外实体直发——例外实体已是物理行，编辑即改自己，无三问。
  // 返回是否进了缓冲（closeDetail 据此拦截关闭防丢稿）
  const queueEdit = (patch: Partial<Task>): boolean => {
    if (!detailTask) return false;
    if (detailTask.recurrence) {
      setPendingEdit((p) => ({ ...(p ?? {}), ...patch }));
      return true;
    }
    onUpdateTask(detailTask.id, patch);
    return false;
  };

  // 备注提交：trim 空归 undefined 抹字段，仅变更才写回（幂等）；重复系列经 queueEdit 进缓冲
  const commitNote = (): boolean => {
    if (!detailTask) return false;
    const note = noteDraft.trim() || undefined;
    if (note === detailTask.note) return false;
    return queueEdit({ note });
  };

  // 标题编辑收尾（口径同收件箱）：commit 时 trim 非空且有变更才写回；重复系列经 queueEdit 进缓冲
  const finishTitle = (commit: boolean): boolean => {
    let queued = false;
    if (commit && detailTask) {
      const title = titleDraft.trim();
      if (title && title !== detailTask.title) queued = queueEdit({ title });
    }
    skipTitleBlur.current = true;
    setEditingTitle(false);
    return queued;
  };

  // —— 时间编辑行（2026-11-11 四处统一批；2026-11-12 三问批）：耗时档位 +
  // WhenPicker 日期药丸 + 改期确认钮——与收件箱已排期卡同构。重复系列 → 范围
  // 三问（SeriesScopeAsk 弹浮层左侧，与拖拽/拉伸同组件同文案，用户拍板同步）；
  // 非重复 → onScheduleChange 直发 ——

  // 耗时档位点击：保日保起点改 end（时长 ≡ end-start 后端不变式，与拉伸同式）。
  // 重复系列三问语义：this/following = 被点实例日的新时长（例外行/新系列），
  // all = 保主行基准改系列时长（allOffset 专设，防重锚丢过去实例）
  const applyDetailDuration = (minutes: number, e?: React.MouseEvent) => {
    if (!detailTask || detailTask.allDay || !detail) return;
    const end = detailTask.start + minutes / 60;
    if (detailTask.recurrence) {
      // 三问气泡贴触发钮（耗时档位）正下方弹出——真机批修正：此前按浮层左侧
      // 偏移定位，浮层靠视口左缘时被钳位推走「飞」到别处
      const rect = e?.currentTarget.getBoundingClientRect();
      setScopeAsk({
        x: rect?.left ?? detail.left,
        y: (rect?.bottom ?? detail.top + 80) + 6,
        move: {
          id: detailTask.id,
          fromOffset: detail.offset,
          newOffset: detail.offset,
          allOffset: detailTask.dateOffset,
          start: detailTask.start,
          end,
        },
      });
      return;
    }
    onScheduleChange(detailTask.id, detailTask.dateOffset, detailTask.start, end);
  };

  // 改期确认：草稿换算相对日 + 时刻，保时长平移（end-start 差值原样平移）。
  // 重复系列三问语义：this = 被点实例挖空 + 例外行落新日新时刻；following =
  // 截断 + 新系列从新日新时刻起；all = 重锚定到新日（BYDAY 后端跟随平移）
  const applyDetailWhen = (e?: React.MouseEvent) => {
    if (!detailTask || !detail) return;
    const rel = manualPickToRelative(whenDraft);
    if (!rel) return;
    const dur =
      detailTask.end !== undefined ? detailTask.end - detailTask.start : undefined;
    const end = dur !== undefined ? rel.start + dur : undefined;
    if (detailTask.recurrence) {
      // 三问气泡贴触发钮（footer 改期钮）正下方弹出（定位修正同 applyDetailDuration）
      const rect = e?.currentTarget.getBoundingClientRect();
      setScopeAsk({
        x: rect?.left ?? detail.left,
        y: (rect?.bottom ?? detail.top + 80) + 6,
        move: {
          id: detailTask.id,
          fromOffset: detail.offset,
          newOffset: rel.dateOffset,
          start: rel.start,
          end,
        },
      });
      return;
    }
    onScheduleChange(detailTask.id, rel.dateOffset, rel.start, end);
  };

  // 统一关闭：提交路径先显式落盘草稿——点外/Esc/滚动路径 textarea 是卸载不走 blur，不提交必丢稿。
  // 重复系列有未决编辑（本次刚聚合进缓冲，或缓冲已有存量）时不关闭浮层：点外收起是"保存"的
  // 肌肉记忆（单次日程如此），静默丢弃 = 丢稿——浮层保留、应用范围气泡常显，由用户显式选范围
  //（或"取消修改"放弃）后再关闭；滚动/切视图锚点失效走 force=true 直接关（罕见场景，丢弃为既定取舍）
  const closeDetail = (commit: boolean, force = false) => {
    if (!detail) return;
    let queued = false;
    if (commit) {
      if (editingTitle) queued = finishTitle(true);
      queued = commitNote() || queued;
    }
    if ((queued || pendingEdit !== null) && !force) return;
    setDetail(null);
  };

  // —— 会后纪要（2026-11-10）：已结束日程浮层的纪要录入 → 提炼行动项 ——————

  // 「会后纪要」入口判定（2026-11-12 实例日纠偏 + 放宽）：过去与今天的非全天
  // 恒显（会议可能提前结束而日程时间没改，不必等过点）；未来的与全天不显示。
  // 必须用 detail.offset（被点实例日）——detailTask 是 events.find(id) 命中的
  // 主行，重复系列主行 dateOffset 是基准日：基准日在过去的系列会让未来实例
  // 浮层误显按钮（真机批发现）；全天判定用任务级 detailTask.allDay
  const detailEnded =
    !!detail && detail.offset <= 0 && !detailTask?.allDay;

  // 提交纪要原文 → minutes 卡（助手面板）。成功收浮层（徽标/面板刷新由 minutes
  // 广播驱动）；失败文案留在录入区（503 detail 直显，同 QuickAdd 错误直显口径）
  const submitMinutesText = async () => {
    if (!detailTask || minutesBusy) return;
    const text = minutesText.trim();
    if (text.length < 10) {
      setMinutesError("纪要原文至少 10 字");
      return;
    }
    setMinutesBusy(true);
    setMinutesError(null);
    try {
      await submitMinutes(detailTask.id, text);
      setDetail(null);
    } catch (e) {
      const msg = (e as { detail?: string }).detail;
      setMinutesError(msg || "提炼失败，请稍后重试");
    } finally {
      setMinutesBusy(false);
    }
  };

  // —— 收件箱待办拖入排期 ——
  // dragover：仅收件箱待办的拖拽允许放置（否则外部拖拽也会触发 drop）
  const onTodoDragOver = (e: ReactDragEvent) => {
    if (isTodoDrag(e)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    }
  };

  // 待办落定 → 把该任务的排期字段写回统一数据源（App 更新后它自然从收件箱/项目列表消失、出现在日历）；
  // 有耗时给 end，无耗时只有开始时刻（展示为一小时卡的既有规则）；优先级沿用任务自带值。
  // 数据源建议条目（suggest 载荷，2026-10-24 直排批）分流到 onSuggestDrop：落点即
  // 排期时间建任务 + settle-items 结算一步完成，不经收件箱中转
  const scheduleTodo = (todo: DraggedTodoWithSuggest, dateOffset: number, start: number) => {
    if (todo.suggest && onSuggestDrop) {
      void onSuggestDrop(todo, dateOffset, start);
      return;
    }
    onScheduleChange(
      todo.id,
      dateOffset,
      start,
      todo.minutes ? start + todo.minutes / 60 : undefined,
    );
  };

  // 日/周视图落点 → 时刻：反用 y(hour) 公式，吸附 15 分钟、clamp 在时间轴内
  // （无耗时按占 1 小时计，与卡片拖动的 clamp 口径一致）
  const hourFromY = (clientY: number, rectTop: number, todo: DraggedTodo) => {
    const dur = todo.minutes ? todo.minutes / 60 : 1;
    const snapped =
      Math.round(((START + (clientY - rectTop) / HOUR_HEIGHT) * 4) / 4);
    return Math.min(
      Math.max(snapped, START),
      Math.max(END - dur, START),
    );
  };

  // 月视图落点 → 时刻（无时间轴，智能放置）：该日无日程排时间轴起点；
  // 已有日程则排到最晚结束（无 end 按 start+1）后半小时，再 clamp 不顶出时间轴末端。
  // **全量口径**（直读 events 不走 viewEvents/occurrencesOn）：项目筛选态下隐藏
  // 日程虽不可见，智能放置仍须避开——防"看似空实则已占"排进冲突时段
  const monthDropHour = (offset: number, todo: DraggedTodo) => {
    const dayEnds = events
      .filter((e) => occursOn(e, offset))
      .map((ev) => ev.end ?? ev.start + 1);
    const start =
      dayEnds.length === 0 ? START : Math.max(...dayEnds) + 0.5;
    const dur = todo.minutes ? todo.minutes / 60 : 1;
    return Math.min(start, Math.max(END - dur, START));
  };

  // 原生构造器自动处理跨月/跨年进位
  const shiftDay = (delta: number) =>
    setViewedDate((d) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta));

  // 月模式翻页：保留日期数字、clamp 到目标月末（如 31 日 → 2 月 = 2 月 28 日）
  const shiftMonth = (delta: number) =>
    setViewedDate((d) => {
      const target = new Date(d.getFullYear(), d.getMonth() + delta, 1);
      const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
      target.setDate(Math.min(d.getDate(), lastDay));
      return target;
    });

  // 全局快捷键（经 App 转派的组件级动作）：视图档位 / 回今天 / 翻页。翻页口径与
  // 上方翻页按钮分派一致（日 ±1 天 / 周 ±7 天 / 月 ±1 月）。依赖 viewMode 重订阅
  // 保处理器读到最新档位（shiftDay/shiftMonth 走函数式 setViewedDate，旧闭包等价）。
  // 与上方 defaultView 对齐 effect 互不覆盖：那个只在设置项变化时重跑。
  useEffect(() => {
    const offs = [
      onShortcutCmd("view-day", () => setViewMode("day")),
      onShortcutCmd("view-week", () => setViewMode("week")),
      onShortcutCmd("view-month", () => setViewMode("month")),
      // 议程无专属快捷键（16 动作表不动）：视图内点击切换即可
      onShortcutCmd("go-today", () => setViewedDate(new Date())),
      onShortcutCmd("prev-period", () =>
        viewMode === "week" ? shiftDay(-7) : viewMode === "month" ? shiftMonth(-1) : shiftDay(-1),
      ),
      onShortcutCmd("next-period", () =>
        viewMode === "week" ? shiftDay(7) : viewMode === "month" ? shiftMonth(1) : shiftDay(1),
      ),
      // 搜索结果跳转（2026-11-06 搜索批）：切日视图并定位该日期（月视图点格
      // 跳日同款口径）；ISO 由 SearchModal 传，画布常驻挂载订阅无竞态
      onCanvasJump((iso) => {
        setViewedDate(new Date(iso));
        setViewMode("day");
      }),
    ];
    return () => offs.forEach((off) => off());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode]);

  // 日程卡片渲染（日/周/月视图共用）：重叠日程经泳道算法分列并排（按事件区宽度平分 + 2px 列缝），
  // 3 列起进入窄卡降级（隐藏项目 pill；周视图列窄恒降级），已过降淡；可拖动调整时间。
  // 月视图经 compact 切紧凑单行卡（muted 时间前缀 + 标题）并改用调用方给定的格内几何，无小时轴。
  // 拖动/拉伸中用临时时刻垂直变化，泳道列不变，松手写回才触发整体重排。
  // 必须是渲染函数而非内嵌组件：组件体会随每次渲染换类型，拖动中重挂卡片会丢 pointer capture
  const renderEventCard = (
    event: ScheduledTask,
    opts: {
      column: number;
      columns: number;
      forceNarrow?: boolean; // 周视图列窄，恒窄卡降级
      left: string;
      right: string;
      isPast: (endHour: number) => boolean; // 传入判定函数：降淡依赖下方拖动临时 end
      compact?: { top: string | number; height: number }; // 月视图紧凑变体：单行内容 + 调用方给定几何
    },
  ) => {
    const { columns } = opts;
    // 同系列多实例并存：仅被拖/被点的那一天实例进入拖动态与高亮（id + 实例日双匹配）
    const dragging = drag?.eventId === event.id && drag.sourceOffset === event.dateOffset;
    const start = dragging ? drag!.start : event.start;
    const end = dragging ? drag!.end : (event.end ?? event.start + 1); // 无结束时间的日程默认一小时
    // 拖拽冲突（批 E2）：目标区间与既有日程相交时被拖卡红描边（仅视觉提示不阻止）
    const conflict = dragging && dragConflict;
    // 越界日程（区间收窄后落在时间轴外）几何 clamp 收边：数据不动（改回设置即恢复），
    // 坐标夹进轴内防负 top / 溢出轴底；区间内日程与原公式逐像素一致
    const top = Math.min(Math.max(y(start), 0), axisHeight);
    const bottom = Math.min(Math.max(y(end), 0), axisHeight);
    const narrow = columns >= 3 || opts.forceNarrow === true;
    return (
      <div
        key={event.id}
        className={`timeline-event card prio-${event.priority}${narrow ? " narrow" : ""}${dragging ? " dragging" : ""}${conflict ? " conflict" : ""}${detail?.id === event.id && detail.offset === event.dateOffset ? " detailing" : ""}${event.done ? " done" : ""}${opts.isPast(end) ? " past" : ""}`}
        style={{
          ...(opts.compact
            ? { top: opts.compact.top, height: opts.compact.height }
            : { top: top + 4, height: Math.max(0, bottom - top - 8) }),
          left: opts.left,
          right: opts.right,
        }}
        onPointerDown={(e) => onCardPointerDown(e, event)}
        onPointerMove={onCardPointerMove}
        onPointerUp={onCardPointerUp}
        onPointerCancel={onCardPointerUp}
      >
        {opts.compact ? (
          <span className="timeline-event-compact">
            <span className="timeline-event-compact-time">
              {event.allDay ? "全天" : fmtTime(start)}
            </span>
            <span className="timeline-event-compact-title">{event.title}</span>
            {event.done && (
              <Check size={12} strokeWidth={2.5} className="timeline-event-done-mark" />
            )}
          </span>
        ) : (
          <>
            <div className="timeline-event-title-row">
              <span className="timeline-event-title">{event.title}</span>
              {/* 右侧组：完成绿对号 + 项目 pill——绿勾与项目名同组并排不重叠；
                  窄卡 pill 隐藏但绿勾保留（完成态信号不受降级） */}
              <span className="timeline-event-title-right">
                {event.done && (
                  <Check size={13} strokeWidth={2.5} className="timeline-event-done-mark" />
                )}
                {event.project && (
                  <span className="timeline-event-tag">{event.project}</span>
                )}
              </span>
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
          </>
        )}
        {!opts.compact && (
          <div
            className="timeline-event-resize"
            onPointerDown={(e) => onResizePointerDown(e, event)}
          />
        )}
      </div>
    );
  };

  // "现在"红线三件套（日/周视图共用：label + 线 + 圆点，跨事件区全宽）
  const renderNowLine = () => (
    <>
      <span className="timeline-now-label" style={{ top: nowTop - 15 }}>
        {nowLabel}
      </span>
      <div className="timeline-now-line" style={{ top: nowTop }} />
      <div className="timeline-now-dot" style={{ top: nowTop - 3.4 }} />
    </>
  );

  // 周视图渲染：sticky 周头（星期 + 日期，今天列 accent）+ 7 个天列（每列独立泳道分列，
  // 卡片恒窄卡降级适配窄列）+ 本周含今天时全宽红线；不含午休建议块
  const renderWeekView = () => (
    <>
      <div className="week-header">
        <div className="week-head-gutter" />
        <div className="week-head-cols">
          {weekOffsets.map((offset, i) => {
            const date = new Date(
              weekStart.getFullYear(),
              weekStart.getMonth(),
              weekStart.getDate() + i,
            );
            // 农历/节气/节日小字 + 休/班角标（口径与月视图 lunarDayMeta 一致）
            const { label, mark } = lunarDayMeta(date);
            return (
              // 列头点击跳该日日视图（2026-11-12 点击对齐批：与日程视图日头、
              // 月视图月格点击同口径——「深入这一天」的统一路径）
              <button
                type="button"
                key={i}
                className={`week-head-col${offset === 0 ? " today" : ""}`}
                title="查看这一天"
                onClick={() => {
                  setViewedDate(date);
                  setViewMode("day");
                }}
              >
                <span className="week-head-weekday">
                  周{WEEKDAY_NAMES[(i + 1) % 7]}
                  {/* 每日负载指示灯：该列所有日程总耗时/每日工时分档（绿/橙/红） */}
                  <i className={`week-head-dot ${dayLoadTone(allOccurrencesOn(weekOffsets[i]), WORK_HOURS)}`} />
                </span>
                <span className="week-head-date">
                  {date.getDate()}
                  <span className="week-head-lunar">{label}</span>
                </span>
                {mark && (
                  <span
                    className={`week-head-mark${mark === "班" ? " work" : " rest"}`}
                  >
                    {mark}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      {/* 全天/跨天行（批 B；二次根修对齐）：几何链与事件区逐层同式——row（relative，
          同 .timeline 角色，高度内联）→ cols（absolute left:64/right:20，字面复制
          .week-columns）→ cell（flex:1 relative，同 .week-col）→ 条（absolute
          left:0/right:0 占满列，同 0 列泳道卡）——与普通日程卡同一套属性同一
          字面量，像素级对齐；每列渲染当日覆盖的紧凑条，跨天条在覆盖每日重复显示 */}
      {weekDayBars.some((bars) => bars.length > 0) && (
        <div
          className="week-allday-row"
          style={{
            height: Math.max(...weekDayBars.map((b) => b.length)) * WEEK_ALLDAY_H - 4,
          }}
        >
          <div className="week-allday-cols">
            {weekDayBars.map((bars, i) => (
              <div key={i} className="week-allday-cell">
                {bars.map((b, j) => {
                  // 跨天拖动中：条 DOM 留源列（保 pointer capture），按整列宽平移视觉
                  // 跨列（口径同周列普通卡）；同系列多实例只平移被拖的那一天实例
                  const dayShift =
                    drag?.eventId === b.id && drag.sourceOffset === weekOffsets[i]
                      ? drag.dayShift
                      : 0;
                  return (
                    <div
                      key={`${b.id}-${b.dateOffset}`}
                      className={`timeline-event card allday prio-${b.priority}${drag?.eventId === b.id && drag.sourceOffset === weekOffsets[i] ? " dragging" : ""}${detail?.id === b.id && detail.offset === b.dateOffset ? " detailing" : ""}${b.done ? " done" : ""}`}
                      style={{
                        top: j * WEEK_ALLDAY_H,
                        height: WEEK_ALLDAY_H - 4,
                        left: dayShift ? `${dayShift * 100}%` : 0,
                        right: dayShift ? `${-dayShift * 100}%` : 0,
                      }}
                      title={allDaySpan(b) > 1 ? `全天 · 跨 ${allDaySpan(b)} 天` : "全天"}
                      onPointerDown={(e) => onCardPointerDown(e, b)}
                      onPointerMove={onCardPointerMove}
                      onPointerUp={onCardPointerUp}
                      onPointerCancel={onCardPointerUp}
                    >
                      <span className="week-allday-title">
                        <i className="week-allday-flag">全天</i>
                        <span className="week-allday-text">{b.title}</span>
                        {b.done && (
                          <Check size={11} strokeWidth={2.5} className="timeline-event-done-mark" />
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="timeline" style={{ height: axisHeight + 40 }}>
        {renderHourMarks(hours, START)}
        <div
          className="week-columns"
          onDragOver={onTodoDragOver}
          onDrop={(e) => {
            const todo = parseTodoDrag(e);
            if (!todo) return;
            e.preventDefault();
            // 落点天列：target 可能是卡片/列自身，沿祖先链找到 .week-col 再反查索引
            const col = (e.target as HTMLElement).closest(".week-col");
            if (!col || !col.parentElement) return;
            const idx = Array.from(col.parentElement.children).indexOf(col);
            if (idx < 0 || idx >= weekOffsets.length) return;
            scheduleTodo(
              todo,
              weekOffsets[idx],
              hourFromY(e.clientY, col.getBoundingClientRect().top, todo),
            );
          }}
        >
          {weekOffsets.map((offset, i) => (
            <div key={i} className="week-col">
              {weekDayEvents[i].map((event) => {
                  const lane = weekLanes[i].get(event)!;
                  // 跨天拖动中：卡片 DOM 留在源列（保 pointer capture），按整列宽平移视觉跨列；
                  // 禁止"优化"成换列渲染——React 换父会重建 DOM 丢 capture。降淡松手前仍按源列；
                  // 同系列多实例并存，只平移被拖的那一天实例（id + 实例日双匹配）
                  const dayShift = drag?.eventId === event.id && drag.sourceOffset === offset ? drag!.dayShift : 0;
                  return renderEventCard(event, {
                    ...lane,
                    forceNarrow: true,
                    left: dayShift
                      ? `calc(${weekLaneLeft(lane)} + ${dayShift} * 100%)`
                      : weekLaneLeft(lane),
                    right: dayShift
                      ? `calc(${weekLaneRight(lane)} - ${dayShift} * 100%)`
                      : weekLaneRight(lane),
                    isPast: (endHour) => isPastOnDay(offset, endHour),
                  });
                })}
            </div>
          ))}
        </div>
        {weekContainsToday && renderNowLine()}
      </div>
    </>
  );

  // 月视图渲染：星期头（周一→周日）+ 7×6 月网格（首格 = 当月 1 号所在周周一），
  // 每格日期数字 + 紧凑单行卡纵排（按开始时刻，溢出由格 overflow 剪裁直接截断）；
  // 点击格空白/日期数字跳该天日视图（点卡片不跳）；无小时刻度/红线/午休块
  const renderMonthView = () => (
    <div className="month-board">
      <div className="month-weekdays">
        {Array.from({ length: 7 }, (_, i) => (
          <span key={i} className="month-weekday">
            周{WEEKDAY_NAMES[(i + 1) % 7]}
          </span>
        ))}
      </div>
      <div
        className="month-grid"
        onDragOver={onTodoDragOver}
        onDrop={(e) => {
          const todo = parseTodoDrag(e);
          if (!todo) return;
          e.preventDefault();
          // 落点格：target 可能是格内卡片/日期数字，沿祖先链找到 .month-cell 再反查索引
          const cell = (e.target as HTMLElement).closest(".month-cell");
          if (!cell || !cell.parentElement) return;
          const idx = Array.from(cell.parentElement.children).indexOf(cell);
          if (idx < 0 || idx >= monthCells.length) return;
          scheduleTodo(todo, monthCells[idx].offset, monthDropHour(monthCells[idx].offset, todo));
        }}
      >
        {monthCells.map((cell, i) => (
          <div
            key={cell.offset}
            className={`month-cell${cell.inMonth ? "" : " out"}${cell.isToday ? " today" : ""}${
              drag && cell.events.some((ev) => ev.id === drag.eventId && ev.dateOffset === drag.sourceOffset)
                ? " drag-source"
                : ""
            }`}
            onClick={(e) => {
              if ((e.target as HTMLElement).closest(".timeline-event")) return; // 点卡片（含拖动落点）不跳视图
              setViewedDate(cell.date);
              setViewMode("day");
            }}
          >
            <span className="month-date">
              <span className="month-date-num">{cell.date.getDate()}</span>
              <span className="month-date-lunar">{cell.lunar}</span>
              {/* 每日负载指示灯：该格所有日程总耗时/每日工时分档（绿/橙/红） */}
              <i className={`month-date-dot ${dayLoadTone(allOccurrencesOn(cell.offset), WORK_HOURS)}`} />
            </span>
            {cell.mark && (
              <span
                className={`month-mark${cell.mark === "班" ? " work" : " rest"}`}
              >
                {cell.mark}
              </span>
            )}
            {cell.events.map((event, idx) => {
              // 跨格拖动中：卡片 DOM 留源格（保 pointer capture，同周视图跨天技巧），
              // 由 dayShift 反解列/行偏移，按格宽/高 % 平移视觉跨格；同系列多实例只平移被拖实例
              const dayShift = drag?.eventId === event.id && drag.sourceOffset === cell.offset ? drag!.dayShift : 0;
              const colShift = dayShift ? ((i + dayShift) % 7) - (i % 7) : 0;
              const rowShift = dayShift
                ? Math.floor((i + dayShift) / 7) - Math.floor(i / 7)
                : 0;
              return renderEventCard(event, {
                column: 0,
                columns: 1,
                compact: {
                  top: rowShift
                    ? `calc(${MONTH_DATE_H + idx * (MONTH_CARD_H + MONTH_CARD_GAP)}px + ${rowShift * 100}%)`
                    : MONTH_DATE_H + idx * (MONTH_CARD_H + MONTH_CARD_GAP),
                  height: MONTH_CARD_H,
                },
                left: colShift ? `calc(5px + ${colShift * 100}%)` : "5px",
                right: colShift ? `calc(5px - ${colShift * 100}%)` : "5px",
                isPast: (endHour) => isPastOnDay(cell.offset, endHour),
              });
            })}
          </div>
        ))}
      </div>
    </div>
  );

  // 日程视图渲染（批 C；2026-11-09 布局重排）：viewedDate 起 14 天按日分组的自适应
  // 日卡网格（.agenda-list grid 窄 1 列/宽自动多列）——今天卡横跨整行作视觉锚；
  // 空日轻量化为虚线单行条（"暂无安排"并入日头）；日头（日期/周几/农历/休班/负载灯）
  // + 条目行（全天标注或钟点 + 标题 + 优先级点/项目标签/重复标/完成态）。
  // 2026-11-12 拖拽批 + 点击对齐批：点行弹详情浮层（与三视图一致，浮层内已有
  // 完整编辑能力——原点行跳日视图退役，跳转诉求转移到日头）；日卡是拖拽落点
  // （收件箱待办拖入 = 智能放置排期，复用月视图链路；行拖到另一天 = 保时刻
  // 改期，载荷带 start 标记与日/周网格的排期链路分流）
  const renderAgendaView = () => (
    <div
      className="agenda-list"
      onDragOver={onTodoDragOver}
      onDrop={(e) => {
        const todo = parseTodoDrag(e);
        if (!todo) return;
        e.preventDefault();
        // 落点日卡：target 可能是行/日头，沿祖先链找 .agenda-day 反查 offset
        const day = (e.target as HTMLElement).closest(".agenda-day");
        if (!day || !day.parentElement) return;
        const idx = Array.from(day.parentElement.children).indexOf(day);
        if (idx < 0 || idx >= agendaDays.length) return;
        const offset = agendaDays[idx].offset;
        if (typeof todo.start === "number") {
          // 已排期条目跨日拖动：保时刻改期（全天条 start=0、minutes=跨距小时数，
          // 同公式落成天级平移）；重复系列行 → 范围三问（与日历卡拖拽同口径，
          // 2026-11-12 拖拽三问批）
          if (todo.rec && typeof todo.fromOffset === "number") {
            setScopeAsk({
              x: e.clientX + 12,
              y: e.clientY + 12,
              move: {
                id: todo.id,
                fromOffset: todo.fromOffset,
                newOffset: offset,
                start: todo.start,
                end: todo.minutes ? todo.start + todo.minutes / 60 : undefined,
              },
            });
            return;
          }
          onScheduleChange(
            todo.id,
            offset,
            todo.start,
            todo.minutes ? todo.start + todo.minutes / 60 : undefined,
          );
        } else {
          scheduleTodo(todo, offset, monthDropHour(offset, todo));
        }
      }}
    >
      {agendaDays.map(({ date, offset }) => {
        const bars = allDayOn(offset);
        const timeline = occurrencesOn(offset)
          .filter((e) => !e.allDay)
          .sort((a, b) => a.start - b.start);
        const rows = [...bars, ...timeline];
        const { label, mark } = lunarDayMeta(date);
        return (
          <div
            key={offset}
            className={`agenda-day${offset === 0 ? " today" : ""}${rows.length === 0 ? " empty" : ""}`}
          >
            {/* 日头点击跳该日日视图（2026-11-12 点击对齐批：条目详情浮层化后，
                "深入这一天"的路径由日头接管） */}
            <button
              type="button"
              className="agenda-day-head"
              title="查看这一天"
              onClick={() => {
                setViewedDate(date);
                setViewMode("day");
              }}
            >
              <span className="agenda-day-date">
                {date.getMonth() + 1}月{date.getDate()}日
                <span className="agenda-day-weekday">周{WEEKDAY_NAMES[date.getDay()]}</span>
                {offset === 0 && <span className="agenda-day-today">今天</span>}
                {mark && (
                  <span className={`agenda-day-mark${mark === "班" ? " work" : " rest"}`}>
                    {mark}
                  </span>
                )}
              </span>
              <span className="agenda-day-lunar">{label}</span>
              {/* 负载灯：口径同周/月视图（dayLoadTone 内已排除全天行）；
                  全量数据源（同负载指示器——容量信号不随项目筛选变） */}
              <i className={`week-head-dot ${dayLoadTone(allOccurrencesOn(offset), WORK_HOURS)}`} />
              {rows.length === 0 && <span className="agenda-day-none">暂无安排</span>}
            </button>
            {rows.map((t) => (
              <button
                type="button"
                key={`${t.id}-${t.dateOffset}`}
                className={`agenda-row${t.done ? " done" : ""}`}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData(
                    TODO_MIME,
                    JSON.stringify({
                      id: t.id,
                      title: t.title,
                      // 改期载荷：原起始时刻 + 时长（分钟）——落日卡据此保时刻
                      // 改期；全天条 start=0、minutes=跨距（drop 公式统一）；
                      // 重复系列行附 rec + 原实例日（drop 侧弹范围三问）；
                      // 拖到日/周网格走既有链路按落点时刻重排
                      start: t.allDay ? 0 : t.start,
                      minutes:
                        t.end !== undefined ? Math.round((t.end - t.start) * 60) : null,
                      ...(t.recurrence
                        ? { rec: true, fromOffset: t.dateOffset }
                        : {}),
                    }),
                  );
                }}
                onClick={(e) =>
                  openDetail(t.id, t.dateOffset, e.currentTarget.getBoundingClientRect())
                }
              >
                <span className="agenda-row-time">
                  {t.allDay
                    ? "全天"
                    : t.end
                      ? `${fmtTime(t.start)}–${fmtTime(t.end)}`
                      : fmtTime(t.start)}
                </span>
                <i className={`agenda-row-dot ${t.priority}`} aria-hidden="true" />
                <span className="agenda-row-title">{t.title}</span>
                {/* 项目标签：所有日卡常显，窄卡与标题互相让位（见 CSS） */}
                {t.project && <span className="agenda-row-project">{t.project}</span>}
                {t.recurrence && (
                  <Repeat size={11} strokeWidth={2} className="agenda-row-icon" />
                )}
                {t.done && (
                  <Check size={12} strokeWidth={2.5} className="timeline-event-done-mark" />
                )}
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );

  // 打开页面滚到"现在"红线居中，直接看到接下来的日程（仅挂载执行一次；日期切换不重定位）
  useEffect(() => {
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = nowTop - el.clientHeight / 2;
    }
  }, []);

  // 浮层存活期的全局关闭路由：Esc / 点浮层外 / 中栏滚动（scroll 不冒泡，须直接挂滚动容器）。
  // 点外关闭天然覆盖"点另一张卡换目标"（卡片 pointerdown 落点不在浮层内 → 关旧，抬起 <4px → 开新）。
  // 监听随草稿 state 重建保 closeDetail 闭包新鲜；closeDetail 本身不入依赖——它读取的
  // 草稿与 pendingEdit 已全在依赖中；滚动路径 force 关闭（浮层不随滚动移动、锚点漂移必须关）
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

  // 切视图/翻页即关浮层（视图语境已变，浮层失去视觉锚点）——force 强制关，未决编辑一并
  // 丢弃（浮层无法在已变的语境里保持打开）；挂载首跑 detail 为 null，closeDetail 内部 no-op
  useEffect(() => {
    closeDetail(true, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, viewedDate]);

  return (
    <section className="schedule" style={style}>
      <header className="schedule-console">
        <div className="schedule-date-row">
          <div className="schedule-date-nav">
            <div className="schedule-date-block">
              <span className="schedule-date">
                {viewMode === "month" ? (
                  monthTitle
                ) : viewMode === "week" ? (
                  weekTitle
                ) : viewMode === "agenda" ? (
                  agendaTitle
                ) : (
                  <>
                    {dateText}
                    {dayMeta.mark && (
                      <span
                        className={`schedule-date-mark${dayMeta.mark === "班" ? " work" : " rest"}`}
                      >
                        {dayMeta.mark}
                      </span>
                    )}
                  </>
                )}
              </span>
              <span className="schedule-week">
                {viewMode === "month"
                  ? monthSub
                  : viewMode === "week"
                    ? weekSub
                    : viewMode === "agenda"
                      ? agendaSub
                      : weekText}
              </span>
            </div>
            {/* 纵向容器：翻页器在上、日视图负载指示器在其下（起点对齐 < 按钮中心） */}
            <div className="schedule-pager-col">
              <div className="schedule-pager">
                <button
                  className="schedule-pager-btn"
                  onClick={() =>
                    viewMode === "week"
                      ? shiftDay(-7)
                      : viewMode === "month"
                        ? shiftMonth(-1)
                        : shiftDay(-1)
                  }
                >
                  <ChevronLeft size={15} strokeWidth={2} />
                </button>
                <button className="schedule-today" onClick={() => setViewedDate(new Date())}>
                  今天
                </button>
                <button
                  className="schedule-pager-btn"
                  onClick={() =>
                    viewMode === "week"
                      ? shiftDay(7)
                      : viewMode === "month"
                        ? shiftMonth(1)
                        : shiftDay(1)
                  }
                >
                  <ChevronRight size={15} strokeWidth={2} />
                </button>
              </div>
              {viewMode === "day" ? (
                <div className="schedule-load" aria-label="当日负载指示">
                  <span className="schedule-load-count">{loadDayEvents.length}个日程</span>
                  {/* 分段进度条：高/中/低优先级耗时依次填充（颜色同 prio-* 色条变量），
                      分母 = max(总耗时, 8h)——超 8h 按总耗时归一化恒填满（相对占比不失真）、
                      不足 8h 以 8h 为分母空闲段浅灰补尾；负载率 > 80% 外包淡红描边提醒 */}
                  <div
                    className={`schedule-load-bar${loadRate > 0.8 ? " over" : ""}`}
                  >
                    {(["high", "medium", "low"] as const).map((p) => (
                      <span
                        key={p}
                        className={`schedule-load-seg ${p}`}
                        style={{ width: `${(prioHours[p] / loadDenom) * 100}%` }}
                      />
                    ))}
                    <span
                      className="schedule-load-seg free"
                      style={{ width: `${Math.max(0, 1 - loadTotal / loadDenom) * 100}%` }}
                    />
                  </div>
                  {/* 耗时占比 = 高优先级总耗时/所有日程总耗时/总工作时间；
                      负载率 ≤50% 黑、51–80% 橙、>80% 红，超载追加"已超载 Nh" */}
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
              ) : null}
            </div>
          </div>
          <div className="schedule-header-actions">
            {/* 项目筛选 chip（2026-11-10）：项目页"已排期"开关联动激活——彩色项目
                点 + 名 + ✕ 常显于此（醒目指示防"日程去哪了"困惑），✕ 一键回全量
                （本次停留不再自动，直至开关/项目再变化）；无项目时灰点兜底 */}
            {projectFilter && (
              <span className="schedule-filter-chip" title={`日历仅显示「${projectFilter}」的日程，点击 ✕ 恢复全部`}>
                <i
                  style={{
                    background:
                      projects.find((p) => p.name === projectFilter)?.color ?? "var(--text-muted)",
                  }}
                />
                <span className="schedule-filter-chip-name">{projectFilter}</span>
                {onClearProjectFilter && (
                  <button
                    type="button"
                    className="schedule-filter-chip-x"
                    aria-label="清除项目筛选"
                    title="恢复显示全部日程"
                    onClick={onClearProjectFilter}
                  >
                    <X size={11} strokeWidth={2.5} />
                  </button>
                )}
              </span>
            )}
            <div className="schedule-segmented">
              <button
                type="button"
                className={`schedule-segment${viewMode === "day" ? " active" : ""}`}
                onClick={() => setViewMode("day")}
              >
                日
              </button>
              <button
                type="button"
                className={`schedule-segment${viewMode === "week" ? " active" : ""}`}
                onClick={() => setViewMode("week")}
              >
                周
              </button>
              <button
                type="button"
                className={`schedule-segment${viewMode === "month" ? " active" : ""}`}
                onClick={() => setViewMode("month")}
              >
                月
              </button>
              <button
                type="button"
                className={`schedule-segment${viewMode === "agenda" ? " active" : ""}`}
                onClick={() => setViewMode("agenda")}
              >
                日程
              </button>
            </div>
            {onExpandCopilot && (
              <button
                type="button"
                className={`schedule-expand-btn${expandDisabled ? " disabled" : ""}`}
                onClick={onExpandCopilot}
                title={expandDisabled ? "AI 助手已关闭，点击查看开启入口" : undefined}
                aria-label="展开右栏面板"
              >
                {expandIcon ?? <Sparkles size={15} strokeWidth={2} />}
                {(expandBadge ?? 0) > 0 && (
                  <span className="schedule-expand-badge" title={`${expandBadge} 条未读助手消息`}>
                    {expandBadge! > 99 ? "99+" : expandBadge}
                  </span>
                )}
              </button>
            )}
          </div>
        </div>

        <QuickAdd
          projects={projects}
          onCreateTask={onCreateTask}
          onJumpDate={jumpToDate}
          hours={{ start: START, end: END }}
        />
      </header>

      <div
        className={`schedule-scroll${viewMode === "week" ? " week-mode" : ""}${viewMode === "month" ? " month-mode" : ""}`}
        ref={scrollRef}
      >
        {viewMode === "week" ? (
          renderWeekView()
        ) : viewMode === "month" ? (
          renderMonthView()
        ) : viewMode === "agenda" ? (
          renderAgendaView()
        ) : (
        <>
        {/* 全天/跨天条区（批 B；二次真机批对齐根修）：条用与泳道卡**完全相同**的
            absolute 双锚定（left:64px / right:20px 同一计算式，像素级对齐事件区，
            不再经 flex/gutter 间接层）；条内布局复刻普通卡两行结构（title 行 +
            右上角完成对号/项目 tag + meta 行）；点击开浮层（改期走浮层日期编辑，
            不参与小时网格拖拽）；复用 .timeline-event.card 盒子（唯一盒子约束） */}
        {dayBars.length > 0 && (
          <div
            className="schedule-allday-strip"
            style={{ height: dayBars.length * ALLDAY_ROW_H - 4 }}
          >
            {dayBars.map((b, i) => (
              <div
                key={`${b.id}-${b.dateOffset}`}
                className={`timeline-event card allday prio-${b.priority}${detail?.id === b.id && detail.offset === b.dateOffset ? " detailing" : ""}${b.done ? " done" : ""}`}
                style={{ top: i * ALLDAY_ROW_H, height: ALLDAY_ROW_H - 4 }}
                onClick={(e) =>
                  openDetail(
                    b.id,
                    b.dateOffset,
                    (e.currentTarget as HTMLElement).getBoundingClientRect(),
                  )
                }
              >
                {/* 单行细条（三次视觉批）：「全天」旗标 + 标题 + 右侧轻信息组——
                    视觉回归普通卡语言（白玻璃底/优先级色条/圆角线宽同款），
                    仅以位置（轴上方）与旗标区分类型（Google Calendar 口径） */}
                <span className="allday-flag">全天</span>
                <span className="timeline-event-title">{b.title}</span>
                <span className="allday-tail">
                  {allDaySpan(b) > 1 && <span className="allday-span">跨 {allDaySpan(b)} 天</span>}
                  {b.recurrence && (
                    <span className="allday-span">
                      <Repeat size={11} strokeWidth={2} />
                      {recurrenceText(b.recurrence, b.dateOffset)}
                    </span>
                  )}
                  {b.done && (
                    <Check size={13} strokeWidth={2.5} className="timeline-event-done-mark" />
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
        <div
          className="timeline"
          style={{ height: axisHeight + 40 }}
          onDragOver={onTodoDragOver}
          onDrop={(e) => {
            const todo = parseTodoDrag(e);
            if (!todo) return;
            e.preventDefault();
            // 整条时间轴都是查看日：clientY 相对 .timeline 顶即落点时刻
            scheduleTodo(
              todo,
              viewedOffset,
              hourFromY(
                e.clientY,
                e.currentTarget.getBoundingClientRect().top,
                todo,
              ),
            );
          }}
        >
          {renderHourMarks(hours, START)}

          {/* 建议块 · 午休（设置驱动，关闭时不渲染；不参与已过灰化，恒为浅绿） */}
          {lunch && lunchLane && (
            <div
              className="timeline-event suggestion"
              style={{
                top: y(lunch.start),
                height: y(lunch.end) - y(lunch.start),
                left: laneLeft(lunchLane),
                right: laneRight(lunchLane),
              }}
            >
              <div className="timeline-suggestion-inner">
                <Coffee size={14} strokeWidth={2} className="timeline-suggestion-icon" />
                <span className="timeline-suggestion-text">建议午休时间</span>
              </div>
            </div>
          )}

          {/* AI 排期草案幽灵方块（draft 事件驱动；纯 tint 虚线盒不挂 backdrop-filter，
              位于真实卡下层；采纳后由真实卡取代） */}
          {dayGhosts.map((ghost, i) => {
            const lane = lanes.get(ghost);
            if (!lane) return null;
            return (
              <div
                key={`ghost-${i}`}
                className="timeline-event ghost"
                style={{
                  top: y(ghost.start),
                  height: y(ghost.end) - y(ghost.start),
                  left: laneLeft(lane),
                  right: laneRight(lane),
                }}
              >
                <span className="timeline-ghost-title">{ghost.title}</span>
                <span className="timeline-ghost-meta">
                  {fmtTime(ghost.start)} · AI 草案
                </span>
              </div>
            );
          })}

          {/* 模拟日程（渲染细节见 renderEventCard；全天条已在上方条区渲染） */}
          {dayTimeline.map((event) => {
            const lane = lanes.get(event)!;
            return renderEventCard(event, {
              ...lane,
              left: laneLeft(lane),
              right: laneRight(lane),
              isPast: isPastEvent,
            });
          })}

          {/* 当前时间线（实时 HH:MM，仅查看今天时显示） */}
          {isToday && renderNowLine()}
        </div>
        </>
        )}
      </div>

      {/* 日程详情浮层（fixed 定位挂在 section 根，不受滚动容器/月格 overflow 裁剪；
          时间只读——改时间走卡片拖动/拉伸；删除走二次确认气泡）。maxHeight 随
          top 动态约束（2026-11-12 修复）：打开时 top 按 360px 内容估算钳位，内容
          动态增高（纪要录入/面板展开）后 100vh-24 的静态上限兜不住「top+高度
          超视口底」（晚上低位卡片必现，底边按钮无法操作）——inline 上限改为
          视口底 - top - 12，内容超高时自身内滚 */}
      {detail && detailTask && (
        <div
          className="schedule-detail"
          ref={detailRef}
          style={{
            left: detail.left,
            top: detail.top,
            maxHeight: `calc(100vh - 12px - ${detail.top}px)`,
          }}
        >
          {/* 顶栏两钮（2026-10-22 二次修订：完成与关闭并列——轻动作聚合；删除回归
              底部 footer，破坏性操作远离关闭热区 + 确认气泡回向上弹主路径）。
              完成 = 即时 PATCH done 不走三问缓冲；未完成系列实例走 mode=this 实体化
              单次行标完成（主行与其余实例不动，后端闸同日放开）且收起浮层（锚点
              语境已换行）；已完成取消直发——此时要么非系列要么已是例外实体 */}
          <div className="schedule-detail-topbar">
            <button
              type="button"
              className={`schedule-detail-done${detailTask.done ? " on" : ""}`}
              aria-label={detailTask.done ? "取消完成" : "标记完成"}
              title={detailTask.done ? "取消完成" : "标记完成"}
              onClick={() => {
                if (detailTask.recurrence && !detailTask.done) {
                  onUpdateTask(detailTask.id, { done: true },
                    { mode: "this", date: dateKeyOfOffset(detail.offset) });
                  setDetail(null);
                } else {
                  onUpdateTask(detailTask.id, { done: !detailTask.done });
                }
              }}
            >
              <Check size={14} strokeWidth={2.5} />
            </button>
            <button
              type="button"
              className="schedule-detail-close"
              aria-label="关闭详情"
              onClick={() => closeDetail(true)}
            >
              <X size={14} strokeWidth={2} />
            </button>
          </div>
          {editingTitle ? (
            <input
              className="schedule-detail-title-input"
              value={titleDraft}
              maxLength={TITLE_MAX_LEN}
              autoFocus
              onChange={(e) => setTitleDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") finishTitle(true);
                else if (e.key === "Escape") {
                  // 标题编辑中的 Esc 只取消编辑：阻止冒泡到 document 的关闭监听（root 处截断）
                  e.stopPropagation();
                  finishTitle(false);
                }
              }}
              onBlur={() => {
                // Enter/Esc 已主动退出的卸载 blur 被吞掉，仅失焦路径走到这里（保存）
                if (skipTitleBlur.current) {
                  skipTitleBlur.current = false;
                  return;
                }
                finishTitle(true);
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
            {detailTask.allDay ? (
              /* 全天态显示日期区（钟点无意义）；跨天附止日与天数 */
              <span>
                {detailDate(detail.offset)} 全天
                {allDaySpan(detailTask) > 1 &&
                  ` – ${detailDate(detail.offset + allDaySpan(detailTask) - 1)} · 跨 ${allDaySpan(detailTask)} 天`}
              </span>
            ) : (
              <span>
                {detailDate(detail.offset)} {fmtTime(detailTask.start)}
                {detailTask.end ? ` – ${fmtTime(detailTask.end)}` : ""}
              </span>
            )}
            {detailProject && (
              <span className="schedule-detail-project">
                <i style={{ background: detailProject.color }} />
                {detailProject.name}
              </span>
            )}
          </div>
          {/* 全天/跨天开关（批 B；真机批去日期输入）：开关只发 all_day（后端按库中
              start/end 归一日期区间）；日期变更走月/周视图的卡片拖拽（天级平移），
              起止区间在上方 meta 行只读展示 */}
          <div className="schedule-detail-allday">
            <label className="schedule-detail-allday-toggle">
              <input
                type="checkbox"
                checked={!!detailTask.allDay}
                onChange={(e) =>
                  onUpdateTask(detailTask.id, { allDay: e.target.checked })
                }
              />
              全天日程
            </label>
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
          {/* 子任务清单（批 C；真机批改直发）：清单是任务级属性（无单次实例语义），
              重复系列也 all 直发——与重复规则编辑同款先例；此前走 queueEdit 三问
              缓冲会让系列日程的勾选/添加无视觉反馈（pendingEdit 不更新浮层态） */}
          <div className="schedule-detail-subtasks">
            <SubtaskList
              items={detailTask.checklist ?? []}
              onChange={(rows) => onUpdateTask(detailTask.id, { checklist: rows })}
            />
          </div>
          {/* 任务属性行 + 时间属性行（2026-11-11 布局对齐批）：复用收件箱
              attr-row / time-row 类族两列 grid——耗时｜优先级一行、重复｜日期一行
              （与收件箱编辑区同构）；改期确认钮在 footer 操作行（对应收件箱
              edit-actions 位）；重复面板/日期面板展开占满整行 */}
          <div className="schedule-detail-attrs">
            <div className="inbox-todo-attr-row">
              {!detailTask.allDay && (
                <div className="inbox-pill-group">
                  <Timer size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="耗时" />
                  {DURATION_PRESETS.map((p) => (
                    <button
                      key={p.minutes}
                      type="button"
                      className={`inbox-todo-duration${
                        detailTask.end !== undefined &&
                        Math.round((detailTask.end - detailTask.start) * 60) === p.minutes
                          ? " on"
                          : ""
                      }`}
                      onClick={(e) => applyDetailDuration(p.minutes, e)}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              )}
              <div className="inbox-pill-group">
                <Flag size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="优先级" />
                {(["high", "medium", "low"] as const).map((pl) => (
                  <button
                    key={pl}
                    type="button"
                    style={{ color: PRIORITY_COLORS[pl] }}
                    className={`inbox-todo-prio-item${detailTask.priority === pl ? " on" : ""}`}
                    aria-label={{ high: "高", medium: "中", low: "低" }[pl]}
                    title={`设为${{ high: "高", medium: "中", low: "低" }[pl]}优先级`}
                    onClick={() => {
                      // 点已激活优先级不进三问缓冲（口径同 commitNote/finishTitle
                      // 的变更守卫）——否则"仅修改此日程"会实体化一条零变更例外行
                      if (pl !== detailTask.priority) queueEdit({ priority: pl });
                    }}
                  >
                    <i />
                  </button>
                ))}
              </div>
            </div>
            <div className="inbox-todo-time-row">
              {/* 重复规则摘要条（原独立设置行挪入左列）：改的是系列字段——
                  "不重复" = 写 undefined 抹字段；例外行（"仅修改此日程"实体化的
                  单次实体）不支持设置重复规则（后端 422 安全闸），入口禁用只读 */}
              <button
                type="button"
                className="schedule-detail-repeat-bar"
                onClick={() => {
                  setRecurrenceOpen((v) => !v);
                  setWhenOpen(false); // 与日期药丸面板互斥
                }}
                disabled={detailTask.isException}
              >
                {/* 图标替文字 Label（口径同收件箱编辑区药丸图标，2026-10-26 微调批） */}
                <span className="schedule-detail-repeat-bar-label">
                  <Repeat size={14} strokeWidth={2} />
                </span>
                <span
                  className={`schedule-detail-repeat-bar-value${detailTask.recurrence ? "" : " muted"}`}
                >
                  {detailTask.isException
                    ? "重复系列的单次例外"
                    : detailTask.recurrence
                      ? // weekly 收起态只显「每周」不列周几（收件箱同口径——列了
                        // 显示不下拉伸药丸，明细见面板圆钮）；daily/monthly 文本短照显
                        detailTask.recurrence.freq === "weekly"
                        ? "每周"
                        : recurrenceText(detailTask.recurrence, detailTask.dateOffset, "、")
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
              {!detailTask.allDay && (
                <WhenPicker
                  value={whenDraft}
                  onChange={setWhenDraft}
                  open={whenOpen}
                  onOpenChange={(o) => {
                    setWhenOpen(o);
                    if (o) setRecurrenceOpen(false);
                  }}
                />
              )}
            </div>
            {/* 重复面板（radio 频率档 + weekly 的星期几多选圆钮）：整行展开 */}
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
                          onUpdateTask(detailTask.id, {
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
                                    onUpdateTask(detailTask.id, {
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
          {/* 会后纪要（2026-11-10；入口 2026-11-11 布局对齐批挪 footer 图标钮）：
              已结束日程粘贴纪要原文提炼行动项（minutes 卡落助手面板，提取不生成
              守反定位）；录入区展开（minutesOpen）或回看区块有内容（批 2：该会议
              已采纳的纪要行动项 sourceKey 前缀匹配清单 + 完成态勾选可切换）时
              才渲染本块——收起态零占位，不再有独立文字钮行 */}
          {detailEnded &&
            (minutesOpen ||
              (tasks ?? []).some((t) => t.sourceKey === `minutes:${detailTask.id}`)) && (
            <div className="schedule-detail-minutes">
              {(() => {
                const mTasks = (tasks ?? []).filter(
                  (t) => t.sourceKey === `minutes:${detailTask.id}`,
                );
                if (!mTasks.length) return null;
                const doneN = mTasks.filter((t) => t.done).length;
                return (
                  <>
                    <p className="schedule-detail-minutes-summary">
                      纪要行动项 {doneN}/{mTasks.length} 完成
                    </p>
                    <ul className="schedule-detail-minutes-list">
                      {mTasks.map((t) => (
                        <li key={t.id} className="schedule-detail-minutes-row">
                          <button
                            type="button"
                            className={`schedule-detail-minutes-done${t.done ? " on" : ""}`}
                            aria-label={t.done ? "撤销完成" : "标记完成"}
                            title={t.done ? "撤销完成" : "标记完成"}
                            onClick={() => onUpdateTask(t.id, { done: !t.done })}
                          >
                            <Check size={11} strokeWidth={2.5} />
                          </button>
                          <span className={`schedule-detail-minutes-title${t.done ? " done" : ""}`}>
                            {t.title}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                );
              })()}
              {minutesOpen && (
                <>
                  <div className="schedule-detail-minutes-wrap">
                    <textarea
                      className="schedule-detail-minutes-input"
                      placeholder="粘贴纪要原文…（Ctrl+Enter 提炼）"
                      maxLength={8000}
                      value={minutesText}
                      autoFocus
                      onChange={(e) => {
                        setMinutesText(e.target.value);
                        if (minutesError) setMinutesError(null);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing)
                          void submitMinutesText();
                      }}
                    />
                    <span className="schedule-detail-minutes-count">
                      {minutesText.length}/8000
                    </span>
                  </div>
                  <div className="schedule-detail-minutes-actions">
                    <button
                      type="button"
                      className="schedule-detail-minutes-cancel"
                      disabled={minutesBusy}
                      onClick={() => {
                        setMinutesOpen(false);
                        setMinutesError(null);
                      }}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="schedule-detail-minutes-submit"
                      disabled={minutesBusy}
                      onClick={() => void submitMinutesText()}
                    >
                      {minutesBusy ? "提炼中…" : "提炼行动项"}
                    </button>
                  </div>
                  {minutesError && <p className="schedule-detail-minutes-error">{minutesError}</p>}
                </>
              )}
            </div>
          )}
          <div className="schedule-detail-footer">
            {/* 会后纪要入口（2026-11-11 布局对齐批挪 footer 图标钮——原中段独立
                文字钮行挤占布局且与操作行空白割裂）：仅已结束日程渲染，点击在上方
                展开录入区 */}
            {detailEnded && (
              <button
                type="button"
                className="inbox-todo-icon"
                aria-label="会后纪要"
                title="会后纪要"
                onClick={() => {
                  setMinutesOpen(true);
                  setMinutesError(null);
                }}
              >
                <NotebookPen size={14} strokeWidth={2} />
              </button>
            )}
            {/* 改期确认（2026-11-11 布局对齐批挪入 footer 操作行——对应收件箱
                edit-actions 位）：时间行日期药丸选完草稿在此确认（保时长平移）；
                全天日程改期走月/周拖拽不渲染 */}
            {!detailTask.allDay && (
              <button
                type="button"
                className="inbox-todo-icon"
                aria-label="改期"
                title={whenChanged ? "按所选时间改期" : "未修改时间"}
                disabled={!whenChanged}
                onClick={applyDetailWhen}
              >
                <CalendarSync size={14} strokeWidth={2} />
              </button>
            )}
            {/* 删除日程：二次确认气泡（任务已删草稿无意义，确认后不走 closeDetail 提交路径）；
                重复系列纵向三选——仅删本次 = 后端墓碑挖空该日实例（系列行保留）、删及后续 =
                UNTIL 截断到被删日前一天、删除所有 = 整任务删（后端按生命周期判定物理删/软删）；
                非重复维持两钮直接删；2026-10-22 曾短暂迁顶栏后回归（破坏性操作远离关闭热区，
                底部带「删除」文字更明确） */}
            <ConfirmPopover
              message={detailTask.recurrence ? "该日程属于重复系列" : "确认删除该日程？"}
              actions={
                detailTask.recurrence
                  ? [
                      {
                        text: "仅删除本次",
                        tone: "danger-outline",
                        onClick: () => {
                          onDeleteTask(detailTask.id, { mode: "this", date: dateKeyOfOffset(detail.offset) });
                          setDetail(null);
                        },
                      },
                      {
                        text: "删除此日程及后续",
                        tone: "danger-outline",
                        onClick: () => {
                          onDeleteTask(detailTask.id, { mode: "following", date: dateKeyOfOffset(detail.offset) });
                          setDetail(null);
                        },
                      },
                      {
                        text: "删除所有",
                        onClick: () => {
                          onDeleteTask(detailTask.id);
                          setDetail(null);
                        },
                      },
                    ]
                  : undefined
              }
              onConfirm={() => {
                onDeleteTask(detailTask.id);
                setDetail(null);
              }}
              buttonClass="schedule-detail-delete"
              buttonTitle="删除"
              icon={<Trash2 size={13} strokeWidth={2} />}
              label="删除"
            />
          </div>
          {/* 应用范围气泡（重复系列编辑三问）：pendingEdit 聚合了待应用的编辑，选范围后
              统一提交——this/following 后端已换成新行（例外实体/新主记录），关浮层（锚点
              失效）；all 更新主记录本体、浮层保持打开即时联动；取消丢弃编辑并回滚备注草稿 */}
          {pendingEdit && (
            <div className="schedule-detail-apply" role="dialog" aria-label="选择修改应用范围">
              <p className="schedule-detail-apply-message">修改应用于？</p>
              <div className="schedule-detail-apply-actions">
                <button
                  type="button"
                  className="schedule-detail-apply-action"
                  onClick={() => {
                    onUpdateTask(detailTask.id, pendingEdit, {
                      mode: "this",
                      date: dateKeyOfOffset(detail.offset),
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
                    onUpdateTask(detailTask.id, pendingEdit, {
                      mode: "following",
                      date: dateKeyOfOffset(detail.offset),
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
                    onUpdateTask(detailTask.id, pendingEdit);
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
      {/* 重复系列范围三问（2026-11-12 拖拽三问批）：拖拽/拉伸/agenda 拖动重复
          实例落定后弹于落点——文案与浮层三问气泡一致（「修改应用于？」） */}
      {scopeAsk && (
        <SeriesScopeAsk
          x={scopeAsk.x}
          y={scopeAsk.y}
          title="修改应用于？"
          onPick={(m) => applyScopePick(m, scopeAsk.move)}
          onCancel={() => setScopeAsk(null)}
        />
      )}
    </section>
  );
}

export default ScheduleCanvas;
