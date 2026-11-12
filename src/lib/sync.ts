// 双窗口同步协议（main ↔ widget，Tauri emitTo 定向事件）：主界面与桌面小部件
// 各自持有一份数据副本，用户写操作完成后把结果广播给对端，接收方纯读应用、
// 绝不回发 → 天然无回环。零后端改动（事件不过边车）；非 Tauri（纯浏览器 dev）
// 全 no-op。载荷里的 tasks/projects 一律前端形状（project 已解析 name），收方
// 零转换直接按 id upsert。2026-10-22 批 2 增第三来源 "reminder"（提醒窗完成/
// 推迟按钮写库后通知 main 重拉——单向，不参与 main↔widget 双向协议）
import { emitTo, listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { Project, Task } from "../data/tasks";
import type { Settings } from "./settings";
import { isTauri } from "./tauri";

// 判别联合：upsert = 任务/项目增量合并；delete = 任务整删；settings 仅 main →
// widget（主题/时间轴区间/负载基准跟随）；reload = 收方重拉自己的小窗口
//（Clone & Override 删除/项目重命名删除等派生变化，不值得对端复刻推导）
export type SyncPayload =
  | { kind: "upsert"; tasks?: Task[]; projects?: Project[] }
  | { kind: "delete"; taskIds: string[] }
  | { kind: "settings"; settings: Settings }
  | { kind: "reload" };

const SYNC_EVENT = "peer-sync";

/** 把写操作结果广播给对端窗口（fire-and-forget，失败仅日志；非 Tauri no-op）。
 * reminder 来源只会发给 main（提醒窗无数据副本，无需双向） */
export function notifyPeer(
  from: "main" | "widget" | "reminder",
  payload: SyncPayload,
): void {
  if (!isTauri()) return;
  const to = from === "main" ? "widget" : "main";
  emitTo(to, SYNC_EVENT, payload).catch((e) =>
    console.error("双窗口同步事件发送失败", e),
  );
}

/** 订阅对端同步事件（返回取消订阅函数；非 Tauri 返回空函数，effect 清理安全） */
export function onPeerSync(
  handler: (payload: SyncPayload) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) return Promise.resolve(() => {});
  return listen<SyncPayload>(SYNC_EVENT, (e) => handler(e.payload));
}
