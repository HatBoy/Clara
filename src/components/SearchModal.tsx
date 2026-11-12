// 搜索弹层（2026-11-06 搜索批 60→80 分档迭代）：居中弹层 + 300ms 防抖 + 结果
// 分组 + 键盘导航 + 跳转定位。弹层骨架复用 .data-modal-overlay/.data-modal
//（frost 走 ::before 垫层，ChatCommSettings 跨页 import DataSettings.css 先例），
// 头部改为搜索输入框（autoFocus）。跳转分两路：日程行 → dispatchCanvasJump
//（画布切日视图定位，ScheduleCanvas 常驻订阅安全）；待办行 → 上报 App
//（onLocateTodo，App 持 {id, seq} 计数器 props 驱动 InboxPanel 定位展开——
// 竞态教训的稳妥形态）。80 分档新增：命中词高亮 / notes 命中摘录（title 未命中
// 时副行展示命中上下文，说明"为什么命中"）/ 相对日期文案 / 优先级圆点。
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { CalendarDays, Circle, Database, Folder, Plus, Search, SquareCheck } from "lucide-react";
import {
  searchAll,
  type SearchProposalItemRow,
  type SearchProjectRow,
  type SearchReply,
  type SearchTaskRow,
} from "../lib/api";
import { dispatchCanvasJump } from "../lib/shortcuts";
import "./settings/DataSettings.css";
import "./SearchModal.css";

// 单组展示上限（后端整包 50，前端按待办/日程各截 25）
const GROUP_LIMIT = 25;

/** 大小写不敏感命中位置（ASCII 折叠比对；中文 indexOf 等价），未命中 -1 */
function indexCi(text: string, q: string): number {
  return text.toLowerCase().indexOf(q.toLowerCase());
}

/** 命中词高亮渲染：把 text 按 q 切段，命中段包 .search-hl。未命中原样返回 */
function highlight(text: string, q: string): ReactNode {
  const at = indexCi(text, q);
  if (at < 0) return text;
  return (
    <>
      {text.slice(0, at)}
      <span className="search-hl">{text.slice(at, at + q.length)}</span>
      {text.slice(at + q.length)}
    </>
  );
}

/** notes 命中摘录：命中处前后各 ~16 字的上下文窗（首尾省略号），带高亮 */
function excerpt(notes: string, q: string): ReactNode {
  const at = indexCi(notes, q);
  const from = Math.max(0, at - 16);
  const to = Math.min(notes.length, at + q.length + 16);
  return (
    <>
      {from > 0 ? "…" : ""}
      {highlight(notes.slice(from, at), q)}
      <span className="search-hl">{notes.slice(at, at + q.length)}</span>
      {highlight(notes.slice(at + q.length, to), q)}
      {to < notes.length ? "…" : ""}
    </>
  );
}

/** 相对日期文案：今天/明天/昨天/后天/前天/N 天前后 + 钟点；跨年带年份。
 *  天数用 floor（日历日差）——round 会把"昨天下午"（距今天 0 点不足 12h）
 *  四舍五入归 0 误标今天（2026-11-06 真机批：11.5 14:30 的日程显示"今天"） */
function fmtWhenRelative(iso: string): string {
  const d = new Date(iso);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const day0 = new Date();
  day0.setHours(0, 0, 0, 0);
  const days = Math.floor((d.getTime() - day0.getTime()) / 86400000);
  const ym =
    d.getFullYear() !== new Date().getFullYear() ? `${d.getFullYear()}年` : "";
  let rel: string;
  if (days === 0) rel = "今天";
  else if (days === 1) rel = "明天";
  else if (days === -1) rel = "昨天";
  else if (days === 2) rel = "后天";
  else if (days === -2) rel = "前天";
  else if (days > 0) rel = `${days} 天后`;
  else rel = `${-days} 天前`;
  return `${ym}${d.getMonth() + 1}月${d.getDate()}日（${rel}）${hm}`;
}

