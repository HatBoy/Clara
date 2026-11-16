// 首启入职访谈（2026-11-17 批 1）：三步确定性向导，不依赖模型即可完成。
// 答案落 app_settings + 记忆规则；「稍后」只关本次会话，「跳过引导」写完成标记。
import { useState } from "react";
import { ArrowLeft, ArrowRight, Check, X } from "lucide-react";
import { completeOnboarding, toFrontendSettings, type ApiSettings } from "../lib/api";
import type { Settings } from "../lib/settings";
import "./OnboardingWizard.css";

const PEAK_OPTIONS = [
  { id: "morning", label: "上午", text: "精力高峰在上午，高认知任务优先排在上午" },
  { id: "afternoon", label: "下午", text: "精力高峰在下午，高认知任务优先排在下午" },
  { id: "evening", label: "晚上", text: "精力高峰在晚上，高认知任务可排晚间" },
] as const;

const MEETING_OPTIONS = [
  { id: "morning", label: "上午集中", text: "会议尽量集中在上午，下午保留整块专注时间" },
  { id: "afternoon", label: "下午集中", text: "会议尽量集中在下午，上午保留整块专注时间" },
  { id: "no_evening", label: "晚间不排", text: "晚间尽量不安排会议" },
] as const;

const LEAD_OPTIONS = [
  { value: 5, label: "提前 5 分钟" },
  { value: 10, label: "提前 10 分钟" },
  { value: 15, label: "提前 15 分钟" },
  { value: 30, label: "提前 30 分钟" },
];

