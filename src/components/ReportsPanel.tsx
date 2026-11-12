// 统计报告页（中栏）：分区单层布局——① AI 洞察（一卡三段：总评/值得关注/本期
// 建议 + 快捷按钮行）→ ② 总览带（健康度雷达 + KPI 六格带环比与历史基线）→
// ③ 时间去向（趋势折线全宽 + 项目花费/优先级分布两列）→ ④ 执行与习惯（四格
// + 习惯分析卡带热力图）→ ⑤ 报告留档（生成/折叠回看/复制/导出）。
// 数据流：挂载初拉 summary+reports+insight，切范围重拉（insight 后端带 TTL 缓存）；
// 生成 await 回包后前插列表并展开；refreshKey（周日自动周报事件）驱动驻留刷新。
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  Brain,
  CalendarClock,
  Check,
  ChevronDown,
  Copy,
  FileDown,
  Flame,
  HeartPulse,
  HelpCircle,
  Inbox,
  LineChart,
  ListChecks,
  ListFilter,
  MessageCircle,
  PieChart,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  createTask,
  exportStatsReport,
  fetchStatsInsight,
  fetchStatsReports,
  fetchStatsSummary,
  generateStatsReport,
  setStatsAdvice,
  type StatsAdviceItem,
  type StatsReportRow,
  type StatsSummary,
} from "../lib/api";
import { saveStatsReportPath } from "../lib/tauri";
import { HBars } from "./StatsCharts";
import HealthRadar from "./HealthRadar";
import HabitHeatmap from "./HabitHeatmap";
import MultiLineTrend from "./MultiLineTrend";
import "./ReportsPanel.css";

const RANGES = [7, 30, 90] as const;
type RangeDays = (typeof RANGES)[number];

const fmtHours = (mins: number) =>
  mins >= 5760 ? `${(mins / 60).toFixed(0)}h` : `${(mins / 60).toFixed(1)}h`;

/** 环比徽章：0 不渲染（比"持平"干净）；涨=绿、跌=红（当前三个有环比的指标均为正向语义） */
const DeltaBadge = ({ value, fmt }: { value: number; fmt?: (n: number) => string }) =>
  value === 0 ? null : (
    <span className={`report-kpi-delta ${value > 0 ? "up" : "down"}`}>
      {value > 0 ? "+" : "-"}
      {fmt ? fmt(Math.abs(value)) : Math.abs(value)}
    </span>
  );

