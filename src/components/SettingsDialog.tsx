import { useEffect, useState } from "react";
import {
  AlarmClock,
  Bot,
  Database,
  Fingerprint,
  Info,
  Keyboard,
  MessageSquare,
  Palette,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import type { Settings } from "../lib/settings";
import AboutSettings from "./settings/AboutSettings";
import AgentSettings from "./settings/AgentSettings";
import DataSettings from "./settings/DataSettings";
import ChatCommSettings from "./settings/ChatCommSettings";
import GeneralSettings from "./settings/GeneralSettings";
import JobsSettings from "./settings/JobsSettings";
import MemorySettings from "./settings/MemorySettings";
import ModelSettings from "./settings/ModelSettings";
import PersonalizeSettings from "./settings/PersonalizeSettings";
import ShortcutSettings from "./settings/ShortcutSettings";
import "./SettingsDialog.css";

// 设置页左栏菜单（右栏内容随选中项切换，具体设置项按模块逐步填充）
const SETTINGS_MENUS = [
  { id: "general", label: "通用偏好", icon: SlidersHorizontal },
  { id: "personalize", label: "个性化", icon: Palette },
  { id: "model", label: "模型接入", icon: Bot },
  { id: "agent", label: "智能体", icon: Sparkles },
  { id: "data", label: "数据接入", icon: Database },
  { id: "chat", label: "聊天通讯", icon: MessageSquare },
  { id: "memory", label: "记忆画像", icon: Fingerprint },
  { id: "jobs", label: "定时任务", icon: AlarmClock },
  { id: "shortcut", label: "快捷键", icon: Keyboard },
  { id: "about", label: "关于我们", icon: Info },
] as const;

// "设置"悬浮页（侧栏底部入口）：左菜单灰底（复刻主侧栏布局语言）+ 右内容区
// （"通用偏好"已填充，其余分区空占位）；Esc / 点遮罩 / 右上角 X 三路关闭。
// 设置数据受控于 App（settings state + onUpdateSettings 回调），本组件不自持数据。
function SettingsDialog({
  onClose,
  settings,
  onUpdateSettings,
  initialMenu,
  onModelChange,
  memoryRefreshKey = 0,
}: {
  onClose: () => void;
  settings: Settings;
  onUpdateSettings: (patch: Partial<Settings>) => void;
  // 初始分区（引导卡直达用，如 "model"；关闭即卸载回落默认第一项）
  initialMenu?: string;
  // 模型配置变更信号（保存/激活/删除成功）→ App 递增 aiRefreshKey，助手面板
  // 重查激活态：零激活引导卡随配置即时消失/出现，不再等到下一轮问答
  onModelChange?: () => void;
  // 记忆页刷新信号（2026-11-03 记忆重构）：夜间工作流广播后 App 递增
  // aiRefreshKey——设置页若开着，记忆规则与情节事实即时重拉（新归纳可见）
  memoryRefreshKey?: number;
}) {
  // 当前选中菜单项（打开默认第一项；id 用 string 而非字面量联合——setActiveMenu 接收同宽类型）
  const [activeMenu, setActiveMenu] = useState<string>(
    initialMenu && SETTINGS_MENUS.some((m) => m.id === initialMenu) ? initialMenu : SETTINGS_MENUS[0].id,
  );
  const activeLabel = SETTINGS_MENUS.find((m) => m.id === activeMenu)?.label;

  // Esc 关闭：capture 阶段 stopPropagation，只关设置页不连锁关底下的浮层/气泡（口径同 ConfirmPopover）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div
        className="settings-dialog"
        role="dialog"
        aria-label="设置"
        onClick={(e) => e.stopPropagation()} // 点浮层本体不透传到遮罩关闭
      >
        <aside className="settings-menu">
          <span className="settings-menu-title">设置</span>
          <nav className="settings-menu-list">
            {SETTINGS_MENUS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                className={`settings-menu-item${activeMenu === id ? " active" : ""}`}
                onClick={() => setActiveMenu(id)}
              >
                <Icon size={15} strokeWidth={2} className="settings-menu-icon" />
                <span className="settings-menu-label">{label}</span>
              </button>
            ))}
          </nav>
        </aside>
        <section className="settings-content">
          <button
            type="button"
            className="settings-close"
            aria-label="关闭设置"
            onClick={onClose}
          >
            <X size={14} strokeWidth={2} />
          </button>
          {/* 右侧具体设置内容按菜单逐步填充，未实现分区为空占位 */}
          {activeMenu === "general" ? (
            <GeneralSettings settings={settings} onUpdate={onUpdateSettings} />
          ) : activeMenu === "personalize" ? (
            <PersonalizeSettings settings={settings} onUpdate={onUpdateSettings} />
          ) : activeMenu === "model" ? (
            <ModelSettings onModelChange={onModelChange} />
          ) : activeMenu === "agent" ? (
            <AgentSettings settings={settings} onUpdate={onUpdateSettings} />
          ) : activeMenu === "data" ? (
            <DataSettings />
          ) : activeMenu === "chat" ? (
            <ChatCommSettings />
          ) : activeMenu === "memory" ? (
            <MemorySettings refreshKey={memoryRefreshKey} />
          ) : activeMenu === "jobs" ? (
            <JobsSettings settings={settings} onUpdate={onUpdateSettings} />
          ) : activeMenu === "shortcut" ? (
            <ShortcutSettings />
          ) : activeMenu === "about" ? (
            <AboutSettings />
          ) : (
            <div className="settings-empty">
              <span className="settings-empty-title">{activeLabel}</span>
              <span className="settings-empty-hint">该模块设置项开发中</span>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

export default SettingsDialog;
