// "智能体"设置页（阶段二）：AI 主开关 + 天气城市（带探测回执测试钮）。设置项为
// 受控组件（变更经 onUpdate(patch) 上抛 App 统一乐观更新 + 持久化）。晨间简报 /
// 数据采集（含自动收录与调用预算）/ 立即采集 / 夜间报点等定时相关项已全部迁
// 「定时任务」页（2026-10-21 复查批 D1 撤本页「数据采集」组）；行样式复用通用
// 偏好的 .settings-* 类（CSS 全局生效，import 同文件去重）
import { useState } from "react";
import { testWeatherCity } from "../../lib/api";
import type { Settings } from "../../lib/settings";
import { SetInput, SetToggle } from "./SettingsControls";
import "./GeneralSettings.css";

function AgentSettings({
  settings: s,
  onUpdate,
}: {
  settings: Settings;
  onUpdate: (patch: Partial<Settings>) => void;
}) {
  // 天气探测回执（2026-10-21 复查批 D2）：错拼静默失效曾无从发现——测试钮命中
  // 回"已识别：城市, 地区"，未命中直显后端 422 detail
  const [weatherHint, setWeatherHint] = useState<{ ok: boolean; text: string } | null>(null);

  const testCity = () => {
    const city = (s.weatherCity ?? "").trim();
    if (!city) {
      setWeatherHint({ ok: false, text: "先填写城市名再测试" });
      return;
    }
    testWeatherCity(city)
      .then((r) => setWeatherHint({ ok: true, text: `已识别：${r.resolved}` }))
      .catch((e) => {
        const detail = (e as { detail?: string }).detail;
        setWeatherHint({ ok: false, text: detail ?? "探测失败：后端服务不可用" });
      });
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">智能体</span>
        <span className="settings-page-sub">AI 助手总开关与行为策略</span>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">AI 助手</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">启用 AI 助手</span>
              <span className="settings-row-desc">关闭后对话、晨间简报与夜间学习全部静默停用</span>
            </div>
            <div className="settings-row-control">
              <SetToggle checked={s.aiEnabled} onChange={(v) => onUpdate({ aiEnabled: v })} />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">天气城市</span>
              <span className="settings-row-desc">
                排期与问答对照未来一周天气预报（户外活动避雨提醒），留空停用
              </span>
            </div>
            <div className="settings-row-control">
              {weatherHint && (
                <span className={`settings-action-hint${weatherHint.ok ? " ok" : " bad"}`}>
                  {weatherHint.text}
                </span>
              )}
              <SetInput
                value={s.weatherCity ?? ""}
                placeholder="如：杭州"
                onChange={(v) => onUpdate({ weatherCity: v })}
              />
              <button type="button" className="settings-action-btn" onClick={testCity}>
                测试
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AgentSettings;
