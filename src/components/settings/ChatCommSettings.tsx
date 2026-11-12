import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { QRCodeSVG } from "qrcode.react";
import { ExternalLink, Eye, EyeOff, HelpCircle, RefreshCw } from "lucide-react";
import {
  deleteChatChannel,
  fetchChatChannels,
  pollWechatLoginStatus,
  saveChatChannel,
  startWechatLogin,
  testChatChannel,
  toggleChannelPush,
  toggleChatChannel,
  type ApiChatChannel,
} from "../../lib/api";
import { BRAND_PATHS, BrandGlyph, FormModal } from "./DataSettings";
import { classifyConnError, connErrBadge } from "../../lib/conn-error";
import { SetToggle } from "./SettingsControls"; // 推送开关行（set-toggle 形态复用）
import "./GeneralSettings.css"; // .settings-body / .settings-page-* 页面骨架（打包去重）
import "./DataSettings.css"; // data-card / data-modal / data-btn 体系复用（打包去重）
import "./McpServerForm.css"; // mcp-input / mcp-form-actions 表单控件复用
import "./ChatCommSettings.css";

// 渠道卡图标：飞书/钉钉复用数据接入的品牌 path；微信/企微用简版自绘
// （微信双气泡 + 企微单气泡带眼点；正式单 path logo 难溯源，同 WECHAT 先例）
const WECHAT_PATH = {
  d: "M8.5 3C4.9 3 2 5.5 2 8.6c0 1.8 1 3.4 2.5 4.4L4 15l2.3-1.2c.7.2 1.4.3 2.2.3h.4c-.2-.5-.3-1-.3-1.6 0-3 2.9-5.5 6.5-5.5h.5C15.6 4.9 12.5 3 8.5 3zm-2.9 3.2c.5 0 .9.4.9.9s-.4.9-.9.9-.9-.4-.9-.9.4-.9.9-.9zm5.8 0c.5 0 .9.4.9.9s-.4.9-.9.9-.9-.4-.9-.9.4-.9.9-.9zM15.5 8c-3 0-5.5 2.1-5.5 4.8s2.5 4.8 5.5 4.8c.6 0 1.2-.1 1.8-.3L19.5 18l-.5-1.7c1.3-.9 2.5-2.2 2.5-3.5C20.5 10.1 18.5 8 15.5 8zm-1.8 2.7c.4 0 .7.3.7.7s-.3.7-.7.7-.7-.3-.7-.7.3-.7.7-.7zm3.6 0c.4 0 .7.3.7.7s-.3.7-.7.7-.7-.3-.7-.7.3-.7.7-.7z",
};
const WECOM_PATH = {
  d: "M12 3C7 3 3 6.1 3 10c0 2.1 1.1 4 2.9 5.3L5.2 18.6a.6.6 0 0 0 .9.7l3.1-1.8c.9.2 1.9.3 2.8.3 5 0 9-3.1 9-7s-4-7.8-9-7.8zm-3.5 5.2a1 1 0 1 1 0 2 1 1 0 0 1 0-2zm7 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2z",
};

const ICON_CLS: Record<string, string> = {
  feishu: "chatcomm-feishu",
  dingtalk: "chatcomm-dingtalk",
  wechat: "chatcomm-wechat",
  wecom: "chatcomm-wecom",
};

// 自绘 path → BrandPath 形状（BrandGlyph 只吃 {d}）
function ChannelIcon({ channel }: { channel: ApiChatChannel }) {
  const local: Record<string, { d: string }> = { wechat: WECHAT_PATH, wecom: WECOM_PATH };
  if (local[channel.provider]) {
    return (
      <span className={`data-icon ${ICON_CLS[channel.provider]}`}>
        <BrandGlyph path={local[channel.provider]} />
      </span>
    );
  }
  return (
    <span className={`data-icon ${ICON_CLS[channel.provider] ?? ""}`}>
      <BrandGlyph path={BRAND_PATHS[channel.provider as "feishu" | "dingtalk"]} />
    </span>
  );
}

// 凭据模态（连接应用 / 编辑共用；飞书/钉钉/企微）：app_id / app_secret 均回填
// （Composio key 原样回显先例），secret 密码态掩码显示 + 眼睛按需查看明文；
// 保存即探测，结果落 conn_* 由卡上徽标回显，失败摘要在模态内直显。
// label/指引按渠道定制（企微凭据形态是 Bot ID + Secret，智能机器人 API 模式）
const CREDENTIAL_META: Record<string, { idLabel: string; hint: string }> = {
  wecom: {
    idLabel: "Bot ID",
    hint: "在企业微信管理后台创建智能机器人（API 模式 · 长连接），把 BotID 与 Secret 粘贴到下方",
  },
};

