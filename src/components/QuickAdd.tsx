// 中栏日程头部快捷录入框（符号指令语法 # 项目 / ~ 耗时 / @ 时间周期，解析见 lib/syntax.ts）。
// 视觉确认：识别成功的 token 经镜像层高亮——input 文字透明、镜像同字体/行高渲染彩色
// span，规避 contentEditable 的中文 IME 坑（组合输入期间 input 值实时变化，镜像自然跟随）；
// 未识别的符号串不高亮 = 留在标题当普通文本，所见即所得。
// 弹层：光标位于 # / @ token 内时输入框正下方弹过滤面板（↑↓ 选择、Enter/Tab 补全、Esc 关闭）。
import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import {
  CalendarClock,
  CalendarDays,
  CalendarRange,
  CloudSun,
  CornerDownLeft,
  Plus,
  Repeat,
  Sunrise,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Project } from "../data/tasks";
import { TITLE_MAX_LEN } from "../data/tasks";
import { onShortcutCmd } from "../lib/shortcuts";
import { parseInput, type ParsedInput } from "../lib/syntax";
import "./QuickAdd.css";

// @ 面板固定选项（打字过滤；完整文法如 @周五9:00 / @每周三 / @11月10日全天 由
// 解析器认，面板只是引导——「X 全天」带不带空格均可（2026-11-06 真机批宽容））
const WHEN_OPTIONS: { label: string; Icon: LucideIcon }[] = [
  { label: "今天", Icon: CalendarDays },
  { label: "明天", Icon: Sunrise },
  { label: "后天", Icon: CloudSun },
  { label: "下周一", Icon: CalendarRange },
  { label: "今天全天", Icon: CalendarDays },
  { label: "明天全天", Icon: CalendarDays },
  { label: "后天全天", Icon: CloudSun },
  { label: "每天", Icon: Repeat },
  { label: "每周一", Icon: Repeat },
  { label: "每月", Icon: CalendarClock },
];

// 弹层选项统一形状：项目（色点）或时间（图标）
type Option = { label: string; color?: string; Icon?: LucideIcon };

