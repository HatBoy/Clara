// "关于我们"设置页：版本号 + 开源地址 + 检查更新 + 数据备份/恢复（2026-11-06
// 批 A 两件套）。骨架复用 GeneralSettings.css（.settings-* 族 + 动作钮
// .settings-action-btn / 结果文案 .settings-action-hint）。
// 版本号经 getVersion() 动态读自 tauri.conf.json（升级只改该文件一处）；
// 纯浏览器 dev / 读取失败回落常量。检查更新走后端出网（weather 同款通道），
// 版本比较在前端做（本页持有 getVersion 真源）。恢复为两段式：staging 落盘
// 后打包版经 Rust restart_backend 编排重启（dev 提示手动重启 uvicorn）。
import { useEffect, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";
import { checkLatestRelease, createBackup, restoreBackup } from "../../lib/api";
import { API_BASE } from "../../lib/api";
import {
  isTauri,
  openExternal,
  pickBackupZip,
  restartBackend,
  saveBackupPath,
} from "../../lib/tauri";
import "./GeneralSettings.css";
import "./AboutSettings.css";

// 非桌面环境回落值（须与 src-tauri/tauri.conf.json 的 version 手动保持一致）
const FALLBACK_VERSION = "v1.0.0";
const REPO_URL = "https://github.com/HatBoy/Clara";

/** "v1.0.1" / "1.0.1" → [1,0,1]，逐段数字比较（tag 无 v 前缀也能比） */
function isNewerTag(tag: string, current: string): boolean {
  const parts = (s: string) => s.trim().replace(/^v/i, "").split(".").map(Number);
  const [a, b] = [parts(tag), parts(current)];
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d > 0;
  }
  return false;
}

type UpdateState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "latest" }
  | { kind: "newer"; tag: string; url: string }
  | { kind: "failed"; reason: string };

type BackupState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; text: string }
  | { kind: "bad"; text: string }
  // 恢复确认态：zip 路径已选定，等用户二次确认（覆盖性操作必须显式点确认）
  | { kind: "confirm"; path: string };