const fmtReportDate = (iso: string) => {
  const d = new Date(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
};

/** 周期区间：周报 = start ~ +6 天；月报 = ~ 当月最后一天 */
const fmtPeriodRange = (type: "week" | "month", startIso: string) => {
  const s = new Date(startIso);
  const e = new Date(s);
  if (type === "week") e.setDate(e.getDate() + 6);
  else e.setMonth(e.getMonth() + 1, 0); // 次月 0 天 = 当月最后一天
  return `${fmtReportDate(startIso)}~${fmtReportDate(e.toISOString())}`;
};

/** 建议打卡条目归一化：旧留档纯字符串 → proposed 形态（打卡时后端顺带升级） */
type AdviceEntry = { text: string; status: "proposed" | "completed" | "dropped" };

/** AI 输出重点标记（纯前端渲染层，不改后端产出契约）：
 *  ① 段首【X】→ ## 小节头（accent 竖条样式；报告首行【总评】单独转 blockquote
 *    引言块）  ② [高优]/[中优]/[低优] 标签 → 行内 code，由 MdCode 上优先级 chip 色 */
const PRIO_TAG = /\[(高|中|低)优\]/g;

const prepInsightMd = (raw: string) =>
  raw.replace(/^【([^】]+)】[ \t]*/gm, "## $1\n").replace(PRIO_TAG, "`$&`");

const prepReportMd = (raw: string) =>
  raw
    .replace(/^【总评】[ \t]*(.+)$/gm, "> **总评** ｜ $1")
    .replace(/^【([^】]+)】[ \t]*/gm, "## $1\n")
    .replace(PRIO_TAG, "`$&`");

function MdCode({ children, className }: React.ComponentPropsWithoutRef<"code">) {
  const text = String(children ?? "");
  if (text === "[高优]") return <code className="high">{text}</code>;
  if (text === "[中优]") return <code className="mid">{text}</code>;
  if (text === "[低优]") return <code className="low">{text}</code>;
  return <code className={className}>{children}</code>;
}

/** ReactMarkdown 公共渲染器（code 上 chip 色；其余走默认） */
const mdComponents = { code: MdCode };

const normalizeAdvice = (
  items: (StatsAdviceItem | string)[] | undefined | null,
): AdviceEntry[] =>
  (items ?? [])
    .map((it) => (typeof it === "string" ? { text: it, status: "proposed" as const } : it))
    .filter((it) => it.text);

/** 建议打卡清单：报告正文上方的交互条（轻动作前置）——确认执行情况
 *  （完成/放弃）回流 payload.advice_items，下期报告对照点评（completed 标注、
 *  dropped 不再追问）；「转待办」把建议落收件箱变成行动（待办状态归收件箱管，
 *  本地标记不复写报告）。再点同状态钮 = 撤销回 proposed。 */
function AdviceChecklist({
  report,
  onUpdate,
}: {
  report: StatsReportRow;
  onUpdate: (row: StatsReportRow) => void;
}) {
  const items = normalizeAdvice(report.payload?.advice_items);
  const [busy, setBusy] = useState<number | null>(null);
  // 已转待办条目（本地态，不写回 payload）
  const [inboxed, setInboxed] = useState<Set<number>>(new Set());
  const [todoBusy, setTodoBusy] = useState<number | null>(null);
  if (items.length === 0) return null;

  const toggle = async (index: number, next: AdviceEntry["status"]) => {
    setBusy(index);
    try {
      onUpdate(await setStatsAdvice(report.id, index, next));
    } catch {
      /* 打卡失败静默（按钮态复位，重试即可） */
    } finally {
      setBusy(null);
    }
  };

  const toTodo = async (index: number, text: string) => {
    setTodoBusy(index);
    try {
      await createTask({ title: text.slice(0, 30) });
      setInboxed((prev) => new Set(prev).add(index));
    } catch {
      /* 转待办失败静默（按钮复位可重试） */
    } finally {
      setTodoBusy(null);
    }
  };

  return (
    <div className="report-advice">
      <span className="report-advice-title">
        <ListChecks size={12} strokeWidth={2} />
        本期建议打卡——确认执行情况（下期报告对照点评）或转入待办
      </span>
      {items.map((it, i) => (
        <div key={i} className={`report-advice-item ${it.status}`}>
          <span className={`report-advice-text${it.status === "completed" ? " done" : ""}`}>
            {it.text}
            {it.status === "completed" && <em>已完成</em>}
            {it.status === "dropped" && <em>已放弃</em>}
          </span>
          <span className="report-advice-ops">
            <button
              type="button"
              className={it.status === "completed" ? "on" : ""}
              disabled={busy === i}
              aria-label="标记完成"
              title="标记完成"
              onClick={() => toggle(i, it.status === "completed" ? "proposed" : "completed")}
            >
              <Check size={13} strokeWidth={2} />
            </button>
            <button
              type="button"
              className={it.status === "dropped" ? "on danger" : ""}
              disabled={busy === i}
              aria-label="标记放弃"
              title="标记放弃（下期不再追问）"
              onClick={() => toggle(i, it.status === "dropped" ? "proposed" : "dropped")}
            >
              <X size={13} strokeWidth={2} />
            </button>
            <button
              type="button"
              className={inboxed.has(i) ? "on" : ""}
              disabled={todoBusy === i || inboxed.has(i)}
              aria-label="转入待办"
              title={inboxed.has(i) ? "已入待办（收件箱可见）" : "加入收件箱变成行动"}
              onClick={() => toTodo(i, it.text)}
            >
              {inboxed.has(i) ? <Check size={13} strokeWidth={2} /> : <Inbox size={13} strokeWidth={2} />}
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}

/** 健康度总分趋势：mini 折线（旧→新 = 历史报告 payload.health.total + 当期）。
 *  单点（无历史留档）不渲染；inline SVG 自绘（HBars 惯例，不引图表库）。 */
function HealthTrend({ history, current }: { history: number[]; current: number }) {
  const points = [...history, current].slice(-8);
  if (points.length < 2) return null;
  const w = 148;
  const h = 26;
  // 量程参考 40 起步（健康度五维多为 0-100，纯 min-max 会让平稳线占满高度）
  const min = Math.min(...points, 40);
  const max = Math.max(...points, 100);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const ys = points.map((v) => h - 3 - ((v - min) / span) * (h - 6));
  const coords = points.map((_, i) => `${(i * step).toFixed(1)},${ys[i].toFixed(1)}`);
  return (
    <div
      className="report-health-trend"
      title={`近 ${points.length} 期健康度总分：${points.join(" → ")}`}
    >
      <span className="report-health-trend-label">趋势</span>
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
        <polyline
          points={coords.join(" ")}
          fill="none"
          stroke="var(--chart-1)"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <circle cx={w} cy={ys[ys.length - 1]} r={2.4} fill="var(--chart-1)" />
      </svg>
      <span className="report-health-trend-value">{points[points.length - 1]}</span>
    </div>
  );
}

/** 历史基线对比箭头：正向指标涨=好，负向指标跌=好（title 带基线数值） */
const CompareArrow = ({ current, baseline, positive = true }: { current: number; baseline: number; positive?: boolean }) => {
  if (Math.abs(current - baseline) < 0.01) return null;
  const isUp = current > baseline;
  const isGood = positive ? isUp : !isUp;
  return (
    <span className={`report-compare ${isGood ? "good" : "bad"}`} title={`个人平均: ${baseline}`}>
      {isUp ? "↑" : "↓"}
    </span>
  );
};

/** stat list 行：label 左灰 / value 右重（执行协作三卡的现代两列读法） */
const KVRow = ({ label, value, title }: { label: string; value: string; title?: string }) => (
  <div className="report-kv-row" title={title}>
    <span className="report-kv-label">{label}</span>
    <span className="report-kv-val">{value}</span>
  </div>
);

export default function ReportsPanel({
  days,
  onDaysChange,
  onOpenProject,
  onOpenInbox,
  refreshKey,
  chatOpen,
  onToggleChat,
  style,
}: {
  /** 统计范围（受控：App 层持有，右侧问答面板共享同一范围） */
  days: RangeDays;
  onDaysChange: (d: RangeDays) => void;
  /** 指标下钻：「时间花费 by 项目」横条点击 → 项目页（「未归类」条不可点） */
  onOpenProject?: (name: string) => void;
  /** 指标下钻：「收件箱积压」KPI 点击 → 收件箱页 */
  onOpenInbox?: () => void;
  /** AI 事件刷新信号（周日晚自动周报生成时驱留页面前插留档；不传则切页重拉） */
  refreshKey?: number;
  /** 右侧统计对话面板是否打开 */
  chatOpen?: boolean;
  /** 切换右侧统计对话面板 */
  onToggleChat?: () => void;
  /** 样式（App.tsx 传入） */
  style?: React.CSSProperties;
}) {
  const [summary, setSummary] = useState<StatsSummary | null>(null);
  const [insight, setInsight] = useState<string | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);
  const [reports, setReports] = useState<StatsReportRow[]>([]);
  const [generating, setGenerating] = useState<"week" | "month" | null>(null);
  const [openReportId, setOpenReportId] = useState<string | null>(null);
  // 历史留档折叠（默认收起——最新一期已前置全文展示）
  const [historyOpen, setHistoryOpen] = useState(false);
  // 复制成功闪现（2s）/ 导出结果提示（3s 自清）——DataSettings CopyButton 同款纪律
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [hint, setHint] = useState<{ id: string; ok: boolean; text: string } | null>(null);
  const copyTimer = useRef<number | undefined>(undefined);
  const hintTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    fetchStatsSummary(days).then((s) => alive && setSummary(s)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [days]);

  // AI 动态洞察（首次加载 + 切换范围走后端 1 小时 TTL 缓存；手动刷新跳过缓存）
  const loadInsight = (refresh: boolean) => {
    setInsightLoading(true);
    fetchStatsInsight(days, refresh)
      .then(setInsight)
      .catch(() => setInsight(null))
      .finally(() => setInsightLoading(false));
  };

  useEffect(() => {
    loadInsight(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- days 变化重拉（缓存态），loadInsight 每渲染重建不宜进依赖
  }, [days]);

  useEffect(() => {
    fetchStatsReports().then(setReports).catch(() => {});
  }, [refreshKey]);

  // 定时器清理（卸载时不留悬挂回调 setState）
  useEffect(
    () => () => {
      window.clearTimeout(copyTimer.current);
      window.clearTimeout(hintTimer.current);
    },
    [],
  );

  const doGenerate = async (period: "week" | "month") => {
    setGenerating(period);
    try {
      const row = await generateStatsReport(period);
      // 前插即成最新一期，全文置顶展示（无需再展开单条）
      setReports((list) => [row, ...list]);
    } catch {
      /* 生成失败静默（按钮态复位）；LLM 不可用时后端已回落数据模板，到这的多为网络层 */
    } finally {
      setGenerating(null);
    }
  };

  // 建议打卡回包就位（AdviceChecklist 的 onUpdate）
  const patchReport = (row: StatsReportRow) =>
    setReports((list) => list.map((r) => (r.id === row.id ? row : r)));

  const doCopy = (r: StatsReportRow) => {
    navigator.clipboard
      .writeText(r.content)
      .then(() => {
        setCopiedId(r.id);
        window.clearTimeout(copyTimer.current);
        copyTimer.current = window.setTimeout(() => setCopiedId(null), 2000);
      })
      .catch(() => {
        /* 复制失败静默（全文就在下方展开区，可手选） */
      });
  };

  const doExport = async (r: StatsReportRow) => {
    const label = r.period_type === "week" ? "周报" : "月报";
    const path = await saveStatsReportPath(`clara-${label}-${r.period_start.slice(0, 10)}.md`);
    if (!path) return; // 取消 / 非桌面零反馈（AboutSettings 备份导出同款）
    try {
      await exportStatsReport(r.id, path);
      setHint({ id: r.id, ok: true, text: "已导出" });
    } catch (e) {
      const detail = (e as Error & { detail?: string }).detail;
      setHint({ id: r.id, ok: false, text: detail || "导出失败" });
    } finally {
      window.clearTimeout(hintTimer.current);
      hintTimer.current = window.setTimeout(() => setHint(null), 3000);
    }
  };

  const ov = summary?.overview;
  const rhythm = summary?.rhythm;
  const habits = summary?.habits;
  // 报告区：最新一期前置全文展示，其余进历史折叠
  const latest = reports[0];
  const history = reports.slice(1);

  return (
    <section className="report-page" style={style}>
      <header className="report-page-head">
        <span className="report-page-title">统计报告</span>
        <div className="report-head-controls">
          <div className="report-range">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                className={`report-range-btn${days === r ? " active" : ""}`}
                onClick={() => onDaysChange(r)}
              >
                近{r}天
              </button>
            ))}
          </div>
          <button
            type="button"
            className={`report-ask-toggle${chatOpen ? " active" : ""}`}
            onClick={() => onToggleChat?.()}
            title="基于当前统计数据提问"
            aria-label="打开统计问答面板"
          >
            <MessageCircle size={15} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="report-body">
        {!summary || !ov ? (
          <div className="report-loading">加载统计数据中…</div>
        ) : (
          <div className="report-body-inner">
            {/* ① AI 洞察：一卡三段（总评/值得关注/本期建议/习惯洞察，Markdown）；
                后端 1 小时缓存，刷新钮跳过缓存手动重新生成 */}
            <section className="report-card report-insight">
              <div className="report-insight-head">
                <span className="report-card-title">
                  <span className="report-card-badge">
                    <Sparkles size={12} strokeWidth={2} />
                  </span>
                  AI 洞察
                </span>
                <span className="report-insight-range">近 {days} 天</span>
                <button
                  type="button"
                  className="report-insight-refresh"
                  onClick={() => loadInsight(true)}
                  disabled={insightLoading}
                  title="重新生成洞察（跳过缓存）"
                  aria-label="重新生成洞察"
                >
                  <RefreshCw size={13} strokeWidth={2} className={insightLoading ? "spinning" : ""} />
                </button>
              </div>
              {insightLoading ? (
                <div className="report-insight-loading">生成洞察中…</div>
              ) : insight ? (
                <div className="report-insight-content">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {prepInsightMd(insight)}
                  </ReactMarkdown>
                </div>
              ) : (
                <div className="report-empty">洞察生成失败——稍后点右上刷新重试</div>
              )}
            </section>

            {/* ② 总览带：健康度雷达 + KPI 六格（这段时间做得怎么样） */}
            <div className="report-overview-band">
              {summary.health && (
                <section className="report-card report-radar-card">
                  <span className="report-card-title">
                    <HeartPulse size={13} strokeWidth={2} className="report-title-ico" />
                    健康度
                  </span>
                  <HealthRadar
                    dimensions={summary.health.dimensions}
                    labels={summary.health.labels}
                    total={summary.health.total}
                    size={188}
                  />
                  <HealthTrend
                    history={[...reports]
                      .reverse()
                      .flatMap((r) => (r.payload?.health?.total != null ? [r.payload.health.total] : []))
                      .slice(-7)}
                    current={summary.health.total}
                  />
                </section>
              )}
              <div className="report-kpis">
                <div className="report-kpi">
                  <span className="report-kpi-head" title="窗口内完成的纯待办数量（不含排期后完成）">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">{ov.todos_done}</span>
                    <DeltaBadge value={ov.delta.todos_done} />
                    {summary.prev_avg && (
                      <CompareArrow current={ov.todos_done} baseline={summary.prev_avg.todos_done} />
                    )}
                  </span>
                  <span className="report-kpi-label">完成待办</span>
                </div>
                <div className="report-kpi">
                  <span className="report-kpi-head" title="窗口内已开始的日程实例数（过去即发生，不依赖勾选）">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">{ov.events_held}</span>
                    <DeltaBadge value={ov.delta.events_held} />
                    {summary.prev_avg && (
                      <CompareArrow current={ov.events_held} baseline={summary.prev_avg.events_held} />
                    )}
                  </span>
                  <span className="report-kpi-label">日程发生</span>
                </div>
                <div className="report-kpi">
                  <span className="report-kpi-head" title="窗口内全部排期实例的计划时长总和">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">{fmtHours(ov.scheduled_minutes)}</span>
                    <DeltaBadge value={ov.delta.scheduled_minutes} fmt={fmtHours} />
                  </span>
                  <span className="report-kpi-label">排期总时长</span>
                </div>
                <div className="report-kpi">
                  <span className="report-kpi-head" title="排期总时长 ÷ 工作容量（每日工时 × 天数）">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">{Math.round(ov.load_rate * 100)}%</span>
                    {summary.prev_avg?.load_rate && (
                      <CompareArrow
                        current={ov.load_rate}
                        baseline={summary.prev_avg.load_rate}
                        positive={ov.load_rate <= 1.0}
                      />
                    )}
                  </span>
                  <span className="report-kpi-label">负载率</span>
                </div>
                <div className="report-kpi">
                  <span className="report-kpi-head" title="完成待办 ÷ 新增待办（>100%说明在消化积压）">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">
                      {ov.todo_digest_rate !== null ? `${Math.round(ov.todo_digest_rate * 100)}%` : "—"}
                    </span>
                    {summary.prev_avg?.todo_digest_rate && ov.todo_digest_rate !== null && (
                      <CompareArrow current={ov.todo_digest_rate} baseline={summary.prev_avg.todo_digest_rate} />
                    )}
                  </span>
                  <span className="report-kpi-label">待办消化率</span>
                </div>
                {/* 积压格可下钻：点开收件箱页（button 复用 tile 视觉） */}
                <button
                  type="button"
                  className="report-kpi clickable"
                  onClick={onOpenInbox}
                >
                  <span className="report-kpi-head" title="当前收件箱未完成待办数量（点击打开收件箱）">
                    <HelpCircle size={11} className="report-kpi-help" />
                  </span>
                  <span className="report-kpi-value-row">
                    <span className="report-kpi-value">{ov.inbox_backlog}</span>
                  </span>
                  <span className="report-kpi-label">收件箱积压</span>
                </button>
              </div>
            </div>

            {/* ③ 时间去向：趋势折线 + 习惯热力图同排（两张时间分布图互补）→
                项目花费/优先级分布两列 */}
            <span className="report-group">时间去向</span>
            <div className="report-trend-row">
              <section className="report-card">
                <span className="report-card-title">
                  <LineChart size={13} strokeWidth={2} className="report-title-ico" />
                  每日趋势（完成与花费）
                </span>
                <MultiLineTrend
                  data={summary.daily_done.map((d) => ({
                    date: d.date,
                    todos: d.todos,
                    events: d.events,
                    minutes: d.minutes,
                  }))}
                />
              </section>
              {habits && (
                <section className="report-card report-habits">
                  <span className="report-card-title">
                    <Flame size={13} strokeWidth={2} className="report-title-ico" />
                    习惯分析
                  </span>
                  <div className="report-habits-body">
                    <div className="report-habit-chips">
                      {habits.best_slot && (
                        <span className="report-habit-chip">最高效时段 · {habits.best_slot}</span>
                      )}
                      {habits.defer_slot && (
                        <span className="report-habit-chip warn">拖延高发 · {habits.defer_slot}</span>
                      )}
                      {habits.top_project && habits.top_project_pct && (
                        <span className="report-habit-chip">
                          偏好项目 · {habits.top_project} {habits.top_project_pct}%
                        </span>
                      )}
                      {(["high", "medium", "low"] as const).map((k) => {
                        const p = habits.priority_completion[k];
                        if (!p || p.rate === null) return null;
                        const label = k === "high" ? "高" : k === "medium" ? "中" : "低";
                        return (
                          <span key={k} className="report-habit-chip">
                            {label}优完成率 {Math.round(p.rate * 100)}%
                          </span>
                        );
                      })}
                      {!habits.best_slot && !habits.defer_slot && !habits.top_project && (
                        <span className="report-empty">暂无习惯数据——完成几项待办后再来看看</span>
                      )}
                    </div>
                    {habits.heatmap_done && (
                      <HabitHeatmap data={habits.heatmap_done} mode="done" />
                    )}
                  </div>
                </section>
              )}
            </div>
            <div className="report-grid-even">
              <section className="report-card">
                <span className="report-card-title" title="按已发生日程实例的排期计划时长近似（全天不计）；实际偏差见下方脚注">
                  <PieChart size={13} strokeWidth={2} className="report-title-ico" />
                  时间花费 · 按项目（排期计划时长）
                </span>
                {summary.by_project.length > 0 ? (
                  <>
                    <HBars
                      rows={summary.by_project.slice(0, 8).map((r) => ({
                        label: r.name,
                        value: r.minutes,
                        // 「未归类」无项目实体（收件箱≠未归类已完成日程，群体不相交）
                        // ——渲染但不可点，假下钻撒谎不做
                        noDrill: r.name === "未归类",
                      }))}
                      fmtValue={fmtHours}
                      onRowClick={onOpenProject}
                    />
                    {summary.behavior.avg_delta_min !== null && (
                      <span className="report-card-footnote">
                        注：按计划时长近似；实际执行平均比计划
                        {summary.behavior.avg_delta_min > 0 ? "长" : "短"}
                        {Math.abs(summary.behavior.avg_delta_min)} 分钟/项
                      </span>
                    )}
                  </>
                ) : (
                  <span className="report-empty">窗口内暂无已完成日程——勾选完成后这里展示时间分布</span>
                )}
              </section>
              <section className="report-card">
                <span className="report-card-title">
                  <ListFilter size={13} strokeWidth={2} className="report-title-ico" />
                  完成分布 · 按优先级
                </span>
                {ov.todos_done > 0 ? (
                  <HBars
                    rows={[
                      { label: "高", value: summary.by_priority.high, color: "var(--danger)" },
                      { label: "中", value: summary.by_priority.medium, color: "var(--orange)" },
                      { label: "低", value: summary.by_priority.low, color: "var(--green)" },
                    ]}
                  />
                ) : (
                  <span className="report-empty">窗口内暂无完成待办</span>
                )}
              </section>
            </div>

            {/* ④ 执行与协作：三卡行为指标（AI 协作并入行为卡——采纳率衡量的是
                AI 表现而非用户时间管理，独立成卡价值低） */}
            <span className="report-group">执行与协作</span>
            <div className="report-grid-even">
              {rhythm && (
                <section className="report-card">
                  <span className="report-card-title">
                    <Activity size={13} strokeWidth={2} className="report-title-ico" />
                    执行节奏（近 14 天）
                  </span>
                  <div className="report-kv">
                    <KVRow label="计划排期" value={`${rhythm.planned_count} 项`} />
                    <KVRow label="勾选完成" value={`${rhythm.done_count} 项`} />
                    <KVRow
                      label="完成任务均时长"
                      value={rhythm.avg_done_mins !== null ? fmtHours(rhythm.avg_done_mins) : "—"}
                    />
                    {rhythm.top_deferred.length > 0 && (
                      <KVRow
                        label="拖延榜首"
                        value={`${rhythm.top_deferred[0].title}（改期 ${rhythm.top_deferred[0].count} 次）`}
                        title={rhythm.top_deferred[0].title}
                      />
                    )}
                  </div>
                </section>
              )}
              <section className="report-card">
                <span className="report-card-title">
                  <Brain size={13} strokeWidth={2} className="report-title-ico" />
                  行为与协作
                </span>
                <div className="report-kv">
                  <KVRow label="改期次数" value={`${summary.behavior.reschedules} 次`} />
                  <KVRow
                    label="完成偏差（实际−计划）"
                    value={
                      summary.behavior.avg_delta_min !== null
                        ? `${summary.behavior.avg_delta_min > 0 ? "+" : ""}${summary.behavior.avg_delta_min} 分钟/项`
                        : "—"
                    }
                  />
                  <KVRow
                    label="AI 建议采纳率"
                    value={`${Math.round(summary.ai.adoption.rate * 100)}%（${summary.ai.adoption.accepted_modified}/${summary.ai.adoption.total}）`}
                    title="近 14 天：采纳或微调 / 总建议数"
                  />
                </div>
              </section>
              <section className="report-card">
                <span className="report-card-title">
                  <CalendarClock size={13} strokeWidth={2} className="report-title-ico" />
                  完成滞后与排期提前量（中位天数）
                </span>
                <div className="report-kv">
                  <KVRow
                    label="待办滞后"
                    value={summary.lag.todos_days !== null ? `${summary.lag.todos_days} 天` : "—"}
                  />
                  <KVRow
                    label="日程提前"
                    value={summary.lag.lead_days !== null ? `${summary.lag.lead_days} 天` : "—"}
                  />
                </div>
              </section>
            </div>

            {/* ⑤ 周期复盘：最新一期前置全文展示 + 历史留档折叠；生成是低频动作
                （周日晚自动生成），描边小钮收进标题行不占主视觉 */}
            <span className="report-group">周期复盘</span>
            <section className="report-card report-ai">
              <div className="report-ai-head">
                <span className="report-card-title">
                  <span className="report-card-badge">
                    <Sparkles size={12} strokeWidth={2} />
                  </span>
                  AI 报告
                </span>
                <div className="report-ai-actions">
                  <button
                    type="button"
                    className="report-gen-btn"
                    disabled={generating !== null}
                    onClick={() => doGenerate("week")}
                  >
                    {generating === "week" ? "生成中…" : "生成周报"}
                  </button>
                  <button
                    type="button"
                    className="report-gen-btn"
                    disabled={generating !== null}
                    onClick={() => doGenerate("month")}
                  >
                    {generating === "month" ? "生成中…" : "生成月报"}
                  </button>
                </div>
              </div>
              {reports.length === 0 ? (
                <span className="report-empty">
                  还没有报告。每周日晚会自动生成一期周复盘——点评上期建议的执行
                  情况、给出下期建议，月报还含量化成长对比；也可现在点右上按钮手动生成。
                </span>
              ) : (
                <>
                  {/* 最新一期：始终全文展开（生成后前插自动成为最新） */}
                  <article className="report-latest">
                    <div className="report-latest-head">
                      <span className={`report-item-type ${latest.period_type}`}>
                        {latest.period_type === "week" ? "周报" : "月报"}
                      </span>
                      <span className="report-item-title">
                        {fmtPeriodRange(latest.period_type, latest.period_start)}
                      </span>
                      <span className="report-item-meta">
                        {fmtReportDate(latest.created_at)} 生成
                      </span>
                      {hint?.id === latest.id && (
                        <span className={`report-item-hint${hint.ok ? "" : " bad"}`}>
                          {hint.text}
                        </span>
                      )}
                      <button
                        type="button"
                        className="report-icon-btn"
                        aria-label="复制 Markdown"
                        title="复制 Markdown"
                        onClick={() => doCopy(latest)}
                      >
                        {copiedId === latest.id ? (
                          <Check size={14} strokeWidth={2} />
                        ) : (
                          <Copy size={14} strokeWidth={2} />
                        )}
                      </button>
                      <button
                        type="button"
                        className="report-icon-btn"
                        aria-label="导出 .md 文件"
                        title="导出 .md 文件"
                        onClick={() => doExport(latest)}
                      >
                        <FileDown size={14} strokeWidth={2} />
                      </button>
                    </div>
                    {/* 打卡清单前置（头行后、正文前）——轻动作前置，重阅读后置 */}
                    <AdviceChecklist report={latest} onUpdate={patchReport} />
                    <div className="report-md">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                        {prepReportMd(latest.content)}
                      </ReactMarkdown>
                    </div>
                  </article>
                  {/* 历史留档：默认收起 */}
                  {history.length > 0 && (
                    <div className="report-history-block">
                      <button
                        type="button"
                        className="report-history-toggle"
                        onClick={() => setHistoryOpen((v) => !v)}
                      >
                        <span>历史留档（{history.length} 期）</span>
                        <ChevronDown
                          size={14}
                          strokeWidth={2}
                          className={`report-item-chev${historyOpen ? " open" : ""}`}
                        />
                      </button>
                      {historyOpen && (
                        <div className="report-history">
                          {history.map((r) => (
                            <div key={r.id} className="report-item">
                              {/* 头部是 div 容器（图标钮不能嵌套 button），折叠区在
                                  内层 toggle 钮；右侧复制/导出图标钮 + 导出结果提示 */}
                              <div className="report-item-head">
                                <button
                                  type="button"
                                  className="report-item-toggle"
                                  onClick={() =>
                                    setOpenReportId((cur) => (cur === r.id ? null : r.id))
                                  }
                                >
                                  <span className={`report-item-type ${r.period_type}`}>
                                    {r.period_type === "week" ? "周报" : "月报"}
                                  </span>
                                  <span className="report-item-title">
                                    {fmtPeriodRange(r.period_type, r.period_start)}
                                  </span>
                                  <span className="report-item-meta">
                                    {fmtReportDate(r.created_at)} 生成
                                  </span>
                                  <ChevronDown
                                    size={14}
                                    strokeWidth={2}
                                    className={`report-item-chev${openReportId === r.id ? " open" : ""}`}
                                  />
                                </button>
                                {hint?.id === r.id && (
                                  <span className={`report-item-hint${hint.ok ? "" : " bad"}`}>
                                    {hint.text}
                                  </span>
                                )}
                                <button
                                  type="button"
                                  className="report-icon-btn"
                                  aria-label="复制 Markdown"
                                  title="复制 Markdown"
                                  onClick={() => doCopy(r)}
                                >
                                  {copiedId === r.id ? (
                                    <Check size={14} strokeWidth={2} />
                                  ) : (
                                    <Copy size={14} strokeWidth={2} />
                                  )}
                                </button>
                                <button
                                  type="button"
                                  className="report-icon-btn"
                                  aria-label="导出 .md 文件"
                                  title="导出 .md 文件"
                                  onClick={() => doExport(r)}
                                >
                                  <FileDown size={14} strokeWidth={2} />
                                </button>
                              </div>
                              {openReportId === r.id && (
                                <>
                                  <div className="report-md">
                                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                      {prepReportMd(r.content)}
                                    </ReactMarkdown>
                                  </div>
                                  <AdviceChecklist report={r} onUpdate={patchReport} />
                                </>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </section>
          </div>
        )}
      </div>
    </section>
  );
}
