// 习惯热力图：横向时间轴网格（横轴 6~22 点、纵轴周一~周日，贡献图式）。
// 格子尺寸与列间距一起按容器宽自适应（剩余宽度分配到间距，canvas 撑满卡片
// 不留两侧空白）；hover 浮层显示具体时段与次数；颜色走设计变量
// （done = --accent 族、defer = --danger 族）。
import { useEffect, useRef, useState } from "react";
import { chartRgb, chartVar } from "./StatsCharts";

type HabitHeatmapProps = {
  data: number[][]; // 7×24 二维数组，[weekday][hour]
  mode: "done" | "defer"; // done=完成热力图，defer=拖延热力图
};

const LABEL_W = 26;
const LABEL_H = 16;
const HOUR_START = 6;
const HOUR_END = 22;
const CELL_MIN = 10;
const CELL_MAX = 22;
const GAP_MIN = 2;
const GAP_MAX = 20;

export default function HabitHeatmap({ data, mode }: HabitHeatmapProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  // cell 与 gap 联合自适应：格子先按保底间距估宽并 clamp，剩余宽度全部分配到
  // 间距（也 clamp）——窄卡紧凑密排、宽卡疏朗排布，canvas 总宽恒等于容器宽
  const [geom, setGeom] = useState({ cell: 18, gap: 4 });
  const [hover, setHover] = useState<{ text: string; x: number; y: number } | null>(null);

  const hourCount = HOUR_END - HOUR_START + 1; // 横轴列数（17）
  const { cell, gap } = geom;
  const width = LABEL_W + hourCount * cell + (hourCount - 1) * gap;
  const height = LABEL_H + 7 * cell + 6 * gap;

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0].contentRect.width;
      if (w <= 0) return;
      const avail = w - LABEL_W;
      const est = (avail - (hourCount - 1) * GAP_MIN) / hourCount;
      const c = Math.round(Math.min(CELL_MAX, Math.max(CELL_MIN, est)) * 10) / 10;
      const g = Math.round(
        Math.min(GAP_MAX, Math.max(GAP_MIN, (avail - hourCount * c) / (hourCount - 1))) * 10,
      ) / 10;
      setGeom({ cell: c, gap: g });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [hourCount]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const base = chartRgb(
      mode === "done" ? "--accent" : "--danger",
      mode === "done" ? [99, 102, 241] : [239, 68, 68],
    );
    const labelColor = chartVar("--text-muted", "#9ca3af");

    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textBaseline = "middle";

    // 小时标签（顶部，偶数点）
    ctx.fillStyle = labelColor;
    ctx.textAlign = "center";
    for (let i = 0; i < hourCount; i++) {
      const hour = HOUR_START + i;
      if (hour % 2 === 0) {
        ctx.fillText(String(hour), LABEL_W + i * (cell + gap) + cell / 2, LABEL_H / 2);
      }
    }

    // 星期标签（左侧）
    const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
    ctx.textAlign = "right";
    weekdays.forEach((w, wd) => {
      ctx.fillText(w, LABEL_W - 5, LABEL_H + wd * (cell + gap) + cell / 2);
    });

    // 色阶归一（最大值）
    let maxValue = 0;
    for (let wd = 0; wd < 7; wd++) {
      for (let h = HOUR_START; h <= HOUR_END; h++) {
        maxValue = Math.max(maxValue, data[wd]?.[h] ?? 0);
      }
    }

    // 热力格子：列 = 小时、行 = 星期
    for (let wd = 0; wd < 7; wd++) {
      for (let i = 0; i < hourCount; i++) {
        const hour = HOUR_START + i;
        const value = data[wd]?.[hour] ?? 0;
        const x = LABEL_W + i * (cell + gap);
        const y = LABEL_H + wd * (cell + gap);
        if (value === 0) {
          ctx.fillStyle = "rgba(148, 163, 184, 0.15)"; // 无数据：淡灰底
        } else {
          const alpha = 0.2 + (value / maxValue) * 0.8; // 最浅 20% → 最深 100%
          ctx.fillStyle = `rgba(${base[0]}, ${base[1]}, ${base[2]}, ${alpha})`;
        }
        const r = Math.min(4, cell / 4); // 圆角随格子缩放
        ctx.beginPath();
        ctx.roundRect(x, y, cell, cell, r);
        ctx.fill();
      }
    }
  }, [data, mode, cell, gap, width, height, hourCount]);

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const hi = Math.floor((x - LABEL_W) / (cell + gap));
    const wd = Math.floor((y - LABEL_H) / (cell + gap));
    if (hi < 0 || hi >= hourCount || wd < 0 || wd > 6) {
      setHover(null);
      return;
    }
    const hour = HOUR_START + hi;
    const value = data[wd]?.[hour] ?? 0;
    const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    setHover({
      text: `${weekdays[wd]} ${hour}点 · ${mode === "done" ? "完成" : "改期"} ${value} 次`,
      x: LABEL_W + hi * (cell + gap) + cell / 2,
      y: LABEL_H + wd * (cell + gap),
    });
  };

  return (
    <div ref={wrapRef} className="habit-heatmap">
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHover(null)}
      />
      {hover && (
        <div className="habit-heatmap-tip" style={{ left: `${hover.x}px`, top: `${hover.y}px` }}>
          {hover.text}
        </div>
      )}
      <div className="habit-heatmap-legend">
        <span className="habit-heatmap-legend-label lead">完成时段</span>
        <span className="habit-heatmap-legend-label">少</span>
        <div className={`habit-heatmap-legend-bar ${mode}`} />
        <span className="habit-heatmap-legend-label">多</span>
      </div>
    </div>
  );
}
