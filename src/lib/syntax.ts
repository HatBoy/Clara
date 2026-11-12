// 快捷录入框的符号指令解析引擎（纯函数、零副作用）：
//   # 项目归属（匹配已有项目名，最长前缀，支持含空格名如 #Alpha Launch）
//   ~ 预估耗时（~30m / ~2h / ~1.5h / ~2h30m → 分钟）
//   @ 时间与周期（@9:00 = 今天 9 点 / @明天 / @明天9:00 / @每天 / @每周一三五 → Recurrence）
// 设计原则：确定性文法 + "未识别即留在标题当普通文本"（视觉确认：不高亮 = 系统没懂），
// 绝不猜测意图——只有耗时没有 @ 的输入落收件箱，排期交给用户拖拽。
// 用户定稿的语义决策（2026-09-14）：日期无钟点默认 09:00；未知 # 项目名（1..20 字）
// 回车提交时顺手创建该项目；已排期任务创建后由调用方跳转查看该日。
import type { Recurrence } from "../data/tasks";
import { PROJECT_NAME_MAX_LEN } from "../data/tasks";

// 时间轴区间默认值（parseInput 的 hours 参数缺省时使用；实际值由设置驱动，
// ScheduleCanvas 经 QuickAdd 透传进来）：钟点越界按未识别处理——画布画不了的
// 时间不诚实接受（区间前 / 区间末端画在时间轴外会被裁剪不可见）
const DEFAULT_BOUNDS = { start: 8, end: 22 };

// 日期无钟点的默认落点（用户决策）：周期即每天 09:00 基准，卡片 9:00–10:00
const DEFAULT_HOUR = 9;

// 高亮 span（镜像层消费）：kind 决定配色——#项目 蓝底 / @时间 红字 / ~耗时 橙字。
// 未识别的符号串不产生 span（渲染为普通文本）
export type SyntaxToken = {
  start: number;
  end: number;
  kind: "project" | "duration" | "when";
};

// 解析产物：title = 已消费 token 剥离 + 空白折叠；同类多个 token 后者胜（更近的
// 输入是最新意图，被覆盖者同样从标题剥离）
export type ParsedInput = {
  title: string;
  /** 已有项目名 */
  project?: string;
  /** 未知但可建的项目名（1..PROJECT_NAME_MAX_LEN 字、不含符号）：提交时顺手创建并归属 */
  newProject?: string;
  /** ~ 耗时（分钟，1..1440） */
  minutes?: number;
  /** 以下三字段置位 = 已排期（@日期 或 @周期 都算；周期锚今天） */
  dateOffset?: number;
  /** 钟点（小时小数），显式或默认 9 */
  startHour?: number;
  /** startHour + minutes/60，clamp ≤ 区间末端（越界截断，后端按 end 重算 duration 自洽） */
  endHour?: number;
  /** @每天 / @每周X / @每月 → 周期规则（入库经 toRrule） */
  recurrence?: Recurrence;
  /** @全天 / @明天全天（2026-11-06 批 B）：全天日程——钟点强制 0、minutes 忽略
   *（占天不占时段，后端 duration 恒空）；周期 + 全天组合不支持 */
  allDay?: boolean;
  tokens: SyntaxToken[];
};

// 中文星期字符 → ISO 序号（1=周一…7=周日；日/天同指周日）
const WEEKDAY_CHARS: Record<string, number> = {
  一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7,
};

// 今天 0 点（每次调用现取，避免模块级常量跨午夜失准）
const todayZero = () => {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());
};

// 本周（weekShift=0）或下周（=1）的周 w（ISO 1..7）相对今天的偏移；
// 本周该日早于今天则顺延下周（周六说"周五"指下周五的直觉语义）
function weekdayOffset(w: number, weekShift: number): number {
  const today = todayZero();
  const monday = new Date(
    today.getFullYear(),
    today.getMonth(),
    today.getDate() - ((today.getDay() + 6) % 7),
  );
  const target = new Date(
    monday.getFullYear(),
    monday.getMonth(),
    monday.getDate() + (w - 1) + weekShift * 7,
  );
  const offset = Math.round((target.getTime() - today.getTime()) / 86_400_000);
  return weekShift === 0 && offset < 0 ? offset + 7 : offset;
}

// 当年 M 月 D 日相对今天的偏移；非法日期（如 2 月 30 日，原生构造器会静默进位）返回 null
function monthDayOffset(month: number, day: number): number | null {
  const today = todayZero();
  const d = new Date(today.getFullYear(), month - 1, day);
  if (d.getMonth() !== month - 1 || d.getDate() !== day) return null;
  return Math.round((d.getTime() - today.getTime()) / 86_400_000);
}

