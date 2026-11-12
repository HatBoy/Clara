// "个性化"页面（2026-10-20；同日修订：哲学严选六则改卡片点选 + 冲突拦截，自定义上限 1000）。
// 纯受控组件——风格/哲学/自定义经 onUpdate(patch) 上抛 App 统一乐观更新 + 后端持久化
// （同 GeneralSettings 惯例）。哲学清单挂载时拉 GET /api/ai/philosophies（含 conflicts
// 冲突声明；提示词只留后端单源，前端不硬编码；失败且已有选中时渲染降级行——已选 id
// 仍可取消勾选，2026-10-21 复查批 D2 修「关不掉已启用哲学」）。人格注入通道复用记忆
// 规则格式，chat / 晨夜简报 / 排期引擎三入口同源生效。
import { useEffect, useRef, useState } from "react";
import type { AiStyle, Settings } from "../../lib/settings";
import { fetchPhilosophies, type Philosophy } from "../../lib/ai";
import { SetSegmented, SetTextarea, SetToggle, type SegmentOption } from "./SettingsControls";
import "./GeneralSettings.css";

const STYLE_OPTIONS: SegmentOption<AiStyle>[] = [
  { value: "balanced", label: "均衡" },
  { value: "concise", label: "简洁" },
  { value: "detailed", label: "详细" },
  { value: "rigorous", label: "严谨" },
];

function PersonalizeSettings({
  settings: s,
  onUpdate,
}: {
  settings: Settings;
  onUpdate: (patch: Partial<Settings>) => void;
}) {
  const [philosophies, setPhilosophies] = useState<Philosophy[]>([]);
  const [conflictHint, setConflictHint] = useState<string | null>(null);
  const hintTimer = useRef<number | null>(null);
  useEffect(() => {
    void fetchPhilosophies()
      .then(setPhilosophies)
      .catch(() => setPhilosophies([])); // 后端未起/失败：清单不渲染，不弹错
    return () => {
      if (hintTimer.current != null) window.clearTimeout(hintTimer.current);
    };
  }, []);

  // 冲突拦截（2026-10-20）：目标卡片与任一已选哲学互斥时拒绝上抛并提示 4s 自清
  // （后端 PATCH 不校验冲突——防 services→agents 反向依赖，同 id 存在性先例）
  const showConflict = (p: Philosophy, heldBy: Philosophy) => {
    setConflictHint(`「${p.name}」与已选「${heldBy.name}」理念冲突，请先取消「${heldBy.name}」`);
    if (hintTimer.current != null) window.clearTimeout(hintTimer.current);
    hintTimer.current = window.setTimeout(() => setConflictHint(null), 4000);
  };

  // 哲学卡片点选：增删 id 后整组上抛（toSettingsPatch 以 "aiPhilosophy" in patch 判
  // 显式设置，改风格等其他字段不会误清哲学）
  const togglePhilosophy = (p: Philosophy) => {
    const held = philosophies.find(
      (q) => q.id !== p.id && p.conflicts.includes(q.id) && s.aiPhilosophy.includes(q.id),
    );
    if (!s.aiPhilosophy.includes(p.id) && held) {
      showConflict(p, held);
      return;
    }
    const next = s.aiPhilosophy.includes(p.id)
      ? s.aiPhilosophy.filter((x) => x !== p.id)
      : [...s.aiPhilosophy, p.id];
    onUpdate({ aiPhilosophy: next });
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">个性化</span>
        <span className="settings-page-sub">
          定义日程助手的回复风格与思考哲学，对话、简报与排期同步生效
        </span>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">回复风格</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">回复风格</span>
              <span className="settings-row-desc">「均衡」为默认，不附加任何风格约束</span>
            </div>
            <div className="settings-row-control">
              <SetSegmented
                value={s.aiStyle}
                options={STYLE_OPTIONS}
                onChange={(v) => onUpdate({ aiStyle: v })}
              />
            </div>
          </div>
        </div>
      </div>

      {philosophies.length > 0 && (
        <div className="settings-group">
          <span className="settings-group-title">管理哲学</span>
          <div className="settings-card">
            <div className="personalize-philo-grid">
              {philosophies.map((p) => {
                // 已选他者占用了与 p 冲突的位置 → p 呈锁定态（开关可点，点了出提示）
                const holder = philosophies.find(
                  (q) => q.id !== p.id && p.conflicts.includes(q.id) && s.aiPhilosophy.includes(q.id),
                );
                const on = s.aiPhilosophy.includes(p.id);
                return (
                  <div
                    key={p.id}
                    className={`personalize-philo-card${on ? " on" : ""}${holder ? " locked" : ""}`}
                  >
                    <div className="philo-head">
                      <span className="philo-name">{p.name}</span>
                      <SetToggle checked={on} onChange={() => togglePhilosophy(p)} />
                    </div>
                    <span className="philo-desc">{p.desc}</span>
                  </div>
                );
              })}
            </div>
            {conflictHint && <div className="personalize-philo-hint">{conflictHint}</div>}
          </div>
        </div>
      )}

      {/* 清单拉取失败但已有选中：降级行逐条可移除（否则已启用哲学关不掉） */}
      {philosophies.length === 0 && s.aiPhilosophy.length > 0 && (
        <div className="settings-group">
          <span className="settings-group-title">管理哲学</span>
          <div className="settings-card">
            <div className="settings-row">
              <div className="settings-row-info">
                <span className="settings-row-name">已启用哲学</span>
                <span className="settings-row-desc">
                  清单加载失败，暂只展示已启用的预设——点击可移除，恢复后可重新选择
                </span>
              </div>
              <div className="settings-row-control">
                {s.aiPhilosophy.map((id) => (
                  <button
                    key={id}
                    type="button"
                    className="settings-action-btn"
                    onClick={() =>
                      onUpdate({ aiPhilosophy: s.aiPhilosophy.filter((x) => x !== id) })
                    }
                  >
                    {id} ✕
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="settings-group">
        <span className="settings-group-title">自定义哲学</span>
        <div className="settings-card">
          <div className="settings-row personalize-custom-row">
            <div className="settings-row-info">
              <span className="settings-row-name">自定义哲学文本</span>
              <span className="settings-row-desc">
                补充你自己的处世或管理信条，失焦保存，Esc 弃改
              </span>
            </div>
          </div>
          <div className="personalize-custom-editor">
            <SetTextarea
              value={s.aiPhilosophyCustom}
              placeholder="例：先完成再完美；会议不带结论不放人……"
              maxLength={1000}
              rows={4}
              onChange={(v) => onUpdate({ aiPhilosophyCustom: v })}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default PersonalizeSettings;