export default function SearchModal({
  onClose,
  onLocateTodo,
  onLocateProject,
  onGotoDatasource,
  onCreateTodo,
}: {
  onClose: () => void;
  /** 未排期未完成待办的定位请求（App 决定切收件箱页还是项目页并递增 seq） */
  onLocateTodo: (row: SearchTaskRow) => void;
  /** 项目行定位（切项目页） */
  onLocateProject: (name: string) => void;
  /** 建议条目跳数据源页 */
  onGotoDatasource: () => void;
  /** 快速创建（120 分档）：建未排期待办并跳收件箱定位（App 侧已关弹层） */
  onCreateTodo: (title: string) => void;
}) {
  const [q, setQ] = useState("");
  const [reply, setReply] = useState<SearchReply | null>(null); // null = 未搜（含空 q）
  const [searching, setSearching] = useState(false);
  const [includeDone, setIncludeDone] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);

  // 防抖 300ms（DataSettings 应用搜索同款 effect+setTimeout+cleanup 形态）；
  // 空 q 立即清空不请求；include_done 开关变化即重查
  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setReply(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const t = window.setTimeout(() => {
      searchAll(query, includeDone)
        .then(setReply)
        .catch(() => setReply({ tasks: [], projects: [], proposal_items: [], parsed_range: null }))
        .finally(() => setSearching(false));
    }, 300);
    return () => window.clearTimeout(t);
  }, [q, includeDone]);

  const rows = reply?.tasks ?? null;
  // 分组：项目（意图明确短平快）→ 待办（找回高频）→ 日程 → 建议条目
  const projectRows = useMemo(() => reply?.projects ?? [], [reply]);
  const proposalRows = useMemo(() => reply?.proposal_items ?? [], [reply]);
  const todos = useMemo(
    () => (rows ?? []).filter((r) => !r.start_time).slice(0, GROUP_LIMIT),
    [rows],
  );
  const events = useMemo(
    () => (rows ?? []).filter((r) => r.start_time).slice(0, GROUP_LIMIT),
    [rows],
  );
  // 键盘导航的扁平序（与渲染组序一致；行对象带类型标记分发行为）。120 分档：
  // 末尾恒挂「快速创建」行（关键词非空且非纯时间词时）——无结果回车即建
  const strippedKw = reply?.parsed_range
    ? q.trim().replace(reply.parsed_range.label, "").trim()
    : q.trim();
  const canCreate = reply !== null && strippedKw.length > 0;
  const flat = useMemo(
    () =>
      [
        ...projectRows.map((p) => ({ kind: "project" as const, p })),
        ...todos.map((t) => ({ kind: "task" as const, t })),
        ...events.map((t) => ({ kind: "task" as const, t })),
        ...proposalRows.map((i) => ({ kind: "proposal" as const, i })),
        ...(canCreate ? [{ kind: "create" as const }] : []),
      ],
    [projectRows, todos, events, proposalRows, canCreate],
  );

  useEffect(() => setActiveIdx(0), [q]);

  // Esc 关闭：window capture（与 FormModal 同层级；输入框聚焦时事件同样先到
  // 这里），stopPropagation 防连锁关底下的浮层
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  // 行为：未排期未完成 → App 定位（收件箱/项目页）；已排期（含已完成）→ 画布
  // 跳该日；已完成未排期 → 不可定位（列表已不显示），灰显不响应
  const activate = (row: SearchTaskRow) => {
    if (!row.start_time) {
      if (row.done_at) return;
      onLocateTodo(row);
      onClose();
      return;
    }
    dispatchCanvasJump(row.start_time);
    onClose();
  };

  // 扁平行的统一行为分发（键盘 Enter 与点击共用）
  const activateRow = (item: (typeof flat)[number]) => {
    if (item.kind === "project") {
      onLocateProject(item.p.name);
      onClose();
    } else if (item.kind === "proposal") {
      onGotoDatasource();
      onClose();
    } else if (item.kind === "create") {
      onCreateTodo(strippedKw);
    } else {
      activate(item.t);
    }
  };

  const onInputKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return; // IME 组合中不劫持方向/回车
    if (e.key === "ArrowDown" && flat.length) {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, flat.length - 1));
    } else if (e.key === "ArrowUp" && flat.length) {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && flat.length) {
      e.preventDefault();
      activateRow(flat[Math.min(activeIdx, flat.length - 1)]);
    }
  };

  // 高亮项滚入可视区（block nearest 防整页跳）
  useEffect(() => {
    listRef.current
      ?.querySelector(".search-row.active")
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIdx]);

  // 行外壳（键盘 active / 灰显 / 悬停联动全局序号），内容按行类型注入
  const rowShell = (
    idx: number,
    key: string,
    dead: boolean,
    content: ReactNode,
    onPick: () => void,
  ) => (
    <button
      type="button"
      key={key}
      className={`search-row${idx === activeIdx ? " active" : ""}${dead ? " dead" : ""}`}
      onMouseEnter={() => setActiveIdx(idx)}
      onClick={() => !dead && onPick()}
    >
      {content}
    </button>
  );

  const query = q.trim();

  const taskContent = (row: SearchTaskRow): ReactNode => {
    const isTodo = !row.start_time;
    const titleHit = query && indexCi(row.title, query) >= 0;
    const notesHit = query && row.notes != null && indexCi(row.notes, query) >= 0;
    let sub: ReactNode;
    if (!titleHit && notesHit) sub = excerpt(row.notes as string, query);
    else if (isTodo)
      sub = row.done_at ? "已完成" : row.project_name ? `项目 · ${row.project_name}` : "收件箱";
    else sub = fmtWhenRelative(row.start_time as string);
    return (
      <>
        {isTodo ? (
          row.done_at ? (
            <SquareCheck size={15} strokeWidth={2} className="search-row-icon done" />
          ) : (
            <Circle size={15} strokeWidth={2} className="search-row-icon" />
          )
        ) : (
          <CalendarDays size={15} strokeWidth={2} className="search-row-icon" />
        )}
        <span className="search-row-main">
          <span className="search-row-title">{query ? highlight(row.title, query) : row.title}</span>
          <span className="search-row-sub">{sub}</span>
        </span>
        <span className={`search-row-dot ${row.priority}`} aria-hidden="true" />
        {row.recurrence_rule && <span className="search-row-pill">重复</span>}
      </>
    );
  };

  const renderTaskRow = (row: SearchTaskRow, idx: number) =>
    rowShell(
      idx,
      `${row.id}-${row.start_time ?? "t"}-${idx}`,
      !row.start_time && !!row.done_at,
      taskContent(row),
      () => activate(row),
    );

  const renderProjectRow = (p: SearchProjectRow, idx: number) =>
    rowShell(idx, `proj-${p.id}`, false, (
      <>
        <Folder size={15} strokeWidth={2} className="search-row-icon" />
        <span className="search-row-main">
          <span className="search-row-title">{query ? highlight(p.name, query) : p.name}</span>
          <span className="search-row-sub">项目</span>
        </span>
      </>
    ), () => {
      onLocateProject(p.name);
      onClose();
    });

  const renderProposalRow = (item: SearchProposalItemRow, idx: number) =>
    rowShell(idx, `prop-${item.proposal_id}-${item.idx}`, false, (
      <>
        <Database size={15} strokeWidth={2} className="search-row-icon" />
        <span className="search-row-main">
          <span className="search-row-title">
            {query ? highlight(item.title, query) : item.title}
          </span>
          <span className="search-row-sub">
            数据源建议 · {item.kind === "inbox" ? "待办" : "日程"}条目
          </span>
        </span>
      </>
    ), () => {
      onGotoDatasource();
      onClose();
    });

  // 快速创建行（120 分档，恒在结果末尾）：无结果时回车即建（flat 非空不走空态）
  const renderCreateRow = (idx: number) =>
    rowShell(idx, "create-row", false, (
      <>
        <Plus size={15} strokeWidth={2} className="search-row-icon" />
        <span className="search-row-main">
          <span className="search-row-title">创建待办“{strippedKw}”</span>
          <span className="search-row-sub">直接添加到收件箱</span>
        </span>
      </>
    ), () => onCreateTodo(strippedKw));

  return (
    <div className="data-modal-overlay" onClick={onClose}>
      <div
        className="data-modal search-modal"
        role="dialog"
        aria-label="搜索"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="search-input-row">
          <Search size={15} strokeWidth={2} className="search-input-icon" />
          <input
            className="search-input"
            value={q}
            autoFocus
            placeholder="搜索日程与待办…"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onInputKey}
          />
          {searching && <span className="search-spinning" />}
          {!searching && q && (
            <button
              type="button"
              className="search-clear"
              aria-label="清空"
              onClick={() => setQ("")}
            >
              ×
            </button>
          )}
        </div>
        <div className="search-results" ref={listRef}>
          {reply === null ? (
            <div className="search-empty">输入关键词，搜索日程、待办、项目与建议</div>
          ) : flat.length === 0 ? (
            <div className="search-empty">没有找到与“{q.trim()}”相关的内容</div>
          ) : (
            <>
              {(reply.tasks.length > 0 || includeDone) && (
                <label className="search-done-toggle">
                  <input
                    type="checkbox"
                    checked={includeDone}
                    onChange={(e) => setIncludeDone(e.target.checked)}
                  />
                  包含已完成
                </label>
              )}
              {reply.parsed_range && (
                <div className="search-range-tag">
                  已按“{reply.parsed_range.label}”过滤
                  <button
                    type="button"
                    aria-label="清除时间过滤"
                    onClick={() => setQ(strippedKw)}
                  >
                    ×
                  </button>
                </div>
              )}
              {projectRows.length > 0 && (
                <div className="search-group">
                  <span className="search-group-title">项目 · {projectRows.length}</span>
                  {projectRows.map((p, i) => renderProjectRow(p, i))}
                </div>
              )}
              {todos.length > 0 && (
                <div className="search-group">
                  <span className="search-group-title">待办 · {todos.length}</span>
                  {todos.map((r, i) => renderTaskRow(r, projectRows.length + i))}
                </div>
              )}
              {events.length > 0 && (
                <div className="search-group">
                  <span className="search-group-title">日程 · {events.length}</span>
                  {events.map((r, i) =>
                    renderTaskRow(r, projectRows.length + todos.length + i),
                  )}
                </div>
              )}
              {proposalRows.length > 0 && (
                <div className="search-group">
                  <span className="search-group-title">建议 · {proposalRows.length}</span>
                  {proposalRows.map((it, i) =>
                    renderProposalRow(it, projectRows.length + todos.length + events.length + i),
                  )}
                </div>
              )}
              {canCreate && renderCreateRow(flat.length - 1)}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
