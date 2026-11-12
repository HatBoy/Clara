// 统计数据对话面板（右栏）：走 POST /api/stats/ask 独立轻通道（stats_summary
// + 复盘上下文注入 system，不进 ChatTurn、不走意图路由——与日程助手上下文
// 完全隔离）。days 由页面传入跟随 7/30/90 范围；组件常驻渲染（App 层
// display:none 切换），折叠不丢对话；切换页面清空历史（临时性问答，不做
// 持久化）。复盘模式（mode=review）：AI 引导「最满意 → 最大卡点 → 下期改进」
// 逐项反思，总结轮的沉淀候选经 facts 帧到达，确认条一键落 Tier 2 记忆。
// UI 与日程助手（CopilotPanel）同规格统一：面板底/header/feed/气泡/一体胶囊
// 输入/发送钮/打字机光标均对齐；斜杠命令 /clear /stop /compact（联想弹层）。
import { useEffect, useRef, useState } from "react";
import { ArrowUp, MessageCircle, NotebookPen, PanelRightClose } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  askStats,
  saveStatsFact,
  type StatsAskTurn,
  type StatsFactCandidate,
} from "../lib/api";
import "./StatsChatPanel.css";

type Message = {
  role: "user" | "assistant";
  content: string;
};

const WELCOME =
  "你好！我是统计复盘助手，已加载当前范围的数据、你的记忆画像与最近报告结论。\n\n可以问我：\n- **完成情况**怎么样\n- **时间**都花在哪了\n- 哪些**习惯**值得调整\n- **负载**是否健康\n\n或点右上「**开始复盘**」进入 AI 引导复盘。输入 `/` 查看可用命令。";

const WELCOME_REVIEW =
  "进入复盘引导——我会围绕「本期最满意的一件事 → 最大卡点 → 下期想改进什么」逐项和你聊聊（每轮一问，会用数据呼应你的回答），结束后可把反思结论沉淀为长期记忆。";

/** 斜杠命令（与日程助手同款交互形态；本面板无持久上下文，/compact 诚实提示） */
const COMMANDS: { name: string; desc: string }[] = [
  { name: "/clear", desc: "清空当前对话" },
  { name: "/stop", desc: "停止生成" },
  { name: "/compact", desc: "压缩上下文（统计问答不保留历史，无需压缩）" },
];

