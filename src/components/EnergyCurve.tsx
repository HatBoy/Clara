// 精力画像卡（统计报告页，2026-11-17 批 3；同日 UI 打磨重构）：
// 左栏 = 逐小时完成倾向柱状图 + 峰谷（2026-11-17 改柱状——小时级数据多数时段
// 无样本，折线的连续性暗示会误导"相邻时段连得起来"，柱各自独立、无样本留
// 空槽更诚实）；右栏 = Energy Profile 白盒（AI 学到的作息规则原文/置信/证据）。
// 数据只读派生（GET /api/stats/energy-profile），无新表；样本不足显示积累中。
import { useEffect, useMemo, useState } from "react";
import { Fingerprint, Zap } from "lucide-react";
import { fetchEnergyProfile, type EnergyProfile } from "../lib/api";
import "./EnergyCurve.css";

const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

// SVG 几何：viewBox 定宽高，CSS 拉满左栏；文字随比例缩放可接受（标题类不走 SVG）
const W = 720;
const H = 172;
const PAD = { l: 30, r: 10, t: 18, b: 22 };

const xOf = (hour: number) => PAD.l + ((hour - 6) / (23 - 6)) * (W - PAD.l - PAD.r);
const yOf = (score: number) => PAD.t + (1 - score / 100) * (H - PAD.t - PAD.b);
const BAR_W = 20; // 槽宽 ≈38，柱 20 + 间隙 ≥2px 表面分隔（dataviz 惯例）

/** 顶端圆角、底端贴基线的柱 path（矮于两倍圆角半径时退化为矩形） */
const barPath = (cx: number, top: number, bottom: number, w = BAR_W, r = 3) => {
  const x = cx - w / 2;
  const h = bottom - top;
  if (h < r * 2) return `M ${x} ${bottom} L ${x} ${top} L ${x + w} ${top} L ${x + w} ${bottom} Z`;
  return `M ${x} ${bottom} L ${x} ${top + r} Q ${x} ${top} ${x + r} ${top} L ${x + w - r} ${top}`
    + ` Q ${x + w} ${top} ${x + w} ${top + r} L ${x + w} ${bottom} Z`;
};

type Point = { hour: number; score: number | null; sample: number };

