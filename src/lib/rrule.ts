// RRULE（RFC 5545 子集）↔ 前端 Recurrence 判别联合的双向转换。
// 后端库里 recurrence_rule 存 RRULE 字符串（如 FREQ=WEEKLY;BYDAY=MO,WE,FR），API 原样透传，
// 本模块在数据入口（api.ts）把字符串解析成组件消费的判别联合，UI 定稿代码零改动；
// 写路径（toRrule）把编辑结果序列化回字符串——与后端 services/seed.py 的 _to_rrule 同构。
import type { Recurrence } from "../data/tasks";

// RFC 5545 星期码 → ISO 星期序号（1=周一…7=周日）
const WEEKDAY_BY_CODE: Record<string, number> = { MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6, SU: 7 };

// ISO 星期序号 → RFC 5545 星期码（toRrule 用，与 WEEKDAY_BY_CODE 互逆）
const WEEKDAY_CODES: Record<number, string> = { 1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU" };

/**
 * 解析 RRULE 字符串；不认识/不完整一律返回 null（调用方降级为"不重复"，读路径不崩）。
 * 支持的子集：FREQ=DAILY / FREQ=MONTHLY / FREQ=WEEKLY;BYDAY=MO,WE,FR（weekly 必带非空
 * BYDAY），均可尾随 ;UNTIL=YYYYMMDDTHHMMSS（"删除此日程及后续"的截断尾，解析为
 * until 日期键——最后可发生日，当日仍出现、次日起不再）。出现 INTERVAL/COUNT 等
 * 其余超集键时同样返回 null——现有"真展开"语义无法表达，宁可不重复也不错排。
 */
export function parseRrule(rrule: string | null | undefined): Recurrence | null {
  if (!rrule) return null;
  const kv = new Map<string, string>();
  for (const part of rrule.split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) return null; // 空段或无等号：格式坏
    kv.set(part.slice(0, eq), part.slice(eq + 1));
  }
  if ([...kv.keys()].some((k) => k !== "FREQ" && k !== "BYDAY" && k !== "UNTIL")) return null; // 超集键，不认
  const until = kv.get("UNTIL");
  if (until !== undefined && !/^\d{8}T\d{6}$/.test(until)) return null; // 只认 DATE-TIME 形式
  // UNTIL 日期段 YYYYMMDD → "YYYY-MM-DD" 日期键（与 cancelledDates 同口径，只取日期）
  const untilKey =
    until && `${until.slice(0, 4)}-${until.slice(4, 6)}-${until.slice(6, 8)}`;
  switch (kv.get("FREQ")) {
    case "DAILY":
      return { freq: "daily", until: untilKey };
    case "MONTHLY":
      return { freq: "monthly", until: untilKey };
    case "WEEKLY": {
      const weekdays = (kv.get("BYDAY") ?? "")
        .split(",")
        .filter(Boolean)
        .map((c) => WEEKDAY_BY_CODE[c]);
      if (weekdays.length === 0 || weekdays.some((n) => n === undefined)) return null;
      // 升序保证 recurrenceText 展示与 toggleWeekday 输入稳定
      return { freq: "weekly", weekdays: weekdays.sort((a, b) => a - b), until: untilKey };
    }
    default:
      return null; // 缺 FREQ 或未知频率
  }
}

// until 日期键 → ";UNTIL=YYYYMMDDT235959" 尾：T235959 = "前一晚 23:59:59"——until
// 当日实例保留、次日砍掉，与后端"删除此日程及后续"的生成形态一致
const untilTail = (until: string) => `;UNTIL=${until.replace(/-/g, "")}T235959`;

/**
 * Recurrence → RRULE 字符串（写路径序列化，与后端校验子集、seed 生成口径一致）。
 * undefined/null → null（清空 = 不重复）。weekly 的 weekdays 复制后升序映射——排序保证
 * 同规则产出同一字符串（与 parseRrule、后端 seed._to_rrule 的形态稳定一致）。
 */
export function toRrule(rec: Recurrence | undefined | null): string | null {
  if (!rec) return null;
  const tail = rec.until ? untilTail(rec.until) : "";
  if (rec.freq === "daily") return "FREQ=DAILY" + tail;
  if (rec.freq === "monthly") return "FREQ=MONTHLY" + tail;
  return (
    "FREQ=WEEKLY;BYDAY=" +
    [...rec.weekdays].sort((a, b) => a - b).map((n) => WEEKDAY_CODES[n]).join(",") +
    tail
  );
}
