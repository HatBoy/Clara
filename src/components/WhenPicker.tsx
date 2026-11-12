// 自绘日期时间选择器（2026-10-26 抽取共享）：药丸整钮（Calendar 图标 + 摘要 +
// chevron）开内联面板——月导航 + 周一首列 42 格月历 + 小时/分钟双列滚动列表。
// 原生 datetime-local 弹层系浏览器 shadow DOM 无法美化故弃用（收件箱八次修订
// 定稿形态）。三处消费（收件箱编辑区 / 数据源页编辑区 / 对话框草案卡编辑区）
// ——原前两处本地复制，第三处出现时抽组件保统一。
// 开合受控（open/onOpenChange，消费侧管互斥——如收件箱与重复面板互斥）；
// value "YYYY-MM-DDTHH:mm" 或 ""（与旧 datetime-local 值同格式，提交链路零改动）。
// 点外即收内置（组件级，原收件箱七次修订行为）；样式类族沿用 inbox-todo-when-*
// （InboxPanel.css 单源，全局加载；面板占整行宽靠消费侧容器 grid-column）。
// 2026-11-10 扩展：WhenTimePicker 时刻版 + WhenTimeColumns 抽出共享（重复系列
// 执行时刻改点，见组件头注释）。2026-11-11 统一批：onSubmit 面板底部文字提交主钮
// 整体退役——确认统一走消费侧操作行图标钮（收件箱/项目页 CalendarPlus/
// CalendarSync，hover 提示即「按所选时间排期/改期/改时间」；数据源走确认钮），
// 用户定稿「面板只负责选，确认一个入口」；WhenTimePicker 同步改受控草稿模式
// （value/onChange，展开时装载当前执行时刻，点时/分写草稿、改期钮提交——与
// 单次卡改期钮同为确认语义）。
import { useEffect, useRef, useState } from "react";
import { Calendar, ChevronDown, Clock } from "lucide-react";

// 月历矩阵：指定年月 → 42 格，首格回退到该月 1 号所在周的周一——与月视图
// (getDay()+6)%7 同口径，周一首列
function monthMatrix(y: number, m: number) {
  const start = new Date(y, m, 1 - ((new Date(y, m, 1).getDay() + 6) % 7));
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    return { date: d, inMonth: d.getMonth() === m };
  });
}

// Date → "YYYY-MM-DD"：面板内今天/过去日/选中判断全用 ISO 串比较，零日期运算
const isoDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

// value（"YYYY-MM-DDTHH:mm"）→ 药丸展示文本 "M月D日 HH:MM"
const formatWhenDraft = (v: string) =>
  v.replace(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})$/,
    (_s, _y, mo: string, da: string, hm: string) => `${Number(mo)}月${Number(da)}日 ${hm}`,
  );

