// 批量排期确认弹窗（2026-10-25 功能边界批）：对话里说「把所有待办排了」类
// 批量意图时，引擎产出的 bulk 草案经弹窗清单确认——对话框只沟通，批量确认
// 收口弹窗（骨架复用 FormModal：Esc/遮罩关闭走取消）。勾选只是核对余地：
// 确认 = 勾选条目写入日历（App 走 applyDraftItems 改期路径），一条没勾则
// 确认钮禁用只能取消（建议行整卡结清，任务本身不丢——留在收件箱未排期）
import { useState } from "react";
import type { DraftPlanItem } from "../lib/ai";
import { FormModal } from "./settings/DataSettings";
import "./BulkScheduleModal.css";

// "MM-DD HH:MM"（naive 本地 ISO 按本地解析，同 fmtPrepWhen 口径——bulk 建议
// 常跨日，日期不可省）
function fmtWhen(start: string) {
  const d = new Date(start);
  const md = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${md} ${hhmm}`;
}

export function BulkScheduleModal({
  items,
  submitting,
  onConfirm,
  onCancel,
}: {
  items: DraftPlanItem[];
  submitting: boolean;
  onConfirm: (items: DraftPlanItem[]) => void;
  onCancel: () => void;
}) {
  const [checked, setChecked] = useState<boolean[]>(() => items.map(() => true));
  const picked = items.filter((_, i) => checked[i]);
  const allOn = picked.length === items.length;
  return (
    <FormModal
      title={`批量排期确认（${items.length} 条）`}
      onClose={submitting ? () => {} : onCancel}
    >
      <p className="bulk-sub">
        已为待排期待办生成时间建议（统一排期引擎：避让占用、午休与每日工时预算），
        勾选后写入日历。
      </p>
      <ul className="bulk-list">
        {items.map((it, i) => (
          <li key={`${it.task_id ?? it.title}#${i}`}>
            <label className="bulk-row">
              <input
                type="checkbox"
                checked={checked[i]}
                disabled={submitting}
                onChange={() =>
                  setChecked((prev) => prev.map((c, j) => (j === i ? !c : c)))
                }
              />
              <span className="bulk-title">{it.title}</span>
              <span className="bulk-when">
                {fmtWhen(it.start)} · {it.duration} 分钟
              </span>
            </label>
          </li>
        ))}
      </ul>
      <div className="bulk-actions">
        <button
          type="button"
          className="bulk-toggle"
          disabled={submitting}
          onClick={() => setChecked(items.map(() => !allOn))}
        >
          {allOn ? "全不选" : "全选"}
        </button>
        <button type="button" className="bulk-cancel" disabled={submitting} onClick={onCancel}>
          取消
        </button>
        <button
          type="button"
          className="bulk-confirm"
          disabled={submitting || picked.length === 0}
          onClick={() => onConfirm(picked)}
        >
          {submitting ? "写入中…" : `确认排期（${picked.length}）`}
        </button>
      </div>
    </FormModal>
  );
}
