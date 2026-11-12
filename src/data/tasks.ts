// 统一任务数据契约与纯函数：数据本体经 src/lib/api.ts 从 FastAPI 后端拉取
// （后端存绝对时间与 RRULE 字符串，api.ts 转换层负责换算回本文件的相对时间体系），
// 本文件不再持有 mock 数据。
// 未排期任务显示在收件箱/项目右栏列表，已排期任务显示在中栏日历；
// 拖拽可在区域间流转（收件箱 → 项目归属、收件箱/项目 → 日历排期），本期修改仅存内存、刷新还原。
// 重复规则：判别联合（由 api.ts 从后端 RRULE 字符串解析而来），空 = 不重复。
// weekly 的 weekdays 为 ISO 星期序号 1=周一…7=周日（至少一项）；monthly = 每月同基准日（getDate()）。
// until = 截断日（"YYYY-MM-DD" 日期键，RRULE 尾的 UNTIL 解析而来）："删除此日程及后续"
// 落点——until 当日实例保留、次日起不再出现（occursOn 判定；改频率/星期几构造的新规则
// 不带 until = 重定义系列、截断自然清除）。
// "真展开"语义：基准日（dateOffset）起（含）在匹配日自动显示同卡；拖动/编辑作用于整个
// 系列；删除分三支（2026-09-14 起）——仅删本次 = 后端墓碑行（经 cancelledDates 下发挖空）、
// 删及后续 = until 截断、删除所有 = 整任务删（后端 6 场景判定物理删/软删）
export type Recurrence =
  | { freq: "daily"; until?: string }
  | { freq: "weekly"; weekdays: number[]; until?: string }
  | { freq: "monthly"; until?: string };

export type Task = {
  id: string;
  title: string;
  priority: "high" | "medium" | "low";
  /** 所属项目 name（须与 Project 的 name 对齐），无则不属于任何项目 */
  project?: string;
  /** 时长展示文本（如 "约 30 分钟"，待办列表侧展示）——api.ts 从 minutes 派生 */
  duration?: string;
  /** 排期耗时（分钟）：拖入日历时换算为日程时长，无则只排开始时刻 */
  minutes?: number;
  /** 执行备注（如注意点、参考链接）：仅在待办卡展开编辑区可见，收起态不展示 */
  note?: string;
  /** 重复规则：空 = 不重复（真展开语义见 Recurrence 与 occursOn） */
  recurrence?: Recurrence;
  /** 全天/跨天（2026-11-06 批 B）：已排期子集上有效——start=0 点（当日 0 时）、
   *  end=跨整天数×24（end ?? 24 = 单日）。占天不占时段：不进时间轴泳道与小时
   *  负载，三视图以「全天条/置顶卡」呈现；duration 口径恒空 */
  allDay?: boolean;
  /** 子任务清单（2026-11-06 批 C）：会议议程/购物清单——编辑语义 = 全量替换
   *  （api.ts toTaskPatch 把整组翻译为 subtasks 键；空 = 清空）。id 由后端生成，
   *  仅展示行携带（编辑产出行无 id，宽化为可选避免两套行类型） */
  checklist?: { id?: string; title: string; done: boolean }[];
  /** 活体例外行（"仅修改此日程"实体化的单次实体，后端 parent_id 非空——api.ts 派生）：
   *  语义上是重复系列某实例的单次化副本，不支持设置重复规则（后端 422 安全闸防
   *  "独立系列 + 主系列挖空日期"双重身份）——编辑 UI 据此禁用重复设置入口 */
  isException?: boolean;
  /** 重复系列的已取消实例日期（"YYYY-MM-DD" 本地绝对日期；后端由墓碑行聚合、经
   *  cancelled_dates 字段下发）：occursOn 展开时跳过这些日期——"仅删除本次"的
   *  单次例外落点，非重复任务不用；前端只读不写（追加走 DELETE mode=this） */
  cancelledDates?: string[];
  /** 已完成（后端 done_at 非空派生）：完成 ≠ 删除——未排期完成任务从收件箱消失，
   *  已排期任务保留在日历（时段已消耗，排期 busy 避让不受影响）。撤销走
   *  PATCH done:false（写路径经 toTaskPatch 翻译）；系列任务 V1 不支持完成（入口隐藏） */
  done?: boolean;
  /** 溯源键（后端 source_key 派生，2026-11-10 批 2）："minutes:<会议id>" = 会后
   *  纪要行动项（详情浮层回看区块按此前缀匹配）；采集溯源键同字段，前端只读 */
  sourceKey?: string;
  /** 以下三项 dateOffset 与 start 同时有值即"已排期"（dateOffset 为相对运行当天的天偏移；小时小数表分钟） */
  dateOffset?: number;
  start?: number;
  /** 无结束时间的日程仅展示开始时刻（默认占 1 小时卡） */
  end?: number;
};

