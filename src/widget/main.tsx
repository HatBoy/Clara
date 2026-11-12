// 桌面小部件独立窗口入口（widget.html）：不跑完整 App，只挂 WidgetWindow——
// 无边框透明置顶窗口的轻量载体页。样式引入照 reminder/main.tsx 模式：
// tokens.css 供全局变量；三个组件 css 被 import 即得时间轴卡片/负载指示器/
// 详情浮层/QuickAdd 外壳/待办卡/删除二次确认气泡全套同名类（全部为类选择器，
// 无 body/html 规则，不命中 widget DOM 的部分无副作用）；widget.css 必须最后
// import——透明压制与覆盖规则同特异性后载胜出
import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/inter";
import "../styles/tokens.css";
import "../components/ScheduleCanvas.css";
import "../components/InboxPanel.css";
import "../components/ConfirmPopover.css";
import "./widget.css";
import WidgetWindow from "./WidgetWindow";
import { initApiBase } from "../lib/api";
import { disablePageContextMenu } from "../lib/contextMenu";

// 先初始化 API 基址（打包版读 Tauri 配置端口）再 render——窗口挂载拉数据时基址已就位
void initApiBase().then(() => {
  disablePageContextMenu();
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <WidgetWindow />
    </React.StrictMode>,
  );
});