/** 日期前缀解析：命中返回 { offset, rest=剩余未消费部分 }，否则 null */
function parseDatePrefix(s: string): { offset: number; rest: string } | null {
  for (const [word, offset] of [["今天", 0], ["明天", 1], ["后天", 2]] as const) {
    if (s.startsWith(word)) return { offset, rest: s.slice(word.length) };
  }
  if (s.startsWith("下周")) {
    const w = WEEKDAY_CHARS[s[2]];
    return w !== undefined ? { offset: weekdayOffset(w, 1), rest: s.slice(3) } : null;
  }
  const weekPrefixLen = s.startsWith("星期") ? 2 : s.startsWith("周") ? 1 : 0;
  if (weekPrefixLen > 0) {
    const w = WEEKDAY_CHARS[s[weekPrefixLen]];
    return w !== undefined
      ? { offset: weekdayOffset(w, 0), rest: s.slice(weekPrefixLen + 1) }
      : null;
  }
  // M月D日（当年，"日"可省；@9月15日）
  const md = s.match(/^(\d{1,2})月(\d{1,2})日?/);
  if (md) {
    const offset = monthDayOffset(Number(md[1]), Number(md[2]));
    return offset !== null ? { offset, rest: s.slice(md[0].length) } : null;
  }
  // MMDD 四位数字（当年，如 @0915 = 9 月 15 日）
  if (/^\d{4}/.test(s)) {
    const offset = monthDayOffset(Number(s.slice(0, 2)), Number(s.slice(2, 4)));
    return offset !== null ? { offset, rest: s.slice(4) } : null;
  }
  return null;
}

/** 钟点解析：H、H:mm（如 9 / 9:00 / 21:30），有效区间 [bounds.start, bounds.end) */
function parseTimePrefix(
  s: string,
  bounds: { start: number; end: number },
): { hour: number; rest: string } | null {
  const m = s.match(/^(\d{1,2})(?::(\d{2}))?/);
  if (!m) return null;
  const minutes = m[2] !== undefined ? Number(m[2]) : 0;
  const hour = Number(m[1]) + minutes / 60;
  if (minutes > 59 || hour < bounds.start || hour >= bounds.end) return null;
  return { hour, rest: s.slice(m[0].length) };
}

// @ 的解析产物：dateOffset 恒有（周期锚今天、纯钟点=今天）；time/recurrence/allDay 可选
type WhenSpec = { dateOffset: number; time?: number; recurrence?: Recurrence; allDay?: boolean };

// "每"周期分支：每天 / 每周+中文星期串（可多选，如 每周一三五）/ 每月，后可紧邻钟点
function parseRecurrence(
  s: string,
  bounds: { start: number; end: number },
): WhenSpec | null {
  let rec: Recurrence | undefined;
  let rest = s;
  if (rest.startsWith("天")) {
    rec = { freq: "daily" };
    rest = rest.slice(1);
  } else if (rest.startsWith("月")) {
    rec = { freq: "monthly" };
    rest = rest.slice(1);
  } else if (rest.startsWith("周")) {
    rest = rest.slice(1);
    const weekdays: number[] = [];
    while (rest.length > 0 && WEEKDAY_CHARS[rest[0]] !== undefined) {
      weekdays.push(WEEKDAY_CHARS[rest[0]]);
      rest = rest.slice(1);
    }
    if (weekdays.length === 0) return null; // 裸"每周"无星期限定，不支持
    rec = { freq: "weekly", weekdays: [...new Set(weekdays)].sort((a, b) => a - b) };
  } else {
    return null;
  }
  let time: number | undefined;
  if (rest !== "") {
    const t = parseTimePrefix(rest, bounds);
    if (!t || t.rest !== "") return null; // 尾部有垃圾 = 未识别
    time = t.hour;
  }
  return { dateOffset: 0, time, recurrence: rec };
}

function parseWhen(
  body: string,
  bounds: { start: number; end: number },
): WhenSpec | null {
  if (body === "") return null;
  if (body.startsWith("每")) return parseRecurrence(body.slice(1), bounds);
  // 日期前缀（可选）+ 紧邻钟点（可选）；整段必须消费完，尾部垃圾 = 未识别。
  // 钟点是可选的：纯日期（@明天 / @周五）与纯钟点（@9:00 = 今天）都合法——
  // 纯日期无钟点 → time undefined，由 parseInput 补默认 9（用户决策）。
  // 全天尾巴（批 B）：@全天（今天）/ @明天全天 / @11月10日全天——置 allDay，
  // 钟点不补（收口处置 0）
  const date = parseDatePrefix(body);
  const rest = date ? date.rest : body;
  if (rest === "全天") {
    return { dateOffset: date ? date.offset : 0, allDay: true };
  }
  const time = parseTimePrefix(rest, bounds);
  if (time ? time.rest !== "" : rest !== "") return null;
  return { dateOffset: date ? date.offset : 0, time: time?.hour };
}

// ~ 耗时：2h / 1.5h / 2h30m / 30m → 分钟；越界（1..1440）或坏格式返回 null
function parseDuration(body: string): number | null {
  const hm = body.match(/^(\d+(?:\.\d+)?)h(?:(\d+)m)?$/);
  if (hm) {
    const mins = Math.round(Number(hm[1]) * 60) + (hm[2] ? Number(hm[2]) : 0);
    return mins >= 1 && mins <= 1440 ? mins : null;
  }
  const mm = body.match(/^(\d+)m$/);
  if (mm) {
    const mins = Number(mm[1]);
    return mins >= 1 && mins <= 1440 ? mins : null;
  }
  return null;
}

