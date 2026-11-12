// 统计页轻量图表：纯 div/flex 自绘 + Canvas 组件共用取色工具，零图表库依赖
// ——项目风格统一（毛玻璃手作感）且量级极小。配色走 tokens 设计变量。
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import "./StatsCharts.css";

/** 读设计颜色变量（缺省回落传入值，防变量未注入时图表无色）。Canvas 绘制用。 */
export const chartVar = (name: string, fallback: string) => {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
};

/** 读设计颜色变量并解析为 [r, g, b]（供 Canvas rgba() 组合透明度）。 */
export const chartRgb = (
  name: string,
  fallback: [number, number, number],
): [number, number, number] => {
  const css = chartVar(name, "");
  const m = css.match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  const h = css.replace("#", "");
  if (/^[0-9a-f]{6}$/i.test(h)) {
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  return fallback;
};

/** 横条组（时间花费 by 项目 / 优先级分布）：label 定宽 + 条形按 max 百分比 + 值右。
 *  onRowClick 传入时行可点（指标下钻，Enter 键同触发）；noDrill 行（如「未归类」）
 *  渲染但不可点。rows 空时由调用方渲染空态。 */
export function HBars({
  rows,
  fmtValue,
  onRowClick,
}: {
  rows: { label: string; value: number; color?: string; noDrill?: boolean }[];
  fmtValue?: (v: number) => string;
  onRowClick?: (label: string) => void;
}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  // label 列宽 = 组内最长标签宽 + 恒定间隙（中文一字 ≈ 12px，上限 110 防
  // 超长名撑爆）——不同卡片的「标签→条形」间隙一致（列宽恰包住文字，无下限）
  const labelWidth = Math.min(
    110,
    Math.max(...rows.map((r) => r.label.length), 1) * 12 + 6,
  );
  return (
    <div className="hbars">
      {rows.map((r) => {
        const clickable = Boolean(onRowClick) && !r.noDrill;
        return (
          <div
            key={r.label}
            className={`hbar-row${clickable ? " clickable" : ""}`}
            {...(clickable
              ? {
                  role: "button",
                  tabIndex: 0,
                  title: `查看项目「${r.label}」`,
                  onClick: () => onRowClick?.(r.label),
                  onKeyDown: (e: ReactKeyboardEvent) => {
                    if (e.key === "Enter") onRowClick?.(r.label);
                  },
                }
              : {})}
          >
            <span className="hbar-label" title={r.label} style={{ width: labelWidth }}>{r.label}</span>
            <div className="hbar-track">
              <div
                className="hbar-fill"
                style={{ width: `${Math.max(r.value > 0 ? 3 : 0, (r.value / max) * 100)}%`, ...(r.color ? { background: r.color } : {}) }}
              />
            </div>
            <span className="hbar-value">
              {fmtValue ? fmtValue(r.value) : r.value}
            </span>
          </div>
        );
      })}
    </div>
  );
}
