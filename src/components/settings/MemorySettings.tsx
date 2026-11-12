// "记忆画像"设置页（阶段二）：记忆规则白盒界面（PRD §4.5）——按固定槽位分组
// 展示 + 来源徽标 / 置信分级徽标 / 硬约束徽标 / 证据列；CRUD 保存前走冲突预检
// （2026-11-03 记忆重构，用户拍板「冲突提示用户确认」——零 LLM 秒回，用户裁决
// 「合并覆盖 / 两者并存」）；另含情节事实（Tier 2）清单：AI 从对话与复盘中记下
// 的临时状态，带过期钟、手动只删。自管理数据（同 ModelSettings 先例）：挂载与
// refreshKey 变更时拉取，写操作乐观入列 / 响应回填。手动创建恒
// source=user_manual / 置信度 1.0（后端口径）；编辑 AI 推导行后端自动升
// user_manual（亲笔即最高裁决）；删除落纠偏账（AI 不再推同义）。总量上限 120
// 由后端守（422 detail 直显）；删除套 ConfirmPopover 二次确认。
import { useEffect, useState } from "react";
import { Check, Pencil, Plus, X } from "lucide-react";
import {
  checkRuleConflicts,
  createMemoryRule,
  deleteEpisodicFact,
  deleteMemoryRule,
  fetchEpisodicFacts,
  fetchMemoryRules,
  updateMemoryRule,
  type AiEpisodicFact,
  type AiMemoryRule,
  type RuleConflict,
} from "../../lib/ai";
import ConfirmPopover from "../ConfirmPopover";
import "./GeneralSettings.css";
import "./MemorySettings.css";

// 行内编辑草稿（展示值 → 输入框；evidence null 归一为空串）
type RowDraft = { category: string; content: string; evidence: string };

// 槽位分组（后端 slot_of 单源同词表；渲染层硬编码可接受——展示名不出端点）
const SLOT_GROUPS: { key: string; label: string }[] = [
  { key: "rhythm", label: "生理节律与作息" },
  { key: "meeting", label: "会议与沟通" },
  { key: "boundary", label: "红线与免打扰" },
  { key: "other", label: "其他" },
];

// 置信分级（三档）：法则 = 用户明示（user_manual/chat 源）；验证 = 0.6-0.8
// （行为反复印证自动升档）；推测 = 单次归纳软参考
function tierOf(r: AiMemoryRule): { key: string; label: string } {
  if (r.source === "user_manual" || r.source === "chat" || r.confidence >= 0.99)
    return { key: "law", label: "法则" };
  if (r.confidence >= 0.6) return { key: "verified", label: "验证" };
  return { key: "speculative", label: "推测" };
}

// 冲突确认挂起态（保存动作被预检拦截，等用户二选一）
type ConflictPending = {
  mode: "create" | "edit";
  editId?: string;
  draft: RowDraft;
  conflicts: RuleConflict[];
};

const toDraft = (r: AiMemoryRule): RowDraft => ({
  category: r.category,
  content: r.content,
  evidence: r.evidence ?? "",
});

// 事实过期相对文案（「约 N 天后过期」/「永久」）
const expiresIn = (iso: string | null): string => {
  if (!iso) return "永久";
  const days = Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
  return days <= 0 ? "即将过期" : `约 ${days} 天后过期`;
};

