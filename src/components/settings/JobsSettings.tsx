// "定时任务"设置页（2026-10-20 增）：每日节奏组（晨间/采集/夜间，受控走
// App 的 settings PATCH 链，自 AgentSettings 迁入；2026-10-21 复查批 D1 并入
// 采集调用预算，四流各加独立开关）+ AI 自动增强组与监控组
// （GET /api/jobs 注册表展示：desc 文案与识别口径后端单源，带最近运行时刻；
// prep / hardtask 行挂受控开关）+ 自定义每日简报组（/api/jobs/briefs 自管理
// CRUD，≤3 条 / 同刻唯一 / 30 分钟粒度，违规 422 行内提示）。行样式复用通用
// 偏好与记忆画像页的 .settings-*/.mem-* 类
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import {
  createBrief,
  deleteBrief,
  fetchBriefs,
  fetchJobs,
  updateBrief,
  type CustomBrief,
  type Job,
} from "../../lib/ai";
import { runIngestNow } from "../../lib/api";
import type { Settings } from "../../lib/settings";
import { SetSelect, SetToggle, type SelectOption } from "./SettingsControls";
import "./GeneralSettings.css";

// 时刻档（小时小数）——晨间/采集（7:30–10:00，迁自 AgentSettings）
const BRIEF_OPTIONS: SelectOption<number>[] = [7.5, 8, 8.5, 9, 9.5, 10].map((h) => ({
  value: h,
  label: fmtHour(h),
}));

// 采集提炼调用预算档（每日 LLM 调用次数代理；0 = 不限，默认 30；迁自 AgentSettings）
const BUDGET_OPTIONS: SelectOption<number>[] = [0, 10, 30, 50, 100].map((n) => ({
  value: n,
  label: n === 0 ? "不限" : `${n} 次`,
}));
// 采集频率档（每日轮数，2026-10-23）：首轮在采集时刻，后续槽当天顺延
// （2 次→+5h；3 次→+4h/+8h；4 次→+3h/+6h/+9h）；默认 2
const INGEST_RUNS_OPTIONS: SelectOption<number>[] = [1, 2, 3, 4].map((n) => ({
  value: n,
  label: `每天 ${n} 次`,
}));
// 夜间报点档（18:00–23:30 每 30 分钟，12 档）
const NIGHT_OPTIONS: SelectOption<number>[] = Array.from({ length: 12 }, (_, i) => {
  const h = 18 + i * 0.5;
  return { value: h, label: fmtHour(h) };
});
// 自定义简报全档（00:00–23:30，48 档）
const ALL_TIME_OPTIONS: SelectOption<number>[] = Array.from({ length: 48 }, (_, i) => {
  const h = i * 0.5;
  return { value: h, label: fmtHour(h) };
});

function fmtHour(h: number): string {
  return `${String(Math.floor(h)).padStart(2, "0")}:${h % 1 ? "30" : "00"}`;
}

// 最近运行时刻文案（jobs 行 last_run_at → "今日 20:05" / "MM-DD HH:MM"；null → 未运行过）
function fmtLastRun(iso: string | null): string {
  if (!iso) return "尚未运行过";
  const d = new Date(iso);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const today = new Date();
  const sameDay =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate();
  const md = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return sameDay ? `今日 ${hm}` : `${md} ${hm}`;
}

