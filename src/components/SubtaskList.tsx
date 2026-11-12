// 子任务清单编辑区（2026-11-06 批 C；真机批根修与补全）：日程详情浮层与收件箱
// 编辑展开区共用。语义 = 全量替换——每次变更把整组（仅 title/done）交调用方提交
// （后端 delete+reinsert，量级无害）。
// 交互：勾选/删行/回车添加/单击标题内联编辑 + 完成 x/y 计数。
// 定位铁律：勾选/删除/编辑一律按**索引**定位——编辑产出的行没有 id（后端重新
// 生成），按 id 定位时 undefined === undefined 会对全体行命中，曾致「点一个全部
// 完成 / 删一个全删」（2026-11-06 真机批根修，勿改回按 id）。
import { useState } from "react";
import { Check, ListChecks, X } from "lucide-react";
import { TITLE_MAX_LEN, type Task } from "../data/tasks";
import "./SubtaskList.css";

type Row = Task["checklist"] extends (infer R)[] | undefined ? R : never;

const strip = (rows: Row[]) => rows.map(({ title, done }) => ({ title, done }));

export default function SubtaskList({
  items,
  onChange,
}: {
  items: Row[];
  onChange: (rows: { title: string; done: boolean }[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const doneCount = items.filter((i) => i.done).length;

  const toggleAt = (i: number) =>
    onChange(strip(items.map((x, j) => (j === i ? { ...x, done: !x.done } : x))));

  const removeAt = (i: number) => onChange(strip(items.filter((_, j) => j !== i)));

  const finishEdit = (save: boolean) => {
    if (save && editIdx !== null && editIdx < items.length) {
      const text = editDraft.trim();
      if (text && text !== items[editIdx].title) {
        onChange(strip(items.map((x, j) => (j === editIdx ? { ...x, title: text } : x))));
      }
    }
    setEditIdx(null);
  };

  const add = () => {
    const text = draft.trim();
    if (!text) return;
    onChange([...strip(items), { title: text, done: false }]);
    setDraft("");
  };

  return (
    <div className="subtask-list">
      <div className="subtask-head">
        <ListChecks size={13} strokeWidth={2} />
        <span>子任务</span>
        {items.length > 0 && (
          <span className="subtask-count">
            {doneCount}/{items.length}
          </span>
        )}
      </div>
      {items.map((it, i) => (
        <div key={it.id ?? `${it.title}-${i}`} className={`subtask-row${it.done ? " done" : ""}`}>
          <button
            type="button"
            className="subtask-check"
            aria-label={it.done ? "取消勾选" : "勾选完成"}
            onClick={() => toggleAt(i)}
          >
            {it.done && <Check size={11} strokeWidth={3} />}
          </button>
          {editIdx === i ? (
            <input
              className="subtask-edit"
              value={editDraft}
              maxLength={TITLE_MAX_LEN}
              autoFocus
              onChange={(e) => setEditDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing) return;
                if (e.key === "Enter") {
                  e.preventDefault();
                  finishEdit(true);
                } else if (e.key === "Escape") {
                  e.stopPropagation(); // 只取消编辑不冒泡关浮层
                  finishEdit(false);
                }
              }}
              onBlur={() => finishEdit(true)}
            />
          ) : (
            <span
              className="subtask-title"
              title="点击编辑"
              onClick={() => {
                setEditIdx(i);
                setEditDraft(it.title);
              }}
            >
              {it.title}
            </span>
          )}
          <button
            type="button"
            className="subtask-del"
            aria-label="删除子任务"
            onClick={() => removeAt(i)}
          >
            <X size={11} strokeWidth={2.5} />
          </button>
        </div>
      ))}
      <input
        className="subtask-add"
        value={draft}
        placeholder="添加子任务，回车确认"
        maxLength={TITLE_MAX_LEN}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          } else if (e.key === "Escape") {
            e.stopPropagation(); // 只清草稿不冒泡关浮层
            setDraft("");
          }
        }}
      />
    </div>
  );
}
