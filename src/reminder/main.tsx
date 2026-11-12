// 提醒独立窗口入口（reminder.html）：不跑完整 App，只挂 ReminderWindow——
// 无边框透明置顶窗口的轻量载体页
import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/inter";
import "../styles/tokens.css";
import "./reminder.css";
import ReminderWindow from "./ReminderWindow";
import { initApiBase } from "../lib/api";
import { disablePageContextMenu } from "../lib/contextMenu";

// 先初始化 API 基址（打包版读 Tauri 配置端口）再 render——窗口挂载拉设置时基址已就位
void initApiBase().then(() => {
  disablePageContextMenu();
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <ReminderWindow />
    </React.StrictMode>,
  );
});
