// 提醒独立窗口（"通知形式 = 居中/右下角悬浮框"的承载页）：无边框/透明/置顶/
// 不占任务栏/不抢焦点的独立 Tauri 窗口——主窗口最小化或被其他程序遮挡时，提醒
// 依然浮在屏幕上可见。主窗口是 SSE 消费侧，收事件后经 lib/tauri.ts 的
// forwardReminderEvent **驱动本窗口显示**（emitTo 事件 + getByLabel().show()
// 并行，显隐控制权在主窗口）；本窗口只负责渲染卡片与按设置定位，并在全部提醒
// 淡出移除后自行 hide。窗口常驻复用不销毁，避免反复重建 WebView。
import { useEffect, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { currentMonitor } from "@tauri-apps/api/window";
import { LogicalPosition } from "@tauri-apps/api/dpi";
import ReminderToast, {
  beginToastExit,
  scheduleToastExit,
  toToastItem,
  toastActionPatch,
  type ReminderAction,
  type ReminderToastItem,
} from "../components/ReminderToast";
import {
  fetchSettings,
  toFrontendSettings,
  updateTask,
  type ReminderEvent,
} from "../lib/api";
import { applyTheme } from "../lib/settings";
import { notifyPeer } from "../lib/sync";

// 窗口尺寸（逻辑像素，与 tauri.conf.json 的 reminder 窗口声明同步）
const WINDOW_W = 420;
const WINDOW_H = 300;
// 右下角模式与屏幕边缘的留白（逻辑像素）
const CORNER_MARGIN = 16;

// 定位（conf 的 center:true 仅作 JS 失败兜底）：corner = 贴**工作区**右下角
// （各留 16px，卡片贴窗口底——多卡向上堆叠；工作区已扣除任务栏，整屏贴底会被
// 任务栏遮住，任务栏在任意边/自动隐藏时也自然正确）；否则 = 水平居中、垂直约
// 屏高 12%（卡片在窗口内垂直居中，卡片中心约落屏高 1/4，"显示在上方的人视角"）。
// monitor 尺寸均为物理像素，除 scaleFactor 换算逻辑坐标
const positionWindow = async (corner: boolean) => {
  try {
    const mon = await currentMonitor();
    if (!mon) return;
    const sf = mon.scaleFactor;
    const mw = mon.size.width / sf;
    const mh = mon.size.height / sf;
    const mx = mon.position.x / sf;
    const my = mon.position.y / sf;
    const wx = mon.workArea.position.x / sf;
    const wy = mon.workArea.position.y / sf;
    const ww = mon.workArea.size.width / sf;
    const wh = mon.workArea.size.height / sf;
    const x = corner ? wx + ww - WINDOW_W - CORNER_MARGIN : mx + (mw - WINDOW_W) / 2;
    const y = corner ? wy + wh - WINDOW_H - CORNER_MARGIN : my + mh * 0.12;
    await getCurrentWebviewWindow().setPosition(
      new LogicalPosition(Math.round(x), Math.round(y)),
    );
  } catch {
    // 非 Tauri 环境直开此页时静默（该页唯一入口是 Tauri 窗口）
  }
};

// 外观对齐（主题 + 位置风格）：每次弹窗前拉一次设置应用——窗口常驻复用，用户
// 改主题或通知形式后下次弹出即生效；失败静默保持上次（默认亮色 + 居中）
const refreshAppearance = async (
  setCorner: (v: boolean) => void,
): Promise<void> => {
  try {
    const s = toFrontendSettings(await fetchSettings());
    applyTheme(s.themeMode);
    setCorner(s.notifyStyle === "corner");
    await positionWindow(s.notifyStyle === "corner");
  } catch {
    // 后端未就绪等场景静默
  }
};

function ReminderWindow() {
  const [items, setItems] = useState<ReminderToastItem[]>([]);
  // 位置风格（corner = 卡片贴窗口底/右下角布局）：随设置更新，默认居中
  const [corner, setCorner] = useState(false);
  const keyRef = useRef(0);

  // 挂载先按默认居中定位一次（不等网络），随后对齐一次设置（主题 + 位置风格）
  useEffect(() => {
    void positionWindow(false);
    void refreshAppearance(setCorner);
  }, []);

  // 事件到达：入列渲染（窗口显示由主窗口 show 驱动，与本监听并行）；到时淡出
  // 后移除，节拍口径在 ReminderToast.tsx 共享
  useEffect(() => {
    const promise = listen<ReminderEvent>("reminder-event", (e) => {
      void refreshAppearance(setCorner);
      const key = ++keyRef.current;
      setItems((prev) => [...prev, toToastItem(e.payload, key)]);
      scheduleToastExit(key, setItems);
    });
    // 浏览器直接开此页时 listen 立即 reject，静默即可（该页唯一入口是 Tauri 窗口）
    promise.catch(() => {});
    return () => {
      promise.then(
        (unlisten) => unlisten(),
        () => {},
      );
    };
  }, []);

  // 全部提醒淡出移除后：隐藏窗口（保留 WebView，下次事件直接 show 复用）
  useEffect(() => {
    if (items.length === 0) void getCurrentWebviewWindow().hide().catch(() => {});
  }, [items]);

  // 完成/推迟按钮（2026-10-22 批 2）：请求构造在 toastActionPatch 共享。写库
  // 成功后淡出本卡并通知主窗重拉——提醒窗无数据副本，reload 即让画布/收件箱
  // 可见（widget 的对应陈旧与 main→widget reload 对等，登记 tech-debt）。推迟
  // 后旧提醒行被后端 claim 改期校验自然作废，新时刻由扫描按新口径重新入列。
  // 失败仅日志留卡（用户可手动关闭重试）
  const handleAction = (it: ReminderToastItem, action: ReminderAction) => {
    if (!it.taskId) return;
    const { body, opts } = toastActionPatch(it, action);
    updateTask(it.taskId, body, opts)
      .then(() => {
        notifyPeer("reminder", { kind: "reload" });
        beginToastExit(it.key, setItems);
      })
      .catch((e) => console.error("提醒操作写回失败", action, e));
  };

  return (
    <ReminderToast
      items={items}
      corner={corner}
      onClose={(key) => beginToastExit(key, setItems)}
      onAction={handleAction}
    />
  );
}

export default ReminderWindow;