export default function StatsChatPanel({
  onCollapse,
  days,
  style,
}: {
  onCollapse: () => void;
  /** 跟随统计页范围（问答上下文随之切换） */
  days: 7 | 30 | 90;
  style?: React.CSSProperties;
}) {
  const [messages, setMessages] = useState<Message[]>([{
    role: "assistant",
    content: WELCOME,
  }]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  // 复盘引导模式（mode=review）；pendingFacts = 总结轮 facts 帧的沉淀候选
  const [reviewMode, setReviewMode] = useState(false);
  const [pendingFacts, setPendingFacts] = useState<StatsFactCandidate[] | null>(null);
  const [factSaving, setFactSaving] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pendingFacts]);

  const updateLast = (content: string) => {
    setMessages((prev) => {
      const next = [...prev];
      next[next.length - 1] = { ...next[next.length - 1], content };
      return next;
    });
  };

  const runCommand = (name: string) => {
    setInput("");
    if (name === "/clear") {
      abortRef.current?.abort();
      setReviewMode(false);
      setPendingFacts(null);
      setMessages([{ role: "assistant", content: WELCOME }]);
    } else if (name === "/stop") {
      abortRef.current?.abort(); // 流中止后部分回答保留
    } else if (name === "/compact") {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "统计问答不保留历史记录（每轮仅携带最近几条对话），无需压缩。" },
      ]);
    }
  };

  /** 发送核心：question 已定形（handleSend 走输入框、startReview 走预设开场）；
   *  base 覆盖历史来源（复盘开场用空历史，不吃 setState 前的旧消息闭包）。 */
  const send = async (question: string, mode: "ask" | "review", base?: Message[]) => {
    setIsStreaming(true);
    // 请求历史 = 已有消息尾部若干轮（排除欢迎语与本次占位）
    const history: StatsAskTurn[] = (base ?? messages)
      .filter((m) => m.content && m.content !== WELCOME && m.content !== WELCOME_REVIEW)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.content }));
    setMessages((prev) => [...prev, { role: "user", content: question }, { role: "assistant", content: "" }]);

    const controller = new AbortController();
    abortRef.current = controller;
    let acc = "";
    try {
      await askStats(days, question, history, (ev) => {
        if (ev.type === "delta" && ev.text) {
          acc += ev.text;
          updateLast(acc);
        } else if (ev.type === "reply" && ev.content) {
          acc = ev.content; // 权威整段覆盖 delta 累积（复盘尾部块已剥离）
          updateLast(acc);
        } else if (ev.type === "facts" && ev.candidates?.length) {
          setPendingFacts(ev.candidates); // 沉淀候选 → 确认条
        } else if (ev.type === "error") {
          acc = ev.message;
          updateLast(acc);
        }
      }, controller.signal, mode);
      if (!acc) updateLast("未收到有效回复，请稍后重试。");
    } catch (e) {
      // /stop 中止（AbortError）：保留已生成的部分回答，静默收尾
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        if (!acc) updateLast("回答生成失败，请稍后重试。");
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  };

  const handleSend = async () => {
    const question = input.trim();
    if (!question || isStreaming) return;

    // 斜杠命令优先（单匹配直接执行）
    if (question.startsWith("/")) {
      const hit = COMMANDS.find((c) => c.name === question);
      if (hit) {
        runCommand(hit.name);
        return;
      }
    }

    setInput("");
    send(question, reviewMode ? "review" : "ask");
  };

  /** 开始/退出复盘引导：进入即清屏发预设开场（空历史）；复盘中再点退出回普通问答 */
  const toggleReview = () => {
    if (isStreaming) return;
    setPendingFacts(null);
    if (reviewMode) {
      setReviewMode(false);
      setMessages([{ role: "assistant", content: WELCOME }]);
      return;
    }
    setReviewMode(true);
    setMessages([{ role: "assistant", content: WELCOME_REVIEW }]);
    send("开始复盘", "review", []);
  };

  /** 沉淀确认：逐条落 Tier 2（后端同主题同结论印证不堆行），失败静默计成功数 */
  const saveFacts = async () => {
    if (!pendingFacts || factSaving) return;
    setFactSaving(true);
    let ok = 0;
    for (const f of pendingFacts) {
      try {
        await saveStatsFact(f.topic, f.fact);
        ok++;
      } catch {
        /* 单条失败静默（多数失败=网络层，结论可再试） */
      }
    }
    setFactSaving(false);
    setPendingFacts(null);
    setMessages((prev) => [
      ...prev,
      {
        role: "assistant",
        content: ok > 0
          ? `已沉淀 ${ok} 条反思结论到长期记忆（设置 → 记忆画像可见，排期与教练也会参考）。`
          : "保存失败，请稍后重试。",
      },
    ]);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // 斜杠命令联想（输入以 / 开头时过滤）
  const cmdMatches = input.startsWith("/")
    ? COMMANDS.filter((c) => c.name.startsWith(input.trim()) || !input.trim())
    : [];

  const streaming = isStreaming && messages[messages.length - 1]?.role === "assistant";

  return (
    <div className="stats-chat-panel" style={style}>
      <header className="stats-chat-header">
        <div className="stats-chat-title">
          <MessageCircle size={16} strokeWidth={2} className="stats-chat-title-icon" />
          <span className="stats-chat-title-text">统计问答</span>
          <span className="stats-chat-range">近 {days} 天</span>
        </div>
        <div className="stats-chat-header-actions">
          <button
            type="button"
            className={`stats-review-btn${reviewMode ? " active" : ""}`}
            onClick={toggleReview}
            disabled={isStreaming}
            title={reviewMode ? "退出复盘引导" : "AI 引导复盘：最满意的一件事 / 最大卡点 / 下期想改进什么"}
          >
            <NotebookPen size={14} strokeWidth={2} />
            <span>{reviewMode ? "复盘中" : "开始复盘"}</span>
          </button>
          <button
            type="button"
            className="stats-chat-close-btn"
            onClick={onCollapse}
            aria-label="折叠统计问答面板"
            title="折叠"
          >
            <PanelRightClose size={18} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="stats-chat-feed">
        {messages.map((msg, i) => {
          const isLastStreaming = i === messages.length - 1 && streaming;
          return (
            <div key={i} className={`stats-msg ${msg.role}`}>
              <div className={`stats-bubble${msg.role === "assistant" && isLastStreaming ? " streaming" : ""}`}>
                {msg.role === "assistant" && msg.content && !isLastStreaming ? (
                  <div className="stats-md">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                  </div>
                ) : (
                  msg.content
                )}
              </div>
            </div>
          );
        })}
        {isStreaming && messages[messages.length - 1]?.content === "" && (
          <div className="stats-typing">思考中…</div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* 复盘总结轮的沉淀候选确认条（facts 帧；确认制——不自动落库） */}
      {pendingFacts && pendingFacts.length > 0 && (
        <div className="stats-facts-bar">
          <span className="stats-facts-title">反思结论沉淀</span>
          {pendingFacts.map((f, i) => (
            <div key={i} className="stats-facts-item">
              <span className="stats-facts-topic">[{f.topic}]</span> {f.fact}
            </div>
          ))}
          <div className="stats-facts-actions">
            <button
              type="button"
              className="stats-facts-save"
              onClick={saveFacts}
              disabled={factSaving}
            >
              {factSaving ? "保存中…" : "记住这些反思"}
            </button>
            <button
              type="button"
              className="stats-facts-skip"
              onClick={() => setPendingFacts(null)}
            >
              忽略
            </button>
          </div>
        </div>
      )}

      <footer className="stats-chat-footer">
        {cmdMatches.length > 0 && (
          <div className="stats-cmd-pop">
            {cmdMatches.map((c) => (
              <button
                key={c.name}
                type="button"
                className="stats-cmd-item"
                onClick={() => runCommand(c.name)}
              >
                <span className="stats-cmd-name">{c.name}</span>
                <span className="stats-cmd-desc">{c.desc}</span>
              </button>
            ))}
          </div>
        )}
        <div className="stats-chat-input">
          <textarea
            className="stats-input-text"
            placeholder={
              reviewMode
                ? "复盘进行中——按 AI 引导逐项回答…"
                : `问我关于近 ${days} 天数据的问题…（/ 查看命令）`
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
          />
          <button
            type="button"
            className="stats-send"
            aria-label="发送"
            disabled={!input.trim() || isStreaming}
            onClick={handleSend}
          >
            <ArrowUp size={15} strokeWidth={2} className="stats-send-icon" />
          </button>
        </div>
      </footer>
    </div>
  );
}
