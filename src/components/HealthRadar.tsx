// 健康度雷达图：5 维度五边形雷达 + 中心综合分。尺寸由布局给定（size prop）；
// 颜色走 --accent / --text-secondary 设计变量。
import { useEffect, useRef } from "react";
import { chartRgb, chartVar } from "./StatsCharts";

type HealthRadarProps = {
  dimensions: {
    digest: number;
    load: number;
    advance: number;
    rhythm: number;
    ai_collab: number;
  };
  labels: {
    digest: string;
    load: string;
    advance: string;
    rhythm: string;
    ai_collab: string;
  };
  total: number;
  /** 画布边长（px，逻辑像素）；缺省 240 */
  size?: number;
};

export default function HealthRadar({ dimensions, labels, total, size = 240 }: HealthRadarProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const accent = chartRgb("--accent", [99, 102, 241]);
    const labelColor = chartVar("--text-secondary", "#6b7280");
    const gridColor = chartVar("--border", "#e5e7eb");

    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.width = `${size}px`;
    canvas.style.height = `${size}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    const centerX = size / 2;
    const centerY = size / 2;
    // 数据多边形半径 + 标签外扩半径：标签（4 字 ≈ 44px 宽）必须完整落在画布
    // 内——外扩钳制 size/2-22（此前固定 radius+24 在小尺寸下左右标签被裁）
    const radius = size * 0.28;
    const labelRadius = Math.min(radius + 22, size / 2 - 24);

    const values = [
      dimensions.digest,
      dimensions.load,
      dimensions.advance,
      dimensions.rhythm,
      dimensions.ai_collab,
    ];
    const labelTexts = [
      labels.digest,
      labels.load,
      labels.advance,
      labels.rhythm,
      labels.ai_collab,
    ];

    // 背景网格（4 层同形五边形）+ 轴线
    ctx.strokeStyle = gridColor;
    ctx.globalAlpha = 0.18;
    ctx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) {
      const r = (radius * i) / 4;
      ctx.beginPath();
      for (let j = 0; j < 5; j++) {
        const angle = (Math.PI * 2 * j) / 5 - Math.PI / 2;
        const x = centerX + r * Math.cos(angle);
        const y = centerY + r * Math.sin(angle);
        if (j === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.stroke();
    }
    for (let i = 0; i < 5; i++) {
      const angle = (Math.PI * 2 * i) / 5 - Math.PI / 2;
      ctx.beginPath();
      ctx.moveTo(centerX, centerY);
      ctx.lineTo(centerX + radius * Math.cos(angle), centerY + radius * Math.sin(angle));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // 数据多边形（半透明填充 + 描边）
    const pointOf = (i: number, value: number) => {
      const angle = (Math.PI * 2 * i) / 5 - Math.PI / 2;
      const r = (radius * Math.max(0, Math.min(100, value))) / 100;
      return [centerX + r * Math.cos(angle), centerY + r * Math.sin(angle)] as const;
    };
    ctx.fillStyle = `rgba(${accent[0]}, ${accent[1]}, ${accent[2]}, 0.2)`;
    ctx.strokeStyle = `rgba(${accent[0]}, ${accent[1]}, ${accent[2]}, 0.8)`;
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((v, i) => {
      const [x, y] = pointOf(i, v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // 数据点
    ctx.fillStyle = `rgb(${accent[0]}, ${accent[1]}, ${accent[2]})`;
    values.forEach((v, i) => {
      const [x, y] = pointOf(i, v);
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });

    // 轴标签（径向外扩，半径已钳制不越画布）
    ctx.fillStyle = labelColor;
    ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    labelTexts.forEach((text, i) => {
      const angle = (Math.PI * 2 * i) / 5 - Math.PI / 2;
      const x = centerX + labelRadius * Math.cos(angle);
      const y = centerY + labelRadius * Math.sin(angle);
      ctx.fillText(text, x, y);
    });
  }, [dimensions, labels, size]);

  return (
    <div className="health-radar">
      <canvas ref={canvasRef} />
      <div className="health-radar-score">
        <span className="health-radar-score-label">综合健康度</span>
        <span className="health-radar-score-value">{total}</span>
      </div>
    </div>
  );
}