function CredentialModal({
  channel,
  onClose,
  onSaved,
}: {
  channel: ApiChatChannel;
  onClose: () => void;
  onSaved: (updated: ApiChatChannel) => void;
}) {
  const meta = CREDENTIAL_META[channel.provider];
  const [appId, setAppId] = useState(channel.app_id ?? "");
  const [secret, setSecret] = useState(channel.app_secret ?? "");
  const [showSecret, setShowSecret] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await saveChatChannel(channel.provider, {
        app_id: appId.trim(),
        app_secret: secret,
      });
      onSaved(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败，请检查后端连接");
      setSaving(false);
    }
  };

  return (
    <FormModal title={channel.name} onClose={onClose}>
      <div className="chatcomm-form">
        <div className="chatcomm-hint">
          {meta?.hint ??
            "在开放平台创建应用并开启机器人能力（消息接收走长连接，无需公网），然后把应用凭据粘贴到下方"}
          {channel.setup_url && (
            <a href={channel.setup_url} target="_blank" rel="noreferrer" className="chatcomm-doc-link">
              官方文档 <ExternalLink size={11} strokeWidth={2} />
            </a>
          )}
        </div>
        <div className="mcp-field">
          <span className="mcp-field-label">{meta?.idLabel ?? "App ID"}</span>
          <input
            className="mcp-input mono"
            value={appId}
            placeholder="cli_xxxxxxxx"
            onChange={(e) => setAppId(e.target.value)}
          />
        </div>
        <div className="mcp-field">
          <span className="mcp-field-label">Secret</span>
          <div className="mcp-key-wrap">
            <input
              className="mcp-input mono"
              type={showSecret ? "text" : "password"}
              value={secret}
              placeholder={channel.configured ? "已保存 · 可直接修改后保存" : "应用密钥"}
              onChange={(e) => setSecret(e.target.value)}
            />
            <button
              type="button"
              className="mcp-key-eye"
              title={showSecret ? "隐藏密钥" : "显示密钥"}
              onClick={() => setShowSecret((v) => !v)}
            >
              {showSecret ? <EyeOff size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
            </button>
          </div>
          <span className="mcp-field-hint">
            {channel.configured ? "已保存的密钥可点右侧眼睛查看；修改后保存即重新校验" : "在开放平台的应用凭证页获取"}
          </span>
        </div>
        {error && <span className="mcp-form-error">{error}</span>}
        <div className="mcp-form-actions">
          <button type="button" className="data-btn ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="data-btn primary"
            disabled={!appId.trim() || saving}
            onClick={() => void save()}
          >
            {saving ? "保存中…" : "保存并测试"}
          </button>
        </div>
      </div>
    </FormModal>
  );
}

