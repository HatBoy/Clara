// 精选 MCP 应用的预制模板（纯前端常量——后端零精选知识，精选与手动添加同走
// 通用 CRUD，仅配置预填差异）。三模板真实配置已逐一调研确认：
// - 钉钉：官方 dingtalk-mcp，stdio + npx，凭据走环境变量（Client ID/Secret）
// - Obsidian：Local REST API 社区插件的 MCP 端点直连（用户须先在插件设置中启用
//   非加密 HTTP 端口 27123——默认 HTTPS 27124 是自签证书，httpx 校验必失败）
// - 滴答清单国内版：官方云端端点，静态 Bearer token（可选——无 token 可达但能力受限）
// （飞书模板已随飞书 MCP 通道于 2026-10-16 全链路移除——接入走飞书 CLI 卡）
import type { McpTransport, McpServerBody } from "../../lib/api";

// 模板里的一个命名输入项（secret 用 password 输入 + 眼睛显隐）
export type McpFieldDef = {
  id: string;
  label: string;
  secret?: boolean;
  required?: boolean;
  hint?: string;
  placeholder?: string;
};

export type McpTemplate = {
  id: string; // 对齐 FEATURED_SOURCES 精选卡 id（dingtalk/obsidian/ticktick-cn）
  defaultName: string; // 预填显示名称；同时是"已接入则隐藏精选卡"的匹配键
  transport: McpTransport;
  command?: string; // stdio 固定命令
  args?: string[]; // stdio 参数，可含 {{fieldId}} 占位
  envKeys?: Record<string, string>; // 固定环境变量（不含用户输入）
  envFields?: McpFieldDef[]; // 环境变量输入项（id = env 键名）
  url?: string; // http 端点
  headerFields?: McpFieldDef[]; // 请求头输入项（id = header 名，如 Authorization）
  fixedHeaderTemplate?: Record<string, string>; // header 值模板，如 { Authorization: "Bearer {{token}}" }
};

export const MCP_TEMPLATES: McpTemplate[] = [
  {
    id: "dingtalk",
    defaultName: "钉钉",
    transport: "stdio",
    command: "npx",
    args: ["-y", "dingtalk-mcp@latest"],
    envKeys: { ACTIVE_PROFILES: "dingtalk-contacts,dingtalk-calendar" },
    envFields: [
      {
        id: "DINGTALK_Client_ID",
        label: "Client ID",
        required: true,
        placeholder: "dingxxxxxxxx",
        hint: "钉钉开放平台 → 应用开发 → 凭证与基础信息",
      },
      {
        id: "DINGTALK_Client_Secret",
        label: "Client Secret",
        secret: true,
        required: true,
      },
    ],
  },
  {
    id: "obsidian",
    defaultName: "Obsidian",
    transport: "streamable_http",
    url: "http://127.0.0.1:27123/mcp/",
    headerFields: [
      {
        id: "token",
        label: "API Key",
        secret: true,
        required: true,
        hint: "前置步骤：Obsidian 安装 Local REST API 社区插件 → 其设置中启用非加密 HTTP 端口 27123（默认 HTTPS 27124 为自签证书无法直连）→ 复制 API Key 填入此处",
      },
    ],
    fixedHeaderTemplate: { Authorization: "Bearer {{token}}" },
  },
  {
    id: "ticktick-cn",
    defaultName: "滴答清单 (国内版)",
    transport: "streamable_http",
    url: "https://mcp.dida365.com",
    headerFields: [
      {
        id: "token",
        label: "Bearer Token",
        secret: true,
        hint: "可选——滴答清单网页版设置中生成；无 Token 时服务可达但能力受限",
      },
    ],
    fixedHeaderTemplate: { Authorization: "Bearer {{token}}" },
  },
];

export const matchTemplate = (id: string) => MCP_TEMPLATES.find((t) => t.id === id);

// 已接入服务行（按 name 匹配模板 defaultName）→ 对应模板；供已连接卡渲染品牌图标
export const templateByName = (name: string) =>
  MCP_TEMPLATES.find((t) => t.defaultName === name);

/**
 * 模板 + 用户输入 → 创建请求体。占位替换规则：
 * - args 里的 {{fieldId}} 逐一替换为对应输入值
 * - env = 固定 envKeys + envFields 中非空的值
 * - headers = fixedHeaderTemplate 模板替换后，值里仍有未填占位（空字段）的整项丢弃
 * - enabled 恒 true（从精选接入即启用）
 */
export function renderTemplateBody(
  t: McpTemplate,
  name: string,
  values: Record<string, string>,
): McpServerBody {
  const fill = (s: string) => s.replace(/\{\{(\w+)\}\}/g, (_, id: string) => values[id] ?? "");
  const body: McpServerBody = { name, transport: t.transport, enabled: true };
  if (t.transport === "stdio") {
    body.command = t.command;
    body.args = (t.args ?? []).map(fill);
    const env: Record<string, string> = { ...(t.envKeys ?? {}) };
    for (const f of t.envFields ?? []) {
      if (values[f.id]) env[f.id] = values[f.id];
    }
    body.env = env;
  } else {
    body.url = t.url;
    const headers: Record<string, string> = {};
    for (const [k, tpl] of Object.entries(t.fixedHeaderTemplate ?? {})) {
      const v = fill(tpl);
      if (!v.includes("{{")) headers[k] = v; // 未填占位整项丢弃（如无 token 时不发 Authorization）
    }
    if (Object.keys(headers).length) body.headers = headers;
  }
  return body;
}

// 缺失的必填字段 label 列表（保存按钮禁用判定）
export function templateMissingRequired(t: McpTemplate, values: Record<string, string>): string[] {
  const fields = [...(t.envFields ?? []), ...(t.headerFields ?? [])];
  return fields.filter((f) => f.required && !values[f.id]).map((f) => f.label);
}
