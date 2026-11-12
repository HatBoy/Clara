import { useRef, useState } from "react";
import type { CSSProperties } from "react";
import { BarChart3, Calendar, Database, Inbox, Plus, Search, Settings, Star, Trash2 } from "lucide-react";
import { PROJECT_NAME_MAX_LEN, type Project } from "../data/tasks";
import { isTodoDrag, parseTodoDrag } from "../lib/dnd";
import ConfirmPopover from "./ConfirmPopover";
import "./Sidebar.css";

const NAV_ITEMS = [
  { id: "schedule", label: "日程", icon: Calendar },
  { id: "inbox", label: "收件箱", icon: Inbox },
  { id: "datasource", label: "数据源", icon: Database },
  { id: "reports", label: "统计报告", icon: BarChart3 },
];

function Sidebar({
  style,
  activeId,
  onNavigate,
  inboxCount,
  // 数据源页待确认采集建议卡数（from_ingest 未决行，7 天审阅窗内）；0 时隐藏
  datasourceCount,
  projects,
  projectCounts,
  activeProject,
  onSelectProject,
  onTodoToProject,
  onAddProject,
  onRenameProject,
  onDeleteProject,
  onToggleProjectStar,
  onOpenSettings,
  onOpenSearch,
}: {
  style?: CSSProperties;
  activeId: string;
  onNavigate: (id: string) => void;
  // 默认收件箱待办数（未排期且未归属项目），徽标随之增减；0 时隐藏
  inboxCount: number;
  datasourceCount: number;
  // 项目列表（App 受控，任务 project 字段以 name 引用）
  projects: Project[];
  // 各项目任务计数（name → { 待排期数, 项目总数 }），侧栏尾格显示「x/y」
  projectCounts: Map<string, { unscheduled: number; total: number }>;
  // 项目页当前项目名（activeId === "project" 时高亮对应项目项）
  activeProject: string | null;
  onSelectProject: (name: string) => void;
  // 待办拖入项目：写入/转移该任务的项目归属
  onTodoToProject: (projectName: string, taskId: string) => void;
  onAddProject: (name: string) => Promise<unknown>;
  // 重命名项目（name 是任务归属与项目页定位的唯一标识，三处联动在 App）
  onRenameProject: (oldName: string, newName: string) => void;
  // 删除项目（该项目下所有任务归属置空；正打开该项目页则切回收件箱）
  onDeleteProject: (name: string) => void;
  // 标记/取消重点项目（侧栏排序靠前的开关）
  onToggleProjectStar: (name: string) => void;
  // 打开"设置"悬浮设置页（App 的 settingsOpen state）
  onOpenSettings: () => void;
  // 打开搜索弹层（App 的 searchOpen state，2026-11-06 搜索批）
  onOpenSearch: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  // 拖拽悬停高亮的项目名（drop/dragleave 清除）
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  // 双击名称进入重命名：renaming 为目标项目名，renameDraft 为草稿
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  // Enter/Esc 主动退出重命名后，吞掉 input 卸载可能触发的 blur，防二次提交
  const skipRenameBlur = useRef(false);

  // 回车创建：非空则交 App 追加项目（随机取色与同名守卫在 App），空则仅退出输入态。
  // 创建失败保留输入不退出（无 toast 基础设施，console 打日志），成功才清空收起
  const submitProject = async () => {
    const name = draft.trim();
    if (!name) {
      setDraft("");
      setAdding(false);
      return;
    }
    try {
      await onAddProject(name);
      setDraft("");
      setAdding(false);
    } catch (e) {
      console.error("新建项目失败", e);
    }
  };

  // 重命名收尾：commit 时 trim 非空、有变更且不与其他项目重名才写回，否则保留原名
  const finishRename = (project: Project, commit: boolean) => {
    if (commit) {
      const name = renameDraft.trim();
      if (name && name !== project.name && !projects.some((p) => p.name === name))
        onRenameProject(project.name, name);
    }
    skipRenameBlur.current = true;
    setRenaming(null);
  };

  // 渲染序：重点项目（starred）稳定排前，组内保持原数组序（默认排序）
  const sortedProjects = [...projects].sort(
    (a, b) => Number(!!b.starred) - Number(!!a.starred),
  );
  return (
    <aside className="sidebar" style={style}>
      {/* 品牌盒已撤（2026-11-05 用户决策：与窗口标题栏图标+Clara 重复）——品牌
          展示位移至「设置-关于我们」页；顶部 padding 对齐中栏日期区起点 */}
      <div className="sidebar-top">
        {/* 搜索入口（2026-11-06 搜索批）：搜索框样式按钮，与 Ctrl+K 等价唤起
            居中弹层；置于导航上方——与中栏日期区的顶部平齐由它接替承担 */}
        <button type="button" className="sidebar-search" onClick={onOpenSearch}>
          <Search size={14} strokeWidth={2} />
          <span className="sidebar-search-label">搜索</span>
          <span className="sidebar-search-kbd">Ctrl K</span>
        </button>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={`sidebar-nav-item${id === activeId ? " active" : ""}`}
              onClick={() => onNavigate(id)}
            >
              <Icon size={15} strokeWidth={2} className="sidebar-nav-icon" />
              <span className="sidebar-nav-label">{label}</span>
              {id === "inbox" && inboxCount > 0 ? (
                <span className="sidebar-badge">{inboxCount}</span>
              ) : null}
              {id === "datasource" && datasourceCount > 0 ? (
                <span className="sidebar-badge">{datasourceCount}</span>
              ) : null}
            </button>
          ))}
        </nav>

        <div className="sidebar-projects">
          <div className="sidebar-projects-header">
            <span className="sidebar-projects-title">项目</span>
            {adding ? null : (
              <button
                type="button"
                className="sidebar-projects-add"
                title="新建项目"
                onClick={() => setAdding(true)}
              >
                <Plus size={14} strokeWidth={2} />
              </button>
            )}
          </div>
          {adding ? (
            <form onSubmit={(e) => { e.preventDefault(); void submitProject(); }}>
              <input
                className="sidebar-project-input"
                value={draft}
                maxLength={PROJECT_NAME_MAX_LEN}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setDraft("");
                    setAdding(false);
                  }
                }}
                onBlur={() => {
                  setDraft("");
                  setAdding(false);
                }}
                placeholder="项目名称，回车创建"
                autoFocus
              />
            </form>
          ) : null}
          {sortedProjects.map((project) => (
            <div
              key={project.name}
              className={`sidebar-project-item${
                activeId === "project" && activeProject === project.name
                  ? " active"
                  : ""
              }${dropTarget === project.name ? " drop-target" : ""}${
                project.starred ? " starred" : ""
              }`}
              // 待办拖入项目（HTML5 DnD）：dragover 判别 + 高亮，drop 解析载荷写回归属
              onDragOver={(e) => {
                if (!isTodoDrag(e)) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                setDropTarget((n) => (n === project.name ? n : project.name));
              }}
              onDragLeave={() =>
                setDropTarget((n) => (n === project.name ? null : n))
              }
              onDrop={(e) => {
                const todo = parseTodoDrag(e);
                if (!todo) return;
                e.preventDefault();
                onTodoToProject(project.name, todo.id);
                setDropTarget(null);
              }}
            >
              {renaming === project.name ? (
                <input
                  className="sidebar-project-rename"
                  value={renameDraft}
                  maxLength={PROJECT_NAME_MAX_LEN}
                  autoFocus
                  onChange={(e) => setRenameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") finishRename(project, true);
                    else if (e.key === "Escape") finishRename(project, false);
                  }}
                  onBlur={() => {
                    // Enter/Esc 已主动退出的卸载 blur 被吞掉，仅失焦路径走到这里（保存）
                    if (skipRenameBlur.current) {
                      skipRenameBlur.current = false;
                      return;
                    }
                    finishRename(project, true);
                  }}
                />
              ) : (
                <button
                  type="button"
                  className="sidebar-project-main"
                  onClick={() => onSelectProject(project.name)}
                >
                  <span
                    className="sidebar-project-dot"
                    style={{ background: project.color }}
                  />
                  <span
                    className="sidebar-project-name"
                    onDoubleClick={() => {
                      // 重置守卫，防上次残留误吞本次编辑的失焦保存
                      skipRenameBlur.current = false;
                      setRenameDraft(project.name);
                      setRenaming(project.name);
                    }}
                  >
                    {project.name}
                  </span>
                </button>
              )}
              <span className="sidebar-project-actions">
                <button
                  type="button"
                  className="sidebar-project-icon star"
                  title={project.starred ? "取消重点项目" : "标记为重点项目"}
                  aria-label={project.starred ? "取消重点项目" : "标记为重点项目"}
                  aria-pressed={!!project.starred}
                  onClick={() => onToggleProjectStar(project.name)}
                >
                  <Star
                    size={13}
                    strokeWidth={2}
                    fill={project.starred ? "currentColor" : "none"}
                  />
                </button>
                {/* 尾格：删除按钮与待排期计数叠放同一位置（互斥显示——hover/focus 删除浮现、
                    计数淡出；平时计数常显，starred 时与左侧常显星标并存），布局恒两格省一列 */}
                <span className="sidebar-project-tail">
                  {/* 删除项目：二次确认气泡（右侧弹出——左栏窄，往中栏方向展开） */}
                  <ConfirmPopover
                    message={`删除项目「${project.name}」？其下任务将回到收件箱`}
                    onConfirm={() => onDeleteProject(project.name)}
                    placement="right"
                    buttonClass="sidebar-project-icon del"
                    buttonTitle="删除项目"
                    icon={<Trash2 size={13} strokeWidth={2} />}
                  />
                  {/* 计数徽标（2026-11-10 改「待排期/总数」）：分子 = 未排期待办
                      （行动信号），分母 = 项目全部任务含已排期日程（全貌）——分母
                      视觉弱化防喧宾夺主，title 悬停消歧义；空项目维持单显 0 */}
                  {(() => {
                    const c = projectCounts.get(project.name);
                    return (
                      <span
                        className="sidebar-project-count"
                        title={
                          c && c.total ? `${c.unscheduled} 项待排期 / 项目共 ${c.total} 项` : undefined
                        }
                      >
                        {c && c.total ? (
                          <>
                            {c.unscheduled}
                            <i className="sidebar-project-total">/{c.total}</i>
                          </>
                        ) : (
                          0
                        )}
                      </span>
                    );
                  })()}
                </span>
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="sidebar-spacer" />

      {/* 设置入口：打开悬浮设置页（hover 态对齐 nav-item 惯例） */}
      <button type="button" className="sidebar-footer" onClick={onOpenSettings}>
        <Settings size={15} strokeWidth={2} className="sidebar-footer-icon" />
        <span className="sidebar-footer-label">设置</span>
      </button>
    </aside>
  );
}

export default Sidebar;
