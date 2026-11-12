import { useEffect, useRef, useState } from "react";
import type { CSSProperties, MouseEvent as ReactMouseEvent, ReactNode } from "react";
import "./ConfirmPopover.css";

// 删除二次确认气泡（popconfirm 模式）：三处删除入口（侧栏项目 / 收件箱待办 / 日程
// 详情浮层）共用——点删除按钮原地弹气泡，确认才执行，取消 / 点外 / Esc / 滚动关闭。
// 气泡 position:fixed 相对视口，不受侧栏与列表滚动容器的 overflow 裁剪；打开瞬间按
// 触发器矩形计算坐标。包裹层 display:contents 不参与布局（按钮仍是父 flex 直接子项），
// confirming 类供父级 :has() 联动——气泡打开期间触发按钮保持可见（hover 显隐的例外）
export default function ConfirmPopover({
  message,
  onConfirm,
  actions,
  placement = "top",
  buttonClass,
  buttonTitle,
  icon,
  label,
}: {
  message: string;
  onConfirm: () => void;
  /** 可选纵向动作列表（如重复系列三选删除）：传入则渲染全宽动作按钮列 + 末尾中性
   *  "取消"钮（三动作横排放不下）；此时 onConfirm 不渲染（各动作自带 onClick） */
  actions?: { text: string; onClick: () => void; tone?: "danger" | "danger-outline" }[];
  placement?: "top" | "right"; // top = 按钮上方（空间不足自动翻下方）；right = 按钮右侧（侧栏窄栏用）
  buttonClass: string; // 复用既有按钮样式类（hover 变红等全部保留）
  buttonTitle: string;
  icon?: ReactNode; // 可选前置图标（数据接入卡按钮为纯文字，不传即无）
  label?: string; // 可选按钮文字（浮层删除钮的"删除"）
}) {
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({ top: -9999, left: -9999 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const close = () => setOpen(false);

  const toggle = (e: ReactMouseEvent) => {
    e.stopPropagation(); // 防冒泡触发外层（项目行/待办卡）既有的点击/收起逻辑
    if (open) {
      close();
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const W = actions ? 240 : 200; // 与 .confirm-pop 定宽一致（动作列模式加宽）
    // 纵向动作列气泡更高：上弹空间阈值同步放大（贴近视口顶即翻下方弹出）
    const topSpace = actions ? 200 : 110;
    let top: number;
    let left: number;
    let transform: string | undefined;
    if (placement === "right") {
      top = rect.top;
      left = Math.min(rect.right + 8, window.innerWidth - W - 8);
    } else {
      // 上弹右缘对齐按钮（删除钮多在卡片右侧），按钮贴近视口顶时翻到下方
      left = Math.max(8, Math.min(rect.right - W, window.innerWidth - W - 8));
      if (rect.top > topSpace) {
        top = rect.top - 8;
        transform = "translateY(-100%)"; // 气泡底边贴按钮顶，无需预知气泡高度
      } else {
        top = Math.min(rect.bottom + 8, window.innerHeight - 96);
      }
    }
    setStyle({ top, left, transform });
    setOpen(true);
  };

  const confirm = (e: ReactMouseEvent) => {
    e.stopPropagation();
    close();
    onConfirm();
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      // capture 阶段拦截（先于浮层等冒泡监听）：Esc 只关气泡，不连带关闭详情浮层
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    };
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!popRef.current?.contains(t) && !triggerRef.current?.contains(t)) close();
    };
    const dismiss = () => close(); // 滚动/缩放后坐标失准，直接关闭（重开代价小）
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismiss, true); // capture 捕获任意滚动容器
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismiss, true);
    };
  }, [open]);

  return (
    <span className={`confirm-wrap${open ? " confirming" : ""}`}>
      <button
        ref={triggerRef}
        type="button"
        className={buttonClass}
        title={buttonTitle}
        aria-label={buttonTitle}
        aria-expanded={open}
        onClick={toggle}
      >
        {icon}
        {label}
      </button>
      {open && (
        <div
          ref={popRef}
          className={`confirm-pop${actions ? " wide" : ""}`}
          style={style}
          role="alertdialog"
          aria-label={buttonTitle}
        >
          <p className="confirm-pop-message">{message}</p>
          {actions ? (
            <>
              <div className="confirm-pop-actions-list">
                {actions.map((a) => (
                  <button
                    key={a.text}
                    type="button"
                    className={`confirm-pop-action${a.tone === "danger-outline" ? " outline" : ""}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      close();
                      a.onClick();
                    }}
                  >
                    {a.text}
                  </button>
                ))}
              </div>
              <button type="button" className="confirm-pop-action cancel" onClick={close}>
                取消
              </button>
            </>
          ) : (
            <div className="confirm-pop-actions">
              <button type="button" className="confirm-pop-cancel" onClick={close}>
                取消
              </button>
              <button type="button" className="confirm-pop-ok" onClick={confirm}>
                删除
              </button>
            </div>
          )}
        </div>
      )}
    </span>
  );
}
