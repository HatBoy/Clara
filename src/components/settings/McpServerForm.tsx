// MCP 服务手动添加 / 编辑行双态共用表单（容器为 DataSettings 的 FormModal 模态，
// 标题在模态头；字段结构仿 ModelSettings 内联表单）。stdio = 命令 + 参数
// （textarea 每行一个参数，免引号地狱）+ 环境变量行编辑器；http/sse = URL +
// 请求头行编辑器。编辑模式发全量字段（切换 transport 后对侧字段显式 null
// 清空，交后端合并态校验）。
// 手动添加态另有双配置模式（2026-09-19 增）：表单配置（上述图形化字段）/
// JSON 配置（粘贴 Cursor / Claude Desktop 等标准 MCP JSON，前端解析为标准
// 请求体——后端零解析知识，测试与保存全复用既有 API）；名称 + 描述两模式共享。
// JSON 模式仅添加态可用（粘贴外部配置是初次接入场景，编辑态数据已结构化）。
import { Eye, EyeOff } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  createMcpServer,
  testMcpServerDraft,
  updateMcpServer,
  type ApiMcpServer,
  type McpDraftBody,
  type McpServerBody,
  type McpTransport,
} from "../../lib/api";
import { SetSegmented } from "./SettingsControls";
import "./McpServerForm.css";

// 键值行（env / headers 共用编辑器形状）
type KvRow = { key: string; value: string };

const rowsFrom = (obj: Record<string, string> | null): KvRow[] =>
  Object.entries(obj ?? {}).map(([key, value]) => ({ key, value }));

// 空键行丢弃；全空返回 undefined（不发该键）
const rowsToRecord = (rows: KvRow[]): Record<string, string> | undefined => {
  const out: Record<string, string> = {};
  for (const r of rows) {
    if (r.key.trim()) out[r.key.trim()] = r.value;
  }
  return Object.keys(out).length ? out : undefined;
};

const TRANSPORT_OPTIONS = [
  { value: "stdio", label: "标准 IO" },
  { value: "streamable_http", label: "HTTP" },
  { value: "sse", label: "SSE" },
] as const;

// 参数 textarea 文本 ↔ 参数数组
const argsToText = (args: string[] | null) => (args ?? []).join("\n");
const textToArgs = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

// —— JSON 配置模式：标准 MCP JSON → 创建请求体（纯前端解析）——

// 添加态配置方式切换
const MODE_OPTIONS = [
  { value: "form", label: "表单配置" },
  { value: "json", label: "JSON 配置" },
] as const;

// MCP 配置的已知配置键（白名单外的键如 alwaysAllow 收集提示，不报错不导入）
const KNOWN_CFG_KEYS = new Set(["type", "command", "args", "env", "url", "headers", "disabled"]);

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

// env/headers 共用解析：值必须全为字符串（数值/嵌套对象报错，不静默强转）；
// 空对象归 undefined（不发键，与表单模式 rowsToRecord 口径一致）
function parseStringRecord(v: unknown, label: string): Record<string, string> | undefined {
  if (!isPlainObject(v)) throw new Error(`${label} 必须是对象`);
  for (const val of Object.values(v)) {
    if (typeof val !== "string") throw new Error(`${label} 的值必须全是字符串`);
  }
  return Object.keys(v).length ? (v as Record<string, string>) : undefined;
}

// JSON 解析结果：成功携带标准请求体 + 概要信息；失败携带中文错误
type JsonParseResult =
  | { ok: true; name: string; body: McpServerBody; extraCount: number; ignored: string[] }
  | { ok: false; error: string };

/**
 * 解析标准 MCP JSON 配置（永不抛出——失败是数据）。支持三种形态：
 * ① {"mcpServers": {名: 配置}} 标准格式；② 顶层即单服务裸配置（键全属已知
 * 配置键，无服务名）；③ 顶层 {名: 配置} 映射（只复制了 mcpServers 的内容）。
 * 多服务取第一个导入（extraCount 提示）；"http"/"streamable-http" 映射
 * streamable_http；type 缺省时有 command 判 stdio、有 url 判 http；
 * disabled: true → enabled: false。白名单外键收集进 ignored 提示。
 */