export default function AboutSettings() {
  const [version, setVersion] = useState(FALLBACK_VERSION);
  const [update, setUpdate] = useState<UpdateState>({ kind: "idle" });
  const [backup, setBackup] = useState<BackupState>({ kind: "idle" });

  useEffect(() => {
    getVersion()
      .then((v) => setVersion(`v${v}`))
      .catch(() => {/* 权限缺失/非桌面：保持回落值 */});
  }, []);

  const doCheckUpdate = async () => {
    setUpdate({ kind: "checking" });
    try {
      const res = await checkLatestRelease();
      if (res.available && res.tag) {
        if (isNewerTag(res.tag, version)) {
          setUpdate({ kind: "newer", tag: res.tag, url: res.html_url || `${REPO_URL}/releases` });
        } else {
          setUpdate({ kind: "latest" });
        }
      } else {
        setUpdate({ kind: "failed", reason: res.reason || "检查失败" });
      }
    } catch {
      setUpdate({ kind: "failed", reason: "后端服务不可用" });
    }
  };

  const doExport = async () => {
    const path = await saveBackupPath();
    if (!path) return; // 用户取消 / 非桌面
    setBackup({ kind: "busy" });
    try {
      const res = await createBackup(path, version);
      setBackup({ kind: "ok", text: `已导出到 ${path}（${(res.size_bytes / 1048576).toFixed(1)} MB）` });
    } catch (e) {
      setBackup({ kind: "bad", text: e instanceof Error ? e.message : "导出失败" });
    }
  };

  /** 恢复后端就绪探测：restart_backend 杀旧拉新有秒级窗口，轮询 health 再刷新页面 */
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
        setBackup({ kind: "bad", text: res.message });
        return;
      }
      if (!(isTauri() && import.meta.env.PROD)) {
        // dev：后端是终端手动 uvicorn，宿主编排不适用——数据已就绪待重启
        setBackup({ kind: "ok", text: "备份已就绪，请重启后端进程（终端 1）后刷新页面" });
        return;
      }
      const r = await restartBackend();
      if (!r.ok) {
        setBackup({ kind: "bad", text: `${r.message}；备份已就绪，重启后生效` });
        return;
      }
      setBackup({ kind: "ok", text: "正在重启后端并应用备份…" });
      const ready = await waitBackendReady();
      if (!ready) {
        setBackup({ kind: "bad", text: "后端重启超时，请手动重启应用完成恢复" });
        return;
      }
      window.location.reload(); // 数据全量刷新（库已被替换）
    } catch (e) {
      setBackup({ kind: "bad", text: e instanceof Error ? e.message : "恢复失败" });
    }
  };

  const updateHint = () => {
    if (update.kind === "checking") return <span className="settings-action-hint">检查中…</span>;
    if (update.kind === "latest")
      return <span className="settings-action-hint ok">已是最新版本</span>;
    if (update.kind === "newer")
      return (
        <>
          <span className="settings-action-hint ok">发现新版 {update.tag}</span>
          <button
            type="button"
            className="about-link"
            onClick={() => openExternal(update.url)}
          >
            前往下载
          </button>
        </>
      );
    if (update.kind === "failed")
      return <span className="settings-action-hint bad">{update.reason}</span>;
    return null;
  };

  const backupHint = () => {
    if (backup.kind === "busy") return <span className="settings-action-hint">处理中…</span>;
    if (backup.kind === "ok") return <span className="settings-action-hint ok">{backup.text}</span>;
    if (backup.kind === "bad") return <span className="settings-action-hint bad">{backup.text}</span>;
    if (backup.kind === "confirm")
      return (
        <>
          <span className="settings-action-hint bad">将覆盖当前全部数据，确认恢复？</span>
          <button
            type="button"
            className="about-link"
            onClick={() => doRestore(backup.path)}
          >
            确认恢复
          </button>
          <button type="button" className="about-link" onClick={() => setBackup({ kind: "idle" })}>
            取消
          </button>
        </>
      );
    return null;
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">关于我们</span>
        <span className="settings-page-sub">AI 日程助手桌面应用</span>
      </div>

      {/* 品牌抬头：与 favicon / 应用图标同源矢量（public/clara.svg） */}
      <div className="about-brand">
        <img className="about-logo" src="/clara.svg" alt="Clara" />
        <span className="about-name">Clara</span>
      </div>

      <div className="settings-group">
        <span className="settings-group-title">应用信息</span>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">版本号</span>
              <span className="settings-row-desc">当前安装的应用版本</span>
            </div>
            <div className="settings-row-control">
              <span className="about-value">{version}</span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">检查更新</span>
              <span className="settings-row-desc">检测 GitHub 上的最新发布版</span>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-btn"
                disabled={update.kind === "checking"}
                onClick={doCheckUpdate}
              >
                检查更新
              </button>
              {updateHint()}
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <span className="settings-row-name">开源地址</span>
              <span className="settings-row-desc">源码与文档（GitHub 仓库）</span>
            </div>
            <div className="settings-row-control">
              {/* 外链走系统默认浏览器（openExternal 先例：DataSettings 官方文档钮；
                  非桌面环境内部已降级 window.open） */}
              <button
                type="button"
                className="about-link"
                title={REPO_URL}
                onClick={() => openExternal(REPO_URL)}
              >
                {REPO_URL}
              </button>
            </div>
          </div>
          <div className="settings-row about-backup-row">
            <div className="settings-row-info">
              <span className="settings-row-name">数据备份</span>
              <span className="settings-row-desc">
                导出数据快照，或从备份包恢复（恢复前自动留存当前数据副本）
              </span>
            </div>
            <div className="settings-row-control">
              <div className="about-backup-actions">
                <button
                  type="button"
                  className="settings-action-btn"
                  disabled={backup.kind === "busy" || backup.kind === "confirm"}
                  onClick={doExport}
                >
                  导出备份
                </button>
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
              </div>
              {backupHint()}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
