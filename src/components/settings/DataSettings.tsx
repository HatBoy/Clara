// "数据接入"页面：已连接数据源 + 精选效率工具推荐 + Composio 云端应用搜索 + MCP
// 服务接入（手动添加 / 精选模板 / 本地文件夹内置服务端）+ CLI 工具接入（精选
// 适配器三态状态机 / 窄口自定义只读命令）。前端只做展示与流程编排（保存 Key /
// 打开系统浏览器完成 OAuth / 轮询连接状态 / 刷新列表 / 表单编排），全部连接与
// 认证逻辑在后端 FastAPI（services/integrations.py 经 Composio SDK 调云端、
// services/mcp_servers.py 经 langchain-mcp-adapters 连 MCP 服务、
// services/local_folder.py 内置 FastMCP 服务端、services/cli_tools.py 安全执行器
// spawn CLI 子进程）——密钥不进浏览器网络面板。
import { Eye, EyeOff, Flame, Folder, Plug, Search, Terminal, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ConfirmPopover from "../ConfirmPopover";
import {
  connectCliTool,
  connectComposioApp,
  createCustomCli,
  deleteCliTool,
  deleteMcpServer,
  disconnectComposio,
  fetchCliAuthStatus,
  fetchCliTools,
  fetchComposioConnections,
  fetchComposioKey,
  fetchConnectStatus,
  fetchIngestSources,
  fetchLocalFolder,
  fetchMcpServers,
  importIcsFile,
  probeCliTool,
  saveComposioKey,
  saveLocalFolder,
  searchComposioApps,
  setComposioEnabled,
  startCliAuth,
  testComposioConnection,
  testCustomCli,
  testCustomCliDraft,
  testLocalFolder,
  testMcpServerById,
  toNaiveLocalISO,
  updateCliTool,
  updateIngestSourceTrusted,
  updateMcpServer,
  verifyComposioKey,
  type ApiCliProbe,
  type ApiCliTool,
  type ApiComposioApp,
  type ApiComposioConnection,
  type ApiComposioKey,
  type ApiComposioTest,
  type ApiLocalFolder,
  type ApiMcpServer,
} from "../../lib/api";
import { isTauri, openExternal, pickDirectory, pickIcsFile } from "../../lib/tauri";
import { connErrBadge } from "../../lib/conn-error";
import McpServerForm from "./McpServerForm";
import McpTemplateForm from "./McpTemplateForm";
import { matchTemplate, templateByName } from "./mcpTemplates";
import "./DataSettings.css";

// MCP 官方服务注册表（"探索更多"外链目标）
const MCP_REGISTRY_URL = "https://registry.modelcontextprotocol.io";

// Composio 控制台（"如何获取？"外链目标）
const COMPOSIO_KEY_URL = "https://dashboard.composio.dev";

// 轮询节拍与上限：2s 一次、至多 60 次（授权在系统浏览器完成，主窗口只等结果）
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 30; // 60s 授权超时：用户不操作浏览器就终止，重点「连接应用」重发起

// 本地文件夹卡在 testingIds/testErrors 中的键（与 MCP 行 UUID id 天然不撞）
const LOCAL_FOLDER_KEY = "local-folder";

// CLI 适配器 ↔ 同名 MCP 模板服务名（双通道数据重复提示判定；无 MCP 模板的工具
// 不登记——Todoist 只有 Composio 通道，飞书 MCP 已于 2026-10-16 全链路移除）
const CLI_DUAL_MCP_NAMES: Record<string, string> = { dingtalk: "钉钉" };

// 品牌 logo 单色 path（Notion/TickTick/Obsidian/Outlook/Todoist/GitLab/GitHub/
// Jira/Linear/Trello 源 simple-icons 24×24，飞书源字节官方 IconPark 48×48；
// 钉钉源 MingCute（simple-icons 未收录，fill 风格同 simple-icons）；Microsoft
// To Do 源 selfh.st icons（simple-icons 已下架，渐变双 path 合并为单 path 512 盒）。
// 颜色不写死：fill 走 currentColor，由外层 .data-icon.<id> 的 color 控品牌色——
// Notion/GitHub 暗色反转由此实现。
export type BrandPath = { d: string; box?: string; rule?: "evenodd" };

export const BRAND_PATHS: Record<string, BrandPath> = {
  notion: {
    d: "M4.459 4.208c.746.606 1.026.56 2.428.466l13.215-.793c.28 0 .047-.28-.046-.326L17.86 1.968c-.42-.326-.981-.7-2.055-.607L3.01 2.295c-.466.046-.56.28-.374.466zm.793 3.08v13.904c0 .747.373 1.027 1.214.98l14.523-.84c.841-.046.935-.56.935-1.167V6.354c0-.606-.233-.933-.748-.887l-15.177.887c-.56.047-.747.327-.747.933zm14.337.745c.093.42 0 .84-.42.888l-.7.14v10.264c-.608.327-1.168.514-1.635.514-.748 0-.935-.234-1.495-.933l-4.577-7.186v6.952L12.21 19s0 .84-1.168.84l-3.222.186c-.093-.186 0-.653.327-.746l.84-.233V9.854L7.822 9.76c-.094-.42.14-1.026.793-1.073l3.456-.233 4.764 7.279v-6.44l-1.215-.139c-.093-.514.28-.887.747-.933zM1.936 1.035l13.31-.98c1.634-.14 2.055-.047 3.082.7l4.249 2.986c.7.513.934.653.934 1.213v16.378c0 1.026-.373 1.634-1.68 1.726l-15.458.934c-.98.047-1.448-.093-1.962-.747l-3.129-4.06c-.56-.747-.793-1.306-.793-1.96V2.667c0-.839.374-1.54 1.447-1.632z",
  },
  microsoftTodo: {
    d: "m6.5 240.3l74.8-74.8c8.6-8.6 22.7-8.6 31.3 0l180.8 180.8c8.6 8.6 8.6 22.7 0 31.3l-74.8 74.8c-8.6 8.6-22.7 8.6-31.3 0L6.5 271.7c-8.7-8.7-8.7-22.7 0-31.4M430.8 59.5l74.8 74.8c8.6 8.6 8.6 22.7 0 31.3l-287 286.9c-8.6 8.6-22.7 8.6-31.3 0l-74.8-74.8c-8.6-8.6-8.6-22.7 0-31.3L399.4 59.5c8.7-8.6 22.7-8.6 31.4 0",
    box: "0 0 512 512",
  },
  todoist: {
    d: "M21 0H3C1.35 0 0 1.35 0 3v3.858s3.854 2.24 4.098 2.38c.31.18.694.177 1.004 0 .26-.147 8.02-4.608 8.136-4.675.279-.161.58-.107.748-.01.164.097.606.348.84.48.232.134.221.502.013.622l-9.712 5.59c-.346.2-.69.204-1.048.002C3.478 10.907.998 9.463 0 8.882v2.02l4.098 2.38c.31.18.694.177 1.004 0 .26-.147 8.02-4.609 8.136-4.676.279-.16.58-.106.748-.008.164.096.606.347.84.48.232.133.221.5.013.62-.208.121-9.288 5.346-9.712 5.59-.346.2-.69.205-1.048.002C3.478 14.951.998 13.506 0 12.926v2.02l4.098 2.38c.31.18.694.177 1.004 0 .26-.147 8.02-4.609 8.136-4.676.279-.16.58-.106.748-.009.164.097.606.348.84.48.232.133.221.5.013.622l-9.712 5.59c-.346.199-.69.204-1.048.001C3.478 18.994.998 17.55 0 16.97V21c0 1.65 1.35 3 3 3h18c1.65 0 3-1.35 3-3V3c0-1.65-1.35-3-3-3z",
  },
  dingtalk: {
    d: "M6.802 2.02a1 1 0 0 1 .849.22l9.751 8.359a2 2 0 0 1 .235 2.799l-1.06 1.272l.87.436a1 1 0 0 1 .134 1.708l-7 5a1 1 0 0 1-1.539-1.101l1.21-4.034c-2.363-.9-3.747-3.055-4.233-5.483A1 1 0 0 1 7.01 10c-.474-.703-.86-1.42-1.134-2.149c-.649-1.73-.658-3.523.23-5.298a1 1 0 0 1 .696-.533",
    rule: "evenodd",
  },
  gitlab: {
    d: "m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 0-.29.4399l-2.2055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 0-.3362.4049L.4332 9.5015l-.0325.0862a6.0657 6.0657 0 0 0 2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 1.4995 1.1321a1.0085 1.0085 0 0 0 1.2197 0l1.4995-1.1321 2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 2.0094-7.003z",
  },
  github: {
    d: "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12",
  },
  jira: {
    d: "M11.571 11.513H0a5.218 5.218 0 0 0 5.232 5.215h2.13v2.057A5.215 5.215 0 0 0 12.575 24V12.518a1.005 1.005 0 0 0-1.005-1.005zm5.723-5.756H5.736a5.215 5.215 0 0 0 5.215 5.214h2.129v2.058a5.218 5.218 0 0 0 5.215 5.214V6.758a1.001 1.001 0 0 0-1.001-1.001zM23.013 0H11.455a5.215 5.215 0 0 0 5.215 5.215h2.129v2.057A5.215 5.215 0 0 0 24 12.483V1.005A1.001 1.001 0 0 0 23.013 0Z",
  },
  linear: {
    d: "M2.886 4.18A11.982 11.982 0 0 1 11.99 0C18.624 0 24 5.376 24 12.009c0 3.64-1.62 6.903-4.18 9.105L2.887 4.18ZM1.817 5.626l16.556 16.556c-.524.33-1.075.62-1.65.866L.951 7.277c.247-.575.537-1.126.866-1.65ZM.322 9.163l14.515 14.515c-.71.172-1.443.282-2.195.322L0 11.358a12 12 0 0 1 .322-2.195Zm-.17 4.862 9.823 9.824a12.02 12.02 0 0 1-9.824-9.824Z",
  },
  trello: {
    d: "M21.147 0H2.853A2.86 2.86 0 000 2.853v18.294A2.86 2.86 0 002.853 24h18.294A2.86 2.86 0 0024 21.147V2.853A2.86 2.86 0 0021.147 0zM10.34 17.287a.953.953 0 01-.953.953h-4a.954.954 0 01-.954-.953V5.38a.953.953 0 01.954-.953h4a.954.954 0 01.953.953zm9.233-5.467a.944.944 0 01-.953.947h-4a.947.947 0 01-.953-.947V5.38a.953.953 0 01.953-.953h4a.954.954 0 01.953.953z",
  },
  feishu: {
    d: "M41.072 5.994L3.31 16.52l9.075 9.294l8.414.146l9.683-9.44q-.384-.787-.384-1.318c0-.794.311-1.422.796-1.868q1.244-1.145 2.994-.342zm1.03.734L31.578 44.49l-9.294-9.075L22.137 27l9.375-9.518a2.54 2.54 0 0 0 1.664.495c.902-.05 1.485-.596 1.759-.917a2.35 2.35 0 0 0 .567-1.649a2.57 2.57 0 0 0-.52-1.464z",
    box: "0 0 48 48",
    rule: "evenodd",
  },
  ticktick: {
    d: "M12 0C5.383 0 0 5.383 0 12s5.383 12 12 12 12-5.383 12-12h-2.7c0 5.128-4.172 9.3-9.3 9.3-5.128 0-9.3-4.172-9.3-9.3 0-5.128 4.172-9.3 9.3-9.3V0Zm7.4 2.583-7.505 9.371L8.388 9.08l-2.002 2.436 4.741 3.888a1.573 1.573 0 0 0 2.231-.233l8.504-10.617L19.4 2.583Z",
  },
  obsidian: {
    d: "M19.355 18.538a68.967 68.959 0 0 0 1.858-2.954.81.81 0 0 0-.062-.9c-.516-.685-1.504-2.075-2.042-3.362-.553-1.321-.636-3.375-.64-4.377a1.707 1.707 0 0 0-.358-1.05l-3.198-4.064a3.744 3.744 0 0 1-.076.543c-.106.503-.307 1.004-.536 1.5-.134.29-.29.6-.446.914l-.31.626c-.516 1.068-.997 2.227-1.132 3.59-.124 1.26.046 2.73.815 4.481.128.011.257.025.386.044a6.363 6.363 0 0 1 3.326 1.505c.916.79 1.744 1.922 2.415 3.5zM8.199 22.569c.073.012.146.02.22.02.78.024 2.095.092 3.16.29.87.16 2.593.64 4.01 1.055 1.083.316 2.198-.548 2.355-1.664.114-.814.33-1.735.725-2.58l-.01.005c-.67-1.87-1.522-3.078-2.416-3.849a5.295 5.295 0 0 0-2.778-1.257c-1.54-.216-2.952.19-3.84.45.532 2.218.368 4.829-1.425 7.531zM5.533 9.938c-.023.1-.056.197-.098.29L2.82 16.059a1.602 1.602 0 0 0 .313 1.772l4.116 4.24c2.103-3.101 1.796-6.02.836-8.3-.728-1.73-1.832-3.081-2.55-3.831zM9.32 14.01c.615-.183 1.606-.465 2.745-.534-.683-1.725-.848-3.233-.716-4.577.154-1.552.7-2.847 1.235-3.95.113-.235.223-.454.328-.664.149-.297.288-.577.419-.86.217-.47.379-.885.46-1.27.08-.38.08-.72-.014-1.043-.095-.325-.297-.675-.68-1.06a1.6 1.6 0 0 0-1.475.36l-4.95 4.452a1.602 1.602 0 0 0-.513.952l-.427 2.83c.672.59 2.328 2.316 3.335 4.711.09.21.175.43.253.653z",
  },
  outlook: {
    d: "M7.88 12.04q0 .45-.11.87-.1.41-.33.74-.22.33-.58.52-.37.2-.87.2t-.85-.2q-.35-.21-.57-.55-.22-.33-.33-.75-.1-.42-.1-.86t.1-.87q.1-.43.34-.76.22-.34.59-.54.36-.2.87-.2t.86.2q.35.21.57.55.22.34.31.77.1.43.1.88zM24 12v9.38q0 .46-.33.8-.33.32-.8.32H7.13q-.46 0-.8-.33-.32-.33-.32-.8V18H1q-.41 0-.7-.3-.3-.29-.3-.7V7q0-.41.3-.7Q.58 6 1 6h6.5V2.55q0-.44.3-.75.3-.3.75-.3h12.9q.44 0 .75.3.3.3.3.75V10.85l1.24.72h.01q.1.07.18.18.07.12.07.25zm-6-8.25v3h3v-3zm0 4.5v3h3v-3zm0 4.5v1.83l3.05-1.83zm-5.25-9v3h3.75v-3zm0 4.5v3h3.75v-3zm0 4.5v2.03l2.41 1.5 1.34-.8v-2.73zM9 3.75V6h2l.13.01.12.04v-2.3zM5.98 15.98q.9 0 1.6-.3.7-.32 1.19-.86.48-.55.73-1.28.25-.74.25-1.61 0-.83-.25-1.55-.24-.71-.71-1.24t-1.15-.83q-.68-.3-1.55-.3-.92 0-1.64.3-.71.3-1.2.85-.5.54-.75 1.3-.25.74-.25 1.63 0 .85.26 1.56.26.72.74 1.23.48.52 1.17.81.69.3 1.56.3zM7.5 21h12.39L12 16.08V17q0 .41-.3.7-.29.3-.7.3H7.5zm15-.13v-7.24l-5.9 3.54Z",
  },
};

// 品牌 logo 渲染：单 path 单色，viewBox 随 path 配套；尺寸交给 .data-icon svg 的 CSS
export function BrandGlyph({ path }: { path: BrandPath }) {
  return (
    <svg viewBox={path.box ?? "0 0 24 24"} aria-hidden="true">
      <path d={path.d} fill="currentColor" fillRule={path.rule} clipRule={path.rule} />
    </svg>
  );
}

// 接入通道三种：Composio（OAuth 云端应用授权，蓝）/ MCP（本地/插件直连，紫）/
// CLI（官方命令行工具直连，绿）——胶囊文案与配色成对出现，单字段派生防错配
type Channel = "composio" | "mcp" | "cli";

const CHANNELS: Record<Channel, { label: string; tone: "blue" | "purple" | "green" }> = {
  composio: { label: "Composio", tone: "blue" },
  mcp: { label: "MCP", tone: "purple" },
  cli: { label: "CLI", tone: "green" },
};

// 精选工具：纵向卡（图标 + 通道胶囊右上 / 名称 / 描述 / 底部全宽"连接应用"按钮）
type FeaturedSource = {
  id: string; // 兼作图标色类名（.data-icon.<id>）
  name: string;
  description: string;
  channel: Channel;
  appId?: string; // Composio toolkit slug（连接与已连接隐藏匹配的标识）
  cliId?: string; // CLI adapter_id（后端注册表键，接入模态与已连接隐藏的标识）
  icon: ReactNode;
};

// 排序与构成 2026-09-18 用户定：飞书/钉钉 MCP 打头，滴答清单国际/国内版相邻，
// 研发类 Composio 卡收尾（禅道 MCP 卡已按用户同日决策移除——配置复杂，用户有
// 需要自己手动添加，见页脚"手动配置 MCP Server"）。2026-10-16 用户决策：飞书
// MCP 全链路移除（精选卡/模板/后端映射/双通道提示一并删除）——lark-cli 实测
// 全面覆盖且采集可用，新接入走飞书 CLI 卡
const FEATURED_SOURCES: FeaturedSource[] = [
  {
    id: "feishu-cli",
    name: "飞书 CLI (lark)",
    description: "官方 lark-cli 直连：日历/文档/待办全量读取，凭据存系统钥匙串。",
    channel: "cli",
    cliId: "lark",
    icon: <BrandGlyph path={BRAND_PATHS.feishu} />,
  },
  {
    id: "dingtalk",
    name: "钉钉 (DingTalk)",
    description: "接入钉钉日程与待办，企业协作事项自动纳入排期视野。",
    channel: "mcp",
    icon: <BrandGlyph path={BRAND_PATHS.dingtalk} />,
  },
  {
    id: "dingtalk-cli",
    name: "钉钉 CLI (dws)",
    description: "官方 dws CLI 直连：日程/待办/DING 能力面更全，无需自建 MCP 服务。",
    channel: "cli",
    cliId: "dingtalk",
    icon: <BrandGlyph path={BRAND_PATHS.dingtalk} />,
  },
  {
    id: "todoist-cli",
    name: "Todoist CLI (td)",
    description: "官方 td CLI 直连：项目与收件箱任务本地读取，不依赖云端中转。",
    channel: "cli",
    cliId: "todoist",
    icon: <BrandGlyph path={BRAND_PATHS.todoist} />,
  },
  {
    id: "ticktick",
    name: "滴答清单 (TickTick)",
    description: "自动将清单中标记了日期的任务拉取到 Clara 的收件箱中统一智能排期。",
    channel: "composio",
    appId: "TICKTICK",
    icon: <BrandGlyph path={BRAND_PATHS.ticktick} />,
  },
  {
    id: "ticktick-cn",
    name: "滴答清单 (国内版)",
    description: "经本地 MCP 插件直连国内版滴答清单，带日期任务自动进入待排期清单。",
    channel: "mcp",
    icon: <BrandGlyph path={BRAND_PATHS.ticktick} />,
  },
  {
    id: "microsoft-todo",
    name: "Microsoft To Do",
    description: "同步微软待办的任务与截止日期，纳入收件箱统一智能排期。",
    channel: "composio",
    appId: "MICROSOFT_TODO",
    icon: <BrandGlyph path={BRAND_PATHS.microsoftTodo} />,
  },
  {
    id: "todoist",
    name: "Todoist",
    description: "接入 Todoist 项目与收件箱，带日期任务自动进入待排期清单。",
    channel: "composio",
    appId: "TODOIST",
    icon: <BrandGlyph path={BRAND_PATHS.todoist} />,
  },
  {
    id: "notion",
    name: "Notion",
    description: "读取 Notion 数据库与文档中的日程和待办，作为 AI 排期的上下文。",
    channel: "composio",
    appId: "NOTION",
    icon: <BrandGlyph path={BRAND_PATHS.notion} />,
  },
  {
    id: "obsidian",
    name: "Obsidian",
    description: "一键安装官方 MCP 插件，安全地直连您的本地 Markdown 知识库。",
    channel: "mcp",
    icon: <BrandGlyph path={BRAND_PATHS.obsidian} />,
  },
  {
    id: "outlook",
    name: "Microsoft Outlook",
    description: "接入企业邮箱与日历，支持 Exchange 协议，自动提取邮件中的重要截止日期。",
    channel: "composio",
    appId: "OUTLOOK",
    icon: <BrandGlyph path={BRAND_PATHS.outlook} />,
  },
  {
    id: "gitlab",
    name: "GitLab",
    description: "跟进 GitLab 里程碑与 Issue 的截止日期，研发节点自动排入日程。",
    channel: "composio",
    appId: "GITLAB",
    icon: <BrandGlyph path={BRAND_PATHS.gitlab} />,
  },
  {
    id: "github",
    name: "GitHub",
    description: "监听 GitHub Issue 与 PR 评审请求，重要节点自动提醒排期。",
    channel: "composio",
    appId: "GITHUB",
    icon: <BrandGlyph path={BRAND_PATHS.github} />,
  },
  {
    id: "jira",
    name: "Jira",
    description: "同步 Jira 任务的迭代截止日，让研发排期与冲刺节奏对齐。",
    channel: "composio",
    appId: "JIRA",
    icon: <BrandGlyph path={BRAND_PATHS.jira} />,
  },
  {
    id: "linear",
    name: "Linear",
    description: "接入 Linear 的 Issue 与 Cycle，研发任务自动纳入日程规划。",
    channel: "composio",
    appId: "LINEAR",
    icon: <BrandGlyph path={BRAND_PATHS.linear} />,
  },
  {
    id: "trello",
    name: "Trello",
    description: "读取 Trello 卡片的截止日期，看板任务自动进入待排期收件箱。",
    channel: "composio",
    appId: "TRELLO",
    icon: <BrandGlyph path={BRAND_PATHS.trello} />,
  },
];

// 精选里匹配该 slug 的 Composio 卡（slug 比较忽略大小写——云端 slug 为小写，
// 精选 appId 为大写惯写）；图标与品牌色类都取自精选定义
const matchFeatured = (slug: string) =>
  FEATURED_SOURCES.find(
    (s) => s.channel === "composio" && s.appId?.toLowerCase() === slug.toLowerCase(),
  );

// 非精选应用图标（已连接/搜索结果通用）：精选匹配 → 内联品牌 SVG；否则加载云端
// logo（csp 为 null 可直载）；加载失败或无 logo → 首字母色块兜底
function AppIcon({ slug, name, logo }: { slug: string; name?: string | null; logo?: string | null }) {
  const [broken, setBroken] = useState(false);
  const featured = matchFeatured(slug);
  if (featured) return featured.icon;
  if (logo && !broken) {
    return <img className="data-app-logo" src={logo} alt="" onError={() => setBroken(true)} />;
  }
  return <span className="data-letter">{(name ?? slug).charAt(0).toUpperCase()}</span>;
}

// 表单三态互斥（手动添加 / 编辑行 / 精选模板），同刻至多一个打开
type FormMode =
  | { kind: "manual" }
  | { kind: "edit"; server: ApiMcpServer }
  | { kind: "template"; id: string }
  | null;

// 配置小模态框（本地文件夹 / MCP 表单三态共用，2026-09-18 由内联展开改模态——
// 不再挤压"已连接"区布局）：fixed 遮罩 + 居中卡片，Esc / 点遮罩 / 右上角 X 三路
// 关闭，关闭 = 取消（未保存草稿丢弃，口径同原内联表单的取消）。
// Esc 挂 window capture——设置页自身的 Esc 是 document capture 且先注册，挂
// document 会按注册序被它先截胡连锁关掉设置页；window 在传播链更外层先收到，
// stopPropagation 后设置页监听不触发。卡片本体不挂 backdrop-filter（毛玻璃
// 红线：filter 非 none 会成为 fixed 后代的包含块），frost 走 ::before 垫层
export function FormModal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return (
    <div className="data-modal-overlay" onClick={onClose}>
      <div
        className="data-modal"
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()} // 点卡片本体不透传到遮罩关闭
      >
        <div className="data-modal-head">
          <span className="data-modal-title">{title}</span>
          <button type="button" className="data-modal-close" aria-label="关闭" onClick={onClose}>
            <X size={14} strokeWidth={2} />
          </button>
        </div>
        <div className="data-modal-body">{children}</div>
      </div>
    </div>
  );
}

