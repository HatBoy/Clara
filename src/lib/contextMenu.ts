// 禁用页面级右键菜单（2026-11-12）：WebView2 默认菜单含「刷新 / 检查」等开发项，
// 桌面应用语境不应出现。输入框（input / textarea / 可编辑区）放行——保留系统的
// 复制/粘贴文本菜单。三个窗口入口（main / widget / reminder）统一挂载。
export function disablePageContextMenu() {
  document.addEventListener("contextmenu", (e) => {
    const el = e.target as HTMLElement | null;
    if (el?.closest("input, textarea, [contenteditable='true']")) return;
    e.preventDefault();
  });
}
