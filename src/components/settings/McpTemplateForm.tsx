// 精选 MCP 应用的快速接入表单（容器为 DataSettings 的 FormModal 模态，标题在
// 模态头）：模板声明的命名输入项（secret 用 password + 眼睛）+ 只读配置预览
// （「高级」折叠）+ 草稿测试 + 保存。与手动添加同走 createMcpServer，差异仅在
// 配置由模板预填、用户只补凭据。
import { ChevronDown, Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import {
  createMcpServer,
  testMcpServerDraft,
  type ApiMcpServer,
  type McpDraftBody,
} from "../../lib/api";
import { renderTemplateBody, templateMissingRequired, type McpTemplate } from "./mcpTemplates";
import "./McpServerForm.css";

export function McpTemplateForm({
  template,
  onClose,
  onSaved,
}: {
  template: McpTemplate;
  onClose: () => void;
  onSaved: (row: ApiMcpServer, isNew: boolean) => void;
}) {
  const [name, setName] = useState(template.defaultName);
  const [values, setValues] = useState<Record<string, string>>({});
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [advOpen, setAdvOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testOk, setTestOk] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const fields = [...(template.envFields ?? []), ...(template.headerFields ?? [])];
  const missing = name.trim() ? templateMissingRequired(template, values) : ["显示名称"];
  const canSave = missing.length === 0;

  const draft = () => renderTemplateBody(template, name.trim(), values);

  const handleTest = async () => {
    if (testing || !canSave) return;
    setTesting(true);
    setTestResult(null);
    try {
      // 探测不发 name/enabled；renderTemplateBody 恒设 transport，断言安全
      const { name: _n, enabled: _e, ...rest } = draft();
      const r = await testMcpServerDraft(rest as McpDraftBody);
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
      const row = await createMcpServer(draft());
      onSaved(row, true);
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
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      {fields.map((f) => (
        <div key={f.id} className="mcp-field">
          <span className="mcp-field-label">
            {f.label}
            {f.required ? "（必填）" : "（可选）"}
          </span>
          <div className="mcp-key-wrap">
            <input
              className="mcp-input mono"
              type={f.secret && !shown[f.id] ? "password" : "text"}
              value={values[f.id] ?? ""}
              maxLength={2000}
              placeholder={f.placeholder}
              onChange={(e) => setValues((v) => ({ ...v, [f.id]: e.target.value }))}
            />
            {f.secret && (
              <button
                type="button"
                className="mcp-key-eye"
                title={shown[f.id] ? "隐藏" : "显示"}
                onClick={() => setShown((s) => ({ ...s, [f.id]: !s[f.id] }))}
              >
                {shown[f.id] ? <EyeOff size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
              </button>
            )}
          </div>
          {f.hint && <span className="mcp-field-hint">{f.hint}</span>}
        </div>
      ))}

      <button type="button" className="mcp-adv-head" onClick={() => setAdvOpen((v) => !v)}>
        高级
        <ChevronDown size={14} strokeWidth={2} className={`mcp-adv-chevron${advOpen ? " open" : ""}`} />
      </button>
      {advOpen && (
        <pre className="mcp-adv-preview mono">{JSON.stringify(draft(), null, 2)}</pre>
      )}

      {formError && <span className="mcp-form-error">{formError}</span>}

      <div className="mcp-form-footer">
        <div className="mcp-form-test">
          <button
            type="button"
            className="data-btn ghost"
            disabled={testing || !canSave}
            title={canSave ? undefined : "先填写必填项"}
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
            title={canSave ? undefined : `待填写：${missing.join("、")}`}
            onClick={handleSave}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default McpTemplateForm;