function parseMcpJson(text: string): JsonParseResult {
  try {
    return { ..._parse(text), ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "配置解析失败" };
  }
}

function _parse(text: string): Omit<Extract<JsonParseResult, { ok: true }>, "ok"> {
  let root: unknown;
  try {
    root = JSON.parse(text);
  } catch {
    throw new Error("JSON 语法错误，请检查格式");
  }
  if (!isPlainObject(root)) throw new Error("配置必须是一个 JSON 对象");

  let name = "";
  let cfg: Record<string, unknown>;
  let extraCount = 0;
  if (isPlainObject(root.mcpServers)) {
    const entries = Object.entries(root.mcpServers);
    if (!entries.length) throw new Error("mcpServers 内未找到服务配置");
    const firstCfg = entries[0][1];
    if (!isPlainObject(firstCfg)) throw new Error("服务配置必须是对象");
    name = entries[0][0];
    cfg = firstCfg;
    extraCount = entries.length - 1;
  } else if (Object.keys(root).length > 0 && Object.keys(root).every((k) => KNOWN_CFG_KEYS.has(k))) {
    cfg = root; // 裸配置（无服务名，名称须用户自填）
  } else {
    const first = Object.entries(root).find(([, v]) => isPlainObject(v));
    if (!first) throw new Error('未找到服务配置（需为 {"mcpServers": {...}} 格式）');
    name = first[0];
    cfg = first[1] as Record<string, unknown>;
    extraCount = Object.entries(root).length - 1;
  }

  const rawType = typeof cfg.type === "string" ? cfg.type.trim().toLowerCase() : undefined;
  let transport: McpTransport;
  if (rawType === "stdio" || rawType === "sse" || rawType === "streamable_http") {
    transport = rawType;
  } else if (rawType === "http" || rawType === "streamable-http") {
    transport = "streamable_http"; // 生态惯写（Cursor / VS Code 用 "http"）
  } else if (rawType === undefined) {
    // 老格式（Claude Desktop）无 type 字段：有 command 判 stdio、有 url 判 http
    if (cfg.command !== undefined) transport = "stdio";
    else if (cfg.url !== undefined) transport = "streamable_http";
    else throw new Error("缺少 command 或 url，无法判断服务类型");
  } else {
    throw new Error(`不支持的传输类型：${rawType}`);
  }

  const body: McpServerBody = { transport, enabled: cfg.disabled !== true };
  if (transport === "stdio") {
    if (typeof cfg.command !== "string" || !cfg.command.trim()) {
      throw new Error("stdio 服务的 command 必须是非空字符串");
    }
    body.command = cfg.command.trim();
    if (cfg.args !== undefined) {
      if (!Array.isArray(cfg.args) || cfg.args.some((a) => typeof a !== "string")) {
        throw new Error("args 必须是字符串数组");
      }
      const args = (cfg.args as string[]).map((a) => a.trim()).filter(Boolean);
      if (args.length) body.args = args;
    }
    if (cfg.env !== undefined) body.env = parseStringRecord(cfg.env, "env");
  } else {
    if (typeof cfg.url !== "string" || !cfg.url.trim()) {
      throw new Error("该传输方式的 url 必须是非空字符串");
    }
    body.url = cfg.url.trim();
    if (cfg.headers !== undefined) body.headers = parseStringRecord(cfg.headers, "headers");
  }
  return { name, body, extraCount, ignored: Object.keys(cfg).filter((k) => !KNOWN_CFG_KEYS.has(k)) };
}