// # 后按已有项目名做最长前缀匹配（前缀重叠时长名胜，如 "Alpha" 与 "Alpha Launch"）
function matchProjectName(after: string, names: string[]): string | undefined {
  let best: string | undefined;
  for (const name of names) {
    if (name && after.startsWith(name) && (best === undefined || name.length > best.length)) {
      best = name;
    }
  }
  return best;
}

/**
 * 解析快捷录入文本。token 触发条件：# / ~ / @ 位于串首或空白之后（防 a@b.com、C# 误伤）。
 * 已消费片段 = 识别成功的 token + 未知项目候选；其余原样进标题。
 * hours = 时间轴合法钟点域（设置驱动，缺省 8–22）。
 */
export function parseInput(
  text: string,
  projects: { name: string }[],
  hours?: { start: number; end: number },
): ParsedInput {
  const bounds = hours ?? DEFAULT_BOUNDS;
  const names = projects.map((p) => p.name);
  const out: ParsedInput = { title: "", tokens: [] };
  // 标题 = 未消费片段拼接后折叠空白
  const parts: string[] = [];
  let cursor = 0; // 当前未消费片段的起点
  const consume = (to: number) => {
    if (to > cursor) parts.push(text.slice(cursor, to));
    cursor = to;
  };
  // token 命中：先把 token 之前的普通文本收进标题，再把 cursor 越过 token 本身——
  // token 永不进标题（"指令从标题剥离"的实现点；直接 consume(token 末尾) 会把
  // 前缀普通文本连同 token 自身整段吞进标题，即旧 bug）
  const skipToken = (from: number, to: number) => {
    consume(from);
    cursor = to;
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    const atTokenStart = i === 0 || /\s/.test(text[i - 1]);
    if (!atTokenStart || (ch !== "#" && ch !== "~" && ch !== "@")) {
      i++;
      continue;
    }
    const after = text.slice(i + 1);
    const body = after.match(/^\S+/)?.[0] ?? "";
    if (ch === "#") {
      const name = matchProjectName(after, names);
      if (name) {
        out.tokens.push({ start: i, end: i + 1 + name.length, kind: "project" });
        out.project = name;
        skipToken(i, i + 1 + name.length);
        i += 1 + name.length;
        continue;
      }
      // 未知项目候选：不含符号、长度 1..20（超长建不了项目，留在标题）
      if (body.length >= 1 && body.length <= PROJECT_NAME_MAX_LEN && /^[^#~@]+$/.test(body)) {
        out.newProject = body;
        skipToken(i, i + 1 + body.length);
        i += 1 + body.length;
        continue;
      }
    } else if (ch === "~") {
      const mins = parseDuration(body);
      if (mins !== null) {
        out.tokens.push({ start: i, end: i + 1 + body.length, kind: "duration" });
        out.minutes = mins;
        skipToken(i, i + 1 + body.length);
        i += 1 + body.length;
        continue;
      }
    } else {
      // @ 分支的 body 允许尾随「 全天」（2026-11-06 真机批：用户自然输入
      // 「@后天 全天」带空格——rawBody 保留原始长度供 skipToken/高亮区间，
      // 归一后交给 parseWhen 判定）
      const rawBody = after.match(/^\S+(?:\s+全天)?/)?.[0] ?? "";
      const when = parseWhen(rawBody.replace(/\s+全天$/, "全天"), bounds);
      if (when) {
        out.tokens.push({ start: i, end: i + 1 + rawBody.length, kind: "when" });
        out.dateOffset = when.dateOffset;
        out.startHour = when.time; // 纯日期无钟点 → undefined，循环后补默认
        out.recurrence = when.recurrence;
        out.allDay = when.allDay;
        skipToken(i, i + 1 + rawBody.length);
        i += 1 + rawBody.length;
        continue;
      }
    }
    // 未识别：符号留在标题里当普通文本
    i++;
  }
  consume(text.length);
  out.title = parts.join(" ").replace(/\s+/g, " ").trim();
  // 排期默认值收口（同类 token 后者胜已定）：无钟点补 9（clamp 进合法域防窄区间跑飞）；
  // 有耗时推 end（跨区间末端截断，后端按 end 重算 duration 自洽）。
  // 全天（批 B）：钟点固定 0（后端归一同口径）、minutes/endHour 不带（占天不占时段）
  if (out.dateOffset !== undefined) {
    if (out.allDay) {
      out.startHour = 0;
      out.minutes = undefined;
      out.endHour = undefined;
    } else {
      if (out.startHour === undefined) {
        out.startHour = Math.min(Math.max(DEFAULT_HOUR, bounds.start), bounds.end - 0.25);
      }
      if (out.minutes !== undefined) {
        out.endHour = Math.min(out.startHour + out.minutes / 60, bounds.end);
      }
    }
  }
  return out;
}
