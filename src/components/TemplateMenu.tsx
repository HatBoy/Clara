// 日程模板入口（2026-11-17 批 2）：QuickAdd 旁「模板」按钮弹层 + 「存为模板」弹窗。
// 模板应用生成普通任务（App 负责 upsert），落库后走既有提醒/冲突/统计链路。
import { useEffect, useRef, useState } from "react";
import { LayoutTemplate, Trash2 } from "lucide-react";
import {
  applyTemplate,
  createTemplate,
  deleteTemplate,
  fetchTemplates,
  type ApiTask,
  type ApiTemplate,
} from "../lib/api";
import type { Task } from "../data/tasks";
import { toRrule } from "../lib/rrule";
import "./TemplateMenu.css";

export function TemplateMenu({
  date,
  onApplied,
}: {
  date: string; // 应用到该日期（YYYY-MM-DD）
  onApplied: (task: ApiTask) => void;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ApiTemplate[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setErr(null);
    fetchTemplates()
      .then(setItems)
      .catch((e) => setErr(e instanceof Error ? e.message : "模板加载失败"));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (ev: PointerEvent) => {
      if (!rootRef.current?.contains(ev.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  const apply = (tpl: ApiTemplate) => {
    applyTemplate(tpl.id, { date })
      .then((task) => {
        onApplied(task);
        setOpen(false);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "应用模板失败"));
  };

  const remove = (tpl: ApiTemplate) => {
    deleteTemplate(tpl.id)
      .then(() => {
        setItems((prev) => (prev ?? []).filter((t) => t.id !== tpl.id));
        setConfirmId(null);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "删除模板失败"));
  };

  return (
    <div className="template-menu" ref={rootRef}>
      <button
        type="button"
        className="template-menu-btn"
        onClick={() => setOpen((v) => !v)}
        title="从模板创建"
      >
        <LayoutTemplate size={14} strokeWidth={2} />
        模板
      </button>
      {open && (
        <div className="template-menu-panel">
          {err && <div className="template-menu-error">{err}</div>}
          {items === null ? (
            <div className="template-menu-empty">加载中…</div>
          ) : items.length === 0 ? (
            <div className="template-menu-empty">
              还没有模板。可在日程详情里「存为模板」。
            </div>
          ) : (
            items.map((tpl) => (
              <div key={tpl.id} className="template-menu-row">
                <button type="button" className="template-menu-apply"
                        onClick={() => apply(tpl)}>
                  <span className="template-menu-name">{tpl.name}</span>
                  <span className="template-menu-meta">
                    {tpl.start_min !== null
                      ? `${String(Math.floor(tpl.start_min / 60)).padStart(2, "0")}:${String(tpl.start_min % 60).padStart(2, "0")}`
                      : "未排期"}
                    {tpl.duration_mins ? ` · ${tpl.duration_mins} 分钟` : ""}
                  </span>
                </button>
                {confirmId === tpl.id ? (
                  <button type="button" className="template-menu-del confirm"
                          onClick={() => remove(tpl)}>
                    删除?
                  </button>
                ) : (
                  <button type="button" className="template-menu-del"
                          onClick={() => setConfirmId(tpl.id)} aria-label="删除模板">
                    <Trash2 size={12} strokeWidth={2} />
                  </button>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export function SaveTemplateModal({
  task,
  onClose,
}: {
  task: Task;
  onClose: () => void;
}) {
  const [name, setName] = useState(task.title.slice(0, 30));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Esc 取消（弹窗是 fixed 遮罩层，键盘理应能退出）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  const startMin = task.start !== undefined ? Math.round(task.start * 60) % 1440 : null;
  const priority = task.priority === "high" ? 1 : task.priority === "low" ? 3 : 2;

  const save = () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("模板名不能为空");
      return;
    }
    setBusy(true);
    createTemplate({
      name: trimmed,
      title: task.title,
      duration_mins: task.minutes ?? null,
      start_min: startMin,
      priority,
      notes: task.note ?? null,
      recurrence_rule: task.recurrence ? toRrule(task.recurrence) : null,
      subtasks: (task.checklist ?? []).map((c) => c.title),
    })
      .then(() => onClose())
      .catch((e) => {
        setErr(e instanceof Error ? e.message : "保存模板失败");
        setBusy(false);
      });
  };

  return (
    <div className="template-modal-overlay" onClick={onClose}>
      <div className="template-modal" role="dialog" aria-label="存为模板"
           onClick={(e) => e.stopPropagation()}>
        <span className="template-modal-title">存为日程模板</span>
        <label className="template-modal-row">
          <span>模板名</span>
          <input value={name} maxLength={30} autoFocus
                 onChange={(e) => setName(e.target.value)} />
        </label>
        <p className="template-modal-note">
          将保存标题、时长、重复规则、备注与清单；时刻存为相对时间（
          {startMin !== null
            ? `${String(Math.floor(startMin / 60)).padStart(2, "0")}:${String(startMin % 60).padStart(2, "0")}`
            : "未排期"}）。
        </p>
        {err && <div className="template-menu-error">{err}</div>}
        <div className="template-modal-foot">
          <button type="button" className="template-menu-btn" onClick={onClose}>
            取消
          </button>
          <button type="button" className="template-menu-btn primary"
                  disabled={busy} onClick={save}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
