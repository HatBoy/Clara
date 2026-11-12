// "模型接入"页面：模型库卡片（radio 单选激活 + 连接徽标 + 上次测速）+ 内联接入表单
// （显示名称 / Base URL / Model ID / API Key + 进阶：上下文长度 / 思考模式）。
// 组件自管理数据（模型数据 App 不消费；激活态真源在后端库，阶段二 agent 直接读库），
// 挂载 fetchModels，增删改 / 激活 / 测速全走后端 /api/models；保存成功后自动对
// 该模型测速一次并把结果记回卡片（last_test_* 三列以 /{id}/test 落库值为真源）。
import { useEffect, useState } from "react";
import { Bot, Eye, EyeOff, Pencil, Plus, Trash2, Zap } from "lucide-react";
import {
  activateModel,
  setAuxModel,
  createModel,
  deleteModel,
  fetchModels,
  testModelById,
  testModelDraft,
  toNaiveLocalISO,
  updateModel,
  type ApiModel,
  type ApiModelTestResult,
} from "../../lib/api";
import ConfirmPopover from "../ConfirmPopover";
import { SetSelect, SetToggle, type SelectOption } from "./SettingsControls";
import "./ModelSettings.css";

// 表单草稿（camelCase 本地形状；保存时转 ModelCreateBody / ModelPatchBody）
type ModelDraft = {
  name: string;
  baseUrl: string;
  modelId: string;
  apiKey: string; // 空串 = 不带 Key（本地免鉴权服务）；保存时归一 null
  thinkingMode: boolean;
  contextLength: number;
};

const EMPTY_DRAFT: ModelDraft = {
  name: "",
  baseUrl: "",
  modelId: "",
  apiKey: "",
  thinkingMode: false,
  contextLength: 32768,
};

// 档位 → 展示文案：1M 档按 1024^2 换算显示 "1M"（直接 /1024 会得 "1024K"）
const fmtContext = (t: number) => (t >= 1048576 ? `${t / 1048576}M` : `${t / 1024}K`);

// 上下文长度档（token 数；label 展示 K/M 换算）
const CONTEXT_OPTIONS: SelectOption<number>[] = [8192, 16384, 32768, 65536, 131072, 204800, 262144, 1048576].map(
  (t) => ({ value: t, label: fmtContext(t) }),
);