// 微信扫码登录模态（wechat 专用凭据形态）：打开即出码 → 2s 一拍轮询 →
// confirmed 自动关 + 刷卡。qr_url 是授权链接文本（2026-10-23 真机实锤——
// 非图片 URL），qrcode.react 渲染成二维码图供手机微信扫。
// need_verifycode = 服务端配对码挑战（手机显示数字码），输入后随轮询带回。
function ScanLoginModal({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [ticket, setTicket] = useState<string | null>(null);
  const [qrUrl, setQrUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("loading"); // 后端八态 + loading
  const [error, setError] = useState<string | null>(null);
  const [verifyCode, setVerifyCode] = useState("");
  const pendingVerify = useRef(""); // 用户提交的配对码（随下一轮轮询带上）
  // 回调 ref 化：props 回调每渲染新引用，直接进轮询 effect 依赖会反复重置
  // 2s 定时器（轮询节拍被拖乱）
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const fetchQr = async () => {
    setError(null);
    setStatus("loading");
    setVerifyCode("");
    pendingVerify.current = "";
    try {
      const res = await startWechatLogin();
      setTicket(res.ticket);
      setQrUrl(res.qr_url);
      setStatus("wait");
    } catch (e) {
      setError(e instanceof Error ? e.message : "获取二维码失败");
    }
  };

  useEffect(() => {
    void fetchQr();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 轮询循环（2s 一拍递归 setTimeout，卸载即停；confirmed 后端已落库拉起 worker）
  useEffect(() => {
    if (!ticket || ["loading", "confirmed"].includes(status)) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const res = await pollWechatLoginStatus(ticket, pendingVerify.current || undefined);
        if (cancelled) return;
        pendingVerify.current = ""; // 每码只送一轮（错码回 need_verifycode 再输）
        if (res.status === "confirmed") {
          setStatus("confirmed");
          await onSavedRef.current();
          onCloseRef.current();
          return;
        }
        setStatus(res.status);
      } catch {
        /* 单轮失败不打断循环（网络抖动下一轮重试） */
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ticket, status]);

  const statusText: Record<string, string> = {
    wait: "打开手机微信，扫一扫识别二维码",
    scaned: "已扫描，请在手机上确认授权",
    need_verifycode: "请在下方输入手机上显示的配对码",
    verify_code_blocked: "配对码错误次数过多，请刷新二维码重试",
    binded_redirect: "该机器人已绑定其他设备，请先在微信 ClawBot 插件中解绑",
    expired: "二维码已过期",
  };

  return (
    <FormModal title="微信扫码接入" onClose={onClose}>
      <div className="chatcomm-form">
        <div className="chatcomm-hint">
          先在手机微信开启 ClawBot 插件（我 → 设置 → 插件），再扫下方二维码授权
          <a
            href="https://docs.openclaw.ai/zh-CN/channels/wechat"
            target="_blank"
            rel="noreferrer"
            className="chatcomm-doc-link"
          >
            接入指引 <ExternalLink size={11} strokeWidth={2} />
          </a>
        </div>
        <div className="chatcomm-qr-box">
          {qrUrl ? (
            <QRCodeSVG value={qrUrl} size={176} level="M" className="chatcomm-qr-img" />
          ) : (
            <span className="chatcomm-qr-placeholder">出码中…</span>
          )}
        </div>
        {status !== "loading" && (
          <div
            className={`chatcomm-qr-status${status === "expired" || ["verify_code_blocked", "binded_redirect"].includes(status) ? " bad" : ""}`}
          >
            {statusText[status] ?? `状态：${status}`}
          </div>
        )}
        {status === "need_verifycode" && (
          <div className="chatcomm-verify-row">
            <input
              className="mcp-input mono"
              value={verifyCode}
              placeholder="配对码（手机上显示的数字）"
              onChange={(e) => setVerifyCode(e.target.value)}
            />
            <button
              type="button"
              className="data-btn ghost"
              disabled={!verifyCode.trim()}
              onClick={() => {
                pendingVerify.current = verifyCode.trim();
                setStatus("wait"); // 立即带码重轮（文案回等待态）
              }}
            >
              提交
            </button>
          </div>
        )}
        {(status === "expired" || status === "verify_code_blocked") && (
          <button type="button" className="data-btn ghost" onClick={() => void fetchQr()}>
            <RefreshCw size={12} strokeWidth={2} /> 刷新二维码
          </button>
        )}
        {error && <span className="mcp-form-error">{error}</span>}
        <div className="mcp-form-actions">
          <button type="button" className="data-btn ghost" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </FormModal>
  );
}

// 推送开关问号悬停说明（2026-10-23 用户定案，替换原生 title——黑边长条难看）：
// fixed 毛玻璃小气泡照 .set-select-pop 规格（视口坐标，设置弹层内先例——
// overlay 的 backdrop-filter 不改 fixed 后代基准），不受卡片 176px 定高
// overflow:hidden 裁剪；上方弹出、贴近视口顶翻下方；hover 与键盘 focus 都能出
function PushHelpTip({ ready, wechat }: { ready: boolean; wechat: boolean }) {
  const [style, setStyle] = useState<CSSProperties | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const W = 260; // 与 .chatcomm-push-tip 定宽一致

  const show = () => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    // 气泡高约 150px：上方空间不足（卡片贴近视口顶）翻下方弹出
    const flip = rect.top < 170;
    setStyle({
      width: W,
      left: Math.max(8, Math.min(rect.left - 8, window.innerWidth - W - 8)),
      ...(flip
        ? { top: rect.bottom + 8 }
        : { top: rect.top - 8, transform: "translateY(-100%)" }),
    });
  };

  return (
    <>
      <span
        ref={ref}
        className="chatcomm-push-help"
        tabIndex={0}
        onMouseEnter={show}
        onMouseLeave={() => setStyle(null)}
        onFocus={show}
        onBlur={() => setStyle(null)}
      >
        <HelpCircle size={12} />
      </span>
      {style && (
        <div className="chatcomm-push-tip" style={style}>
          <p className="chatcomm-push-tip-title">
            {ready ? "推送已激活" : "推送尚未激活"}
          </p>
          <p>晨间简报、前夜综述、会前准备、高难协助、定时简报与过载预警也会推送到该渠道。</p>
          <p className="chatcomm-push-tip-note">
            {wechat
              ? "微信平台限制：仅能回复 24 小时内有过对话的用户——隔久未对话时推送自动跳过，发一条消息即恢复。"
              : "在渠道单聊里给助手发一条消息即激活，之后长期有效。"}
          </p>
        </div>
      )}
    </>
  );
}

// 「设置 → 聊天通讯」：四渠道卡（目录后端单源）；保存/启停/删除即时生效——
// 后端写操作挂 worker 全量重启（docs/chat-comm.md）
export default function ChatCommSettings() {
  const [channels, setChannels] = useState<ApiChatChannel[] | null>(null);
  const [openProvider, setOpenProvider] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // `${provider}:${action}`
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | undefined>(undefined);
  // 测试结果按钮态闪变（✓/✗ 2s 复位，Composio key 先例）——反馈局限在卡片内，
  // 不插页面级提示条挤动布局（2026-10-23 真机反馈）
  const [testFlash, setTestFlash] = useState<{ provider: string; ok: boolean } | null>(null);
  const testFlashTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    fetchChatChannels()
      .then((items) => {
        if (!cancelled) setChannels(items);
      })
      .catch(() => {
        if (!cancelled) setChannels([]);
      });
    return () => {
      cancelled = true;
      window.clearTimeout(noticeTimer.current);
      window.clearTimeout(testFlashTimer.current);
    };
  }, []);

  const flash = (text: string) => {
    setNotice(text);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 4000);
  };

  const flashTest = (provider: string, ok: boolean) => {
    setTestFlash({ provider, ok });
    window.clearTimeout(testFlashTimer.current);
    testFlashTimer.current = window.setTimeout(() => setTestFlash(null), 2000);
  };

  const refresh = async () => {
    try {
      setChannels(await fetchChatChannels());
    } catch {
      /* 后端缺席保持现状 */
    }
  };

  const act = async (provider: string, action: string, fn: () => Promise<unknown>) => {
    setBusy(`${provider}:${action}`);
    try {
      await fn();
      await refresh();
    } catch (e) {
      flash(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  if (channels === null) {
    return null; // 本地回环毫秒级，不做 loading UI（ModelSettings 先例）
  }
  const openChannel = channels.find((c) => c.provider === openProvider) ?? null;

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">聊天通讯</span>
        <span className="settings-page-sub">
          在手机飞书 / 钉钉 / 企微 / 微信的单聊窗口里直接与日程助手对话；桌面应用运行时渠道在线（本地长连接，无需公网）
        </span>
      </div>
      {notice && <div className="chatcomm-notice">{notice}</div>}
      <div className="data-grid">
        {channels.map((ch) => {
          const busyKey = (action: string) => busy === `${ch.provider}:${action}`;
          const isWechat = ch.provider === "wechat"; // 扫码形态：文案与模态全链特化
          return (
            <div
              key={ch.provider}
              className={`data-card data-card-col${ch.configured && !ch.enabled ? " disabled" : ""}`}
            >
              <div className="data-card-col-head">
                <ChannelIcon channel={ch} />
                {ch.provider === "dingtalk" ? <span className="data-pill blue">Stream</span> : null}
                {ch.provider === "feishu" ? <span className="data-pill purple">长连接</span> : null}
              </div>
              <div className="data-card-name-line">
                <span className="data-card-name" title={ch.name}>
                  {ch.name}
                </span>
                {ch.placeholder ? (
                  <span className="data-key-badge idle" title="个人微信暂无官方机器人 API，可靠通道开放后接入">
                    建设中
                  </span>
                ) : ch.configured ? (
                  ch.enabled ? null : (
                    <span className="data-key-badge idle" title="已停用：不监听该渠道消息">已停用</span>
                  )
                ) : null}
                {ch.configured && ch.conn_ok === true && (
                  <span className="data-key-badge ok" title={ch.conn_at ? `上次探测通过：${ch.conn_at.replace("T", " ")}` : "凭据有效"}>
                    {isWechat ? "已登录" : "凭据有效"}
                  </span>
                )}
                {ch.configured && ch.conn_ok === false && (
                  <span className="data-key-badge bad" title={ch.conn_error ?? "凭据校验失败"}>
                    {/* 网络不通与凭据错误分开展示（2026-11-05）：微信除网络外只有
                        过期一态，保持重扫指引不被「凭据无效」盖掉 */}
                    {isWechat
                      ? classifyConnError(ch.conn_error) === "network"
                        ? "网络不通"
                        : "已过期 · 重新扫码"
                      : connErrBadge(ch.conn_error, "凭据无效")}
                  </span>
                )}
                {ch.configured && ch.conn_ok === null && (
                  <span className="data-key-badge idle" title="从未探测，点「测试」校验凭据">
                    未检测
                  </span>
                )}
              </div>
              <div
                className={`data-card-desc${ch.configured ? " chatcomm-desc-one" : ""}`}
                title={ch.desc}
              >
                {ch.desc}
              </div>
              {ch.placeholder ? null : ch.configured ? (
                /* 推送开关行（二批）：desc 让出一行换开关；与监听启停正交——
                   停用（不监听）下推送仍可独立开启。push_ready 未就绪时开关可开
                   （后端有目标才真发，首条消息后自动生效）——激活条件与渠道差异
                   （微信 24h 会话令牌限制）收进问号悬停气泡（PushHelpTip） */
                <div className="chatcomm-push-row">
                  <span className="chatcomm-push-label">
                    推送定时简报
                    <PushHelpTip ready={ch.push_ready} wechat={isWechat} />
                  </span>
                  <SetToggle
                    checked={ch.push_enabled}
                    disabled={busy !== null}
                    onChange={(v) =>
                      void act(ch.provider, "push", () => toggleChannelPush(ch.provider, v))
                    }
                  />
                </div>
              ) : null}
              {ch.placeholder ? null : ch.configured ? (
                <div className="data-row-actions">
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={busy !== null}
                    onClick={() =>
                      void act(ch.provider, "test", async () => {
                        const updated = await testChatChannel(ch.provider);
                        // 200 + conn_ok=false 是正常返回（catch 只接网络错）——
                        // 按钮态闪变反馈（✓/✗），失败详情看卡上「凭据无效」徽标悬停
                        flashTest(ch.provider, updated.conn_ok === true);
                        return updated;
                      })
                    }
                  >
                    {busyKey("test")
                      ? "测试中…"
                      : testFlash?.provider === ch.provider
                        ? testFlash.ok
                          ? "✓ 有效"
                          : `✗ ${connErrBadge(ch.conn_error, "无效")}`
                        : "测试"}
                  </button>
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={busy !== null}
                    onClick={() => setOpenProvider(ch.provider)}
                  >
                    {isWechat ? "重新扫码" : "编辑"}
                  </button>
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={busy !== null}
                    onClick={() =>
                      void act(ch.provider, "toggle", () => toggleChatChannel(ch.provider, !ch.enabled))
                    }
                  >
                    {ch.enabled ? "停用" : "启用"}
                  </button>
                  <button
                    type="button"
                    className="data-btn ghost"
                    disabled={busy !== null}
                    onClick={() => void act(ch.provider, "delete", () => deleteChatChannel(ch.provider))}
                  >
                    删除
                  </button>
                </div>
              ) : (
                /* 精选卡同构：直接子元素按钮命中 .data-card-col > .data-btn 全宽钉底；
                   官方文档外链在凭据模态内（点击连接即达） */
                <button
                  type="button"
                  className="data-btn ghost"
                  disabled={busy !== null}
                  onClick={() => setOpenProvider(ch.provider)}
                >
                  {isWechat ? "扫码接入" : "连接应用"}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {openChannel?.provider === "wechat" ? (
        <ScanLoginModal onClose={() => setOpenProvider(null)} onSaved={refresh} />
      ) : openChannel ? (
        <CredentialModal
          channel={openChannel}
          onClose={() => setOpenProvider(null)}
          onSaved={async (updated) => {
            setOpenProvider(null);
            setChannels((prev) =>
              prev ? prev.map((c) => (c.provider === updated.provider ? updated : c)) : prev
            );
            if (updated.conn_ok === false) {
              flash(updated.conn_error ?? "凭据校验未通过，请检查后重试");
            }
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}
