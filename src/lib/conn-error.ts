// 连接类错误文案归类（数据接入 / 聊天通讯两设置页共用）。
//
// 背景（2026-11-05 网关 DNS 劫持排障）：探测/测试失败时用户分不清「网络不通」与
// 「凭据错误」，误往凭证方向排查。后端各探测端点已产中文分类文案（Composio
// _translate_upstream / MCP _describe_error / chat_channels probe 的「无法连接…」
// 前缀），本函数把这些文案归为三档，供卡片徽标 / 测试钮闪变显示短标签；全文仍进
// 悬停 title 不丢信息。前缀归类依赖后端文案措辞（协议约定），新增端点文案需带
// 「无法连接 / 网络 / 超时」或「凭据 / 鉴权」关键词才能被正确归类。
export type ConnErrKind = "network" | "auth" | "unknown";

/** 把后端错误文案归类。network 判定优先于 auth（「无法连接」开头的是网络文案）。 */
export function classifyConnError(text?: string | null): ConnErrKind {
  if (!text) return "unknown";
  if (/无法连接|网络|超时|连接失败|拦截|ConnectError|Timeout|proxy|代理/i.test(text)) {
    return "network";
  }
  if (/凭据|鉴权|Key 无效|API Key|授权|登录已过期|密码|认证/.test(text)) {
    return "auth";
  }
  return "unknown";
}

/** 归类 → 徽标短文案 */
export const CONN_ERR_BADGE: Record<ConnErrKind, string> = {
  network: "网络不通",
  auth: "凭据无效",
  unknown: "失败",
};

/** 便捷形态：unknown（无可归类的文案）回落调用方原徽标文案——各卡语义不同
 *  （MCP「离线」/ CLI 与 Composio「失败」/ 聊天通讯「凭据无效」）。 */
export function connErrBadge(text: string | null | undefined, fallback: string): string {
  const kind = classifyConnError(text);
  return kind === "unknown" ? fallback : CONN_ERR_BADGE[kind];
}