function JobsSettings({
  settings: s,
  onUpdate,
}: {
  settings: Settings;
  onUpdate: (patch: Partial<Settings>) => void;
}) {
  // 注册表（AI 自动增强 / 监控行 + 简报行最近运行时刻）；拉取失败静默（desc 已有静态兜底，
  // 仅缺最近运行时刻）
  const [jobs, setJobs] = useState<Job[]>([]);
  // 自定义简报行（自管理 CRUD）
  const [briefs, setBriefs] = useState<CustomBrief[]>([]);
  const [error, setError] = useState<string | null>(null);
  // 新增行表单（newMin 分钟语义，与 brief_min 同口径——曾初始 12 被 value 当分钟除 60
  // 显示"0.2"，且 addBrief 二次 ×60 与 onChange 的 ×60 叠加致改选时刻提交 60 倍脏值；
  // 默认 12:00 = 720 分钟，2026-10-21 修复）
  const [newMin, setNewMin] = useState(720);
  const [newInstruction, setNewInstruction] = useState("");
  // 立即采集行内反馈（自 AgentSettings 迁入，3 秒自清）
  const [runHint, setRunHint] = useState<{ ok: boolean; text: string } | null>(null);
  const [hintTimer, setHintTimer] = useState<number | null>(null);

  const reload = useCallback(() => {
    void fetchJobs().then((r) => setJobs(r.jobs)).catch(() => {});
    void fetchBriefs().then(setBriefs).catch(() => {});
  }, []);

  useEffect(() => {
    reload();
    return () => {
      if (hintTimer !== null) window.clearTimeout(hintTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const jobByKey = (key: string): Job | undefined => jobs.find((j) => j.key === key);
  // requires_ai 行在 AI 总闸关闭时盖「已随 AI 关闭」角标（开关在智能体页）
  const aiOff = (j: Job) => j.requires_ai && !s.aiEnabled;

  const runNow = () => {
    runIngestNow()
      .then(() => {
        setRunHint({ ok: true, text: "已开始，结果稍后推送到助手面板" });
        const t = window.setTimeout(() => setRunHint(null), 3000);
        setHintTimer(t);
      })
      .catch((e) => {
        const status = (e as { status?: number }).status;
        if (status === 409) setRunHint({ ok: false, text: "已有一次采集在跑，稍候再试" });
        // 422 显示后端真实 detail（采集开关/AI 总闸/零激活三条件指引各不同，不合并笼统文案）
        else if (status === 422) setRunHint({ ok: false, text: extractDetail(e) });
        else setRunHint({ ok: false, text: "请求失败：后端服务不可用" });
      });
  };

  // 简报 CRUD（违规 422 → error 行内提示，3s 自清）
  const flashError = (text: string) => {
    setError(text);
    window.setTimeout(() => setError(null), 3000);
  };
  const addBrief = () => {
    const instruction = newInstruction.trim();
    if (!instruction) {
      flashError("请先填写简报指令");
      return;
    }
    createBrief({ brief_min: newMin, instruction })
      .then(() => {
        setNewInstruction("");
        reload();
      })
      .catch((e) => flashError(extractDetail(e)));
  };
  const patchBrief = (id: string, body: Parameters<typeof updateBrief>[1]) => {
    updateBrief(id, body)
      .then(reload)
      .catch((e) => flashError(extractDetail(e)));
  };

  const morningJob = jobByKey("morning");
  const ingestJob = jobByKey("ingest");
  const nightlyJob = jobByKey("nightly");
  const prep = jobByKey("prep");
  const hardtask = jobByKey("hardtask");
  const weekly = jobByKey("weekly_report");
  const minutesInvite = jobByKey("minutes_invite");
  const autocompact = jobByKey("chat_autocompact");
  const heartbeat = jobByKey("heartbeat");
  // 每日节奏行也盖「已随 AI 关闭」（requires_ai 流，角标曾只盖 prep/hardtask）
  const dailyAiOff = <span className="settings-action-hint bad">已随 AI 关闭</span>;

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">定时任务</span>
        <span className="settings-page-sub">每日节奏与 AI 自动增强的触发时刻</span>
      </div>

      {error && <div className="mem-error">{error}</div>}

      {/* —— 每日节奏（受控走 settings PATCH 链；成对行合并：开关 + 时刻并排一行，
          2026-10-21 五点修缮——原开关/时刻拆行太重复；AI 总闸关闭时全组控件禁点，
          各流自身设置保留，重开总闸即恢复）—— */}
      <div className="settings-group">
        <span className="settings-group-title">每日节奏</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">
                晨间对齐
                {!s.aiEnabled && dailyAiOff}
              </span>
              <span className="settings-row-desc">
                {morningJob?.desc ?? "每天早上生成今日简报：日程总览、任务预警与防坑指南。"}
              </span>
              {morningJob && (
                <span className="settings-row-desc">最近运行 {fmtLastRun(morningJob.last_run_at)}</span>
              )}
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={s.morningEnabled}
                onChange={(v) => onUpdate({ morningEnabled: v })}
                disabled={!s.aiEnabled}
              />
              <SetSelect
                value={s.morningBriefAt}
                options={BRIEF_OPTIONS}
                onChange={(h) => onUpdate({ morningBriefAt: h })}
                disabled={!s.aiEnabled}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">
                每日数据采集
                {!s.aiEnabled && dailyAiOff}
              </span>
              <span className="settings-row-desc">
                {ingestJob?.desc ?? "定时汇聚各数据源的新动态，提炼成收件箱建议与素材。"}
              </span>
              {ingestJob && (
                <span className="settings-row-desc">最近运行 {fmtLastRun(ingestJob.last_run_at)}</span>
              )}
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={s.ingestEnabled}
                onChange={(v) => onUpdate({ ingestEnabled: v })}
                disabled={!s.aiEnabled}
              />
              <SetSelect
                value={s.ingestAt}
                options={BRIEF_OPTIONS}
                onChange={(h) => onUpdate({ ingestAt: h })}
                disabled={!s.aiEnabled}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">
                采集频率
                {!s.aiEnabled && dailyAiOff}
              </span>
              <span className="settings-row-desc">
                首轮在上方时刻，后续轮次当天顺延（2 次→+5 小时；3 次→+4/+8；4 次→+3/+6/+9）；无新数据时静默执行不打扰。
              </span>
            </div>
            <div className="settings-row-control">
              <SetSelect
                value={s.ingestRunsPerDay}
                options={INGEST_RUNS_OPTIONS}
                onChange={(n) => onUpdate({ ingestRunsPerDay: n })}
                disabled={!s.aiEnabled}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">采集调用预算</span>
              <span className="settings-row-desc">
                每日采集提炼的模型调用次数上限，超限后当日只出综述不再建议（0 = 不限）
              </span>
            </div>
            <div className="settings-row-control">
              <SetSelect
                value={s.ingestLlmBudget}
                options={BUDGET_OPTIONS}
                onChange={(n) => onUpdate({ ingestLlmBudget: n })}
                disabled={!s.aiEnabled}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">立即采集</span>
              <span className="settings-row-desc">不等采集时刻，手动跑一次全源采集与提炼</span>
            </div>
            <div className="settings-row-control">
              {runHint && (
                <span className={`settings-action-hint${runHint.ok ? " ok" : " bad"}`}>
                  {runHint.text}
                </span>
              )}
              <button
                type="button"
                className="settings-action-btn"
                onClick={runNow}
                disabled={!s.aiEnabled}
              >
                立即采集
              </button>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">
                夜间重要整理
                {!s.aiEnabled && dailyAiOff}
              </span>
              <span className="settings-row-desc">
                {nightlyJob?.desc ?? "到点后趁空闲按优先级挑出未排期待办（至多 5 件）预排时间草案供确认，并整理记忆画像。"}
              </span>
              {nightlyJob && (
                <span className="settings-row-desc">最近运行 {fmtLastRun(nightlyJob.last_run_at)}</span>
              )}
              <span className="settings-row-desc">报点同时触发会前准备与高难协助</span>
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={s.nightlyEnabled}
                onChange={(v) => onUpdate({ nightlyEnabled: v })}
                disabled={!s.aiEnabled}
              />
              <SetSelect
                value={s.nightlyStartAt}
                options={NIGHT_OPTIONS}
                onChange={(h) => onUpdate({ nightlyStartAt: h })}
                disabled={!s.aiEnabled}
              />
            </div>
          </div>
        </div>
      </div>

      {/* —— AI 自动增强（注册表行 + 各自受控开关）—— */}
      <div className="settings-group">
        <span className="settings-group-title">AI 自动增强</span>
        <div className="settings-card">
          {prep && (
            <JobRow
              job={prep}
              aiOff={aiOff(prep)}
              control={
                <SetToggle
                  checked={s.prepEnabled}
                  onChange={(v) => onUpdate({ prepEnabled: v })}
                  disabled={!s.aiEnabled}
                />
              }
            />
          )}
          {hardtask && (
            <JobRow
              job={hardtask}
              aiOff={aiOff(hardtask)}
              control={
                <SetToggle
                  checked={s.hardtaskEnabled}
                  onChange={(v) => onUpdate({ hardtaskEnabled: v })}
                  disabled={!s.aiEnabled}
                />
              }
            />
          )}
          {/* 周报自动生成（2026-11-07 统计 120 分档）：周日晚随夜间报点（时刻由
              夜间整理行统一管，本行无时刻选择）；time_min 非 null 使 desc 副行
              走「最近运行 + 仅周日晚间运行」分支 */}
          {weekly && (
            <JobRow
              job={weekly}
              aiOff={aiOff(weekly)}
              control={
                <SetToggle
                  checked={s.weeklyReportEnabled}
                  onChange={(v) => onUpdate({ weeklyReportEnabled: v })}
                  disabled={!s.aiEnabled}
                />
              }
            />
          )}
          {/* 会后纪要邀请（2026-11-10 批 3）：事件窗驱动非报点（time_min=null 走
              time_note 副行「重要会议结束后 15 分钟」）；拒绝过的会议不再重复邀 */}
          {minutesInvite && (
            <JobRow
              job={minutesInvite}
              aiOff={aiOff(minutesInvite)}
              control={
                <SetToggle
                  checked={s.minutesEnabled}
                  onChange={(v) => onUpdate({ minutesEnabled: v })}
                  disabled={!s.aiEnabled}
                />
              }
            />
          )}
          {/* 对话上下文压缩（2026-10-26）：挂轮末非 cron，JobRow time_min=null 走
              time_note 副行；关 = 轮末零 LLM（/compact 手动指令不受影响） */}
          {autocompact && (
            <JobRow
              job={autocompact}
              aiOff={aiOff(autocompact)}
              control={
                <SetToggle
                  checked={s.chatAutocompactEnabled}
                  onChange={(v) => onUpdate({ chatAutocompactEnabled: v })}
                  disabled={!s.aiEnabled}
                />
              }
            />
          )}
          {/* 教练溯源深探（2026-11-01 优化批 9）：有界工具循环——开 = 任务教练
              回源探测从首命中扩到至多 2 目标（每源每日帽与补采共享）；默认关 */}
          <JobRow
            job={{
              key: "coach_probe_deep",
              title: "教练溯源深探",
              desc: "任务教练回源搜索更多素材（至多 2 个目标，与补采共享每日限额）",
              enabled: true,
              requires_ai: true,
              time_min: null,
              time_note: "仅对话时触发 · 默认关",
              last_run_at: null,
            }}
            aiOff={!s.aiEnabled}
            control={
              <SetToggle
                checked={s.coachProbeDeep}
                onChange={(v) => onUpdate({ coachProbeDeep: v })}
                disabled={!s.aiEnabled}
              />
            }
          />
        </div>
      </div>

      {/* —— 自定义每日简报（自管理 CRUD，≤3 条）—— */}
      <div className="settings-group">
        <span className="settings-group-title">
          自定义每日简报（{briefs.length}/3）
        </span>
        <div className="settings-card mem-list">
          {briefs.length === 0 && (
            <div className="mem-empty">
              每天固定时刻按你的指令生成简报——比如「早 8 点给我今日待办重点」
            </div>
          )}
          {briefs.map((b) => {
            const j = jobByKey(`brief:${b.id}`);
            return (
              <div key={b.id} className="mem-row">
                <SetToggle
                  checked={b.enabled}
                  onChange={(v) => patchBrief(b.id, { enabled: v })}
                  disabled={!s.aiEnabled}
                />
                <SetSelect
                  value={b.brief_min / 60}
                  options={ALL_TIME_OPTIONS}
                  width={76}
                  onChange={(h) => patchBrief(b.id, { brief_min: Math.round(h * 60) })}
                  disabled={!s.aiEnabled}
                />
                <input
                  className="mem-input mem-input-content"
                  maxLength={200}
                  defaultValue={b.instruction}
                  placeholder="简报指令，如：总结今日未完成事项"
                  // AI 总闸关闭时禁编辑（生成必停，改了也不跑）；删除保留——数据清理与 AI 无关
                  disabled={!s.aiEnabled}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v && v !== b.instruction) patchBrief(b.id, { instruction: v });
                    else e.target.value = b.instruction; // 空白改回现值
                  }}
                />
                <span className="mem-confidence" title="最近一次生成的时刻">
                  {j ? fmtLastRun(j.last_run_at) : ""}
                </span>
                <div className="mem-ops">
                  <button
                    type="button"
                    className="mem-icon-btn danger"
                    aria-label="删除简报"
                    onClick={() =>
                      deleteBrief(b.id)
                        .then(reload)
                        .catch((e) => flashError(extractDetail(e)))
                    }
                  >
                    <X size={14} strokeWidth={2} />
                  </button>
                </div>
              </div>
            );
          })}
          {briefs.length < 3 && s.aiEnabled && (
            <div className="mem-row">
              <SetSelect
                value={newMin / 60}
                options={ALL_TIME_OPTIONS}
                width={76}
                onChange={(h) => setNewMin(Math.round(h * 60))}
              />
              <input
                className="mem-input mem-input-content"
                maxLength={200}
                value={newInstruction}
                placeholder="新简报指令（每天此刻按指令汇总当日数据）"
                onChange={(e) => setNewInstruction(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) addBrief();
                }}
              />
              <button type="button" className="mem-btn primary" onClick={addBrief}>
                添加
              </button>
            </div>
          )}
        </div>
      </div>

      {/* —— 监控（只读）—— */}
      <div className="settings-group">
        <span className="settings-group-title">监控</span>
        <div className="settings-card">{heartbeat && <JobRow job={heartbeat} aiOff={false} />}</div>
      </div>
    </div>
  );
}