export default function EnergyCurve({
  days,
  onOpenMemory,
}: {
  days: 7 | 30 | 90;
  /** 「去记忆画像管理」跳转（App 打开设置 → 记忆画像分区） */
  onOpenMemory?: () => void;
}) {
  const [data, setData] = useState<EnergyProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null = 全部星期汇总；0..6 = 周一..周日（后端 weekday() 口径）
  const [weekday, setWeekday] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    fetchEnergyProfile(days)
      .then((d) => alive && setData(d))
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : "精力画像加载失败");
      });
    return () => {
      alive = false;
    };
  }, [days]);

  const points = useMemo<Point[]>(() => {
    if (!data) return [];
    const byHour = new Map<number, { done: number; defer: number }>();
    for (const s of data.slots) {
      if (weekday !== null && s.weekday !== weekday) continue;
      const b = byHour.get(s.hour) ?? { done: 0, defer: 0 };
      b.done += s.done;
      b.defer += s.defer;
      byHour.set(s.hour, b);
    }
    return data.hours.map((hour) => {
      const b = byHour.get(hour);
      const sample = b ? b.done + b.defer : 0;
      const score =
        b && sample >= data.threshold ? Math.round((100 * b.done) / sample) : null;
      return { hour, score, sample };
    });
  }, [data, weekday]);

  const hasScore = points.some((p) => p.score !== null);
  // 峰谷 chips 随星期 tab 联动：选定星期时从该日有分点就地取峰/谷
  // （tie-break 取样本多者，与后端口径一致）；「全部」= 后端全量口径不变
  const scored = points.filter(
    (p): p is { hour: number; score: number; sample: number } => p.score !== null,
  );
  const scopedPeak =
    weekday !== null && scored.length
      ? scored.reduce((a, b) =>
          b.score > a.score || (b.score === a.score && b.sample > a.sample) ? b : a)
      : null;
  const scopedLow =
    weekday !== null && scored.length
      ? scored.reduce((a, b) =>
          b.score < a.score || (b.score === a.score && b.sample > a.sample) ? b : a)
      : null;
  const chipPeak = weekday === null ? data?.peak ?? null : scopedPeak;
  const chipLow = weekday === null ? data?.low ?? null : scopedLow;
  // 柱顶直接标注（选择性直接标注：只标峰/谷两柱，不逐柱标数）
  const labelHours = new Set(
    [chipPeak, chipLow].flatMap((p) => (p ? [p.hour] : [])),
  );

  return (
    <section className="report-card energy-card">
      <div className="energy-head">
        <span className="report-card-title">
          <Zap size={13} strokeWidth={2} className="report-title-ico" />
          精力画像
        </span>
        <span className="energy-head-sub">柱高 = 该小时完成倾向（完成 ÷ 完成+改期）</span>
      </div>

      {error ? (
        <div className="report-empty">{error}</div>
      ) : !data ? (
        <div className="report-loading">加载精力画像中…</div>
      ) : (
        <div className="energy-body">
          <div className="energy-left">
            {data.status === "collecting" ? (
              <div className="energy-collecting">
                数据积累中（已完成/改期共 {data.sample_count} 次，至少需要{" "}
                {data.total_threshold} 次）；右侧先看 AI 学到的作息规则。
              </div>
            ) : (
              <div className="energy-chips">
                {chipPeak && (
                  <span className="energy-chip up">
                    高峰 {String(chipPeak.hour).padStart(2, "0")}:00 · 完成倾向{" "}
                    {chipPeak.score}%
                  </span>
                )}
                {chipLow && chipPeak && chipLow.hour !== chipPeak.hour && (
                  <span className="energy-chip down">
                    低谷 {String(chipLow.hour).padStart(2, "0")}:00 · 完成倾向{" "}
                    {chipLow.score}%
                  </span>
                )}
              </div>
            )}

            <div className="energy-tabs" role="tablist" aria-label="按星期筛选">
              <button
                type="button"
                className={`energy-tab${weekday === null ? " active" : ""}`}
                onClick={() => setWeekday(null)}
              >
                全部
              </button>
              {WEEKDAY_LABELS.map((label, i) => (
                <button
                  key={label}
                  type="button"
                  className={`energy-tab${weekday === i ? " active" : ""}`}
                  onClick={() => setWeekday(i)}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="energy-chart">
              <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="各小时完成倾向柱状图">
                {[25, 50, 75].map((v) => (
                  <g key={v}>
                    <line
                      className="energy-grid"
                      x1={PAD.l}
                      x2={W - PAD.r}
                      y1={yOf(v)}
                      y2={yOf(v)}
                    />
                    <text className="energy-axis" x={PAD.l - 6} y={yOf(v) + 3} textAnchor="end">
                      {v}
                    </text>
                  </g>
                ))}
                {/* 基线（柱底锚） */}
                <line
                  className="energy-baseline"
                  x1={PAD.l}
                  x2={W - PAD.r}
                  y1={yOf(0)}
                  y2={yOf(0)}
                />
                {[6, 9, 12, 15, 18, 21, 23].map((h) => (
                  <text key={h} className="energy-axis" x={xOf(h)} y={H - 6} textAnchor="middle">
                    {h}
                  </text>
                ))}

                {points.map((p) => {
                  const cx = xOf(p.hour);
                  if (p.score === null) {
                    // 无样本小时：基线小刻度（空槽可辨，不会误读为 0 分）
                    return (
                      <line
                        key={p.hour}
                        className="energy-tick"
                        x1={cx - 3}
                        x2={cx + 3}
                        y1={yOf(0)}
                        y2={yOf(0)}
                      />
                    );
                  }
                  const top = p.score === 0 ? yOf(0) - 2 : yOf(p.score);
                  return (
                    <g key={p.hour}>
                      <path className="energy-bar" d={barPath(cx, top, yOf(0))}>
                        <title>
                          {String(p.hour).padStart(2, "0")}:00 · 完成倾向 {p.score}%（样本{" "}
                          {p.sample}）
                        </title>
                      </path>
                      {labelHours.has(p.hour) && (
                        <text
                          className="energy-bar-label"
                          x={cx}
                          y={top - 5}
                          textAnchor="middle"
                        >
                          {p.score}
                        </text>
                      )}
                    </g>
                  );
                })}
                {!hasScore && (
                  <text className="energy-axis" x={W / 2} y={H / 2} textAnchor="middle">
                    当前筛选下样本不足（每时段 ≥{data.threshold} 次才出柱）
                  </text>
                )}
              </svg>
            </div>

            {data.insights.length > 0 && (
              <ul className="energy-insights">
                {data.insights.map((it) => (
                  <li key={it.text}>
                    <span>{it.text}</span>
                    <span className="energy-insight-evi">{it.evidence}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="energy-right">
            <div className="energy-rules-head">
              <span className="energy-rules-title">
                <Fingerprint size={12} strokeWidth={2} className="report-title-ico" />
                AI 学到的作息规则
              </span>
              {onOpenMemory && (
                <button type="button" className="energy-memory-link" onClick={onOpenMemory}>
                  去记忆画像管理
                </button>
              )}
            </div>
            {data.profile.rules.length === 0 ? (
              <div className="energy-rule-empty">
                暂无作息规则——夜间反思会自动学习，也可在记忆画像中手写。
              </div>
            ) : (
              <ul className="energy-rules">
                {data.profile.rules.map((r) => (
                  <li key={r.id} className="energy-rule">
                    <span className="energy-rule-content">{r.content}</span>
                    <span className="energy-rule-meta">
                      {r.category} ·{" "}
                      {r.source === "user_manual" ? "你手写" : `AI 学习 · 置信 ${r.confidence}`}
                      {r.evidence ? ` · 依据：${r.evidence}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