function MemorySettings({ refreshKey = 0 }: { refreshKey?: number }) {
  const [rules, setRules] = useState<AiMemoryRule[]>([]);
  const [facts, setFacts] = useState<AiEpisodicFact[]>([]);
  const [error, setError] = useState<string | null>(null);
  // 新增表单草稿
  const [nc, setNc] = useState("");
  const [nt, setNt] = useState("");
  const [ne, setNe] = useState("");
  const [creating, setCreating] = useState(false);
  // 行内编辑态（editingId = null 为无）
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<RowDraft>({ category: "", content: "", evidence: "" });
  // 冲突确认挂起（非空 = 有疑似冲突待裁决）
  const [conflict, setConflict] = useState<ConflictPending | null>(null);

  useEffect(() => {
    void fetchMemoryRules()
      .then((rows) => {
        setRules(rows);
        setError(null);
      })
      .catch(() => setError("加载记忆规则失败"));
    void fetchEpisodicFacts().then(setFacts).catch(() => undefined);
  }, [refreshKey]);

  // 错误信息归一：后端 422 detail 优先直显（"已达上限 120 条"等中文引导本可直出），
  // 无 detail 才落静态兜底
  const errText = (e: unknown, fallback: string) => {
    console.error(fallback, e);
    const detail = (e as { detail?: string }).detail;
    setError(detail ?? fallback);
  };

  // —— 保存管线（预检 → 冲突挂起 / 直存）——

  const doCreate = (d: RowDraft) => {
    setCreating(true);
    setError(null);
    createMemoryRule({ category: d.category, content: d.content, evidence: d.evidence || null })
      .then((row) => {
        setRules((prev) => [...prev, row]);
        setNc("");
        setNt("");
        setNe("");
        setConflict(null);
      })
      .catch((e) => errText(e, "创建失败：分类 1–30 字、内容 1–200 字，或已达 120 条上限"))
      .finally(() => setCreating(false));
  };

  const doEdit = (id: string, d: RowDraft) => {
    setError(null);
    updateMemoryRule(id, { category: d.category, content: d.content, evidence: d.evidence || null })
      .then((row) => {
        setRules((prev) => prev.map((r) => (r.id === id ? row : r)));
        setEditingId(null);
        setConflict(null);
      })
      .catch((e) => errText(e, "保存失败：分类 1–30 字、内容 1–200 字"));
  };

  // 合并覆盖（create 场景）：用新值 PATCH 冲突行（后端自动升 user_manual/1.0）
  const doMergeOver = (targetId: string, d: RowDraft) => {
    setCreating(true);
    setError(null);
    updateMemoryRule(targetId, { category: d.category, content: d.content, evidence: d.evidence || null })
      .then((row) => {
        setRules((prev) => prev.map((r) => (r.id === targetId ? row : r)));
        setNc("");
        setNt("");
        setNe("");
        setConflict(null);
      })
      .catch((e) => errText(e, "合并失败：分类 1–30 字、内容 1–200 字"))
      .finally(() => setCreating(false));
  };

  const add = () => {
    const d = { category: nc.trim(), content: nt.trim(), evidence: ne.trim() };
    if (!d.category || !d.content || creating) return;
    checkRuleConflicts({ category: d.category, content: d.content })
      .then(({ conflicts }) => {
        if (conflicts.length > 0) {
          setConflict({ mode: "create", draft: d, conflicts });
        } else {
          doCreate(d);
        }
      })
      .catch(() => doCreate(d)); // 预检失败不挡保存（直存兜底）
  };

  const saveEdit = (id: string) => {
    const d = { category: draft.category.trim(), content: draft.content.trim(), evidence: draft.evidence.trim() };
    if (!d.category || !d.content) {
      setError("分类与内容不能为空");
      return;
    }
    checkRuleConflicts({ category: d.category, content: d.content, exclude_id: id })
      .then(({ conflicts }) => {
        if (conflicts.length > 0) {
          setConflict({ mode: "edit", editId: id, draft: d, conflicts });
        } else {
          doEdit(id, d);
        }
      })
      .catch(() => doEdit(id, d));
  };

  const remove = (id: string) => {
    setError(null);
    deleteMemoryRule(id)
      .then(() => setRules((prev) => prev.filter((r) => r.id !== id)))
      .catch((e) => errText(e, "删除记忆规则失败"));
  };

  const removeFact = (id: string) => {
    deleteEpisodicFact(id)
      .then(() => setFacts((prev) => prev.filter((f) => f.id !== id)))
      .catch((e) => errText(e, "删除事实失败"));
  };

  // 槽位分组（slot null 归 other；组序 = SLOT_GROUPS）
  const grouped = SLOT_GROUPS.map((g) => ({
    ...g,
    rows: rules.filter((r) => (r.slot ?? "other") === g.key),
  }));

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">记忆画像</span>
        <span className="settings-page-sub">AI 对你作息与偏好的长期记忆，注入每次对话</span>
      </div>

      {error && (
        <div className="mem-error">{error}</div>
      )}

      <div className="settings-group">
        <span className="settings-group-title">新增规则（手动规则注入优先级最高）</span>
        <div className="settings-card">
          <div className="mem-form">
            <input
              className="mem-input mem-input-cat"
              placeholder="分类（如 开会习惯 / 改进目标）"
              maxLength={30}
              value={nc}
              onChange={(e) => setNc(e.target.value)}
            />
            <input
              className="mem-input mem-input-content"
              placeholder="规则内容（如 不排晚 8 点后的会）"
              maxLength={200}
              value={nt}
              onChange={(e) => setNt(e.target.value)}
            />
            <input
              className="mem-input mem-input-evidence"
              placeholder="证据（选填）"
              maxLength={200}
              value={ne}
              onChange={(e) => setNe(e.target.value)}
            />
            <button
              type="button"
              className="mem-btn primary"
              disabled={!nc.trim() || !nt.trim() || creating}
              onClick={add}
            >
              <Plus size={14} strokeWidth={2} />
              添加
            </button>
          </div>
          {conflict && (
            <div className="mem-conflict">
              <div className="mem-conflict-text">
                与已有规则疑似重复（相似度 {Math.round(conflict.conflicts[0].similarity * 100)}%）：
                「{conflict.conflicts[0].content.slice(0, 30)}」——合并覆盖会以新内容替换它。
              </div>
              <div className="mem-conflict-ops">
                {conflict.mode === "create" ? (
                  <>
                    <button
                      type="button"
                      className="mem-btn primary"
                      disabled={creating}
                      onClick={() => doMergeOver(conflict.conflicts[0].id, conflict.draft)}
                    >
                      合并覆盖
                    </button>
                    <button
                      type="button"
                      className="mem-btn"
                      disabled={creating}
                      onClick={() => doCreate(conflict.draft)}
                    >
                      两者并存
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="mem-btn primary"
                      onClick={() => conflict.editId && doEdit(conflict.editId, conflict.draft)}
                    >
                      仍要保存
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="mem-btn"
                  onClick={() => setConflict(null)}
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">
          已有规则（{rules.length}/120，超出容量的低置信规则会被淘汰）
        </span>
        <div className="settings-card mem-list">
          {rules.length === 0 && <div className="mem-empty">暂无记忆规则——AI 归纳与你手动添加的规则都会出现在这里</div>}
          {grouped.map((g) =>
            g.rows.length > 0 ? (
              <div key={g.key} className="mem-slot-group">
                <div className="mem-slot-title">{g.label}</div>
                {g.rows.map((r) =>
                  editingId === r.id ? (
                    <div key={r.id} className="mem-row editing">
                      <input
                        className="mem-input mem-input-cat"
                        maxLength={30}
                        value={draft.category}
                        onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                      />
                      <input
                        className="mem-input mem-input-content"
                        maxLength={200}
                        value={draft.content}
                        onChange={(e) => setDraft({ ...draft, content: e.target.value })}
                      />
                      <input
                        className="mem-input mem-input-evidence"
                        maxLength={200}
                        placeholder="证据（选填）"
                        value={draft.evidence}
                        onChange={(e) => setDraft({ ...draft, evidence: e.target.value })}
                      />
                      <div className="mem-ops">
                        <button
                          type="button"
                          className="mem-icon-btn"
                          aria-label="保存"
                          onClick={() => saveEdit(r.id)}
                        >
                          <Check size={14} strokeWidth={2} />
                        </button>
                        <button
                          type="button"
                          className="mem-icon-btn"
                          aria-label="取消"
                          onClick={() => setEditingId(null)}
                        >
                          <X size={14} strokeWidth={2} />
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div key={r.id} className="mem-row">
                      <span className="mem-cat">{r.category}</span>
                      <div className="mem-body">
                        <span className="mem-content">{r.content}</span>
                        {r.evidence && <span className="mem-evidence">证据：{r.evidence}</span>}
                      </div>
                      <div className="mem-meta">
                        {r.structured_anchor && (
                          <span className="mem-anchor" title="该规则已解析为避让窗，排期引擎硬性避让">
                            硬约束
                          </span>
                        )}
                        <span className={`mem-source ${r.source}`}>
                          {r.source === "user_manual" ? "手动" : r.source === "chat" ? "对话设置" : "AI 归纳"}
                        </span>
                        <span
                          className={`mem-tier ${tierOf(r).key}`}
                          title={`置信度 ${Math.round(r.confidence * 100)}%${r.observation_count > 0 ? ` · 已印证 ${r.observation_count} 次` : ""}`}
                        >
                          {tierOf(r).label}
                        </span>
                      </div>
                      <div className="mem-ops">
                        <button
                          type="button"
                          className="mem-icon-btn"
                          aria-label="编辑"
                          onClick={() => {
                            setEditingId(r.id);
                            setDraft(toDraft(r));
                            setConflict(null);
                            setError(null);
                          }}
                        >
                          <Pencil size={13} strokeWidth={2} />
                        </button>
                        <ConfirmPopover
                          message={`删除规则「${r.content.slice(0, 20)}」？此操作不可恢复，AI 将不再归纳同义规则`}
                          onConfirm={() => remove(r.id)}
                          buttonClass="mem-icon-btn danger"
                          buttonTitle="删除规则"
                          icon={<X size={14} strokeWidth={2} />}
                        />
                      </div>
                    </div>
                  ),
                )}
              </div>
            ) : null,
          )}
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">情节事实（AI 从对话与复盘中记下的近期状态，到期自动失效）</span>
        <div className="settings-card mem-list">
          {facts.length === 0 && <div className="mem-empty">暂无近期事实——「这周感冒」「下周出差」这类临时状态会记在这里</div>}
          {facts.map((f) => (
            <div key={f.id} className="mem-row">
              <span className="mem-cat">{f.topic}</span>
              <div className="mem-body">
                <span className="mem-content">{f.fact}</span>
                <span className="mem-evidence">{expiresIn(f.expires_at)}</span>
              </div>
              <div className="mem-meta">
                <span className="mem-source chat">{f.source === "chat" ? "对话" : "复盘"}</span>
              </div>
              <div className="mem-ops">
                <ConfirmPopover
                  message={`删除事实「${f.fact.slice(0, 20)}」？`}
                  onConfirm={() => removeFact(f.id)}
                  buttonClass="mem-icon-btn danger"
                  buttonTitle="删除事实"
                  icon={<X size={14} strokeWidth={2} />}
                />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default MemorySettings;