// CLI 卡图标：精选适配器出品牌 SVG（cliId 反查精选定义），自定义行 lucide Terminal
function CliIcon({ tool }: { tool: ApiCliTool }) {
  const featured = FEATURED_SOURCES.find((f) => f.cliId === tool.adapter_id);
  return featured ? (
    featured.icon
  ) : (
    <Terminal size={18} strokeWidth={2} />
  );
}

// CLI 卡图标色类名（精选行复用精选卡 id 的品牌色类，自定义行无色 = 默认前景色）
const cliIconClass = (tool: ApiCliTool) =>
  FEATURED_SOURCES.find((f) => f.cliId === tool.adapter_id)?.id ?? "";

// 高可信直通徽标开关（2026-10-24）：复用 .data-key-badge 形态的可点开关——开 =
// 绿「高可信直通」/ 关 = 灰「开直通」。语义：该源待办/日历域条目免确认直接进入
// 收件箱/日历；笔记文档提炼（doc 域）仍进数据源页确认（提取的不准确，用户定案）
function TrustBadge({
  trusted,
  busy,
  onToggle,
}: {
  trusted: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={`data-key-badge trust${trusted ? " ok" : " idle"}`}
      title={
        trusted
          ? "高可信直通已开：该源待办/日历域条目免确认直接进入收件箱/日历；笔记文档提炼的建议仍需在「数据源」页确认"
          : "高可信直通：开启后该源待办/日历域条目免确认直接进入收件箱/日历（笔记文档提炼仍需确认）"
      }
      disabled={busy}
      onClick={onToggle}
    >
      {trusted ? "高可信直通" : "开直通"}
    </button>
  );
}

