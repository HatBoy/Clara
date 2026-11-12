// 多维度趋势折线图：三条线叠加（完成待办/日程发生走左轴，时间花费走右轴）
// 宽度与高度都自适应（ResizeObserver 重绘——趋势行里拉满卡片高度，与右列
// 习惯卡等高无空白）；颜色走 --chart-1/2/3 设计变量；hover 竖直参考线 +
// 浮层 tooltip 显示当日三值。
import { useEffect, useRef, useState } from "react";
import { chartVar } from "./StatsCharts";

type DataPoint = {
  date: string;
  todos: number;
  events: number;
  minutes: number;
};

type MultiLineTrendProps = {
  data: DataPoint[];
  /** 图表最小高度（容器更高时拉伸填满；缺省 220） */
  minHeight?: number;
};

type HoverInfo = {
  date: string;
  todos: number;
  events: number;
  hours: number;
  x: number;
  y: number;
};

const PADDING = { top: 20, right: 50, bottom: 30, left: 50 };
const LEGEND_RESERVE = 44; // 图例 + 行间距 + 余量（canvas 高 = 容器高 − 此值）

export default function MultiLineTrend({ data, minHeight = 220 }: MultiLineTrendProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 640, h: minHeight });
  const [hover, setHover] = useState<HoverInfo | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      if (r.width <= 0) return;
      setSize({
        w: Math.round(r.width),
        h: Math.max(minHeight, Math.round(r.height - LEGEND_RESERVE)),
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [minHeight]);

  const { w: width, h: height } = size;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const c1 = chartVar("--chart-1", "#3b82f6");
    const c2 = chartVar("--chart-2", "#a855f7");
    const c3 = chartVar("--chart-3", "#f97316");
    const axisColor = chartVar("--text-muted", "#9ca3af");

    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (data.length === 0) return;

    const chartWidth = width - PADDING.left - PADDING.right;
    const chartHeight = height - PADDING.top - PADDING.bottom;
    const stepX = chartWidth / Math.max(data.length - 1, 1);

    const maxLeft = Math.max(...data.map((d) => Math.max(d.todos, d.events)), 1);
    const maxRight = Math.max(...data.map((d) => d.minutes), 1) / 60;
    const toYLeft = (v: number) => height - PADDING.bottom - (v / maxLeft) * chartHeight;
    const toYRight = (v: number) => height - PADDING.bottom - (v / maxRight) * chartHeight;
    const xOf = (i: number) => PADDING.left + i * stepX;

    // 横向网格线 + 双轴刻度
    ctx.strokeStyle = chartVar("--border", "#e5e7eb");
    ctx.globalAlpha = 0.5;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = PADDING.top + (chartHeight / 4) * i;
      ctx.beginPath();
      ctx.moveTo(PADDING.left, y);
      ctx.lineTo(width - PADDING.right, y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = axisColor;
    ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const y = PADDING.top + (chartHeight / 4) * i;
      ctx.fillText(String(Math.round((maxLeft / 4) * (4 - i))), PADDING.left - 6, y);
    }
    ctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) {
      const y = PADDING.top + (chartHeight / 4) * i;
      ctx.fillText(`${((maxRight / 4) * (4 - i)).toFixed(1)}h`, width - PADDING.right + 6, y);
    }

    // hover 竖直参考线
    if (hover) {
      ctx.strokeStyle = axisColor;
      ctx.globalAlpha = 0.4;
      ctx.beginPath();
      ctx.moveTo(hover.x, PADDING.top);
      ctx.lineTo(hover.x, height - PADDING.bottom);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // 三条折线 + 数据点
    const series: Array<{ key: "todos" | "events" | "minutes"; color: string; toY: (v: number) => number }> = [
      { key: "todos", color: c1, toY: toYLeft },
      { key: "events", color: c2, toY: toYLeft },
      { key: "minutes", color: c3, toY: (v: number) => toYRight(v / 60) },
    ];
    for (const s of series) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      data.forEach((d, i) => {
        const x = xOf(i);
        const y = s.toY(d[s.key]);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = s.color;
      data.forEach((d, i) => {
        ctx.beginPath();
        ctx.arc(xOf(i), s.toY(d[s.key]), 3, 0, Math.PI * 2);
        ctx.fill();
      });
    }

    // 日期标签（首/中/末）
    ctx.fillStyle = axisColor;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const i of [0, Math.floor(data.length / 2), data.length - 1]) {
      if (i < data.length) ctx.fillText(data[i].date.slice(5), xOf(i), height - PADDING.bottom + 6);
    }
  }, [data, width, height, hover]);

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (data.length === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const chartWidth = width - PADDING.left - PADDING.right;
    const stepX = chartWidth / Math.max(data.length - 1, 1);
    const idx = Math.round((mouseX - PADDING.left) / stepX);
    if (idx < 0 || idx >= data.length) {
      setHover(null);
      return;
    }
    const d = data[idx];
    setHover({
      date: d.date,
      todos: d.todos,
      events: d.events,
      hours: Math.round(d.minutes / 6) / 10,
      x: PADDING.left + idx * stepX,
      y: e.clientY - rect.top,
    });
  };

  return (
    <div ref={wrapRef} className="multi-line-trend">
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHover(null)}
      />
      {hover && (
        <div
          className="multi-line-tooltip"
          style={{
            left: `${Math.min(Math.max(hover.x, 76), width - 76)}px`,
            top: `${hover.y}px`,
          }}
        >
          <div className="multi-line-tooltip-date">{hover.date}</div>
          <div className="multi-line-tooltip-row">
            <span className="multi-line-tooltip-dot c1" />
            完成待办：{hover.todos}
          </div>
          <div className="multi-line-tooltip-row">
            <span className="multi-line-tooltip-dot c2" />
            日程发生：{hover.events}
          </div>
          <div className="multi-line-tooltip-row">
            <span className="multi-line-tooltip-dot c3" />
            时间花费：{hover.hours}h
          </div>
        </div>
      )}
      <div className="multi-line-legend">
        <span className="multi-line-legend-item">
          <span className="multi-line-legend-dot c1" />
          完成待办
        </span>
        <span className="multi-line-legend-item">
          <span className="multi-line-legend-dot c2" />
          日程发生
        </span>
        <span className="multi-line-legend-item">
          <span className="multi-line-legend-dot c3" />
          时间花费
        </span>
      </div>
    </div>
  );
}
