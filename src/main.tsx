import "@fontsource-variable/inter";
import "./styles/tokens.css";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { initApiBase } from "./lib/api";
import { disablePageContextMenu } from "./lib/contextMenu";
import { loadPanelWidths } from "./lib/tauri";

// render 前初始化：API 基址（打包版读 Tauri 配置端口）+ 栏宽记忆（layout.json）
// ——App 挂载时两者已就位，栏宽首帧即记忆值无跳变；右键菜单禁用（WebView2
// 默认菜单含「刷新/检查」开发项，输入框放行保留复制粘贴）
void Promise.all([initApiBase(), loadPanelWidths()]).then(() => {
  disablePageContextMenu();
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
});