// 复制按钮（安装命令 / 手动授权命令）：成功闪现"已复制"2s；WebView 剪贴板被拒
// 静默回落原文案（命令文本就在旁边，用户可手选）
function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | undefined>(undefined);
  useEffect(() => () => clearTimeout(timerRef.current), []);
  return (
    <button
      type="button"
      className="data-btn ghost"
      onClick={() => {
        navigator.clipboard
          .writeText(text)
          .then(() => {
            setCopied(true);
            clearTimeout(timerRef.current);
            timerRef.current = setTimeout(() => setCopied(false), 2000);
          })
          .catch(() => {});
      }}
    >
      {copied ? "✓ 已复制" : label}
    </button>
  );
}

// CLI 接入模态：三态状态机（未安装 → 已安装未授权 → 已授权），内容全来自 GET
// /tools 的 spec 字段（前端零适配器知识——命令声明必须与执行器同源在后端）。
// 「检测」probe 驱动分支；授权 auto 档捕获 URL 开系统浏览器 + 5s 轮询 CLI 自身
// 的 auth 状态，manual 档给可复制终端命令；已授权亮「接入」（connect 建行并
// 落库探测快照，凭据恒留 CLI 的系统钥匙串，Clara 零凭据）。connect 幂等
function CliConnectModal({
  tool,
  dualChannelHint,
  onClose,
  onConnected,
}: {
  tool: ApiCliTool;
  dualChannelHint: boolean; // 已接入同名 MCP 服务（双通道数据重复提示）
  onClose: () => void;
  onConnected: (row: ApiCliTool) => void;
}) {
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ApiCliProbe | null>(null);
  const [authStarting, setAuthStarting] = useState(false);
  const [waitingAuth, setWaitingAuth] = useState(false);
  const [manualCmd, setManualCmd] = useState<string | null>(null); // manual 档授权命令（startCliAuth 响应带回）
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | undefined>(undefined);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      clearTimeout(timerRef.current); // 终止在途授权轮询链
    };
  }, []);

  const runProbe = async () => {
    if (probing) return;
    setProbing(true);
    setError(null);
    setWaitingAuth(false);
    try {
      setProbe(await probeCliTool(tool.adapter_id));
    } catch (e) {
      setError(
        (e as Error).name === "AbortError"
          ? "检测超时：CLI 未在限定时间内响应，请稍后重试"
          : "检测失败：后端服务不可用",
      );
    } finally {
      setProbing(false);
    }
  };

  // auto 档授权：spawn CLI 的 auth login 捕获 URL → 系统浏览器 → 轮询 CLI 自身
  // 授权状态（5s×60，同 Composio OAuth 轮询节拍）；完成即自动探测刷新徽标
  const handleAuth = async () => {
    if (authStarting || waitingAuth) return;
    setAuthStarting(true);
    setError(null);
    try {
      const r = await startCliAuth(tool.adapter_id);
      if (r.ok && r.url) {
        await openExternal(r.url);
        setWaitingAuth(true);
        const poll = async (attempt: number) => {
          let done = false;
          try {
            const st = await fetchCliAuthStatus(tool.adapter_id);
            if (st.authenticated) {
              done = true;
              setWaitingAuth(false);
              void runProbe();
            }
          } catch {
            /* 单次失败继续轮询 */
          }
          if (!aliveRef.current || done) return;
          if (attempt + 1 >= 60) {
            setWaitingAuth(false);
            setError("等待授权超时：若已在浏览器完成授权，点击「检测」确认");
            return;
          }
          timerRef.current = setTimeout(() => void poll(attempt + 1), 5000);
        };
        void poll(0);
      } else if (r.manual_command) {
        setManualCmd(r.manual_command); // manual 档：渲染命令供复制，终端自理
      } else {
        setError(r.error ?? "发起授权失败");
      }
    } catch (e) {
      setError(
        (e as Error).name === "AbortError"
          ? "发起授权超时：CLI 未响应，请稍后重试"
          : "发起授权失败：后端服务不可用",
      );
    } finally {
      setAuthStarting(false);
    }
  };

  const handleConnect = async () => {
    if (connecting) return;
    setConnecting(true);
    setError(null);
    try {
      onConnected(await connectCliTool(tool.adapter_id));
    } catch (e) {
      const status = (e as Error & { status?: number }).status;
      setError(
        status === 422
          ? "接入失败：未检测到 CLI 可执行文件，请先完成安装与检测"
          : "接入失败，请稍后重试",
      );
    } finally {
      setConnecting(false);
    }
  };

  // 三态判定：探测后 bin_ok=false 未安装；auth_ok===false 未授权（null = 不适用
  // 视为已授权，auth_mode=none / 自定义行）；其余已授权
  const installed = probe !== null && probe.bin_ok;
  const authed = installed && probe!.auth_ok !== false;

  return (
    <FormModal title={tool.name} onClose={onClose}>
      <div className="cli-connect">
        <span className="data-card-desc">{tool.description}</span>
        {dualChannelHint && (
          <span className="cli-hint">
            检测到已接入同名 MCP 服务：双通道同时启用会读到重复数据（已排期内容会自动去重），
            建议只保留一个通道。
          </span>
        )}
        {probe === null ? (
          <>
            <span className="cli-hint">需要先在本机安装官方 CLI（依赖 Node.js ≥ 18），安装步骤见官方文档：</span>
            <div className="cli-actions">
              {tool.install_url && (
                <button
                  type="button"
                  className="data-btn ghost"
                  onClick={() => void openExternal(tool.install_url!)}
                >
                  官方文档
                </button>
              )}
              <button
                type="button"
                className="data-btn primary"
                disabled={probing}
                onClick={() => void runProbe()}
              >
                {probing ? "检测中…" : "检测"}
              </button>
            </div>
          </>
        ) : !installed ? (
          <>
            <span className="cli-hint bad">未检测到 CLI 可执行文件，请按官方文档完成安装：</span>
            <div className="cli-actions">
              {tool.install_url && (
                <button
                  type="button"
                  className="data-btn ghost"
                  onClick={() => void openExternal(tool.install_url!)}
                >
                  官方文档
                </button>
              )}
              <button
                type="button"
                className="data-btn primary"
                disabled={probing}
                onClick={() => void runProbe()}
              >
                {probing ? "检测中…" : "已安装，检测"}
              </button>
            </div>
          </>
        ) : !authed ? (
          <>
            <span className="cli-hint bad">
              {`已安装${probe.bin_version ? ` v${probe.bin_version}` : ""} · 未授权，CLI 需要完成账号授权后才能读取数据。`}
            </span>
            {tool.auth_mode === "manual" && manualCmd && (
              <div className="cli-install-row">
                <code className="cli-cmd mono">{manualCmd}</code>
                <CopyButton text={manualCmd} label="复制命令" />
              </div>
            )}
            <div className="cli-actions">
              {tool.auth_mode === "auto" ? (
                <button
                  type="button"
                  className="data-btn primary"
                  disabled={authStarting || waitingAuth}
                  onClick={() => void handleAuth()}
                >
                  {waitingAuth ? "等待授权完成…" : authStarting ? "发起中…" : "去授权"}
                </button>
              ) : (
                <button
                  type="button"
                  className="data-btn primary"
                  disabled={authStarting}
                  onClick={() => void handleAuth()}
                >
                  {authStarting ? "获取中…" : "获取授权命令"}
                </button>
              )}
              <button
                type="button"
                className="data-btn ghost"
                disabled={probing}
                onClick={() => void runProbe()}
              >
                {probing ? "检测中…" : "已完成授权，检测"}
              </button>
            </div>
          </>
        ) : (
          <>
            <span className="cli-hint ok">
              {`已安装${probe.bin_version ? ` v${probe.bin_version}` : ""} · ${probe.auth_ok === true ? "已授权" : "无需授权"}，可以接入。`}
              {!tool.fetch_ready && "（采集命令待实测适配，接入后先参与检测通道就绪）"}
            </span>
            <div className="cli-actions">
              <button
                type="button"
                className="data-btn ghost"
                disabled={probing}
                onClick={() => void runProbe()}
              >
                {probing ? "检测中…" : "重新检测"}
              </button>
              <button
                type="button"
                className="data-btn primary"
                disabled={connecting}
                onClick={() => void handleConnect()}
              >
                {connecting ? "接入中…" : "接入"}
              </button>
            </div>
          </>
        )}
        {error && <span className="mcp-form-error">{error}</span>}
      </div>
    </FormModal>
  );
}