function fmtHour(h: number): string {
  const hh = Math.floor(h);
  const mm = Math.round((h - hh) * 60);
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

export default function OnboardingWizard({
  settings,
  onClose,
  onDone,
  onOpenModel,
}: {
  settings: Settings;
  onClose: () => void; // 稍后：不写标记，仅关本次会话
  onDone: (next: Settings) => void; // 完成/跳过：回填设置并关闭
  onOpenModel: () => void;
}) {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 第 1 步：你的一天
  const [dayStart, setDayStart] = useState(settings.dayStart);
  const [dayEnd, setDayEnd] = useState(settings.dayEnd);
  const [workHours, setWorkHours] = useState(settings.workHours);
  const [lunchEnabled, setLunchEnabled] = useState(settings.lunchEnabled);
  const [lunchStart, setLunchStart] = useState(settings.lunchStart);
  const [lunchEnd, setLunchEnd] = useState(settings.lunchEnd);
  // 第 2 步：你的节奏
  const [peak, setPeak] = useState<string>("morning");
  const [meetingPref, setMeetingPref] = useState<string>("morning");
  const [leadMin, setLeadMin] = useState<number>(settings.reminderLeadMin ?? 15);
  // 第 3 步：AI 助手
  const [aiEnabled, setAiEnabled] = useState(settings.aiEnabled);

  const submit = (skipped: boolean) => {
    setBusy(true);
    setError(null);
    const rules = skipped
      ? []
      : [
          ...(lunchEnabled
            ? [{
                category: "作息规律",
                content: `${fmtHour(lunchStart)}-${fmtHour(lunchEnd)} 是午休时段，尽量不安排会议或高强度任务`,
                evidence: "首启入职访谈",
              }]
            : []),
          {
            category: "精力节律",
            content: PEAK_OPTIONS.find((o) => o.id === peak)?.text ?? "",
            evidence: "首启入职访谈",
          },
          {
            category: "会议习惯",
            content: MEETING_OPTIONS.find((o) => o.id === meetingPref)?.text ?? "",
            evidence: "首启入职访谈",
          },
        ].filter((r) => r.content);
    completeOnboarding({
      settings: skipped
        ? undefined
        : {
            calendar_start_min: Math.round(dayStart * 60),
            calendar_end_min: Math.round(dayEnd * 60),
            work_minutes_per_day: Math.round(workHours * 60),
            lunch_enabled: lunchEnabled,
            lunch_start_min: Math.round(lunchStart * 60),
            lunch_end_min: Math.round(lunchEnd * 60),
            reminder_lead_min: leadMin,
            ai_enabled: aiEnabled,
          },
      rules,
      skipped,
    })
      .then((res) => onDone(toFrontendSettings(res.settings as ApiSettings)))
      .catch((e) => {
        setError(e instanceof Error ? e.message : "保存失败，请重试");
        setBusy(false);
      });
  };

  return (
    <div className="onb-overlay" onClick={onClose}>
      <div
        className="onb-dialog"
        role="dialog"
        aria-label="入职访谈"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="onb-head">
          <span className="onb-title">欢迎使用 Clara · 3 分钟了解你</span>
          <button type="button" className="onb-close" onClick={onClose} aria-label="稍后再说">
            <X size={15} strokeWidth={2} />
          </button>
        </div>
        <div className="onb-steps">
          {["你的一天", "你的节奏", "AI 助手"].map((t, i) => (
            <span key={t} className={`onb-step${i === step ? " active" : ""}`}>
              {i + 1}. {t}
            </span>
          ))}
        </div>

        <div className="onb-body">
          {step === 0 && (
            <div className="onb-form">
              <label className="onb-row">
                <span>每天开始工作</span>
                <input type="number" min={0} max={23} step={0.5} value={dayStart}
                       onChange={(e) => setDayStart(Number(e.target.value))} />
              </label>
              <label className="onb-row">
                <span>每天结束工作</span>
                <input type="number" min={1} max={24} step={0.5} value={dayEnd}
                       onChange={(e) => setDayEnd(Number(e.target.value))} />
              </label>
              <label className="onb-row">
                <span>每日有效工时（小时）</span>
                <input type="number" min={1} max={12} step={0.5} value={workHours}
                       onChange={(e) => setWorkHours(Number(e.target.value))} />
              </label>
              <label className="onb-row">
                <span>有午休时段</span>
                <input type="checkbox" checked={lunchEnabled}
                       onChange={(e) => setLunchEnabled(e.target.checked)} />
              </label>
              {lunchEnabled && (
                <div className="onb-row">
                  <span>午休 {fmtHour(lunchStart)} - {fmtHour(lunchEnd)}</span>
                  <span className="onb-inline">
                    <input type="number" min={0} max={23} step={0.5} value={lunchStart}
                           onChange={(e) => setLunchStart(Number(e.target.value))} />
                    <input type="number" min={1} max={24} step={0.5} value={lunchEnd}
                           onChange={(e) => setLunchEnd(Number(e.target.value))} />
                  </span>
                </div>
              )}
            </div>
          )}

          {step === 1 && (
            <div className="onb-form">
              <label className="onb-row">
                <span>精力高峰</span>
                <select value={peak} onChange={(e) => setPeak(e.target.value)}>
                  {PEAK_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </label>
              <label className="onb-row">
                <span>会议偏好</span>
                <select value={meetingPref} onChange={(e) => setMeetingPref(e.target.value)}>
                  {MEETING_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </label>
              <label className="onb-row">
                <span>提醒提前量</span>
                <select value={leadMin} onChange={(e) => setLeadMin(Number(e.target.value))}>
                  {LEAD_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </label>
            </div>
          )}

          {step === 2 && (
            <div className="onb-form">
              <label className="onb-row">
                <span>启用 AI 助手</span>
                <input type="checkbox" checked={aiEnabled}
                       onChange={(e) => setAiEnabled(e.target.checked)} />
              </label>
              <p className="onb-note">
                AI 排期与建议需要模型接入。可以先跳过，随时在设置里配置。
              </p>
              <button type="button" className="onb-link" onClick={onOpenModel}>
                去配置模型 →
              </button>
            </div>
          )}
          {error && <div className="onb-error">{error}</div>}
        </div>

        <div className="onb-foot">
          <button type="button" className="onb-skip" disabled={busy}
                  onClick={() => submit(true)}>
            跳过引导
          </button>
          <span className="onb-spacer" />
          {step > 0 && (
            <button type="button" className="onb-btn" disabled={busy}
                    onClick={() => setStep(step - 1)}>
              <ArrowLeft size={13} strokeWidth={2} /> 上一步
            </button>
          )}
          {step < 2 ? (
            <button type="button" className="onb-btn primary"
                    onClick={() => setStep(step + 1)}>
              下一步 <ArrowRight size={13} strokeWidth={2} />
            </button>
          ) : (
            <button type="button" className="onb-btn primary" disabled={busy}
                    onClick={() => submit(false)}>
              <Check size={13} strokeWidth={2.4} /> 完成
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
