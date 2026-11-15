// "同步备份"设置页（2026-11-14）：WebDAV 同步 / 导入导出 / 自动备份 三组卡片。
// 骨架复用 GeneralSettings.css（.settings-* 族）；合并导入冲突列表等自有样式在
// SyncSettings.css（.sync-* 族）。自管理面板：mount 拉取 + 动作后局部刷新；
// refreshKey 由 App 的 SSE sync 事件驱动（同步完成后状态行即时更新）。
//
// WebDAV 同步：三路合并引擎在后端 services/sync_engine.py（防抖 3s + 30s 远端
// 轻探测 + 周期 300s 兜底全自动跑），本页只做配置与状态回显；冷启动双端有
// 数据同样自动 union 合并（2026-11-15 迭代四起无确认流程——免手动，用户拍板）。
// 导入导出：备份两件套自"关于我们"迁入（覆盖恢复 = 整库替换需重启后端），
// 合并导入 = 两阶段（无冲突直通应用；有冲突先看明细再选覆盖/跳过），原地
// 合并不需重启、不删除本地行。
import { useCallback, useEffect, useRef, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";
import {
  analyzeMerge,
  applyMerge,
  createBackup,
  fetchSyncConfig,
  resetSyncBase,
  restoreBackup,
  runSyncNow,
  testWebdav,
  updateSyncConfig,
  type MergeAnalyzeResult,
  type SyncConfig,
  type SyncConfigPatch,
} from "../../lib/api";
import { API_BASE } from "../../lib/api";
import { isTauri, pickBackupZip, pickDirectory, restartBackend, saveBackupPath } from "../../lib/tauri";
import { SetInput, SetSegmented, SetToggle } from "./SettingsControls";
import "./GeneralSettings.css";
import "./AboutSettings.css"; // .about-link（确认/取消文字钮，与备份恢复行同款）
import "./SyncSettings.css";

/** iso → "MM-DD HH:mm"（今年省年份口径不搞，简单稳定） */
function fmtTime(iso: string | null): string {
  if (!iso) return "从未";
  try {
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  } catch {
    return iso;
  }
}

function extractDetail(e: unknown): string {
  if (e instanceof Error && "detail" in e && typeof (e as { detail?: unknown }).detail === "string") {
    return (e as { detail: string }).detail;
  }
  return e instanceof Error ? e.message : "操作失败";
}

type TestState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "done"; ok: boolean; ms: number; message: string };

type RunState =
  | { kind: "idle" }
  | { kind: "polling" } // 已触发，轮询 syncing 至完成
  | { kind: "done"; text: string; ok: boolean };

type BackupState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; text: string }
  | { kind: "bad"; text: string }
  | { kind: "confirm"; path: string };

type MergeState =
  | { kind: "idle" }
  | { kind: "analyzing" }
  | { kind: "review"; path: string; result: MergeAnalyzeResult; strategy: "overwrite" | "skip" }
  | { kind: "applying" }
  | { kind: "done"; text: string }
  | { kind: "bad"; text: string };

const RUN_POLL_MS = 2_000;
const RUN_POLL_MAX = 150; // 5 分钟兜底停