// 自定义只读命令表单（窄口：一条固定 argv、无参数插值、仅时间窗占位；写操作
// 特征词后端 deny-list 拦截）。参数 textarea 空白切分为 argv 数组（1-8 项）。
// initial = 编辑态（回填三字段、保存走 PUT——后端清测试结果，旧结果对旧命令是谎言）
function CustomCliForm({
  initial,
  onClose,
  onSaved,
}: {
  initial?: ApiCliTool;
  onClose: () => void;
  onSaved: (row: ApiCliTool) => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [binPath, setBinPath] = useState(initial?.bin_path ?? "");
  const [argvText, setArgvText] = useState(initial?.custom_argv?.join(" ") ?? "");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 「测试」结果（会话态草稿反馈：成功绿行 / 失败红行；编辑后再测即覆盖）
  const [testNotice, setTestNotice] = useState<{ ok: boolean; text: string } | null>(null);

  const draftBody = () => {
    const argv = argvText.split(/\s+/).filter(Boolean);
    if (!name.trim() || !binPath.trim() || !argv.length) {
      setError("请填写显示名称、可执行文件路径与至少一项参数");
      return null;
    }
    return { name: name.trim(), bin_path: binPath.trim(), argv };
  };

  // 保存前先试：表单当前值直测后端（不建行不落库）——与卡上「测试」同执行核心
  const handleTest = async () => {
    if (testing || saving) return;
    const body = draftBody();
    if (!body) return;
    setTesting(true);
    setError(null);
    setTestNotice(null);
    try {
      const r = await testCustomCliDraft(body);
      setTestNotice(
        r.ok
          ? { ok: true, text: `命令可执行，返回 ${r.items_count ?? 0} 条数据` }
          : { ok: false, text: r.error ?? "命令执行失败" },
      );
    } catch (e) {
      const status = (e as Error & { status?: number }).status;
      setTestNotice({
        ok: false,
        text:
          status === 422
            ? "测试被拒：命令含写操作特征词，或参数超过 8 项 / 单项超过 100 字符"
            : (e as Error).name === "AbortError"
              ? "测试超时：命令未在限定时间内返回，请稍后重试"
              : "测试失败：后端服务不可用",
      });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (saving) return;
    const body = draftBody();
    if (!body) return;
    setSaving(true);
    setError(null);
    try {
      onSaved(
        initial
          ? await updateCliTool(initial.adapter_id, body)
          : await createCustomCli(body),
      );
    } catch (e) {
      const status = (e as Error & { status?: number }).status;
      setError(
        status === 422
          ? "保存被拒：命令含写操作特征词，或参数超过 8 项 / 单项超过 100 字符"
          : "保存失败：后端服务不可用",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <FormModal title={initial ? "编辑自定义只读命令" : "添加自定义只读命令"} onClose={onClose}>
      <div className="custom-cli-form">
        <div className="mcp-field">
          <span className="mcp-field-label">显示名称</span>
          <input
            className="mcp-input"
            value={name}
            maxLength={30}
            placeholder="如：我的工作清单"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="mcp-field">
          <span className="mcp-field-label">可执行文件路径</span>
          <input
            className="mcp-input mono"
            value={binPath}
            maxLength={300}
            placeholder="如：C:\Program Files\mytool\mytool.exe"
            onChange={(e) => setBinPath(e.target.value)}
          />
        </div>
        <div className="mcp-field">
          <span className="mcp-field-label">命令参数（每项以空格分隔，1-8 项）</span>
          <textarea
            className="mcp-input mono custom-cli-argv"
            value={argvText}
            maxLength={900}
            placeholder={"list --json --since={since}\n（{since}/{until} 会被替换为采集时间窗，其余须为固定只读参数）"}
            onChange={(e) => setArgvText(e.target.value)}
          />
          <span className="mcp-field-hint">
            仅限只读命令：写入/发送类特征词会被拒绝；不参与 AI 补采，仅每日静态采集
          </span>
        </div>
        {error && <span className="mcp-form-error">{error}</span>}
        {testNotice && (
          <span className={testNotice.ok ? "cli-hint ok" : "mcp-form-error"}>
            {testNotice.text}
          </span>
        )}
        {/* 布局同 MCP 表单定稿：流程钮（取消/保存）靠右，「测试」工具钮左推对齐
            左缘——左工具右流程 */}
        <div className="mcp-form-actions">
          <button
            type="button"
            className="data-btn ghost push-left"
            disabled={testing || saving}
            onClick={() => void handleTest()}
          >
            {testing ? "测试中…" : "测试"}
          </button>
          <button type="button" className="data-btn ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="data-btn primary"
            disabled={saving}
            onClick={() => void handleSave()}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </FormModal>
  );
}

function DataSettings() {
  // —— MCP 服务列表与本地文件夹配置（真源在后端 SQLite）——
  const [mcpServers, setMcpServers] = useState<ApiMcpServer[] | null>(null);
  const [localFolder, setLocalFolder] = useState<ApiLocalFolder | null>(null);
  const [formMode, setFormMode] = useState<FormMode>(null);
  // —— CLI 工具（第四源）：注册表全量 + 接入行合并（null = 挂载在途）——
  const [cliTools, setCliTools] = useState<ApiCliTool[] | null>(null);
  const [cliOpenId, setCliOpenId] = useState<string | null>(null); // 精选卡「连接应用」接入模态
  const [customCliOpen, setCustomCliOpen] = useState(false); // 自定义只读命令表单（添加）
  const [customEditId, setCustomEditId] = useState<string | null>(null); // 自定义行编辑模态
  // —— ics 日历文件导入（2026-11-11 批，页脚 geek 行入口）——默认信任直落：日程
  // 直接建日历、待办进收件箱，不经建议结算；指纹防重复导入（同文件重导全跳过）。
  // 中栏/收件箱刷新靠后端 tasks_changed 广播（App SSE → reloadTasks）。成功即关
  // 弹窗、页面上绿色提示条 4s 自清（汇总从简）；失败保留弹窗内错误详情供重选重试
  const [icsOpen, setIcsOpen] = useState(false);
  const [icsPath, setIcsPath] = useState<string | null>(null);
  const [icsBusy, setIcsBusy] = useState(false);
  const [icsError, setIcsError] = useState<string | null>(null);
  const [icsNotice, setIcsNotice] = useState<string | null>(null);

  // 成功提示条 4s 自清（模式同 DataSourcePanel notice）
  useEffect(() => {
    if (!icsNotice) return;
    const t = setTimeout(() => setIcsNotice(null), 4000);
    return () => clearTimeout(t);
  }, [icsNotice]);

  const openIcsModal = () => {
    setIcsPath(null);
    setIcsBusy(false);
    setIcsError(null);
    setIcsOpen(true);
  };

  const handlePickIcs = async () => {
    const p = await pickIcsFile();
    if (p) {
      setIcsPath(p);
      setIcsError(null);
    }
  };

  const handleImportIcs = async () => {
    if (!icsPath || icsBusy) return;
    setIcsBusy(true);
    setIcsError(null);
    try {
      const r = await importIcsFile(icsPath);
      setIcsOpen(false);
      setIcsNotice(
        `已导入「${r.calendar}」：创建日程 ${r.created_events}、待办 ${r.created_todos}`,
      );
    } catch (e) {
      const err = e as Error & { detail?: string; name?: string };
      setIcsError(
        err.name === "AbortError"
          ? "导入超时：文件过大或后端繁忙，请稍后再试"
          : (err.detail ?? "导入失败：文件无法解析（或后端服务未运行）"),
      );
    } finally {
      setIcsBusy(false);
    }
  };
  // 各卡"测试"在态独立（2026-09-20 修正：服务互相独立，一张卡测试不得联动
  // 禁用其他卡——键 = MCP 行 id 或本地文件夹的 LOCAL_FOLDER_KEY）
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set());
  // 最近一次测试的错误摘要（会话态，供"离线/无效"徽标 title；不落库——重启后
  // 徽标仍显示，仅丢失悬停详情）
  const [testErrors, setTestErrors] = useState<Record<string, string>>({});
  // Composio 连接「测试」结果（会话态——云端真源不落库；未测连接不显徽标）
  const [connTests, setConnTests] = useState<Record<string, ApiComposioTest>>({});
  // 高可信直通（2026-10-24）：key→trusted 映射（/api/ingest/sources 单源），三类
  // 已连接卡（CLI/MCP/Composio）的开关徽标共消费；接入/保存后重拉——任务型精选
  // 工具后端默认预开，前端 map 缺 key 会显示错误关态
  const [trustedMap, setTrustedMap] = useState<Record<string, boolean>>({});
  const [trustBusy, setTrustBusy] = useState<Set<string>>(new Set());

  const loadTrusted = async () => {
    try {
      const rows = await fetchIngestSources();
      setTrustedMap(Object.fromEntries(rows.map((r) => [r.key, r.trusted])));
    } catch {
      /* 后端缺席保持现状（开关显示关态，点击会 404 静默不变） */
    }
  };

  // 切换直通开关：await 回填不乐观（同卡上启停钮口径）；停用中的源不在后端
  // 枚举内（PUT 404）静默保持原态——停用源无采集，开关本就无行为意义
  const toggleTrusted = async (key: string) => {
    if (trustBusy.has(key)) return;
    const next = !trustedMap[key];
    setTrustBusy((s) => new Set(s).add(key));
    try {
      await updateIngestSourceTrusted(key, next);
      setTrustedMap((m) => ({ ...m, [key]: next }));
    } catch {
      /* 失败保持原值（徽标即真值） */
    } finally {
      setTrustBusy((s) => {
        const n = new Set(s);
        n.delete(key);
        return n;
      });
    }
  };

  // 本地文件夹配置（folderOpen 时弹出 FormModal 配置模态——2026-09-18 由内联
  // 展开改模态；启停走卡上按钮，模态只管目录列表——开关与卡上按钮控同一状态属
  // 冗余，2026-09-18 删面板开关；同日扩多目录：草稿为路径数组，最多 10 项）
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderPaths, setFolderPaths] = useState<string[]>([]);
  const [folderSaving, setFolderSaving] = useState(false);
  const [folderTesting, setFolderTesting] = useState(false);
  const [folderTestResult, setFolderTestResult] = useState<string | null>(null);
  const [folderTestOk, setFolderTestOk] = useState(false);
  const [folderError, setFolderError] = useState<string | null>(null);

  // —— Composio Key 配置 ——
  const [keyInfo, setKeyInfo] = useState<ApiComposioKey | null>(null);
  const [keyDraft, setKeyDraft] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [savingKey, setSavingKey] = useState(false);
  const [verifyingKey, setVerifyingKey] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null); // 保存/重验请求本身的失败
  // 验证成功反馈：来源按钮文案闪现"✓ 有效"3 秒——不占布局（曾用卡片下方成功行，
  // 会撑高卡片推动整页下移，2026-10-15 用户反馈改此形态）
  const [keyOkFlash, setKeyOkFlash] = useState<"save" | "verify" | null>(null);
  const keyOkTimerRef = useRef<number | undefined>(undefined);

  // —— 已连接列表（null = 挂载在途；未配 Key 的 409 静默归空）——
  const [connections, setConnections] = useState<ApiComposioConnection[] | null>(null);

  // —— 应用搜索（切换态：输入即离开展示精选，防抖 300ms 后请求）——
  const [searchInput, setSearchInput] = useState("");
  const [searchResults, setSearchResults] = useState<ApiComposioApp[] | null>(null);
  const [searching, setSearching] = useState(false);

  // —— 连接流程 ——
  const [connectingSlug, setConnectingSlug] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const pollTimerRef = useRef<number | undefined>(undefined);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    // 四段独立并行（原先串行：Composio 云端慢/断网会拖住后面的 MCP 区渲染）；
    // 各段 try/catch 归空语义与原串行版逐段相同
    void Promise.all([
      (async () => {
        try {
          const key = await fetchComposioKey();
          if (!cancelled) {
            setKeyInfo(key);
            setKeyDraft(key.api_key ?? "");
          }
        } catch {
          /* 后端缺席：保持未配置态 */
        }
      })(),
      (async () => {
        try {
          const conns = await fetchComposioConnections();
          if (!cancelled) setConnections(conns);
        } catch {
          if (!cancelled) setConnections([]); // 未配 Key（409）或后端缺席都归空
        }
      })(),
      (async () => {
        try {
          const [servers, folder] = await Promise.all([fetchMcpServers(), fetchLocalFolder()]);
          if (!cancelled) {
            setMcpServers(servers);
            setLocalFolder(folder);
          }
        } catch {
          if (!cancelled) setMcpServers([]); // 后端缺席归空（本地文件夹保持 null = 未配置态）
        }
      })(),
      (async () => {
        try {
          const tools = await fetchCliTools();
          if (!cancelled) setCliTools(tools);
        } catch {
          if (!cancelled) setCliTools([]); // 后端缺席归空
        }
      })(),
      (async () => {
        const rows = await fetchIngestSources().catch(() => []);
        if (!cancelled) setTrustedMap(Object.fromEntries(rows.map((r) => [r.key, r.trusted])));
      })(),
    ]);
    return () => {
      cancelled = true;
      mountedRef.current = false;
      clearTimeout(pollTimerRef.current); // 终止在途轮询链
      clearTimeout(keyOkTimerRef.current); // 终止在途成功反馈闪现
    };
  }, []);

  // 搜索防抖：输入停 300ms 才打后端；清空立即恢复精选（searchResults 同步置空）
  useEffect(() => {
    const q = searchInput.trim();
    if (!q) return;
    const t = setTimeout(async () => {
      setSearching(true);
      try {
        const apps = await searchComposioApps(q);
        if (mountedRef.current) setSearchResults(apps);
      } catch {
        if (mountedRef.current) setSearchResults([]); // 后端 409/502 都归零结果
      } finally {
        if (mountedRef.current) setSearching(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Key 已配置且有效才解锁搜索与连接（后端也会拦，前端先禁用省一次必败请求）
  const keyReady = Boolean(keyInfo?.api_key);

  // 已连接 slug 集（小写口径，供精选隐藏与搜索结果过滤）
  const connectedSlugs = useMemo(
    () => new Set((connections ?? []).map((c) => c.toolkit_slug.toLowerCase())),
    [connections],
  );

  // 精选展示集：Composio 卡已连接则隐藏；MCP 卡已存在 name 等于模板 defaultName
  // 的行则隐藏（删除该行自然回位——改名也会回位但同名行已在已连接区，信息不丢）；
  // CLI 卡已接入（connected 行）则隐藏
  const featuredVisible = useMemo(
    () =>
      FEATURED_SOURCES.filter((s) => {
        if (s.channel === "composio") return !connectedSlugs.has((s.appId ?? "").toLowerCase());
        if (s.channel === "cli")
          return !(cliTools ?? []).some((t) => t.adapter_id === s.cliId && t.connected);
        const tpl = matchTemplate(s.id);
        return !tpl || !(mcpServers ?? []).some((row) => row.name === tpl.defaultName);
      }),
    [connectedSlugs, mcpServers, cliTools],
  );

  const query = searchInput.trim();
  const searchVisible = useMemo(
    () => (searchResults ?? []).filter((a) => !connectedSlugs.has(a.slug.toLowerCase())),
    [searchResults, connectedSlugs],
  );

  const refreshConnections = async () => {
    try {
      setConnections(await fetchComposioConnections());
    } catch {
      /* 保持现状 */
    }
    void loadTrusted(); // 连接集变化：新连接的直通默认态同步（Composio 无预开但口径统一）
  };

  // 成功反馈：来源按钮闪现"✓ 有效"3 秒（重验已验证过的 Key 时徽标本就无变化，
  // 按钮文案是唯一可感知反馈）；失败摘要走 keyErrorLine
  const flashKeyOk = (from: "save" | "verify") => {
    setKeyOkFlash(from);
    clearTimeout(keyOkTimerRef.current);
    keyOkTimerRef.current = setTimeout(() => setKeyOkFlash(null), 3000);
  };

  const handleSaveKey = async () => {
    setSavingKey(true);
    setKeyError(null);
    setKeyOkFlash(null);
    clearTimeout(keyOkTimerRef.current);
    try {
      const r = await saveComposioKey(keyDraft.trim() || null); // 后端保存后自动验证
      setKeyInfo(r);
      if (r.key_ok) flashKeyOk("save");
    } catch (e) {
      setKeyError(
        (e as Error).name === "AbortError"
          ? "验证超时：网络不佳或 Composio 云端无响应，请稍后重试"
          : "Key 保存失败：请确认后端服务正在运行",
      );
    } finally {
      setSavingKey(false);
    }
  };

  const handleVerifyKey = async () => {
    setVerifyingKey(true);
    setKeyError(null);
    setKeyOkFlash(null);
    clearTimeout(keyOkTimerRef.current);
    try {
      const r = await verifyComposioKey();
      setKeyInfo(r);
      if (r.key_ok) flashKeyOk("verify");
    } catch (e) {
      setKeyError(
        (e as Error).name === "AbortError"
          ? "验证超时：网络不佳或 Composio 云端无响应，请稍后重试"
          : "验证请求失败：请确认后端服务正在运行",
      );
    } finally {
      setVerifyingKey(false);
    }
  };

  // 轮询连接状态：ACTIVE → 刷新列表收尾；失败态/超时 → 提示；INITIALIZED 前的中断
  // 态（网络抖动、404 抖动）不终止，继续等
  const pollConnect = (caId: string, attempt: number) => {
    pollTimerRef.current = setTimeout(async () => {
      let terminal = false;
      try {
        const st = await fetchConnectStatus(caId);
        if (st.status === "ACTIVE") {
          terminal = true;
          setConnectingSlug(null);
          refreshConnections();
        } else if (["FAILED", "EXPIRED", "REVOKED", "INACTIVE"].includes(st.status)) {
          terminal = true;
          setConnectingSlug(null);
          setActionError("授权未完成或已失败，请重试");
        }
      } catch {
        /* 单次失败继续轮询 */
      }
      if (!mountedRef.current || terminal) return;
      if (attempt + 1 >= POLL_MAX_ATTEMPTS) {
        setConnectingSlug(null);
        setActionError("授权超时未完成：请重新点击「连接应用」再次发起授权");
        return;
      }
      pollConnect(caId, attempt + 1);
    }, POLL_INTERVAL_MS);
  };

  const handleConnect = async (slug: string) => {
    if (connectingSlug) return; // 同刻只允许一个连接流程
    setConnectingSlug(slug);
    setActionError(null);
    try {
      const { connected_account_id, redirect_url } = await connectComposioApp(slug);
      if (redirect_url) await openExternal(redirect_url); // 系统浏览器完成 OAuth
      pollConnect(connected_account_id, 0);
    } catch (e) {
      setConnectingSlug(null);
      if ((e as Error & { status?: number }).status === 409) {
        setActionError("该应用已连接");
        refreshConnections();
      } else {
        setActionError("连接发起失败：无法访问 Composio 云端");
      }
    }
  };

  // 连接级"测试"：与采集同源真调云端数条目（失败也是 200——失败是数据，徽标
  // 会话态展示，云端真源不落库；testingIds 键 = 连接 ID，与 MCP 行 UUID 不撞）
  const handleTestConnection = async (c: ApiComposioConnection) => {
    if (testingIds.has(c.id)) return;
    setTesting(c.id, true);
    try {
      const r = await testComposioConnection(c.id);
      setConnTests((prev) => ({ ...prev, [c.id]: r }));
    } catch (e) {
      // detail 优先：后端 502 带中文分类文案（_translate_upstream——「无法连接
      // Composio 云端：检查网络」/「API Key 无效…」），此前被丢弃导致网络/后端
      // 缺席合并成一句笼统话（2026-11-05 网络排障定案）
      const msg =
        (e as { detail?: string }).detail ??
        ((e as Error).name === "AbortError"
          ? "测试超时：云端未在限定时间内响应，请稍后重试"
          : "测试失败：后端服务不可用或无法连接 Composio 云端");
      setConnTests((prev) => ({
        ...prev,
        [c.id]: { ok: false, latency_ms: null, items_count: null, note: null, error: msg },
      }));
    } finally {
      setTesting(c.id, false);
    }
  };

  // 采集启停：await 成功才本地翻转（不做乐观；后端镜像 ingest_sources——停用即
  // 不参与每日采集，云端连接本体不动，启用无感恢复）
  const handleToggleConnection = async (c: ApiComposioConnection) => {
    try {
      await setComposioEnabled(c.id, !c.enabled);
      setConnections((cs) =>
        (cs ?? []).map((x) => (x.id === c.id ? { ...x, enabled: !c.enabled } : x)),
      );
    } catch {
      setActionError("启停失败，请稍后重试");
    }
  };

  // 删除（断开云端连接）是破坏性操作：await 成功才本地移除（不乐观）
  const handleDisconnect = async (caId: string) => {
    try {
      await disconnectComposio(caId);
      setConnections((cs) => (cs ?? []).filter((c) => c.id !== caId));
    } catch {
      setActionError("删除失败，请稍后重试");
    }
  };

  // —— MCP 服务：upsert 行 / 表单保存 / 测试 / 启停 / 删除 ——
  const upsertServer = (row: ApiMcpServer) => {
    setMcpServers((list) => {
      const arr = list ?? [];
      const i = arr.findIndex((s) => s.id === row.id);
      if (i < 0) return [...arr, row];
      const next = [...arr];
      next[i] = row;
      return next;
    });
  };

  // 各卡"测试"在态读写助手（独立刷新：只动自己那张卡）
  const setTesting = (key: string, on: boolean) => {
    setTestingIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  // 测试错误摘要记入/清除（成功清掉旧摘要，失败写入最新——徽标 title 用）
  const rememberTestError = (key: string, error: string | null) => {
    setTestErrors((prev) => {
      if (error == null && !(key in prev)) return prev;
      const next = { ...prev };
      if (error == null) delete next[key];
      else next[key] = error;
      return next;
    });
  };

  // 表单保存收尾：入列 → 关表单 → 保存后自动测试一次（口径同模型页）。自动测试
  // 接入 testingIds 在态（原先 fire-and-forget 无任何反馈：卡上按钮不显示"测试
  // 中…"、失败静默）——与手动"测试"共用同一状态机与错误摘要通道
  const handleFormSaved = (row: ApiMcpServer) => {
    upsertServer(row);
    setFormMode(null);
    void loadTrusted(); // 新建/编辑后行集变化：滴答模板接入默认预开的直通态同步
    void (async () => {
      setTesting(row.id, true);
      try {
        const r = await testMcpServerById(row.id);
        const at = toNaiveLocalISO(new Date());
        setMcpServers((list) =>
          (list ?? []).map((s) =>
            s.id === row.id
              ? {
                  ...s,
                  last_test_ok: r.ok,
                  last_test_ms: r.latency_ms,
                  last_test_at: at,
                  last_test_tools: r.tools_count,
                }
              : s,
          ),
        );
        rememberTestError(row.id, r.ok ? null : r.error);
      } catch (e) {
        if ((e as Error).name === "AbortError") {
          setActionError("自动测试超时：服务未在限定时间内完成握手，请稍后重试");
        }
        /* 其余（后端缺席）：卡片保持原徽标（口径同模型页） */
      } finally {
        setTesting(row.id, false);
      }
    })();
  };

  // 已连接卡"测试"按钮：结果落库并由响应本地 patch 四列（含 tools 数——标题旁
  // 徽标"连通 · Nms · N 个工具"）。前端 80s 超时兜底——后端探测挂起时按钮必然
  // 复位（"测试中"不再无界）。每张卡独立在态，不联动其他卡
  const handleTestServer = async (id: string) => {
    if (testingIds.has(id)) return;
    setTesting(id, true);
    try {
      const r = await testMcpServerById(id);
      const at = toNaiveLocalISO(new Date());
      setMcpServers((list) =>
        (list ?? []).map((s) =>
          s.id === id
            ? {
                ...s,
                last_test_ok: r.ok,
                last_test_ms: r.latency_ms,
                last_test_at: at,
                last_test_tools: r.tools_count,
              }
            : s,
        ),
      );
      rememberTestError(id, r.ok ? null : r.error);
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        setActionError("测试超时：服务未在限定时间内完成握手，请稍后重试");
      }
      /* 其余（后端缺席）：徽标不动 */
    } finally {
      setTesting(id, false);
    }
  };

  // 启停：PATCH 响应为整行，await 后直接回填（卡的停用态绑行真值，不做乐观）
  const handleToggleServer = async (s: ApiMcpServer) => {
    try {
      upsertServer(await updateMcpServer(s.id, { enabled: !s.enabled }));
    } catch {
      setActionError("启停失败，请稍后重试");
    }
  };

  // 删除是破坏性操作：await 成功才本地移除（不乐观）
  const handleDeleteServer = async (id: string) => {
    try {
      await deleteMcpServer(id);
      setMcpServers((list) => (list ?? []).filter((s) => s.id !== id));
    } catch {
      setActionError("删除失败，请稍后重试");
    }
  };

  // —— CLI 工具：行 upsert / 测试（探测落库）/ 启停 / 移除 / 接入收尾 ——

  const upsertCliTool = (row: ApiCliTool) => {
    setCliTools((list) => {
      const arr = list ?? [];
      const i = arr.findIndex((t) => t.adapter_id === row.adapter_id);
      if (i < 0) return [...arr, row];
      const next = [...arr];
      next[i] = row;
      return next;
    });
  };

  // 卡上"测试"：精选行 = 探测落库（bin/auth）并由响应本地 patch 探测列（与 MCP
  // 卡同口径，每卡独立在态）；自定义行 = 近 24h 窗真跑采集命令，结果落库 test_*
  // （本地 patch 三列 + last_error）
  const handleTestCli = async (t: ApiCliTool) => {
    if (testingIds.has(t.adapter_id)) return;
    setTesting(t.adapter_id, true);
    try {
      if (t.custom) {
        const r = await testCustomCli(t.adapter_id);
        upsertCliTool({
          ...t,
          test_ok: r.ok,
          test_items: r.items_count,
          last_error: r.ok ? null : r.error,
        });
        rememberTestError(t.adapter_id, r.ok ? null : r.error);
      } else {
        const r = await probeCliTool(t.adapter_id);
        upsertCliTool({
          ...t,
          bin_ok: r.bin_ok,
          bin_version: r.bin_version,
          bin_path: r.bin_path,
          auth_ok: r.auth_ok,
          last_error: r.ok ? null : r.error,
        });
        rememberTestError(t.adapter_id, r.ok ? null : r.error);
      }
    } catch (e) {
      const msg =
        (e as Error).name === "AbortError"
          ? "测试超时：CLI 未在限定时间内响应，请稍后重试"
          : "请求失败：后端服务不可用";
      rememberTestError(t.adapter_id, msg);
    } finally {
      setTesting(t.adapter_id, false);
    }
  };

  // 启停：await 回填（卡的停用态绑行真值，不做乐观；后端联动镜像 ingest_sources）
  const handleToggleCli = async (t: ApiCliTool) => {
    try {
      upsertCliTool(await updateCliTool(t.adapter_id, { enabled: !t.enabled }));
    } catch {
      setActionError("启停失败，请稍后重试");
    }
  };

  // 删除是破坏性操作：await 成功才回填（不乐观）。只删 Clara 侧接入行，CLI
  // 自己的凭据留在系统钥匙串（重接入免再授权——ConfirmPopover 注明）。
  // 须整表重拉而非本地 filter：后端清单是"注册表全量 + 行合并"，精选适配器
  // 删行后仍返回条目（connected=false）——本地抹掉精选条目会让接入模态按
  // cliOpenId find 不到行而静默不开（曾致删除后「连接应用」点击无反应）
  const handleDeleteCli = async (id: string) => {
    try {
      await deleteCliTool(id);
      setCliTools(await fetchCliTools());
    } catch {
      setActionError("删除失败，请稍后重试");
    }
  };

  // 接入模态收尾：connect/create 响应为整行 → 入列 + 关模态
  const handleCliConnected = (row: ApiCliTool) => {
    upsertCliTool(row);
    setCliOpenId(null);
    void loadTrusted(); // 接入即默认预开的精选（todoist/dingtalk）徽标同步
  };

  // 卡上"测试"按钮：不带 paths 测库中已保存路径（后端读库并落库 test_ok/
  // test_files——结果持久化展示在卡标题旁徽标，直到下一次测试或目录变更）；
  // 未配置时后端不落库，响应错误记入徽标 title。与 MCP 卡测试互相独立
  const handleTestFolderCard = async () => {
    if (testingIds.has(LOCAL_FOLDER_KEY)) return;
    setTesting(LOCAL_FOLDER_KEY, true);
    try {
      const r = await testLocalFolder();
      setLocalFolder((f) =>
        f ? { ...f, test_ok: r.ok, test_files: r.ok ? r.files_count : null } : f,
      );
      rememberTestError(
        LOCAL_FOLDER_KEY,
        r.ok ? null : r.error ?? "测试失败",
      );
    } catch (e) {
      const msg =
        (e as Error).name === "AbortError"
          ? "测试超时：目录文件过多或后端繁忙，请稍后重试"
          : "请求失败：后端服务不可用";
      rememberTestError(LOCAL_FOLDER_KEY, msg);
    } finally {
      setTesting(LOCAL_FOLDER_KEY, false);
    }
  };

  // —— 本地文件夹（内置 MCP 服务端，多目录）——
  // 草稿清洗：每行 trim、丢空行（发送与测试都只取有效项）
  const draftPaths = () => folderPaths.map((p) => p.trim()).filter(Boolean);

  // 配置按钮：开模态时从后端真值对齐一次草稿（关闭模态丢弃未保存编辑）；未配置
  // 时给一行空输入让用户直接可填/可浏览
  const openFolderPanel = () => {
    const current = localFolder?.paths ?? [];
    setFolderPaths(current.length ? [...current] : [""]);
    setFolderError(null);
    setFolderTestResult(null);
    setFolderOpen((v) => !v);
  };

  // 卡上启停按钮：无路径时不直接失败，改为弹出配置模态引导填写；有路径则保存启停。
  // 路径来源：模态开着优先当前草稿（选完文件夹即可启用，不等保存—— ?? 只拦 null
  // 会永远用库中旧值，2026-09-18 修复），模态关着用库值。失败错误就地显示在
  // 配置模态内（自动弹出），不再沉到页面底部 actionError（同日修复）
  const handleFolderToggle = async () => {
    const paths = folderOpen ? draftPaths() : localFolder?.paths ?? [];
    if (!paths.length) {
      setFolderPaths([""]);
      setFolderOpen(true);
      return;
    }
    try {
      const row = await saveLocalFolder(paths, !(localFolder?.enabled ?? false));
      setLocalFolder(row);
      setFolderPaths(row.paths.length ? [...row.paths] : [""]);
    } catch {
      setFolderOpen(true); // 弹出配置模态就地展示错误
      setFolderError("启停失败：每个目录须为存在的文件夹绝对路径，请在面板中修正后重试");
    }
  };

  // 行编辑：改值 / 浏览选目录（写入指定行）/ 删行（可删到 0 行 = 清空配置）/ 加行
  const handlePathChange = (i: number, v: string) => {
    setFolderPaths((ps) => ps.map((p, idx) => (idx === i ? v : p)));
  };

  const handleBrowse = async (i: number) => {
    const dir = await pickDirectory();
    if (dir) handlePathChange(i, dir);
  };

  const handleRemovePath = (i: number) => {
    setFolderPaths((ps) => ps.filter((_, idx) => idx !== i));
  };

  const handleAddPath = () => {
    setFolderPaths((ps) => (ps.length >= 10 ? ps : [...ps, ""]));
  };

  // 面板保存只管目录列表：启停态保持库现值（启停入口唯一 = 卡上按钮）；后端恒校验
  // 每个路径有效（绝对路径 + 目录存在），坏路径不落库；空列表 = 清除全部。
  // 保存成功即关闭模态（2026-09-18 修复：此前不关闭且无提示，用户感知"没保存"）
  const handleSaveFolder = async () => {
    if (folderSaving) return;
    setFolderSaving(true);
    setFolderError(null);
    try {
      setLocalFolder(await saveLocalFolder(draftPaths(), localFolder?.enabled ?? false));
      setFolderOpen(false);
    } catch {
      setFolderError(
        "保存失败：每个目录须为绝对路径且存在，最多 10 个（或后端服务未运行）",
      );
    } finally {
      setFolderSaving(false);
    }
  };

  // 测试用面板当前草稿做探测（不读库、无需先保存）；草稿为空本地提示不发请求
  const handleTestFolder = async () => {
    if (folderTesting) return;
    const draft = draftPaths();
    if (!draft.length) {
      setFolderTestOk(false);
      setFolderTestResult("请先添加文件夹目录");
      return;
    }
    setFolderTesting(true);
    setFolderTestResult(null);
    try {
      const r = await testLocalFolder(draft);
      setFolderTestOk(r.ok);
      setFolderTestResult(r.ok ? `有效 · ${r.files_count} 个文件` : r.error);
    } catch (e) {
      setFolderTestOk(false);
      setFolderTestResult(
        (e as Error).name === "AbortError"
          ? "测试超时：目录文件过多或后端繁忙，请稍后重试"
          : "请求失败：后端服务不可用",
      );
    } finally {
      setFolderTesting(false);
    }
  };

  // Key 徽标三态：有效（绿）/ 无效（橙）/ 未验证（灰）——口径同模型卡连接徽标
  const keyBadge =
    keyInfo?.key_ok === true ? (
      <span className="data-key-badge ok">有效</span>
    ) : keyInfo?.key_ok === false ? (
      <span className="data-key-badge bad">无效</span>
    ) : (
      <span className="data-key-badge idle">未验证</span>
    );

  // Key 错误行：请求失败的本地提示优先，其次后端验证摘要（key_error）
  const keyErrorLine =
    keyError ?? (keyInfo?.key_ok === false ? keyInfo.key_error ?? "验证失败" : null);

  const keyBusy = savingKey || verifyingKey;

  // 本地文件夹卡描述（首目录 + 多目录计数）；悬停 title 看全部路径（换行分隔）
  const folderDesc =
    localFolder && localFolder.paths.length
      ? localFolder.paths.length > 1
        ? `${localFolder.paths[0]} 等 ${localFolder.paths.length} 个目录`
        : localFolder.paths[0]
      : "未配置文件夹";

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">数据接入</span>
        <span className="settings-page-sub">连接您常用的效率工具，让 AI 拥有完整的上下文。</span>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">Composio 配置</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">
                Composio API Key {keyBadge}
              </span>
              <span className="settings-row-desc">
                连接云端应用的统一凭证（1500+ 应用授权）。
                <a
                  className="data-key-link"
                  href={COMPOSIO_KEY_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => {
                    e.preventDefault();
                    openExternal(COMPOSIO_KEY_URL);
                  }}
                >
                  如何获取？
                </a>
              </span>
            </div>
            <div className="settings-row-control">
              <div className="data-key-wrap">
                <input
                  className="data-key-input mono"
                  type={showKey ? "text" : "password"}
                  value={keyDraft}
                  maxLength={200}
                  placeholder="在 Composio 控制台申请"
                  onChange={(e) => setKeyDraft(e.target.value)}
                />
                <button
                  type="button"
                  className="data-key-eye"
                  title={showKey ? "隐藏密钥" : "显示密钥"}
                  onClick={() => setShowKey((v) => !v)}
                >
                  {showKey ? <EyeOff size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
                </button>
              </div>
              <button
                type="button"
                className="data-btn ghost-accent"
                disabled={keyBusy}
                onClick={handleSaveKey}
              >
                {savingKey ? "验证中…" : keyOkFlash === "save" ? "✓ 有效" : "保存"}
              </button>
              <button
                type="button"
                className="data-btn ghost"
                disabled={keyBusy || !keyReady}
                title={keyReady ? "重新验证已保存的 Key" : "先保存 Key"}
                onClick={handleVerifyKey}
              >
                {verifyingKey ? "验证中…" : keyOkFlash === "verify" ? "✓ 有效" : "重新验证"}
              </button>
            </div>
          </div>
          {keyErrorLine && <div className="data-key-error">{keyErrorLine}</div>}
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">已连接</span>
        <div className="data-grid">
          {/* 本地文件夹：内置 MCP 服务端，永远显示在已连接区、不可删除。
              按钮行对齐 MCP 服务卡（测试 / 配置 / 停用·启用），仅无删除钮；
              标题旁徽标绑库中持久化的 test_ok/test_files（有效·N 个文件 /
              无效 / 未测），目录变更时后端清空回"未测" */}
          <div
            className={`data-card data-card-col${localFolder?.enabled ? "" : " disabled"}`}
          >
            <div className="data-card-col-head">
              <span className="data-icon local-folder">
                <Folder size={18} strokeWidth={2} />
              </span>
              <span className="data-pill purple">MCP</span>
            </div>
            <div className="data-card-name-line">
              <span className="data-card-name">本地文件夹</span>
              {localFolder?.test_ok === true ? (
                <span className="data-key-badge ok">有效 · {localFolder.test_files} 个文件</span>
              ) : localFolder?.test_ok === false ? (
                <span
                  className="data-key-badge bad"
                  title={testErrors[LOCAL_FOLDER_KEY] ?? "上次测试失败，点击「测试」重试"}
                >
                  无效
                </span>
              ) : (
                <span className="data-key-badge idle">未测</span>
              )}
            </div>
            <span
              className="data-card-desc mono"
              title={localFolder?.paths.length ? localFolder.paths.join("\n") : undefined}
            >
              {folderDesc}
            </span>
            <div className="data-row-actions">
              <button
                type="button"
                className="data-btn ghost"
                disabled={testingIds.has(LOCAL_FOLDER_KEY)}
                onClick={handleTestFolderCard}
              >
                {testingIds.has(LOCAL_FOLDER_KEY) ? "测试中…" : "测试"}
              </button>
              <button type="button" className="data-btn ghost" onClick={openFolderPanel}>
                配置
              </button>
              <button type="button" className="data-btn ghost" onClick={handleFolderToggle}>
                {localFolder?.enabled ? "停用" : "启用"}
              </button>
            </div>
          </div>
          {/* MCP 服务行：图标按 name 命中模板出品牌 SVG（否则 lucide Plug 兜底），
              标题旁徽标绑 last_test_*（与 enabled 正交）：连通·N 个工具（title 含
              ms 完整信息）/ 离线（title = 最近错误摘要）/ 未测，数据持久化（后端
              四列落库） */}
          {(mcpServers ?? []).map((s) => {
            const tpl = templateByName(s.name);
            // 模板描述与品牌图标同源：McpTemplate 不带 description，精选静态描述
            // 在 FEATURED_SOURCES（id 对齐模板 id）——一次查找两用
            const featured = tpl
              ? FEATURED_SOURCES.find((f) => f.id === tpl.id)
              : undefined;
            const brandIcon = featured?.icon;
            // desc 显示文本（用户自定义描述 > 精选模板描述——已连接不丢 > 技术串
            // 回落）——title 复用同值，截断时悬停看全文
            const descText =
              s.description ||
              featured?.description ||
              (s.transport === "stdio"
                ? [s.command, ...(s.args ?? []).slice(0, 2)].filter(Boolean).join(" ")
                : s.url ?? "");
            return (
              <div key={s.id} className={`data-card data-card-col${s.enabled ? "" : " disabled"}`}>
                <div className="data-card-col-head">
                  <span className={`data-icon ${tpl?.id ?? ""}`}>
                    {brandIcon ?? <Plug size={18} strokeWidth={2} />}
                  </span>
                  <span className="data-pill purple">MCP</span>
                </div>
                <div className="data-card-name-line">
                  <span className="data-card-name" title={s.name}>
                    {s.name}
                  </span>
                  {s.last_test_ok === true ? (
                    <span
                      className="data-key-badge ok"
                      title={`连通 · ${s.last_test_ms}ms · ${s.last_test_tools} 个工具（服务可达；凭据需实际调用时验证）`}
                    >
                      连通 · {s.last_test_tools} 个工具
                    </span>
                  ) : s.last_test_ok === false ? (
                    <span
                      className="data-key-badge bad"
                      title={testErrors[s.id] ?? "上次测试失败，点击「测试」重试"}
                    >
                      {connErrBadge(testErrors[s.id], "离线")}
                    </span>
                  ) : (
                    <span className="data-key-badge idle">未测</span>
                  )}
                  <TrustBadge
                    trusted={Boolean(trustedMap[s.name])}
                    busy={trustBusy.has(s.name)}
                    onToggle={() => void toggleTrusted(s.name)}
                  />
                </div>
                <span
                  className={`data-card-desc${s.description || featured?.description ? "" : " mono"}`}
                  title={descText}
                >
                  {descText}
                </span>
                <div className="data-row-actions">
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={testingIds.has(s.id)}
                    onClick={() => handleTestServer(s.id)}
                  >
                    {testingIds.has(s.id) ? "测试中…" : "测试"}
                  </button>
                  <button
                    type="button"
                    className="data-btn ghost"
                    onClick={() => setFormMode({ kind: "edit", server: s })}
                  >
                    配置
                  </button>
                  <button type="button" className="data-btn ghost" onClick={() => handleToggleServer(s)}>
                    {s.enabled ? "停用" : "启用"}
                  </button>
                  <ConfirmPopover
                    message={`删除 MCP 服务「${s.name}」？该操作不可恢复`}
                    onConfirm={() => handleDeleteServer(s.id)}
                    buttonClass="data-btn ghost"
                    buttonTitle="删除服务"
                    label="删除"
                  />
                </div>
              </div>
            );
          })}
          {/* CLI 工具行（第四源）：精选行徽标三态绑探测列（已安装 · 已授权 /
              已安装 · 未授权 / 未检测——connect 建行即落库探测快照，正常不再出现
              未检测），自定义行徽标三态绑测试列（连通 · N 条数据 / 失败 / 未测——
              测试 = 近 24h 窗真跑采集命令）；描述位 mono bin_path。按钮与 MCP 卡
              同构：测试 / 配置（仅自定义行）/ 停用 / 删除 */}
          {(cliTools ?? [])
            .filter((t) => t.connected)
            .map((t) => (
              <div
                key={t.adapter_id}
                className={`data-card data-card-col${t.enabled ? "" : " disabled"}`}
              >
                <div className="data-card-col-head">
                  <span className={`data-icon ${cliIconClass(t)}`}>
                    <CliIcon tool={t} />
                  </span>
                  <span className="data-pill green">CLI</span>
                </div>
                <div className="data-card-name-line">
                  <span className="data-card-name" title={t.name}>
                    {t.name}
                  </span>
                  {t.custom ? (
                    t.test_ok === true ? (
                      <span
                        className="data-key-badge ok"
                        title={`命令可执行，返回 ${t.test_items ?? 0} 条数据（未测时间窗，报告为原始返回数）`}
                      >
                        连通 · {t.test_items ?? 0} 条数据
                      </span>
                    ) : t.test_ok === false ? (
                      <span
                        className="data-key-badge bad"
                        title={testErrors[t.adapter_id] ?? t.last_error ?? "命令执行失败"}
                      >
                        {connErrBadge(testErrors[t.adapter_id] ?? t.last_error, "失败")}
                      </span>
                    ) : (
                      <span className="data-key-badge idle" title="从未测试，点「测试」真跑一遍命令">
                        未测
                      </span>
                    )
                  ) : t.auth_ok === true ? (
                    <span className="data-key-badge ok" title="CLI 已安装且账号授权有效">已安装 · 已授权</span>
                  ) : t.bin_ok === true ? (
                    <span
                      className="data-key-badge bad"
                      title={testErrors[t.adapter_id] ?? "CLI 未完成账号授权，点「测试」重新检测"}
                    >
                      已安装 · 未授权
                    </span>
                  ) : (
                    <span
                      className="data-key-badge idle"
                      title={t.last_error ?? "从未探测，点「测试」检测安装与授权状态"}
                    >
                      未检测
                    </span>
                  )}
                  {!t.custom && (
                    <TrustBadge
                      trusted={Boolean(trustedMap[`cli:${t.adapter_id}`])}
                      busy={trustBusy.has(`cli:${t.adapter_id}`)}
                      onToggle={() => void toggleTrusted(`cli:${t.adapter_id}`)}
                    />
                  )}
                </div>
                {/* 描述位：精选行复用注册表描述（连接后不丢，bin_path 诊断并入
                    悬停 title）；自定义行无精选描述，保持 mono 路径 */}
                {t.custom ? (
                  <span className="data-card-desc mono" title={t.bin_path ?? undefined}>
                    {t.bin_path ?? "未探测到可执行文件"}
                  </span>
                ) : (
                  <span
                    className="data-card-desc"
                    title={t.bin_path ? `${t.description}\n${t.bin_path}` : t.description}
                  >
                    {t.description}
                  </span>
                )}
                <div className="data-row-actions">
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={testingIds.has(t.adapter_id)}
                    onClick={() => void handleTestCli(t)}
                  >
                    {testingIds.has(t.adapter_id) ? "测试中…" : "测试"}
                  </button>
                  {t.custom && (
                    <button
                      type="button"
                      className="data-btn ghost"
                      onClick={() => setCustomEditId(t.adapter_id)}
                    >
                      配置
                    </button>
                  )}
                  <button type="button" className="data-btn ghost" onClick={() => void handleToggleCli(t)}>
                    {t.enabled ? "停用" : "启用"}
                  </button>
                  {t.custom ? (
                    <ConfirmPopover
                      message={`删除自定义命令「${t.name}」？该操作不可恢复`}
                      onConfirm={() => void handleDeleteCli(t.adapter_id)}
                      buttonClass="data-btn ghost"
                      buttonTitle="删除自定义命令"
                      label="删除"
                    />
                  ) : (
                    <ConfirmPopover
                      message={`删除 CLI 工具「${t.name}」？Clara 侧接入记录将删除（CLI 自身的登录凭据保留，重新接入免再次授权）`}
                      onConfirm={() => void handleDeleteCli(t.adapter_id)}
                      buttonClass="data-btn ghost"
                      buttonTitle="删除接入"
                      label="删除"
                    />
                  )}
                </div>
              </div>
            ))}
          {/* Composio 真连接：真源在云端实时拉取（~1-2s，无骨架占位——曾有的
              在途骨架卡在其余卡 SQLite 秒到的现实下成了每进页面必现的幻影卡，
              2026-10-16 用户报 bug 后删除；连接到达自然 append）。按钮三件套：测试
              （与采集同源真调云端，徽标会话态）/ 停用（采集开关，云端连接不动）/
              删除（断开云端 + 清理本地镜像行） */}
          {(connections ?? []).map((c) => {
            const featured = matchFeatured(c.toolkit_slug);
            const test = connTests[c.id];
            const testName = c.name ?? c.toolkit_slug;
            return (
              <div key={c.id} className={`data-card data-card-col${c.enabled ? "" : " disabled"}`}>
                <div className="data-card-col-head">
                  <span className={`data-icon ${featured?.id ?? ""}`}>
                    <AppIcon slug={c.toolkit_slug} name={c.name} logo={c.logo} />
                  </span>
                  <span className="data-pill blue">Composio</span>
                </div>
                <div className="data-card-name-line">
                  <span className="data-card-name" title={testName}>
                    {testName}
                  </span>
                  {test &&
                    (test.ok ? (
                      <span
                        className="data-key-badge ok"
                        title={
                          test.note ??
                          `连通 · ${test.latency_ms ?? 0}ms · 返回 ${test.items_count ?? 0} 条数据（未过时间窗，报告为原始返回数）`
                        }
                      >
                        {test.items_count != null ? `连通 · ${test.items_count} 条数据` : "连通"}
                      </span>
                    ) : (
                      <span className="data-key-badge bad" title={test.error ?? "测试失败"}>
                        {connErrBadge(test.error, "失败")}
                      </span>
                    ))}
                  <TrustBadge
                    trusted={Boolean(trustedMap[c.id])}
                    busy={trustBusy.has(c.id)}
                    onToggle={() => void toggleTrusted(c.id)}
                  />
                </div>
                {/* 描述复用精选区（连接后不丢）：精选静态描述优先，回落云端
                    enrich 的 toolkit description；两行钳制显示不全直接截断 */}
                <span
                  className="data-card-desc"
                  title={featured?.description ?? c.description ?? undefined}
                >
                  {featured?.description ?? c.description ?? ""}
                </span>
                <div className="data-row-actions">
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={testingIds.has(c.id)}
                    onClick={() => void handleTestConnection(c)}
                  >
                    {testingIds.has(c.id) ? "测试中…" : "测试"}
                  </button>
                  <button type="button" className="data-btn ghost" onClick={() => void handleToggleConnection(c)}>
                    {c.enabled ? "停用" : "启用"}
                  </button>
                  <ConfirmPopover
                    message={`删除「${testName}」的连接？删除后需重新授权`}
                    onConfirm={() => handleDisconnect(c.id)}
                    buttonClass="data-btn ghost"
                    buttonTitle="删除连接"
                    label="删除"
                  />
                </div>
              </div>
            );
          })}
        </div>

        {/* 本地文件夹配置（配置按钮弹出模态；关闭丢弃未保存编辑） */}
        {folderOpen && (
          <FormModal title="配置本地文件夹" onClose={() => setFolderOpen(false)}>
            <div className="local-folder-panel">
              <div className="mcp-field">
              <span className="mcp-field-label">文件夹目录（最多 10 个）</span>
                {folderPaths.map((p, i) => (
                  <div key={i} className="local-folder-path-row">
                    <input
                      className="mcp-input mono"
                      value={p}
                      maxLength={500}
                      placeholder="如：D:\Documents\Clara"
                      onChange={(e) => handlePathChange(i, e.target.value)}
                    />
                    {isTauri() && (
                      <button
                        type="button"
                        className="data-btn ghost"
                        onClick={() => handleBrowse(i)}
                      >
                        浏览
                      </button>
                    )}
                    <button
                      type="button"
                      className="local-folder-remove"
                      title="移除该目录"
                      onClick={() => handleRemovePath(i)}
                    >
                      ×
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="local-folder-add"
                  disabled={folderPaths.length >= 10}
                  onClick={handleAddPath}
                >
                  + 添加目录
                </button>
                <span className="mcp-field-hint">
                  保存后经卡上「启用」按钮开启。开启后 AI 可读取所有已配置文件夹内的文件
                  （word / pdf / excel 等自动解析为 Markdown），单文件上限 10MB
                </span>
              </div>
              {folderError && <span className="mcp-form-error">{folderError}</span>}
              <div className="mcp-form-footer">
                <div className="mcp-form-test">
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={folderTesting}
                    onClick={handleTestFolder}
                  >
                    {folderTesting ? "测试中…" : "测试文件夹"}
                  </button>
                  {folderTestResult && (
                    <span className={`mcp-test-result ${folderTestOk ? "ok" : "bad"}`}>
                      {folderTestResult}
                    </span>
                  )}
                </div>
                <div className="mcp-form-actions">
                  <button
                    type="button"
                    className="data-btn ghost"
                    onClick={() => setFolderOpen(false)}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="data-btn primary"
                    disabled={folderSaving}
                    onClick={handleSaveFolder}
                  >
                    {folderSaving ? "保存中…" : "保存"}
                  </button>
                </div>
              </div>
            </div>
          </FormModal>
        )}

        {/* MCP 表单（模态三态互斥）：手动添加 / 编辑行 / 精选模板接入，标题在
            模态头。key 锚定目标（行 id / 模板 id）——表单开着直切另一目标时
            强制重挂，草稿不串台 */}
        {formMode?.kind === "manual" && (
          <FormModal title="添加 MCP 服务" onClose={() => setFormMode(null)}>
            <McpServerForm server={null} onClose={() => setFormMode(null)} onSaved={handleFormSaved} />
          </FormModal>
        )}
        {formMode?.kind === "edit" && (
          <FormModal title="编辑 MCP 服务" onClose={() => setFormMode(null)}>
            <McpServerForm
              key={formMode.server.id}
              server={formMode.server}
              onClose={() => setFormMode(null)}
              onSaved={handleFormSaved}
            />
          </FormModal>
        )}
        {formMode?.kind === "template" && matchTemplate(formMode.id) && (
          <FormModal
            title={`接入「${matchTemplate(formMode.id)!.defaultName}」`}
            onClose={() => setFormMode(null)}
          >
            <McpTemplateForm
              key={formMode.id}
              template={matchTemplate(formMode.id)!}
              onClose={() => setFormMode(null)}
              onSaved={handleFormSaved}
            />
          </FormModal>
        )}

        {/* CLI 接入模态（精选卡「连接应用」唯一入口；key 锚定 adapter_id——直切
            另一工具时强制重挂，探测/授权草稿不串台）。双通道提示：同名 MCP 服务
            （钉钉）已接入时模态内说明数据重复 */}
        {cliOpenId &&
          (() => {
            const tool = (cliTools ?? []).find(
              (t) => t.adapter_id === cliOpenId && !t.custom,
            );
            if (!tool) return null;
            const dualName = CLI_DUAL_MCP_NAMES[cliOpenId];
            const dual = dualName !== undefined && (mcpServers ?? []).some((r) => r.name === dualName);
            return (
              <CliConnectModal
                key={tool.adapter_id}
                tool={tool}
                dualChannelHint={dual}
                onClose={() => setCliOpenId(null)}
                onConnected={handleCliConnected}
              />
            );
          })()}

        {/* 自定义只读命令表单（入口在页脚 geek 行）。收尾须额外关自己的开态：
            handleCliConnected 只关 cliOpenId 接入模态——曾致保存成功后表单不关闭 */}
        {customCliOpen && (
          <CustomCliForm
            onClose={() => setCustomCliOpen(false)}
            onSaved={(row) => {
              handleCliConnected(row);
              setCustomCliOpen(false);
            }}
          />
        )}

        {/* ics 日历导入模态（入口在页脚 geek 行）：选文件 → 导入；成功即关弹窗
            （提示条在 geek 行上方），失败详情留弹窗内供重选重试。类名全套复用
            本地文件夹模态 */}
        {icsOpen && (
          <FormModal title="导入 ics 日历" onClose={() => setIcsOpen(false)}>
            <div className="local-folder-panel">
              <div className="mcp-field">
                <span className="mcp-field-label">日历文件（.ics）</span>
                <div className="local-folder-path-row">
                  <input
                    className="mcp-input mono"
                    readOnly
                    value={icsPath ?? ""}
                    placeholder="选择 .ics 日历文件"
                  />
                  {isTauri() && (
                    <button type="button" className="data-btn ghost" onClick={handlePickIcs}>
                      选择文件
                    </button>
                  )}
                </div>
                <span className="mcp-field-hint">
                  日程（含全天 / 重复 / 过去事件）直接创建为日历日程，待办进入收件箱；
                  同一文件重复导入会自动跳过已导入条目
                </span>
                {!isTauri() && (
                  <span className="mcp-form-error">文件选择需在桌面应用中使用</span>
                )}
              </div>
              {icsError && <span className="mcp-form-error">{icsError}</span>}
              <div className="mcp-form-footer">
                <div />
                <div className="mcp-form-actions">
                  <button type="button" className="data-btn ghost" onClick={() => setIcsOpen(false)}>
                    关闭
                  </button>
                  <button
                    type="button"
                    className="data-btn primary"
                    disabled={!icsPath || icsBusy}
                    onClick={handleImportIcs}
                  >
                    {icsBusy ? "导入中…" : "导入"}
                  </button>
                </div>
              </div>
            </div>
          </FormModal>
        )}

        {/* 自定义行「配置」编辑模态（key 锚定行——响应整行入列后关闭） */}
        {customEditId &&
          (() => {
            const tool = (cliTools ?? []).find(
              (t) => t.adapter_id === customEditId && t.custom,
            );
            if (!tool) return null;
            return (
              <CustomCliForm
                key={tool.adapter_id}
                initial={tool}
                onClose={() => setCustomEditId(null)}
                onSaved={(row) => {
                  handleCliConnected(row);
                  setCustomEditId(null);
                }}
              />
            );
          })()}
      </div>

      <div className="settings-group">
        <div className="data-featured-head">
          <span className="settings-group-title">精选效率工具</span>
          <span className="data-featured-badge">
            <Flame size={11} strokeWidth={2} />
            推荐接入
          </span>
          <div
            className="data-search"
            title={keyReady ? "搜索 Composio 支持的全部应用" : "先在上方配置 Composio API Key"}
          >
            <Search size={13} strokeWidth={2} />
            <input
              className="data-search-input"
              value={searchInput}
              maxLength={60}
              placeholder={keyReady ? "搜索 1500+ Composio 应用" : "需先配置 API Key"}
              disabled={!keyReady}
              onChange={(e) => {
                setSearchInput(e.target.value);
                setSearchResults(null); // 输入变化即失效旧结果（渲染层回落"搜索中…"）
              }}
            />
            {searchInput && (
              <button
                type="button"
                className="data-search-clear"
                title="清空搜索"
                onClick={() => {
                  setSearchInput("");
                  setSearchResults(null);
                }}
              >
                ×
              </button>
            )}
          </div>
        </div>
        <div className="data-grid">
          {query ? (
            searching || searchResults === null ? (
              <div className="data-empty">搜索中…</div>
            ) : searchVisible.length === 0 ? (
              <div className="data-empty">未找到匹配的应用</div>
            ) : (
              searchVisible.map((a) => (
                <div key={a.slug} className="data-card data-card-col">
                  <div className="data-card-col-head">
                    <span className={`data-icon ${matchFeatured(a.slug)?.id ?? ""}`}>
                      <AppIcon slug={a.slug} name={a.name} logo={a.logo} />
                    </span>
                    <span className="data-pill blue">Composio</span>
                  </div>
                  <span className="data-card-name" title={a.name}>
                    {a.name}
                  </span>
                  <span className="data-card-desc" title={a.description ?? undefined}>
                    {a.description ?? ""}
                  </span>
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={connectingSlug !== null}
                    onClick={() => handleConnect(a.slug)}
                  >
                    {connectingSlug === a.slug ? "连接中…" : "连接应用"}
                  </button>
                </div>
              ))
            )
          ) : (
            featuredVisible.map((s) => (
              <div key={s.id} className="data-card data-card-col">
                <div className="data-card-col-head">
                  <span className={`data-icon ${s.id}`}>{s.icon}</span>
                  <span className={`data-pill ${CHANNELS[s.channel].tone}`}>
                    {CHANNELS[s.channel].label}
                  </span>
                </div>
                <span className="data-card-name" title={s.name}>{s.name}</span>
                <span className="data-card-desc" title={s.description}>{s.description}</span>
                {s.channel === "composio" ? (
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={!keyReady || connectingSlug !== null}
                    title={keyReady ? undefined : "先在上方配置 Composio API Key"}
                    onClick={() => handleConnect(s.appId ?? "")}
                  >
                    {connectingSlug === s.appId ? "连接中…" : "连接应用"}
                  </button>
                ) : s.channel === "cli" ? (
                  <button
                    type="button"
                    className="data-btn ghost"
                    onClick={() => setCliOpenId(s.cliId ?? "")}
                  >
                    连接应用
                  </button>
                ) : (
                  <button
                    type="button"
                    className="data-btn ghost"
                    onClick={() => setFormMode({ kind: "template", id: s.id })}
                  >
                    连接应用
                  </button>
                )}
              </div>
            ))
          )}
        </div>
        {actionError && <div className="data-action-error">{actionError}</div>}
        {icsNotice && <div className="data-action-ok">{icsNotice}</div>}
      </div>

      <div className="data-geek">
        <span>找不到需要的服务？</span>
        <a
          className="data-geek-link"
          href={MCP_REGISTRY_URL}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => {
            e.preventDefault();
            openExternal(MCP_REGISTRY_URL);
          }}
        >
          探索更多
        </a>
        <span>或</span>
        <button
          type="button"
          className="data-geek-link"
          onClick={() => setFormMode({ kind: "manual" })}
        >
          手动配置 MCP Server
        </button>
        <span>或</span>
        <button
          type="button"
          className="data-geek-link"
          onClick={() => setCustomCliOpen(true)}
        >
          添加自定义只读命令
        </button>
        <span>或</span>
        <button type="button" className="data-geek-link" onClick={openIcsModal}>
          导入 ics 日历
        </button>
      </div>
    </div>
  );
}

export default DataSettings;
