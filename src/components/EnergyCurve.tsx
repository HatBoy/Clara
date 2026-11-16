// 精力曲线卡（统计报告页，2026-11-17 批 3）：行为遥测派生的完成倾向曲线 +
// Energy Profile 白盒（AI 学到的作息规则原文/置信度/证据）。
// 数据只读派生（GET /api/stats/energy-profile），无新表；样本不足显示积累中。
import { useEffect, useMemo, useState } from "react";
import { Fingerprint, LineChart } from "lucide-react";
import { fetchEnergyProfile, type EnergyProfile } from "../lib/api";
import "./EnergyCurve.css";

const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

// SVG 几何：viewBox 定宽高，CSS 拉满；文字随比例缩放可接受（标题类不走 SVG）
const W = 720;
const H = 180;
const PAD = { l: 30, r: 14, t: 14, b: 24 };

const xOf = (hour: number) => PAD.l + ((hour - 6) / (23 - 6)) * (W - PAD.l - PAD.r);
const yOf = (score: number) => PAD.t + (1 - score / 100) * (H - PAD.t - PAD.b);

type Point = { hour: number; score: number | null; sample: number };

/** 连续有分点连成段（样本不足的时段断开，不画误导性的连线） */
const segments = (points: Point[]) => {
  const out: { hour: number; score: number }[][] = [];
  let cur: { hour: number; score: number }[] = [];
  for (const p of points) {
    if (p.score === null) {
      if (cur.length) out.push(cur);
      cur = [];
    } else {
      cur.push({ hour: p.hour, score: p.score });
    }
  }
  if (cur.length) out.push(cur);
  return out;
};

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
  const segs = segments(points);
  // 峰谷 chips 随星期 tab 联动（二轮批 1）：选定星期时从该日有分点就地取峰/谷
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

  return (
    <section className="report-card energy-card">
      <div className="energy-head">
        <span className="report-card-title">
          <LineChart size={13} strokeWidth={2} className="report-title-ico" />
          精力曲线
        </span>
        <span className="energy-head-sub">完成倾向 = 该时段完成 ÷（完成 + 改期）</span>
      </div>

      {error ? (
        <div className="report-empty">{error}</div>
      ) : !data ? (
        <div className="report-loading">加载精力画像中…</div>
      ) : (
        <>
          {data.status === "collecting" ? (
            <div className="energy-collecting">
              数据积累中（已完成/改期共 {data.sample_count} 次，至少需要{" "}
              {data.total_threshold} 次）；目前展示 AI 学到的作息规则。
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
            <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="精力曲线">
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
              {[6, 9, 12, 15, 18, 21, 23].map((h) => (
                <text key={h} className="energy-axis" x={xOf(h)} y={H - 6} textAnchor="middle">
                  {h}
                </text>
              ))}

              {segs.map((seg, i) =>
                seg.length === 1 ? null : (
                  <path
                    key={i}
                    className="energy-area"
                    d={`M ${seg.map((p) => `${xOf(p.hour)} ${yOf(p.score)}`).join(" L ")} L ${xOf(seg[seg.length - 1].hour)} ${yOf(0)} L ${xOf(seg[0].hour)} ${yOf(0)} Z`}
                  />
                ),
              )}
              {segs.map((seg, i) => (
                <path
                  key={`l${i}`}
                  className="energy-line"
                  d={`M ${seg.map((p) => `${xOf(p.hour)} ${yOf(p.score)}`).join(" L ")}`}
                />
              ))}
              {points.map((p) =>
                p.score === null ? null : (
                  <circle
                    key={p.hour}
                    className="energy-dot"
                    cx={xOf(p.hour)}
                    cy={yOf(p.score)}
                    r={2.6}
                  >
                    <title>
                      {String(p.hour).padStart(2, "0")}:00 · 完成倾向 {p.score}%（样本{" "}
                      {p.sample}）
                    </title>
                  </circle>
                ),
              )}
              {!hasScore && (
                <text className="energy-axis" x={W / 2} y={H / 2} textAnchor="middle">
                  当前筛选下样本不足（每时段 ≥{data.threshold} 次才画点）
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

          <div className="energy-rules-head">
            <span className="energy-rules-title">
              <Fingerprint size={12} strokeWidth={2} className="report-title-ico" />
              AI 学到的作息规则（白盒）
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
        </>
      )}
    </section>
  );
}