// 已排期任务（类型谓词收窄用）：日历画布只消费这一子集
export type ScheduledTask = Task & { dateOffset: number; start: number };
export const isScheduled = (t: Task): t is ScheduledTask =>
  typeof t.dateOffset === "number" && typeof t.start === "number";

// 优先级取色（收件箱编辑区与日历详情浮层的优先级圆点共用，与日历卡 prio-* 色条同源）
export const PRIORITY_COLORS: Record<Task["priority"], string> = {
  high: "var(--danger)",
  medium: "var(--orange)",
  low: "var(--green)",
};

// 字段长度契约（与后端 schemas.py 一致）：标题 1..30、备注 0..500（可为空 = 写 undefined 抹字段），
// 日程与待办共用同一限制；输入面用 maxLength 硬限制，标题下限由提交时 trim 非空守卫保证
export const TITLE_MAX_LEN = 30;
export const NOTE_MAX_LEN = 500;

// 耗时展示文本（分钟 → "约 30 分钟" / "约 2 小时"）：toFrontendTask 派生、排期重算、
// 耗时快捷档三处共用同一口径——≥1 小时且被 60 整除展示小时，否则分钟
export const durationText = (minutes: number) =>
  minutes >= 60 && minutes % 60 === 0 ? `约 ${minutes / 60} 小时` : `约 ${minutes} 分钟`;

// offset → 绝对日期（运行当天为 0；原生构造器跨月/跨年自动进位，中国无 DST 无偏移）
const dateOfOffset = (offset: number) => {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d;
};

