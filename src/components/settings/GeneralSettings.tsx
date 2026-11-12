// "通用偏好"页面：四组十二行（系统与启动 / 外观（含小部件透明度滑条）/ 日历与排期 / 提醒与通知）。
// 纯受控组件——所有变更经 onUpdate(patch) 上抛 App 统一乐观更新 + 后端持久化
// （弹层开合等局部态在各控件内部）。唯一例外"服务端口"行：真源在 Tauri 侧配置
// 文件（不进 SQLite），组件内自治读写（同 ModelSettings 自管理先例）。
import { useEffect, useState } from "react";
import { Calendar, CalendarDays, CalendarRange, Monitor, Moon, Sun } from "lucide-react";
import type {
  CloseAction,
  DefaultView,
  NotifyStyle,
  Settings,
  Sound,
  ThemeMode,
} from "../../lib/settings";
import { playSound } from "../../lib/sound";
import { fetchServerPort, isTauri, setServerPort } from "../../lib/tauri";
import {
  SetSegmented,
  SetSelect,
  SetSlider,
  SetTimeRangeSelect,
  SetToggle,
  type SegmentOption,
  type SelectOption,
} from "./SettingsControls";
import "./GeneralSettings.css";

const CLOSE_OPTIONS: SegmentOption<CloseAction>[] = [
  { value: "hide", label: "隐藏到托盘" },
  { value: "quit", label: "完全退出" },
];

const THEME_OPTIONS: SegmentOption<ThemeMode>[] = [
  { value: "system", label: "跟随系统", icon: <Monitor size={14} strokeWidth={2} /> },
  { value: "light", label: "浅色", icon: <Sun size={14} strokeWidth={2} /> },
  { value: "dark", label: "深色", icon: <Moon size={14} strokeWidth={2} /> },
];

// 启动默认视图（格式同外观模式：三段带图标；与中栏"日/周/月"分段控件同值域）
const VIEW_OPTIONS: SegmentOption<DefaultView>[] = [
  { value: "day", label: "日", icon: <Calendar size={14} strokeWidth={2} /> },
  { value: "week", label: "周", icon: <CalendarDays size={14} strokeWidth={2} /> },
  { value: "month", label: "月", icon: <CalendarRange size={14} strokeWidth={2} /> },
];

const NOTIFY_OPTIONS: SegmentOption<NotifyStyle>[] = [
  { value: "system", label: "系统通知" },
  { value: "floating", label: "居中悬浮框" },
  { value: "corner", label: "右下角悬浮框" },
];

// 每日工作时长档（小时）
const WORK_HOUR_OPTIONS: SelectOption<number>[] = [4, 6, 7, 8, 9, 10, 12].map((h) => ({
  value: h,
  label: `${h} 小时`,
}));

// 提醒档（null = 不提醒；0 = 准时）
const REMINDER_OPTIONS: SelectOption<number | null>[] = [
  { value: null, label: "不提醒" },
  { value: 0, label: "准时" },
  { value: 5, label: "提前 5 分钟" },
  { value: 10, label: "提前 10 分钟" },
  { value: 15, label: "提前 15 分钟" },
  { value: 30, label: "提前 30 分钟" },
];

const SOUND_OPTIONS: SelectOption<Sound>[] = [
  { value: "chime", label: "铃声" },
  { value: "bowl", label: "颂钵" },
  { value: "water", label: "水滴" },
  { value: "off", label: "关闭" },
];

// 服务端口行仅在打包版（Tauri + PROD）可编辑；dev 手动 uvicorn 固定 8000 置灰
const PORT_EDITABLE = isTauri() && import.meta.env.PROD;

