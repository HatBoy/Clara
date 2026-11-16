// 全局撤销条（2026-11-17 批 1）：主窗底部居中，展示最近一次可撤销操作；
// 6 秒后自动消失，Ctrl+Z 或点「撤销」触发后端快照回写。
import { Undo2, X } from "lucide-react";
import "./UndoBar.css";

export default function UndoBar({
  label,
  onUndo,
  onClose,
}: {
  label: string;
  onUndo: () => void;
  onClose: () => void;
}) {
  return (
    <div className="undo-bar" role="status" aria-live="polite">
      <span className="undo-bar-label">{label}</span>
      <button type="button" className="undo-bar-action" onClick={onUndo}>
        <Undo2 size={13} strokeWidth={2.2} />
        撤销
      </button>
      <button
        type="button"
        className="undo-bar-close"
        onClick={onClose}
        aria-label="关闭撤销提示"
      >
        <X size={13} strokeWidth={2} />
      </button>
    </div>
  );
}
