// "关于我们"设置页：版本号 + 开源地址 + 检查更新（2026-11-06 批 A）。
// 数据备份/恢复两件套已于 2026-11-14 同步备份迁至"同步备份"页
//（SyncSettings.tsx，端点 /api/sync/*）。骨架复用 GeneralSettings.css。
// 版本号经 getVersion() 动态读自 tauri.conf.json（升级只改该文件一处）；
// 纯浏览器 dev / 读取失败回落常量。检查更新走后端出网（weather 同款通道），
// 版本比较在前端做（本页持有 getVersion 真源）。
import { useEffect, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";
import { checkLatestRelease } from "../../lib/api";
import { openExternal } from "../../lib/tauri";
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

export default function AboutSettings() {
  const [version, setVersion] = useState(FALLBACK_VERSION);
  const [update, setUpdate] = useState<UpdateState>({ kind: "idle" });

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
        </div>
      </div>
    </div>
  );
}
