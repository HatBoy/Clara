// 重复系列范围三问气泡（2026-11-12 拖拽三问批）：拖动/拉伸重复日程实例落定后，
// 在落点弹「仅此 / 及后续 / 所有」范围气泡（Google/Apple/Outlook 共识语义——
// 绝不静默改全系列）。ConfirmPopover 不能程序化弹出（内部 state、按钮触发），
// 拖拽场景由 pointerup/drop 事件驱动，故独立轻组件；视觉复用
// .schedule-detail-apply 语言（ScheduleCanvas.css 全局加载）。
// 关闭路由同 ConfirmPopover：取消钮 / 点外（pointerdown）/ Esc；不挂滚动关闭
// （拖拽落点弹窗的存活窗极短，选完即走）。
import { useEffect, useRef } from "react";

export type SeriesScopeMode = "this" | "following" | "all";

export default function SeriesScopeAsk({
  x,
  y,
  title,
  onPick,
  onCancel,
}: {
  // 视口坐标（拖拽落点 pointerup 的 clientX/Y 或 drop 目标的 rect 参考）
  x: number;
  y: number;
  title: string;
  onPick: (mode: SeriesScopeMode) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current?.contains(e.target as Node)) return;
      onCancel();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 视口钳位（2026-11-12 定位修正批）：x/y 语义 = 气泡期望**左上角**（浮层
  // 时间行传触发钮 rect 左缘/下缘；拖拽落点传 clientX/Y+偏移），12px 边距内收
  const left = Math.min(Math.max(x, 12), Math.max(window.innerWidth - 252, 12));
  const top = Math.min(Math.max(y, 12), Math.max(window.innerHeight - 220, 12));

  return (
    <div ref={ref} className="series-scope-ask" style={{ left, top }} role="dialog">
      <p className="series-scope-title">{title}</p>
      <button type="button" className="series-scope-btn" onClick={() => onPick("this")}>
        仅此日程
      </button>
      <button type="button" className="series-scope-btn" onClick={() => onPick("following")}>
        此日程及后续
      </button>
      <button type="button" className="series-scope-btn solid" onClick={() => onPick("all")}>
        所有日程
      </button>
      <button type="button" className="series-scope-cancel" onClick={onCancel}>
        取消
      </button>
    </div>
  );
}