// 小时/分钟双列滚动列表（2026-11-10 自 WhenPicker 抽出共享，WhenTimePicker 复用）：
// 项高 28px、选中 accent-softer 底，滚动条照 .schedule-scroll 惯例（CSS 同源）；
// 点小时保留分钟、点分钟保留小时（未选侧默认 09/00）；mount 时各列滚到当前选中项
// ——面板 open 才渲染本组件，即"每次打开定位一次"，之后用户自由滚动不重定位
//（选中高亮实时反映）。disabled = 整区禁用（日期未选时时刻不可先选）
export function WhenTimeColumns({
  hour,
  minute,
  onPick,
  disabled,
}: {
  hour: string;
  minute: string;
  onPick: (hh: string, mm: string) => void;
  disabled?: boolean;
}) {
  const hourColRef = useRef<HTMLDivElement>(null);
  const minColRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (hourColRef.current) hourColRef.current.scrollTop = (hour ? Number(hour) : 9) * 28;
    if (minColRef.current) minColRef.current.scrollTop = (minute ? Number(minute) : 0) * 28;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="inbox-todo-when-time-cols">
      <div className="inbox-todo-when-time-col" ref={hourColRef}>
        {Array.from({ length: 24 }, (_, h) => {
          const hh = String(h).padStart(2, "0");
          return (
            <button
              key={h}
              type="button"
              className={`inbox-todo-when-time-item${hour === hh ? " on" : ""}`}
              disabled={disabled}
              onClick={() => onPick(hh, minute || "00")}
            >
              {hh}
            </button>
          );
        })}
      </div>
      <div className="inbox-todo-when-time-col" ref={minColRef}>
        {Array.from({ length: 60 }, (_, m) => {
          const mm = String(m).padStart(2, "0");
          return (
            <button
              key={m}
              type="button"
              className={`inbox-todo-when-time-item${minute === mm ? " on" : ""}`}
              disabled={disabled}
              onClick={() => onPick(hour || "09", mm)}
            >
              {mm}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function WhenPicker({
  value,
  onChange,
  open,
  onOpenChange,
}: {
  value: string;
  onChange: (v: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // 浏览月（打开时按已选值定位其月，否则当前月）
  const [whenView, setWhenView] = useState(() => {
    const n = new Date();
    return { y: n.getFullYear(), m: n.getMonth() };
  });
  const today = isoDay(new Date());
  const [whenDate, whenTime] = value ? [value.slice(0, 10), value.slice(11)] : ["", ""];
  // 浏览月翻页（Date 自动进位跨年）
  const shiftWhenView = (delta: number) =>
    setWhenView((v) => {
      const d = new Date(v.y, v.m + delta, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  // 点外即收（内置，原收件箱行为）：面板开着时点组件外任意处收起；点药丸本身
  // 走 onClick toggle、面板内交互（日格/时间列）豁免。消费侧其他药丸（如收件
  // 箱重复药丸）不在豁免清单——点它收起本面板，互斥由消费侧 onClick 兜底
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest(".inbox-todo-when-panel, .inbox-todo-when"))
        return;
      onOpenChange(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <>
      <button
        type="button"
        className="inbox-pill-group inbox-todo-when"
        onClick={() => {
          onOpenChange(!open);
          // 浏览月初始化：已选值定位其月，否则当前月
          const base = value ? new Date(`${value.slice(0, 10)}T00:00`) : new Date();
          setWhenView({ y: base.getFullYear(), m: base.getMonth() });
        }}
      >
        <Calendar size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="选择时间" />
        <span className={`inbox-todo-when-text${value ? "" : " muted"}`}>
          {value ? formatWhenDraft(value) : "选择时间"}
        </span>
        <ChevronDown
          size={13}
          strokeWidth={2}
          className={`inbox-todo-chevron${open ? " open" : ""}`}
        />
      </button>
      {open && (
        <div className="inbox-todo-when-panel">
          <div className="inbox-todo-when-nav">
            <button type="button" aria-label="上个月" onClick={() => shiftWhenView(-1)}>
              ‹
            </button>
            <span>
              {whenView.y}年{whenView.m + 1}月
            </span>
            <button type="button" aria-label="下个月" onClick={() => shiftWhenView(1)}>
              ›
            </button>
          </div>
          <div className="inbox-todo-when-grid">
            {["一", "二", "三", "四", "五", "六", "日"].map((w) => (
              <span key={w} className="inbox-todo-when-week">
                {w}
              </span>
            ))}
            {monthMatrix(whenView.y, whenView.m).map(({ date, inMonth }) => {
              const day = isoDay(date);
              return (
                <button
                  key={day}
                  type="button"
                  className={`inbox-todo-when-day${inMonth ? "" : " out"}${
                    day === whenDate ? " on" : ""
                  }${day === today ? " today" : ""}`}
                  disabled={day < today}
                  onClick={() => onChange(`${day}T${whenTime || "09:00"}`)}
                >
                  {date.getDate()}
                </button>
              );
            })}
          </div>
          {/* 时间区：点小时保留分钟、点分钟保留小时；未选日期整区禁用（先点日历，
              选日默认 09:00）、「清除」只清值不关面板 */}
          <div className="inbox-todo-when-time">
            <span>时间</span>
            <span className="inbox-todo-when-time-val">{whenTime || "--:--"}</span>
            <button type="button" onClick={() => onChange("")}>
              清除
            </button>
          </div>
          <WhenTimeColumns
            hour={whenTime ? whenTime.slice(0, 2) : ""}
            minute={whenTime ? whenTime.slice(3) : ""}
            disabled={!whenDate}
            onPick={(hh, mm) => onChange(`${whenDate}T${hh}:${mm}`)}
          />
        </div>
      )}
    </>
  );
}

// 重复系列执行时刻选择器（2026-11-10 问题 2）：药丸（Clock 图标 + "HH:MM" +
// chevron）开纯时/分双列面板——只改系列执行时刻，不动日期锚点与星期规则（改天
// 走重复面板圆钮、整体挪移走日历实例拖拽），end 随时长平移、全系列生效（列表本
// 就是系列语义，同重复药丸 all 直发）。受控草稿（同 WhenPicker 模式；2026-11-11
// 统一批定稿）：value 即草稿（消费侧展开时装载当前执行时刻），点时/分写草稿不
// 提交不闭面板——确认走消费侧操作行改期图标钮（与单次卡改期钮同为确认语义，
// 「面板只负责选，确认一个入口」）；类族复用 inbox-todo-when-*（同面板语言，
// 同类名点外即收豁免天然覆盖）。消费侧：收件箱/项目页编辑区时间行右位（已
// 排期重复卡）。
export function WhenTimePicker({
  value,
  onChange,
  open,
  onOpenChange,
}: {
  value: string; // "HH:mm"（草稿：消费侧展开时装载当前执行时刻）
  onChange: (v: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // 点外即收（同 WhenPicker 模式：药丸与面板外任意处收起）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest(".inbox-todo-when-panel, .inbox-todo-when"))
        return;
      onOpenChange(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <>
      <button
        type="button"
        className="inbox-pill-group inbox-todo-when"
        onClick={() => onOpenChange(!open)}
      >
        <Clock size={14} strokeWidth={2} className="inbox-pill-icon" aria-label="执行时刻" />
        <span className="inbox-todo-when-text">{value || "--:--"}</span>
        <ChevronDown
          size={13}
          strokeWidth={2}
          className={`inbox-todo-chevron${open ? " open" : ""}`}
        />
      </button>
      {open && (
        <div className="inbox-todo-when-panel">
          <div className="inbox-todo-when-time">
            <span>时间</span>
            <span className="inbox-todo-when-time-val">{value || "--:--"}</span>
          </div>
          <WhenTimeColumns
            hour={value ? value.slice(0, 2) : ""}
            minute={value ? value.slice(3) : ""}
            onPick={(hh, mm) => onChange(`${hh}:${mm}`)}
          />
        </div>
      )}
    </>
  );
}