// 光标所在的触发 token（# / @ 打字中即可用，不依赖解析成功——"@明" 未成完整文法
// 也要弹面板）：从光标向前找最近空白，若该段起点是 #/@ 且位于串首/空白后即激活
function activeTrigger(
  text: string,
  caret: number,
): { kind: "#" | "@"; start: number; end: number; query: string } | null {
  if (caret <= 0) return null;
  const m = text.slice(0, caret).match(/([#@])(\S*)$/);
  if (!m || m.index === undefined) return null;
  const start = m.index;
  if (start > 0 && !/\s/.test(text[start - 1])) return null;
  const after = text.slice(start + 1);
  const endRel = after.search(/\s/);
  const end = start + 1 + (endRel === -1 ? after.length : endRel);
  return { kind: m[1] as "#" | "@", start, end, query: text.slice(start + 1, end) };
}

// @ 查询的尾部钟点段（如 "明天9:00" → "9:00"）：补全替换日期段时保留已输入的钟点
const timeSuffixOf = (query: string) => query.match(/\d{1,2}(:\d{2})?$/)?.[0] ?? "";

export default function QuickAdd({
  projects,
  onCreateTask,
  onJumpDate,
  hours,
}: {
  projects: Project[];
  // 创建任务（App 持久化后端后 resolve；失败 reject 由本组件保留输入文本不丢稿）
  onCreateTask: (draft: ParsedInput) => Promise<unknown>;
  // 创建已排期任务后跳转查看该日（dateOffset 相对今天）
  onJumpDate?: (dateOffset: number) => void;
  // 时间轴合法钟点域（设置驱动，ScheduleCanvas 透传；缺省 8–22 保持纯函数兼容）
  hours?: { start: number; end: number };
}) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0); // 光标位置（selectionStart），驱动弹层激活
  const [focused, setFocused] = useState(false);
  const [hi, setHi] = useState(0); // 弹层高亮项索引
  const [dismissed, setDismissed] = useState<string | null>(null); // Esc 关闭的 token 键（继续打字重开）
  const [invalid, setInvalid] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const mirrorRef = useRef<HTMLDivElement>(null);
  // 提交在途标记：await 期间连按 Enter 不重入（任务无唯一约束，双发即两条重复任务）
  const submittingRef = useRef(false);

  // 全局快捷键「添加日程」（经 App 转派）：唤窗动作已由 Rust 完成，此处只把光标
  // 落到录入框。不预填、不选中原有文本——快捷键语义只是定位，草稿文本保留可继续写。
  useEffect(() => onShortcutCmd("quick-add", () => inputRef.current?.focus()), []);

  // hours 为每次渲染新建的对象字面量，依赖取其原始值防无谓重算
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const parsed = useMemo(() => parseInput(text, projects, hours), [
    text,
    projects,
    hours?.start,
    hours?.end,
  ]);
  const trigger = useMemo(() => activeTrigger(text, caret), [text, caret]);

  // 弹层选项：# → 项目名 substring 过滤；@ → 剥掉尾部钟点段后与固定选项 label 做 includes
  const options = useMemo<Option[]>(() => {
    if (!trigger) return [];
    if (trigger.kind === "#") {
      const q = trigger.query.toLowerCase();
      return projects
        .filter((p) => p.name.toLowerCase().includes(q))
        .map((p) => ({ label: p.name, color: p.color }));
    }
    const dateQuery = trigger.query.slice(0, trigger.query.length - timeSuffixOf(trigger.query).length);
    return WHEN_OPTIONS.filter((o) => o.label.includes(dateQuery)).map((o) => ({
      label: o.label,
      Icon: o.Icon,
    }));
  }, [trigger, projects]);

  // 选项集变化（token 切换/过滤结果数变化）时高亮回到首项
  useEffect(() => {
    setHi(0);
  }, [trigger?.start, trigger?.query, options.length]);

  // 未知 # 项目的可建提示（解析器口径：不含符号、1..20 字才可建；零匹配时面板只显提示）
  const newProjectHint =
    trigger?.kind === "#" && options.length === 0 && parsed.newProject === trigger.query
      ? `未匹配已有项目，回车将新建「${trigger.query}」`
      : null;

  const triggerKey = trigger ? `${trigger.kind}:${trigger.start}:${trigger.query}` : null;
  const popupOpen = focused && trigger !== null && triggerKey !== dismissed && (options.length > 0 || newProjectHint !== null);
  const activeHi = Math.min(hi, Math.max(0, options.length - 1));

  // 补全：替换整个 token。# 补全为 "#名称 "（尾随空格自然收弹层）；@ 补全替换日期段、
  // 保留已输入的钟点段（@明9:00 选"明天" → @明天9:00），光标停在 token 末（弹层保持）
  const applyOption = (opt: Option) => {
    if (!trigger) return;
    const token =
      trigger.kind === "#"
        ? `#${opt.label} `
        : `@${opt.label}${timeSuffixOf(trigger.query)}`;
    const next = text.slice(0, trigger.start) + token + text.slice(trigger.end);
    const nextCaret = trigger.start + token.length;
    setText(next);
    setCaret(nextCaret);
    // 受控 input 换值后浏览器把光标放末尾，token 居中时需显式复位
    requestAnimationFrame(() => inputRef.current?.setSelectionRange(nextCaret, nextCaret));
  };

  // Enter 的补全判定：补全核心（不含 # 的尾随空格）与当前 token 文本一致 = 已完整，
  // 此时 Enter 应提交而非重复补全（#Alpha Launch / @明天9:00 已完整时直接回车建任务）
  const completionCore = (label: string) =>
    trigger && (trigger.kind === "#" ? `#${label}` : `@${label}${timeSuffixOf(trigger.query)}`);

  const flashInvalid = () => {
    setInvalid(true);
    window.setTimeout(() => setInvalid(false), 800);
  };

  const submit = async () => {
    if (submittingRef.current) return; // 在途短路：防快速连按 Enter 双发建任务
    if (!parsed.title) return; // 没有标题文本：静默忽略
    if (parsed.title.length > TITLE_MAX_LEN) {
      flashInvalid(); // 超长不截断——红边提示用户自行精简
      return;
    }
    submittingRef.current = true;
    try {
      await onCreateTask(parsed);
      setText("");
      setCaret(0);
      if (parsed.dateOffset !== undefined) onJumpDate?.(parsed.dateOffset);
    } catch (e) {
      console.error("快捷录入建任务失败", e);
      flashInvalid(); // 保留输入文本不丢稿，用户可重试
    } finally {
      submittingRef.current = false;
    }
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return; // IME 组合中不抢输入法的 Enter/方向键
    if (popupOpen && options.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHi((h) => (h + 1) % options.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHi((h) => (h - 1 + options.length) % options.length);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        applyOption(options[activeHi]);
        return;
      }
      if (e.key === "Enter" && completionCore(options[activeHi].label) !== text.slice(trigger!.start, trigger!.end)) {
        e.preventDefault();
        applyOption(options[activeHi]);
        return;
      }
    }
    if (e.key === "Enter") {
      e.preventDefault();
      void submit();
    } else if (e.key === "Escape") {
      // 只关弹层不冒泡（不触发页面级 Esc 逻辑），继续打字自动重开
      e.stopPropagation();
      if (triggerKey) setDismissed(triggerKey);
    }
  };

  // 镜像分段：识别成功的 token 上色，其余普通文本
  const mirrorParts = (): ReactNode[] => {
    const parts: ReactNode[] = [];
    let pos = 0;
    for (const tok of parsed.tokens) {
      if (tok.start > pos) parts.push(<span key={`t${pos}`}>{text.slice(pos, tok.start)}</span>);
      parts.push(
        <span key={`k${tok.start}`} className={`quick-add-tok quick-add-tok-${tok.kind}`}>
          {text.slice(tok.start, tok.end)}
        </span>,
      );
      pos = tok.end;
    }
    parts.push(<span key={`t${pos}`}>{text.slice(pos)}</span>);
    return parts;
  };

  return (
    <div className={`schedule-quick-add${invalid ? " invalid" : ""}`}>
      <Plus size={16} strokeWidth={2} className="schedule-quick-add-icon" />
      <div className="quick-add-wrap">
        <div className="quick-add-mirror" aria-hidden="true" ref={mirrorRef}>
          {mirrorParts()}
        </div>
        <input
          ref={inputRef}
          className="quick-add-input"
          value={text}
          placeholder="添加日程，或输入指令（#项目 ~耗时 @时间，如：看书 #阅读 ~2h @明天9:00）..."
          spellCheck={false}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onChange={(e) => {
            setText(e.target.value);
            setCaret(e.target.selectionStart ?? e.target.value.length);
          }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
          onScroll={() => {
            if (mirrorRef.current && inputRef.current) {
              mirrorRef.current.scrollLeft = inputRef.current.scrollLeft;
            }
          }}
        />
        {popupOpen && (
          <div className="quick-add-pop">
            {options.map((o, idx) => (
              <button
                key={o.label}
                type="button"
                className={`quick-add-option${idx === activeHi ? " active" : ""}`}
                // 阻止按下先触发 input blur 关面板，click 仍可选中
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => applyOption(o)}
              >
                {o.color !== undefined ? (
                  <span className="quick-add-dot" style={{ background: o.color }} />
                ) : (
                  o.Icon && <o.Icon size={14} strokeWidth={2} />
                )}
                <span>{o.label}</span>
              </button>
            ))}
            {newProjectHint && <div className="quick-add-hint">{newProjectHint}</div>}
          </div>
        )}
      </div>
      <span className="schedule-enter-pill">
        <CornerDownLeft size={11} strokeWidth={2} />
        Enter
      </span>
    </div>
  );
}