// 注册表行：名称 + desc（含识别口径，后端单源）+ 副文案（最近运行/时刻补充）；
// control 挂受控开关（prep / hardtask 各自独立启用，走 settings PATCH 链）
function JobRow({ job, aiOff, control }: { job: Job; aiOff: boolean; control?: ReactNode }) {
  return (
    <div className="settings-row">
      <div className="settings-row-info">
        <span className="settings-row-name">
          {job.title}
          {aiOff && <span className="settings-action-hint bad">已随 AI 关闭</span>}
        </span>
        <span className="settings-row-desc">{job.desc}</span>
        {/* 时刻驱动行：最近运行（无记录不谎报）；非时刻驱动行（心跳）：纯规则不落
            workflow_runs，展示内存口径的最近检查时刻（后端 getter 带出，重启重计）+ 频率 */}
        {job.time_min !== null ? (
          <span className="settings-row-desc">
            {job.last_run_at ? `最近运行 ${fmtLastRun(job.last_run_at)}` : "尚未运行过"}
            {job.time_note ? ` · ${job.time_note}` : ""}
          </span>
        ) : (
          (job.last_run_at || job.time_note) && (
            <span className="settings-row-desc">
              {job.last_run_at ? `最近检查 ${fmtLastRun(job.last_run_at)}` : ""}
              {job.last_run_at && job.time_note ? " · " : ""}
              {job.time_note ?? ""}
            </span>
          )
        )}
      </div>
      {control && <div className="settings-row-control">{control}</div>}
    </div>
  );
}

// 后端 422 detail 优先（中文引导），网络失败兜底
function extractDetail(e: unknown): string {
  const status = (e as { status?: number }).status;
  const detail = (e as { detail?: string }).detail;
  if (detail) return detail;
  return status ? `请求失败：${status}` : "请求失败：后端服务不可用";
}

export default JobsSettings;