function ModelSettings({ onModelChange }: { onModelChange?: () => void }) {
  // null = 加载中（本地回环毫秒级，不做 loading UI）；loadError = 拉取失败占位
  const [models, setModels] = useState<ApiModel[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null); // null = 新建
  const [draft, setDraft] = useState<ModelDraft>(EMPTY_DRAFT);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ApiModelTestResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [autoTestId, setAutoTestId] = useState<string | null>(null); // 保存后自动测速中的卡片

  // 挂载拉全量；空库自动开表单（首次使用引导）。cancelled 防 StrictMode 双跑的
  // 卸载分支 setState；GET 幂等，双发本身无害
  useEffect(() => {
    let cancelled = false;
    fetchModels()
      .then((rows) => {
        if (cancelled) return;
        setModels(rows);
        if (rows.length === 0) setFormOpen(true);
      })
      .catch((e) => {
        console.error("加载模型列表失败:", e);
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const patch = (p: Partial<ModelDraft>) => setDraft((d) => ({ ...d, ...p }));

  const openForm = (m: ApiModel | null) => {
    setEditingId(m?.id ?? null);
    setDraft(
      m
        ? {
            name: m.name,
            baseUrl: m.base_url ?? "",
            modelId: m.model_name,
            apiKey: m.api_key ?? "", // GET 原样回显，编辑免重填
            thinkingMode: m.thinking_mode,
            contextLength: m.context_length,
          }
        : EMPTY_DRAFT,
    );
    setShowKey(false);
    setTestResult(null);
    setFormError(null);
    setFormOpen(true);
  };

  const closeForm = () => {
    setFormOpen(false);
    setEditingId(null);
    setTesting(false);
    setTestResult(null);
    setFormError(null);
  };

  // 激活：乐观本地互斥翻转 → 成功回填后端行 / 失败整表回滚快照（互斥态半更新不可接受）
  const handleActivate = (id: string) => {
    const target = models?.find((m) => m.id === id);
    if (!target || target.is_active) return;
    const snapshot = models;
    setModels((prev) =>
      prev ? prev.map((m) => ({ ...m, is_active: m.id === id })) : prev,
    );
    activateModel(id)
      .then((row) => {
        setModels((prev) => (prev ? prev.map((m) => (m.id === row.id ? row : m)) : prev));
        onModelChange?.(); // 激活态变更 → 通知 App（助手面板零激活引导卡即时消失）
      })
      .catch((e) => {
        console.error("激活模型失败:", e);
        setModels(snapshot);
      });
  };

  // 设/取消轻量助手（优化批 9，toggle）：小调用白名单（路由/分类/反思等）走
  // 此行——本地小模型亚秒级；未配置回落激活模型（行为不变）。乐观翻转 + 回滚
  const handleAux = (id: string) => {
    const snapshot = models;
    setModels((prev) =>
      prev ? prev.map((m) => ({ ...m, is_aux: m.id === id ? !m.is_aux : false })) : prev,
    );
    setAuxModel(id)
      .then((row) => {
        setModels((prev) => (prev ? prev.map((m) => (m.id === row.id ? row : m)) : prev));
      })
      .catch((e) => {
        console.error("设置助手模型失败:", e);
        setModels(snapshot);
      });
  };

  // 表单"测试连通性"：编辑态也用表单现值草稿探测（与"当前填写参数测"语义一致，不落库）
  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(
        await testModelDraft({
          base_url: draft.baseUrl.trim() || null,
          model_name: draft.modelId.trim(),
          api_key: draft.apiKey.trim() || null,
        }),
      );
    } catch {
      // 后端不在等请求层失败（探测失败本身是 200 数据，走不到这）
      setTestResult({ ok: false, latency_ms: null, error: "请求失败：后端服务不可用" });
    } finally {
      setTesting(false);
    }
  };

  // 保存后自动测速：结果由后端落库（last_test_* 真源），前端本地同步 patch 卡片；
  // 时刻用本地 naive ISO 口径同 toNaiveLocalISO（下次 fetch 被后端真值覆盖）
  const runAutoTest = (id: string) => {
    setAutoTestId(id);
    testModelById(id)
      .then((r) => {
        const at = toNaiveLocalISO(new Date());
        setModels((prev) =>
          prev
            ? prev.map((m) =>
                m.id === id
                  ? { ...m, last_test_ok: r.ok, last_test_ms: r.latency_ms, last_test_at: at }
                  : m,
              )
            : prev,
        );
      })
      .catch((e) => console.error("保存后自动测速失败:", e))
      .finally(() => setAutoTestId(null));
  };

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    const body = {
      name: draft.name.trim(),
      base_url: draft.baseUrl.trim() || null,
      model_name: draft.modelId.trim(),
      api_key: draft.apiKey.trim() || null, // 清空输入框 = 显式 null = 清空
      thinking_mode: draft.thinkingMode,
      context_length: draft.contextLength,
    };
    try {
      const row = editingId ? await updateModel(editingId, body) : await createModel(body);
      setModels((prev) =>
        editingId
          ? prev
            ? prev.map((m) => (m.id === row.id ? row : m))
            : prev
          : [...(prev ?? []), row],
      );
      closeForm();
      onModelChange?.(); // 新建/编辑都可能改变激活态 → 通知 App
      runAutoTest(row.id);
    } catch (e) {
      const status = (e as Error & { status?: number }).status;
      setFormError(status === 409 ? "显示名称已存在，请换一个" : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteModel(id);
    } catch (e) {
      console.error("删除模型失败:", e);
      return; // 失败不动列表（删除是破坏性操作，不做乐观）
    }
    setModels((prev) => (prev ? prev.filter((m) => m.id !== id) : prev));
    onModelChange?.(); // 删除激活行会回到零激活 → 引导卡需即时出现
    if (editingId === id) closeForm(); // 表单正编辑该行则一并关闭
  };

  // Base URL 可选（空 = LiteLLM 官方路由），必填仅显示名称 + 模型标识
  const canSave = Boolean(draft.name.trim() && draft.modelId.trim());

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">模型接入</span>
        <span className="settings-page-sub">接入多家大模型服务（经 LiteLLM 统一协议），选择一个激活使用</span>
      </div>

      {loadError ? (
        <div className="settings-empty">
          <span className="settings-empty-title">模型服务不可用</span>
          <span className="settings-empty-hint">请确认后端服务已启动后重新打开设置</span>
        </div>
      ) : (
        models && (
          <>
            <div className="settings-group">
              <span className="settings-group-title">我的模型库（{models.length}）</span>
              <div className="model-list" role="radiogroup" aria-label="选择激活的模型">
                {models.map((m) => (
                  <div
                    key={m.id}
                    role="radio"
                    aria-checked={m.is_active}
                    className={`model-card${m.is_active ? " active" : ""}`}
                    onClick={() => handleActivate(m.id)}
                  >
                    <span className="model-card-radio" />
                    <span className="model-card-icon">
                      <Bot size={16} strokeWidth={2} />
                    </span>
                    <div className="model-card-info">
                      <div className="model-card-name-line">
                        <span className="model-card-name">{m.name}</span>
                        {/* 徽标语义绑上次连通测试结果（非激活态）——避免"激活但失联显示在线" */}
                        <span
                          className={`model-badge${
                            m.last_test_ok === true ? " ok" : m.last_test_ok === false ? " bad" : " idle"
                          }`}
                        >
                          {m.last_test_ok === true ? "在线" : m.last_test_ok === false ? "离线" : "未测试"}
                        </span>
                      </div>
                      <span className="model-card-sub">
                        {m.model_name} · {fmtContext(m.context_length)} Context
                      </span>
                    </div>
                    <div className="model-card-meta">
                      <span className="model-card-meta-label">上次测速</span>
                      <span
                        className={`model-card-meta-value${
                          autoTestId === m.id
                            ? " pending"
                            : m.last_test_ok === true
                              ? " ok"
                              : m.last_test_ok === false
                                ? " bad"
                                : ""
                        }`}
                      >
                        {autoTestId === m.id
                          ? "测速中…"
                          : m.last_test_ok === true
                            ? `${m.last_test_ms}ms`
                            : m.last_test_ok === false
                              ? "失败"
                              : "—"}
                      </span>
                    </div>
                    <div className="model-card-actions">
                      <button
                        type="button"
                        className={`model-card-btn${m.is_aux ? " aux-on" : ""}`}
                        title={m.is_aux
                          ? "轻量助手模型（点击取消）：路由/分类/反思等小调用走此行，未配置回落激活模型"
                          : "设为轻量助手模型：小调用（路由/分类/反思/采集摘要）走此行，本地小模型可亚秒级"}
                        onClick={(e) => {
                          e.stopPropagation(); // 不触发整卡激活
                          handleAux(m.id);
                        }}
                      >
                        <Zap size={13} strokeWidth={2} />
                      </button>
                      <button
                        type="button"
                        className="model-card-btn"
                        title="编辑模型"
                        onClick={(e) => {
                          e.stopPropagation(); // 不触发整卡激活
                          openForm(m);
                        }}
                      >
                        <Pencil size={13} strokeWidth={2} />
                      </button>
                      <ConfirmPopover
                        message={`删除模型「${m.name}」？此操作不可恢复`}
                        onConfirm={() => handleDelete(m.id)}
                        buttonClass="model-card-btn model-del-btn"
                        buttonTitle="删除模型"
                        icon={<Trash2 size={13} strokeWidth={2} />}
                      />
                    </div>
                  </div>
                ))}
              </div>
              {!formOpen && (
                <button type="button" className="model-add-entry" onClick={() => openForm(null)}>
                  <Plus size={14} strokeWidth={2} />
                  <span>接入新模型</span>
                </button>
              )}
            </div>

            {formOpen && (
              <div className="settings-group">
                <div className="settings-card model-form">
                  <span className="model-form-title">{editingId ? "编辑模型" : "接入新模型"}</span>

                  <label className="model-field">
                    <span className="model-field-label">显示名称</span>
                    <input
                      className="model-input"
                      value={draft.name}
                      maxLength={30}
                      placeholder="例如：DeepSeek 云端"
                      onChange={(e) => patch({ name: e.target.value })}
                    />
                  </label>

                  <div className="model-field-row">
                    <label className="model-field">
                      <span className="model-field-label">模型地址（Base URL，可选）</span>
                      <input
                        className="model-input mono"
                        value={draft.baseUrl}
                        maxLength={200}
                        placeholder="留空走官方，或 https://…/v1"
                        onChange={(e) => patch({ baseUrl: e.target.value })}
                      />
                      <span className="model-field-hint">留空 = 服务商官方接口；自定义/本地端点填完整地址（OpenAI 兼容需含 /v1）</span>
                    </label>
                    <label className="model-field">
                      <span className="model-field-label">模型名称（Model ID）</span>
                      <input
                        className="model-input mono"
                        value={draft.modelId}
                        maxLength={100}
                        placeholder="deepseek/deepseek-chat"
                        onChange={(e) => patch({ modelId: e.target.value })}
                      />
                      <span className="model-field-hint">LiteLLM 格式：服务商/模型名（deepseek/、openai/、ollama/、anthropic/…；其他服务商用 openai/ + Base URL 接入）</span>
                    </label>
                  </div>

                  <div className="model-field">
                    <span className="model-field-label">API 密钥</span>
                    <div className="model-key-wrap">
                      <input
                        className="model-input mono"
                        type={showKey ? "text" : "password"}
                        value={draft.apiKey}
                        maxLength={200}
                        placeholder="sk-…（本地服务可留空）"
                        onChange={(e) => patch({ apiKey: e.target.value })}
                      />
                      <button
                        type="button"
                        className="model-key-eye"
                        title={showKey ? "隐藏密钥" : "显示密钥"}
                        onClick={() => setShowKey((v) => !v)}
                      >
                        {showKey ? <EyeOff size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
                      </button>
                    </div>
                  </div>

                  <div className="model-form-adv">
                    <div className="model-form-adv-head">
                      <span>进阶设定</span>
                      <span className="model-experimental">Experimental</span>
                    </div>
                    <div className="settings-row">
                      <div className="settings-row-info">
                        <span className="settings-row-name">上下文长度</span>
                        <span className="settings-row-desc">传给模型的历史对话窗口大小</span>
                      </div>
                      <div className="settings-row-control">
                        <SetSelect
                          value={draft.contextLength}
                          options={CONTEXT_OPTIONS}
                          onChange={(v) => patch({ contextLength: v })}
                          width={90}
                        />
                      </div>
                    </div>
                    <div className="settings-row">
                      <div className="settings-row-info">
                        <span className="settings-row-name">深度思考模式</span>
                        <span className="settings-row-desc">{"调用时带 reasoning_effort 思考参数（DeepSeek-R1/Claude/Gemini 等）"}</span>
                      </div>
                      <div className="settings-row-control">
                        <SetToggle
                          checked={draft.thinkingMode}
                          onChange={(v) => patch({ thinkingMode: v })}
                        />
                      </div>
                    </div>
                  </div>

                  {formError && <span className="model-form-error">{formError}</span>}

                  <div className="model-form-footer">
                    <div className="model-form-test">
                      <button
                        type="button"
                        className="model-btn ghost-accent"
                        disabled={testing || !draft.modelId.trim()}
                        onClick={handleTest}
                      >
                        {testing ? "测试中…" : "测试连通性"}
                      </button>
                      {testResult && (
                        <span className={`model-test-result${testResult.ok ? " ok" : " bad"}`}>
                          {testResult.ok ? `连通 · ${testResult.latency_ms}ms` : testResult.error}
                        </span>
                      )}
                    </div>
                    <div className="model-form-actions">
                      <button type="button" className="model-btn ghost" onClick={closeForm}>
                        取消
                      </button>
                      <button
                        type="button"
                        className="model-btn primary"
                        disabled={saving || !canSave}
                        onClick={handleSave}
                      >
                        保存配置
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </>
        )
      )}
    </div>
  );
}

export default ModelSettings;