function GeneralSettings({
  settings: s,
  onUpdate,
}: {
  settings: Settings;
  onUpdate: (patch: Partial<Settings>) => void;
}) {
  // 服务端口（组件内自治行）：serverPort = 已存配置（dev 显示固定 8000），
  // portDraft = 输入框草稿；挂载拉一次 Tauri 配置（StrictMode 双跑幂等无害）
  const [serverPort, setServerPortState] = useState(8000);
  const [portDraft, setPortDraft] = useState("8000");
  useEffect(() => {
    if (!PORT_EDITABLE) return;
    void fetchServerPort().then((p) => {
      if (p !== null) {
        setServerPortState(p);
        setPortDraft(String(p));
      }
    });
  }, []);

  // 失焦提交：整数且 1024–65535 且有变化才写 Tauri 配置；非法 / 未变 / 写失败回退旧值
  const commitPort = () => {
    const p = parseInt(portDraft, 10);
    if (Number.isInteger(p) && p >= 1024 && p <= 65535 && p !== serverPort) {
      void setServerPort(p).then((ok) => {
        if (ok) setServerPortState(p);
        else setPortDraft(String(serverPort));
      });
    } else {
      setPortDraft(String(serverPort));
    }
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">通用偏好</span>
        <span className="settings-page-sub">管理系统行为、外观表现与日历基础规则</span>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">系统与启动</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">开机自启动</span>
              <span className="settings-row-desc">登录系统后自动在后台运行 Clara</span>
            </div>
            <div className="settings-row-control">
              <SetToggle checked={s.autoStart} onChange={(v) => onUpdate({ autoStart: v })} />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">关闭主窗口时</span>
            </div>
            <div className="settings-row-control">
              <SetSegmented
                value={s.closeAction}
                options={CLOSE_OPTIONS}
                onChange={(v) => onUpdate({ closeAction: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">服务端口</span>
              <span className="settings-row-desc">
                {PORT_EDITABLE
                  ? "后台服务监听的本地端口，修改后重启应用生效"
                  : "开发模式下固定 8000，仅正式版可修改"}
              </span>
            </div>
            <div className="settings-row-control">
              <input
                className="set-port-input"
                disabled={!PORT_EDITABLE}
                inputMode="numeric"
                maxLength={5}
                value={portDraft}
                onChange={(e) => setPortDraft(e.target.value.replace(/\D/g, "").slice(0, 5))}
                onBlur={commitPort}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                }}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">外观</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">外观模式</span>
            </div>
            <div className="settings-row-control">
              <SetSegmented
                value={s.themeMode}
                options={THEME_OPTIONS}
                onChange={(v) => onUpdate({ themeMode: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">启动默认视图</span>
              <span className="settings-row-desc">应用启动时日历默认打开的视图</span>
            </div>
            <div className="settings-row-control">
              <SetSegmented
                value={s.defaultView}
                options={VIEW_OPTIONS}
                onChange={(v) => onUpdate({ defaultView: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">小部件透明度</span>
              <span className="settings-row-desc">桌面小部件窗口的透出程度，越大越透</span>
            </div>
            <div className="settings-row-control">
              <SetSlider
                value={s.widgetTransparency}
                min={10}
                max={80}
                format={(v) => `${v}%`}
                onCommit={(v) => onUpdate({ widgetTransparency: v })}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">日历与排期</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">日历可视时间范围</span>
              <span className="settings-row-desc">日历时间轴的显示区间</span>
            </div>
            <div className="settings-row-control">
              <SetTimeRangeSelect
                start={s.dayStart}
                end={s.dayEnd}
                minSpan={240}
                onStartChange={(h) => onUpdate({ dayStart: h })}
                onEndChange={(h) => onUpdate({ dayEnd: h })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">午休时间</span>
              <span className="settings-row-desc">日历上标注建议午休时段</span>
            </div>
            <div className="settings-row-control">
              <div className={`set-lunch-group${s.lunchEnabled ? "" : " off"}`}>
                <SetTimeRangeSelect
                  start={s.lunchStart}
                  end={s.lunchEnd}
                  minSpan={30}
                  onStartChange={(h) => onUpdate({ lunchStart: h })}
                  onEndChange={(h) => onUpdate({ lunchEnd: h })}
                />
              </div>
              <SetToggle
                checked={s.lunchEnabled}
                onChange={(v) => onUpdate({ lunchEnabled: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">每日工作时长</span>
              <span className="settings-row-desc">负载指示与工作量统计的基准</span>
            </div>
            <div className="settings-row-control">
              <SetSelect
                value={s.workHours}
                options={WORK_HOUR_OPTIONS}
                onChange={(h) => onUpdate({ workHours: h })}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">提醒与通知</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">日程提醒时间</span>
              <span className="settings-row-desc">日程开始前多久弹出提醒</span>
            </div>
            <div className="settings-row-control">
              <SetSelect
                value={s.reminderLeadMin}
                options={REMINDER_OPTIONS}
                onChange={(v) => onUpdate({ reminderLeadMin: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">通知形式</span>
              <span className="settings-row-desc">仅作用于日程提醒（助手消息在日程助手聊天框查看）</span>
            </div>
            <div className="settings-row-control">
              <SetSegmented
                value={s.notifyStyle}
                options={NOTIFY_OPTIONS}
                onChange={(v) => onUpdate({ notifyStyle: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">提示音</span>
              <span className="settings-row-desc">日程开始时播放的音效（"不提醒"档仍准时播放）</span>
            </div>
            <div className="settings-row-control">
              <SetSelect
                value={s.sound}
                options={SOUND_OPTIONS}
                onChange={(v) => {
                  playSound(v);
                  onUpdate({ sound: v });
                }}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">助手消息轻提示音</span>
              <span className="settings-row-desc">
                晨报 / 夜报 / 过载预警等助手消息到达时轻响一次（用上方所选音色）；关闭则完全静默，未读仅以日程页 ✨ 按钮角标提示（右栏折叠时可见）
              </span>
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={s.assistantChime}
                onChange={(v) => onUpdate({ assistantChime: v })}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default GeneralSettings;
