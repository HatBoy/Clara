// 统一空态/失败态（2026-11-17 批 1）：标题 + 说明 + 明确的下一步动作。
// 用法：面板空数据、加载失败、未配置引导；每个空态至少给一个可点动作。
import type { ReactNode } from "react";
import "./EmptyState.css";

export default function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  tone = "empty",
  compact = false,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  secondaryAction?: { label: string; onClick: () => void };
  tone?: "empty" | "error";
  compact?: boolean; // 右栏窄容器用
}) {
  return (
    <div
      className={`empty-state${compact ? " compact" : ""}${tone === "error" ? " error" : ""}`}
      role="status"
      aria-live="polite"
    >
      {icon && <span className="empty-state-icon">{icon}</span>}
      <span className="empty-state-title">{title}</span>
      {description && <span className="empty-state-desc">{description}</span>}
      {(action || secondaryAction) && (
        <span className="empty-state-actions">
          {action && (
            <button type="button" className="empty-state-btn primary"
                    onClick={action.onClick}>
              {action.label}
            </button>
          )}
          {secondaryAction && (
            <button type="button" className="empty-state-btn"
                    onClick={secondaryAction.onClick}>
              {secondaryAction.label}
            </button>
          )}
        </span>
      )}
    </div>
  );
}