export function McpServerForm({
  server,
  onClose,
  onSaved,
}: {
  server: ApiMcpServer | null; // null = 手动添加；行 = 编辑
  onClose: () => void;
  onSaved: (row: ApiMcpServer, isNew: boolean) => void;
}) {
  const editing = server !== null;
  const [name, setName] = useState(server?.name ?? "");
  const [description, setDescription] = useState(server?.description ?? "");
  const [transport, setTransport] = useState<McpTransport>(server?.transport ?? "stdio");
  const [command, setCommand] = useState(server?.command ?? "");
  const [argsText, setArgsText] = useState(argsToText(server?.args ?? null));
  const [url, setUrl] = useState(server?.url ?? "");
  const [envRows, setEnvRows] = useState<KvRow[]>(rowsFrom(server?.env ?? null));
  const [headerRows, setHeaderRows] = useState<KvRow[]>(rowsFrom(server?.headers ?? null));
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [showEnv, setShowEnv] = useState(false); // env 值显隐（凭据可能落在 env）
  // JSON 配置模式（仅添加态可切；两模式 state 各自保留，切换不回填——JSON 模式
  // 自身可完整编辑，反向同步徒增隐式行为）
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testOk, setTestOk] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // JSON 草稿实时解析（useMemo 派生无同步问题）：空文本不解析不报错
  const jsonParse = useMemo(
    () => (jsonMode && jsonText.trim() ? parseMcpJson(jsonText) : null),
    [jsonMode, jsonText],
  );

  // 解析成功且名称框为空时预填 JSON 里的服务名（用户手改过/清空后不覆盖——
  // jsonText 不变则 jsonParse 引用稳定，effect 不重跑）
  useEffect(() => {
    if (jsonParse?.ok && !name.trim() && jsonParse.name) setName(jsonParse.name);
  }, [jsonParse]);

  // 解析成功概要（JSON 模式反馈行）：传输摘要 + 多服务/忽略字段/停用提示
  const jsonSummary = jsonParse?.ok
    ? [
        jsonParse.body.transport === "stdio"
          ? `stdio · ${[jsonParse.body.command, ...(jsonParse.body.args ?? []).slice(0, 3)].filter(Boolean).join(" ")}`
          : `${jsonParse.body.transport === "sse" ? "SSE" : "HTTP"} · ${jsonParse.body.url}`,
        ...(jsonParse.extraCount > 0 ? [`检测到 ${jsonParse.extraCount + 1} 个服务，将导入第一个`] : []),
        ...(jsonParse.ignored.length ? [`已忽略字段：${jsonParse.ignored.join("、")}`] : []),
        ...(jsonParse.body.enabled === false ? ["配置标记 disabled，将以停用状态导入"] : []),
      ].join("；")
    : null;

  const isStdio = transport === "stdio";
  const formCanSave =
    name.trim().length > 0 && (isStdio ? command.trim().length > 0 : url.trim().length > 0);
  const canSave = jsonMode ? Boolean(jsonParse?.ok && name.trim()) : formCanSave;

  // 当前草稿 → 草稿探测入参（探测不发 name/enabled）
  const draftBody = (): McpDraftBody => {
    const body: McpDraftBody = { transport };
    if (isStdio) {
      body.command = command.trim();
      const args = textToArgs(argsText);
      if (args.length) body.args = args;
      const env = rowsToRecord(envRows);
      if (env) body.env = env;
    } else {
      body.url = url.trim();
      const headers = rowsToRecord(headerRows);
      if (headers) body.headers = headers;
    }
    return body;
  };

  // JSON 模式：解析结果 → 草稿探测入参（同 draftBody 口径，不发 name/description/enabled）
  const jsonDraft = (): McpDraftBody | null => {
    if (!jsonParse?.ok) return null;
    const { name: _n, description: _d, enabled: _e, ...rest } = jsonParse.body;
    return rest as McpDraftBody;
  };

  const handleTest = async () => {
    if (testing) return;
    const draft = jsonMode ? jsonDraft() : draftBody();
    if (!draft) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await testMcpServerDraft(draft);
      setTestOk(r.ok);
      setTestResult(
        r.ok ? `连通 · ${r.latency_ms}ms · ${r.tools_count} 个工具 · 未验证凭据` : r.error,
      );
    } catch (e) {
      setTestOk(false);
      setTestResult(
        (e as Error).name === "AbortError" ? "测试超时，请稍后重试" : "请求失败：后端服务不可用",
      );
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (!canSave || saving) return;
    setSaving(true);
    setFormError(null);
    try {
      if (jsonMode) {
        // JSON 模式（仅添加态）：解析体自带技术字段与 enabled（disabled 映射）
        const parsed = jsonParse;
        if (!parsed?.ok) return;
        const row = await createMcpServer({
          name: name.trim(),
          description: description.trim() || null,
          ...parsed.body,
        });
        onSaved(row, true);
        return;
      }
      const body: McpServerBody = {
        name: name.trim(),
        description: description.trim() || null, // 空 → 显式 null（编辑态清空口径）
        enabled,
        ...draftBody(),
      };
      // 编辑发全量：对侧字段显式 null 清空（后端切 transport 也会清，双保险）；
      // 当前分支删光的字段（参数/env/headers 全空时 draftBody 不发键）同样补
      // null 显式清空——否则"键不出现 = 不动"会让删光操作静默失效
      if (editing) {
        if (isStdio) {
          body.url = null;
          body.args = body.args ?? null;
          body.env = body.env ?? null;
        } else {
          body.command = null;
          body.args = null;
          body.env = null;
          body.headers = body.headers ?? null;
        }
      }
      const row = editing
        ? await updateMcpServer(server.id, body)
        : await createMcpServer(body);
      onSaved(row, !editing);
    } catch (e) {
      if ((e as Error & { status?: number }).status === 409) {
        setFormError("显示名称已存在，请换一个");
      } else {
        setFormError("保存失败：请确认后端服务正在运行");
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mcp-form">
      <div className="mcp-field">
        <span className="mcp-field-label">显示名称</span>
        <input
          className="mcp-input"
          value={name}
          maxLength={30}
          placeholder="如：团队日历"
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div className="mcp-field">
        <span className="mcp-field-label">描述（选填）</span>
        <input
          className="mcp-input"
          value={description}
          maxLength={100}
          placeholder="该服务的用途说明，最多 100 字"
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>

      {/* 配置方式切换仅添加态（编辑态数据已结构化，保持纯表单）；切换不回填 */}
      {!editing && (
        <div className="mcp-field">
          <span className="mcp-field-label">配置方式</span>
          <SetSegmented
            value={jsonMode ? "json" : "form"}
            options={MODE_OPTIONS}
            onChange={(v) => setJsonMode(v === "json")}
          />
        </div>
      )}

      {jsonMode ? (
        <div className="mcp-field">
          <span className="mcp-field-label">JSON 配置</span>
          <textarea
            className="mcp-json-textarea mono"
            value={jsonText}
            spellCheck={false}
            placeholder={
              '粘贴标准 MCP JSON 配置，如：\n{\n  "mcpServers": {\n    "dida365": {\n      "type": "http",\n      "url": "https://mcp.dida365.com"\n    }\n  }\n}'
            }
            onChange={(e) => setJsonText(e.target.value)}
          />
          {jsonParse === null ? (
            <span className="mcp-field-hint">
              支持标准 mcpServers 格式与单服务裸配置；仅导入 command / args / env / url / headers 字段
            </span>
          ) : jsonParse.ok ? (
            <span className="mcp-field-hint">{jsonSummary}</span>
          ) : (
            <span className="mcp-form-error">{jsonParse.error}</span>
          )}
        </div>
      ) : (
        <>
          <div className="mcp-field">
            <span className="mcp-field-label">传输方式</span>
            <SetSegmented value={transport} options={TRANSPORT_OPTIONS} onChange={setTransport} />
          </div>

      {isStdio ? (
        <>
          <div className="mcp-field">
            <span className="mcp-field-label">命令</span>
            <input
              className="mcp-input mono"
              value={command}
              maxLength={200}
              placeholder="如：npx / uvx / python"
              onChange={(e) => setCommand(e.target.value)}
            />
            <span className="mcp-field-hint">
              本地命令型服务（npm 包用 npx 按需运行，Python 包用 uvx）
            </span>
          </div>
          <div className="mcp-field">
            <span className="mcp-field-label">参数</span>
            <textarea
              className="mcp-args-textarea mono"
              value={argsText}
              placeholder={"每行一个参数，如：\n-y\n@modelcontextprotocol/server-filesystem\nD:/Docs"}
              onChange={(e) => setArgsText(e.target.value)}
            />
            <span className="mcp-field-hint">每行一个参数（无需引号），空行忽略</span>
          </div>
          <div className="mcp-field">
            <span className="mcp-field-label">环境变量</span>
            <KvEditor
              rows={envRows}
              onChange={setEnvRows}
              keyPlaceholder="如：API_KEY"
              masked={!showEnv}
            />
            {envRows.some((r) => r.key.trim()) && (
              <button
                type="button"
                className="mcp-eye-btn"
                onClick={() => setShowEnv((v) => !v)}
              >
                {showEnv ? <EyeOff size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
                {showEnv ? " 隐藏变量值" : " 显示变量值"}
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="mcp-field">
            <span className="mcp-field-label">服务 URL</span>
            <input
              className="mcp-input mono"
              value={url}
              maxLength={500}
              placeholder="https://example.com/mcp"
              onChange={(e) => setUrl(e.target.value)}
            />
            <span className="mcp-field-hint">MCP 端点完整地址（Streamable HTTP 或 SSE）</span>
          </div>
          <div className="mcp-field">
            <span className="mcp-field-label">请求头</span>
            <KvEditor
              rows={headerRows}
              onChange={setHeaderRows}
              keyPlaceholder="如：Authorization"
              masked={false}
            />
            <span className="mcp-field-hint">
              鉴权头示例：Authorization → Bearer &lt;token&gt;
            </span>
          </div>
        </>
      )}
        </>
      )}

      {editing && (
        <div className="mcp-form-toggle">
          <span className="mcp-field-label">启用服务</span>
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            className={`set-toggle${enabled ? " on" : ""}`}
            onClick={() => setEnabled((v) => !v)}
          />
          <span className="mcp-field-hint">停用仅不装载给 AI，配置保留、仍可测试</span>
        </div>
      )}

      {formError && <span className="mcp-form-error">{formError}</span>}

      <div className="mcp-form-footer">
        <div className="mcp-form-test">
          <button
            type="button"
            className="data-btn ghost"
            disabled={testing || !canSave}
            onClick={handleTest}
          >
            {testing ? "测试中…" : "测试连通性"}
          </button>
          {testResult && (
            <span className={`mcp-test-result ${testOk ? "ok" : "bad"}`}>{testResult}</span>
          )}
        </div>
        <div className="mcp-form-actions">
          <button type="button" className="data-btn ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="data-btn primary"
            disabled={!canSave || saving}
            onClick={handleSave}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

// 键值行编辑器（env / headers 共用）：key + value 两输入 + 删除 ×，底部「+ 添加一项」
function KvEditor({
  rows,
  onChange,
  keyPlaceholder,
  masked,
}: {
  rows: KvRow[];
  onChange: (rows: KvRow[]) => void;
  keyPlaceholder: string;
  masked: boolean;
}) {
  return (
    <div className="kv-editor">
      {rows.map((r, i) => (
        <div key={i} className="kv-row">
          <input
            className="mcp-input mono kv-key"
            value={r.key}
            maxLength={100}
            placeholder={keyPlaceholder}
            onChange={(e) => {
              const next = [...rows];
              next[i] = { ...r, key: e.target.value };
              onChange(next);
            }}
          />
          <input
            className="mcp-input mono"
            type={masked ? "password" : "text"}
            value={r.value}
            maxLength={2000}
            placeholder="值"
            onChange={(e) => {
              const next = [...rows];
              next[i] = { ...r, value: e.target.value };
              onChange(next);
            }}
          />
          <button
            type="button"
            className="kv-del"
            title="删除该项"
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="kv-add" onClick={() => onChange([...rows, { key: "", value: "" }])}>
        + 添加一项
      </button>
    </div>
  );
}

export default McpServerForm;