export default function SyncSettings({ refreshKey = 0 }: { refreshKey?: number }) {
  const [cfg, setCfg] = useState<SyncConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ kind: "idle" });
  const [run, setRun] = useState<RunState>({ kind: "idle" });
  const [resetConfirm, setResetConfirm] = useState(false);
  const [backup, setBackup] = useState<BackupState>({ kind: "idle" });
  const [merge, setMerge] = useState<MergeState>({ kind: "idle" });
  const [version, setVersion] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  const reload = useCallback(async () => {
    try {
      const c = await fetchSyncConfig();
      if (!mounted.current) return;
      setCfg(c);
      setLoadError(null);
    } catch (e) {
      if (mounted.current) setLoadError(extractDetail(e));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    reload();
    getVersion().then((v) => setVersion(`v${v}`)).catch(() => {/* 非桌面 */});
    return () => {
      mounted.current = false;
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [reload]);

  // SSE sync 事件（App 转发 refreshKey）：设置页开着时状态行即时刷新
  useEffect(() => {
    if (refreshKey > 0) reload();
  }, [refreshKey, reload]);

  // 手动同步轮询：syncing 变 false → 完成态（结果看 last_sync_*）
  const pollRunDone = useCallback((rounds = 0) => {
    if (!mounted.current || rounds > RUN_POLL_MAX) return;
    pollTimer.current = window.setTimeout(async () => {
      try {
        const c = await fetchSyncConfig();
        if (!mounted.current) return;
        setCfg(c);
        if (!c.syncing) {
          setRun({
            kind: "done",
            ok: c.last_sync_ok === true,
            text:
              c.last_sync_ok === true
                ? `已同步（${fmtTime(c.last_sync_at)}）`
                : c.last_sync_error || "同步失败",
          });
          return;
        }
        pollRunDone(rounds + 1);
      } catch {
        if (mounted.current) pollRunDone(rounds + 1);
      }
    }, RUN_POLL_MS);
  }, []);

  /** 单字段保存（SetInput 失焦 / 开关翻转；空串归 null 停用） */
  const save = async (patch: SyncConfigPatch) => {
    if (!cfg) return;
    try {
      const next = await updateSyncConfig(patch);
      if (!mounted.current) return;
      setCfg(next);
      setSaveError(null);
    } catch (e) {
      if (mounted.current) setSaveError(extractDetail(e));
    }
  };

  const doTest = async () => {
    if (!cfg) return;
    setTest({ kind: "busy" });
    try {
      // 带草稿三件（未保存的输入即时可测；后端回落库内值）
      const res = await testWebdav({
        url: cfg.webdav_url ?? undefined,
        username: cfg.webdav_username ?? undefined,
        password: cfg.webdav_password ?? undefined,
      });
      if (!mounted.current) return;
      setTest({ kind: "done", ok: res.ok, ms: res.ms, message: res.message });
      reload(); // last_test_* 已落库
    } catch (e) {
      if (mounted.current) setTest({ kind: "done", ok: false, ms: 0, message: extractDetail(e) });
    }
  };

  const doRun = async () => {
    setRun({ kind: "polling" });
    try {
      const res = await runSyncNow();
      if (!mounted.current) return;
      if (!res.started) {
        setRun({ kind: "done", ok: false, text: res.error || "无法启动同步" });
        return;
      }
      reload();
      pollRunDone();
    } catch (e) {
      if (mounted.current) setRun({ kind: "done", ok: false, text: extractDetail(e) });
    }
  };

  const doResetBase = async () => {
    setResetConfirm(false);
    try {
      await resetSyncBase();
      if (mounted.current) setRun({ kind: "done", ok: true, text: "同步基线已重置，下轮同步将重新对齐" });
      reload();
    } catch (e) {
      if (mounted.current) setRun({ kind: "done", ok: false, text: extractDetail(e) });
    }
  };

  // —— 备份两件套（自 AboutSettings 迁移；覆盖恢复 = 整库替换需重启）———

  const doExport = async () => {
    const path = await saveBackupPath();
    if (!path) return;
    setBackup({ kind: "busy" });
    try {
      const res = await createBackup(path, version);
      if (!mounted.current) return;
      setBackup({ kind: "ok", text: `已导出到 ${path}（${(res.size_bytes / 1048576).toFixed(1)} MB）` });
    } catch (e) {
      if (mounted.current) setBackup({ kind: "bad", text: extractDetail(e) });
    }
  };

  const waitBackendReady = async (rounds = 40): Promise<boolean> => {
    for (let i = 0; i < rounds; i++) {
      try {
        const res = await fetch(`${API_BASE}/api/health`);
        if (res.ok) return true;
      } catch { /* 尚未监听，继续轮询 */ }
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  };

  const doRestore = async (path: string) => {
    setBackup({ kind: "busy" });
    try {
      const res = await restoreBackup(path);
      if (!res.ok) {
        if (mounted.current) setBackup({ kind: "bad", text: res.message });
        return;
      }
      if (!(isTauri() && import.meta.env.PROD)) {
        if (mounted.current)
          setBackup({ kind: "ok", text: "备份已就绪，请重启后端进程（终端 1）后刷新页面" });
        return;
      }
      const r = await restartBackend();
      if (!r.ok) {
        if (mounted.current) setBackup({ kind: "bad", text: `${r.message}；备份已就绪，重启后生效` });
        return;
      }
      if (mounted.current) setBackup({ kind: "ok", text: "正在重启后端并应用备份…" });
      const ready = await waitBackendReady();
      if (!ready) {
        if (mounted.current) setBackup({ kind: "bad", text: "后端重启超时，请手动重启应用完成恢复" });
        return;
      }
      window.location.reload();
    } catch (e) {
      if (mounted.current) setBackup({ kind: "bad", text: extractDetail(e) });
    }
  };

  // —— 合并导入两阶段 ——————————————————————————————————————————

  const doAnalyze = async () => {
    const path = await pickBackupZip();
    if (!path) return;
    setMerge({ kind: "analyzing" });
    try {
      const res = await analyzeMerge(path);
      if (!mounted.current) return;
      if (res.updated_count === 0) {
        // 无冲突直通（2026-11-15 迭代四）：没有同键内容分歧就不必停在选择
        // 界面——直接导入并显示结果；有冲突才进 review 看明细选策略
        const applied = await applyMerge(path, "skip");
        if (!mounted.current) return;
        setMerge({ kind: "done", text: applied.message });
        return;
      }
      setMerge({ kind: "review", path, result: res, strategy: "skip" });
    } catch (e) {
      if (mounted.current) setMerge({ kind: "bad", text: extractDetail(e) });
    }
  };

  const doApply = async () => {
    if (merge.kind !== "review") return;
    setMerge({ kind: "applying" });
    try {
      const res = await applyMerge(merge.path, merge.strategy);
      if (!mounted.current) return;
      setMerge({ kind: "done", text: res.message });
    } catch (e) {
      if (mounted.current) setMerge({ kind: "bad", text: extractDetail(e) });
    }
  };

  // —— 渲染 ————————————————————————————————————————————————————

  if (loadError)
    return (
      <div className="settings-body">
        <div className="settings-page-head">
          <span className="settings-page-title">同步备份</span>
          <span className="settings-page-sub">WebDAV 同步 · 导入导出 · 自动备份</span>
        </div>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">加载失败</span>
              <span className="settings-row-desc">{loadError}</span>
            </div>
          </div>
        </div>
      </div>
    );
  if (!cfg) return null;

  /** 「同步操作」卡底部提示行（占满行宽、位置恒定——按钮行不再被 hint 挤动）：
   *  动作结果轮显（重置确认 > 本次测试 > 本次同步），无动作时回落持久化的
   *  最近同步状态。测试结果只在本轮动作后显示，不做历史回显（用户拍板）。 */
  const bottomHint = () => {
    if (resetConfirm)
      return (
        <>
          <span className="settings-action-hint bad">
            将清空本地同步快照，下次同步按首次同步重新对齐，确认？
          </span>
          <button type="button" className="about-link" onClick={doResetBase}>确认</button>
          <button type="button" className="about-link" onClick={() => setResetConfirm(false)}>取消</button>
        </>
      );
    if (test.kind === "busy") return <span className="settings-action-hint">测试中…</span>;
    if (test.kind === "done")
      return (
        <span className={`settings-action-hint ${test.ok ? "ok" : "bad"}`}>
          {test.ok ? `连接成功（${test.ms}ms），可读写` : test.message}
        </span>
      );
    if (run.kind === "polling") return <span className="settings-action-hint">同步中…</span>;
    if (run.kind === "done")
      return <span className={`settings-action-hint ${run.ok ? "ok" : "bad"}`}>{run.text}</span>;
    // 持久化兜底：最近一次同步状态（失败带错误摘要）
    if (cfg.last_sync_at)
      return (
        <span className={`settings-action-hint ${cfg.last_sync_ok ? "ok" : "bad"}`}>
          最近同步 {fmtTime(cfg.last_sync_at)} · {cfg.last_sync_ok ? "成功" : "失败"}
          {cfg.last_sync_error ? ` · ${cfg.last_sync_error}` : ""}
        </span>
      );
    return <span className="settings-action-hint">尚未同步过（开启开关后自动进行首次同步）</span>;
  };

  const backupHint = () => {
    if (backup.kind === "busy") return <span className="settings-action-hint">处理中…</span>;
    if (backup.kind === "ok") return <span className="settings-action-hint ok">{backup.text}</span>;
    if (backup.kind === "bad") return <span className="settings-action-hint bad">{backup.text}</span>;
    if (backup.kind === "confirm")
      return (
        <>
          <span className="settings-action-hint bad">将覆盖当前全部数据，确认恢复？</span>
          <button type="button" className="about-link" onClick={() => doRestore(backup.path)}>确认恢复</button>
          <button type="button" className="about-link" onClick={() => setBackup({ kind: "idle" })}>取消</button>
        </>
      );
    return null;
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">同步备份</span>
        <span className="settings-page-sub">WebDAV 同步 · 导入导出 · 自动备份</span>
      </div>

      {/* —— 组1：WebDAV 同步 —— */}
      <div className="settings-group">
        <span className="settings-group-title">WebDAV 同步</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">启用同步</span>
              <span className="settings-row-desc">
                同步全部数据（功能设置、日程任务、聊天记录、记忆画像等）到 WebDAV
                上的 tables/*.json 散文件（云端附有使用说明.txt），变更自动增量同步；
                快捷键与窗口布局等本机偏好（分辨率相关）不在其列。云端目录可随时
                手动压成 zip，经下方「导入导出」恢复——设备丢失时的灾备路径
              </span>
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={cfg.webdav_enabled}
                onChange={(v) => save({ webdav_enabled: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">服务器地址</span>
              <span className="settings-row-desc">
                WebDAV 服务地址（可含子目录），如 https://dav.jianguoyun.com/dav/
              </span>
            </div>
            <div className="settings-row-control">
              <SetInput
                value={cfg.webdav_url ?? ""}
                placeholder="https://…"
                maxLength={300}
                onChange={(v) => save({ webdav_url: v || null })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">账号与密码</span>
              <span className="settings-row-desc">
                用户名（匿名服务可留空）+ 应用密码（坚果云等请在服务端生成；本地明文存储）
              </span>
            </div>
            <div className="settings-row-control sync-cred-group">
              <SetInput
                value={cfg.webdav_username ?? ""}
                placeholder="用户名"
                maxLength={100}
                onChange={(v) => save({ webdav_username: v || null })}
              />
              <SetInput
                type="password"
                value={cfg.webdav_password ?? ""}
                placeholder="密码"
                maxLength={200}
                onChange={(v) => save({ webdav_password: v || null })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">同步操作</span>
              <span className="settings-row-desc">
                测试验证服务可达且账号有写权限；重置基线 = 清空本地同步快照
                （同步被安全守卫拦下、或远端数据已整体更换时，用它重新对齐）
              </span>
              {saveError && <span className="settings-row-desc sync-error">{saveError}</span>}
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                disabled={test.kind === "busy"}
                onClick={doTest}
              >
                测试连接
              </button>
              <button
                type="button"
                className="settings-action-btn"
                disabled={run.kind === "polling" || cfg.syncing}
                onClick={() => doRun()}
              >
                {run.kind === "polling" || cfg.syncing ? "同步中…" : "立即同步"}
              </button>
              <button
                type="button"
                className="settings-action-btn"
                onClick={() => setResetConfirm(true)}
              >
                重置基线
              </button>
            </div>
          </div>
          <div className="sync-hint-row">{bottomHint()}</div>
        </div>
      </div>

      {/* —— 组2：导入导出 —— */}
      <div className="settings-group">
        <span className="settings-group-title">导入导出</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">导出备份</span>
              <span className="settings-row-desc">
                全量数据快照（含本机偏好）压成单个 zip，可跨设备搬运
              </span>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                disabled={backup.kind === "busy" || backup.kind === "confirm"}
                onClick={doExport}
              >
                导出备份
              </button>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">覆盖恢复</span>
              <span className="settings-row-desc">
                用备份包整库替换当前数据（恢复前自动留存当前数据副本），需重启后端生效
              </span>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                disabled={backup.kind === "busy" || backup.kind === "confirm"}
                onClick={async () => {
                  const path = await pickBackupZip();
                  if (path) setBackup({ kind: "confirm", path });
                }}
              >
                恢复备份
              </button>
              {backupHint()}
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">合并导入</span>
              <span className="settings-row-desc">
                把备份包的数据合并进当前库（保留双方）：新增照收，同条目冲突时按
                下方选择处理；不会删除当前库已有的数据，无需重启。备份 zip 与
                WebDAV 同步目录压的 zip 均可导入
              </span>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                disabled={merge.kind === "analyzing" || merge.kind === "applying"}
                onClick={doAnalyze}
              >
                {merge.kind === "analyzing" ? "分析中…" : merge.kind === "applying" ? "导入中…" : "选择文件"}
              </button>
              {merge.kind === "done" && <span className="settings-action-hint ok">{merge.text}</span>}
              {merge.kind === "bad" && <span className="settings-action-hint bad">{merge.text}</span>}
            </div>
          </div>
        </div>
        {merge.kind === "review" && (
          <div className="settings-card sync-review-card">
            <div className="settings-row">
              <div className="settings-row-info">
                <span className="settings-row-name">
                  分析结果：新增 {merge.result.new_count} 项 · 冲突 {merge.result.updated_count} 项
                </span>
                <span className="settings-row-desc">
                  冲突 = 同一条目两侧内容不同（下表最多展示 50 条；无冲突时直接导入）
                </span>
              </div>
              <div className="settings-row-control">
                <SetSegmented
                  value={merge.strategy}
                  options={[
                    { value: "skip", label: "冲突保留本地" },
                    { value: "overwrite", label: "冲突用备份覆盖" },
                  ]}
                  onChange={(v) => setMerge({ ...merge, strategy: v })}
                />
                <button type="button" className="settings-action-btn" onClick={doApply}>
                  开始导入
                </button>
                <button
                  type="button"
                  className="settings-action-btn"
                  onClick={() => setMerge({ kind: "idle" })}
                >
                  取消
                </button>
              </div>
            </div>
            {merge.result.conflicts.length > 0 && (
              <div className="sync-conflict-table">
                <div className="sync-conflict-head">
                  <span>表</span>
                  <span>条目</span>
                  <span>本地更新于</span>
                  <span>备份更新于</span>
                </div>
                {merge.result.conflicts.map((c, i) => (
                  <div className="sync-conflict-row" key={i}>
                    <span>{c.table}</span>
                    <span>{c.pk}</span>
                    <span>{c.local_updated_at?.slice(0, 19).replace("T", " ") || "—"}</span>
                    <span>{c.backup_updated_at?.slice(0, 19).replace("T", " ") || "—"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* —— 组3：自动备份 —— */}
      <div className="settings-group">
        <span className="settings-group-title">自动备份</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">启用自动备份</span>
              <span className="settings-row-desc">
                每天 08:00 / 12:00 / 18:00 三次全量备份（错过顺延补跑），只保留最近
                7 天的备份文件
              </span>
            </div>
            <div className="settings-row-control">
              <SetToggle
                checked={cfg.auto_backup_enabled}
                onChange={(v) => save({ auto_backup_enabled: v })}
              />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">备份目录</span>
              <span className="settings-row-desc">
                当前：{cfg.auto_backup_dir_resolved} · 最近备份 {fmtTime(cfg.auto_backup_last_at)}
              </span>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                onClick={async () => {
                  const dir = await pickDirectory();
                  if (dir) save({ auto_backup_dir: dir });
                }}
              >
                更改目录
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