// offset → 绝对日期键 "YYYY-MM-DD"：重复系列取消实例的存储与比对格式（后端
// cancelled_dates 同口径）；ScheduleCanvas"仅删除本次"时用它生成要追加的日期键
export const dateKeyOfOffset = (offset: number) => {
  const d = dateOfOffset(offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

// 日期键 ± 天（"删除此日程及后续"算 until = 被删日前一天用；原生构造器跨月/跨年进位）
export const shiftDateKey = (key: string, days: number) => {
  const [y, m, d] = key.split("-").map(Number);
  const dt = new Date(y, m - 1, d + days);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
};

// 星期序号 1..7 → 中文（索引 n-1；与 ScheduleCanvas 的 WEEKDAY_NAMES[getDay()] 不同序）
export const RECURRENCE_WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

// 频率档（浮层与收件箱编辑区的 radio 面板共用）
export const RECURRENCE_FREQ_OPTIONS = [
  { value: "none", label: "不重复" },
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
] as const;

// 规则 → 展示文本：sep ""（卡片/列表紧凑式"每周一三五"、"每月15日"）；sep "、"（摘要行"每周一、三、五"）。
// monthly 取 anchorOffset 的 getDate()——occursOn 保证实例日与基准日同 getDate()，传实例 offset 也正确；
// 无基准（未排期预设）退化为 "每月"
export const recurrenceText = (r: Recurrence, anchorOffset?: number, sep = ""): string => {
  switch (r.freq) {
    case "daily":
      return "每天";
    case "weekly":
      return `每周${r.weekdays.map((n) => RECURRENCE_WEEKDAY_LABELS[n - 1]).join(sep)}`;
    case "monthly":
      return anchorOffset !== undefined ? `每月${dateOfOffset(anchorOffset).getDate()}日` : "每月";
  }
};

// 切换频率构造新规则（浮层/收件箱面板共用）：切到 weekly 沿用已有勾选，否则默认基准日周几（无基准默认周一）
export const setRecurrenceFreq = (
  current: Recurrence | undefined,
  freq: "none" | "daily" | "weekly" | "monthly",
  anchorOffset?: number,
): Recurrence | undefined => {
  switch (freq) {
    case "none":
      return undefined;
    case "daily":
      return { freq: "daily" };
    case "monthly":
      return { freq: "monthly" };
    case "weekly":
      return {
        freq: "weekly",
        weekdays:
          current?.freq === "weekly" && current.weekdays.length > 0
            ? current.weekdays
            : [anchorOffset !== undefined ? ((dateOfOffset(anchorOffset).getDay() + 6) % 7) + 1 : 1],
      };
  }
};

// 星期多选切换：升序去重；结果为空返回 null = 拒绝（最后一个不可取消）
export const toggleWeekday = (weekdays: number[], n: number): number[] | null => {
  const next = weekdays.includes(n)
    ? weekdays.filter((w) => w !== n)
    : [...weekdays, n].sort((a, b) => a - b);
  return next.length > 0 ? next : null;
};

// 重复任务是否在 offset 日出现（展开范围 = 基准日起（含），防 daily 淹没全窗口月视图）：
// 不重复仅基准日；每天 = 基准起每日；每周 = 星期几 ∈ weekdays；每月 = 同 getDate()（31 号遇短月跳过）；
// until 截断（日期键 > until）与已取消实例（cancelledDates 墓碑日期）都跳过——
// "删及后续"/"仅删本次"的展开语义
export const occursOn = (task: ScheduledTask, offset: number): boolean => {
  if (offset < task.dateOffset) return false;
  const dateKey = dateKeyOfOffset(offset);
  const r = task.recurrence;
  if (!r) return offset === task.dateOffset; // 挖空只对系列生效（后端聚合同口径）——非重复行不受 cancelledDates 影响
  if (task.cancelledDates?.includes(dateKey)) return false;
  if (r.until && dateKey > r.until) return false; // until 当日仍发生、次日起不再
  switch (r.freq) {
    case "daily":
      return true;
    case "weekly":
      return r.weekdays.includes(((dateOfOffset(offset).getDay() + 6) % 7) + 1);
    case "monthly":
      return dateOfOffset(offset).getDate() === dateOfOffset(task.dateOffset).getDate();
  }
};

// 重复系列是否仍活着（UNTIL 未越过今天）——项目页"已排期"开关的放行条件：
// 日历 occursOn 天然隐藏死系列，列表侧需要显式判定（until 当日及之前 = 活）
export const seriesAlive = (t: Task): boolean => {
  const until = t.recurrence?.until;
  return until === undefined || until >= dateKeyOfOffset(0);
};

// 项目（侧栏分组与任务 project 字段都以 name 为唯一标识）；starred = 重点项目（侧栏排序靠前）。
// id 为后端 UUID（写路径阶段项目寻址将改用 id——name 可变，不是稳定标识）
export type Project = { id?: string; name: string; color: string; starred?: boolean };
// 项目名称长度上限（重命名输入框 maxLength，与新建共用）
export const PROJECT_NAME_MAX_LEN = 20;
// 项目十色板（新建项目随机取一）：与 tokens.css 现有 accent/green/orange/danger 同族
// （Tailwind 500 系），色相均匀分布
export const PROJECT_COLORS = [
  "#6366f1", // 靛紫（--accent 同值）
  "#3b82f6", // 蓝
  "#06b6d4", // 青
  "#22c55e", // 绿（--green 同值）
  "#84cc16", // 草绿
  "#f59e0b", // 琥珀（--orange 同值）
  "#f97316", // 橙
  "#ef4444", // 红（--danger 同值）
  "#ec4899", // 粉
  "#8b5cf6", // 紫
];
