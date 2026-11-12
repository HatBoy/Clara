// "快捷键"设置页：15 个全局动作的键位表与改键入口。键位真源在 Rust 侧
// shortcuts.json（启动即读表注册热键，后端缺席时窗口级动作照常可用），本页只做
// 读表 / 改键 / 恢复默认，不参与触发——按下由 Rust handler 反查动作后 emit 给 App。
// 组卡内两列格子（.shortcut-grid 复刻个性化页哲学网格先例）：头行名称 + 状态徽标
// （「自定义」= 与默认键不同（纯前端判定）；「被占用」= Rust failed 集合，该键注册
// 失败被外部应用抢占）、键位钮整宽。骨架类复用 GeneralSettings.css（.settings-* 族）；
// 自管理数据照 JobsSettings 先例（mount 自 fetch、提示 3s 自清）。
// 键位捕获控件内联本文件（单次使用不进 SettingsControls 共享池）。
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchShortcuts, resetShortcuts, updateShortcut, type ShortcutTable } from "../../lib/tauri";
import { SHORTCUT_ACTIONS, SHORTCUT_GROUPS } from "../../lib/shortcuts";
import "./GeneralSettings.css";
import "./ShortcutSettings.css";

// 行内反馈（id = 动作 id，"reset" = 页脚恢复默认钮）
type Hint = { id: string; ok: boolean; text: string };

// 符号键 → 字面字符（Rust parse_key 双写法同源："[" 与 "BRACKETLEFT" 同码，
// 故展示串与提交串同一个值，与默认表 "Alt+[" 的写法一致）
const SYMBOL_KEYS: Record<string, string> = {
  BracketLeft: "[",
  BracketRight: "]",
  Backslash: "\\",
  Minus: "-",
  Equal: "=",
  Semicolon: ";",
  Quote: "'",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Backquote: "`",
};

// 功能键用 Web 码名提交（parse_key 同名接受：ARROWLEFT / SPACE / ENTER …）
const NAMED_KEYS = [
  "Space",
  "Enter",
  "Tab",
  "Backspace",
  "Delete",
  "Insert",
  "Home",
  "End",
  "PageUp",
  "PageDown",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "PrintScreen",
];

function isFunctionKey(code: string): boolean {
  return /^F([1-9]|1[0-9]|2[0-4])$/.test(code);
}

// 按下的一次 keydown → "Ctrl+Alt+N" 式键串；无法作为主键（只按修饰键 / 无修饰键的非
// F 键 / parse_key 不认的键如 IntlBackslash）返回 null 由调用方提示重按。
function comboFromKeyEvent(e: KeyboardEvent): string | null {
  const mods: string[] = [];
  if (e.ctrlKey) mods.push("Ctrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  if (e.metaKey) mods.push("Super"); // Win 键（parse 侧 SUPER）
  if (mods.length === 0 && !isFunctionKey(e.code)) return null;
  const code = e.code;
  let key: string | null;
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3); // KeyA → A
  else if (/^Digit[0-9]$/.test(code)) key = code.slice(5); // Digit1 → 1
  else if (isFunctionKey(code)) key = code;
  else if (/^Numpad/.test(code)) key = code; // parse_key 接受 NUMPAD* 全族
  else if (code in SYMBOL_KEYS) key = SYMBOL_KEYS[code];
  else if (NAMED_KEYS.includes(code)) key = code;
  else key = null;
  if (!key) return null;
  return [...mods, key].join("+");
}

export default function ShortcutSettings() {
  // 键位表 + 注册失败集合（Rust ShortcutState）；null = 非桌面环境（读不到表，改键钮禁用）
  const [table, setTable] = useState<ShortcutTable | null>(null);
  const [capturingId, setCapturingId] = useState<string | null>(null);
  const [hint, setHint] = useState<Hint | null>(null);
  const hintTimerRef = useRef<number | null>(null);

  const flash = useCallback((id: string, ok: boolean, text: string) => {
    setHint({ id, ok, text });
    if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current);
    hintTimerRef.current = window.setTimeout(() => setHint(null), 3000);
  }, []);

  useEffect(() => {
    void fetchShortcuts().then(setTable);
    return () => {
      if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current);
    };
  }, []);

  // 提交改键：Rust 侧 unregister 旧键 → register 新键，失败回滚旧键且不落盘，
  // 成功才写 shortcuts.json——故成功后重拉权威表（占用冲突返回的文案直接展示）
  const commit = useCallback(
    (id: string, combo: string) => {
      void updateShortcut(id, combo).then((err) => {
        if (err) {
          flash(id, false, err);
          return;
        }
        flash(id, true, "已生效");
        void fetchShortcuts().then(setTable);
      });
    },
    [flash],
  );

  // 捕获态按键：window 捕获先于 SettingsDialog 的 document 捕获（Esc），二者都 stopPropagation
  // 才不连带关设置页；preventDefault 防浏览器把 Tab/方向键等吃掉。Esc 语义 = 取消捕获。
  useEffect(() => {
    if (!capturingId) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;
      if (e.key === "Escape") {
        setCapturingId(null);
        return;
      }
      const combo = comboFromKeyEvent(e);
      if (!combo) {
        flash(capturingId, false, "请用「修饰键 + 字母 / 数字 / 符号」组合（F1-F24 可单键）");
        return;
      }
      setCapturingId(null);
      commit(capturingId, combo);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [capturingId, commit, flash]);

  const handleReset = () => {
    void resetShortcuts().then((t) => {
      if (!t) {
        flash("reset", false, "恢复默认失败");
        return;
      }
      setTable(t);
      flash("reset", true, "已恢复默认键位");
    });
  };

  return (
    <div className="settings-body">
      <div className="settings-page-head">
        <span className="settings-page-title">快捷键</span>
        <span className="settings-page-sub">全局热键，窗口最小化或隐藏时同样生效</span>
      </div>

      {table === null && <div className="shortcut-note">仅在桌面应用中可配置快捷键</div>}

      {SHORTCUT_GROUPS.map((group) => (
        <div className="settings-group" key={group.id}>
          <span className="settings-group-title">{group.label}</span>
          <div className="settings-card">
            <div className="shortcut-grid">
              {SHORTCUT_ACTIONS.filter((a) => a.group === group.id).map((action) => {
                const combo = table?.bindings[action.id] ?? action.defaultCombo;
                const occupied = (table?.failed ?? []).includes(action.id);
                return (
                  <div
                    className="shortcut-cell"
                    key={action.id}
                    title={occupied ? "该组合已被其他应用注册，点键位钮改键" : action.desc}
                  >
                    <div className="shortcut-cell-head">
                      <span className="shortcut-cell-name">{action.label}</span>
                      {occupied ? (
                        <span className="shortcut-flag conflict">被占用</span>
                      ) : combo !== action.defaultCombo ? (
                        <span className="shortcut-flag custom">自定义</span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      className={`shortcut-key-btn${capturingId === action.id ? " capturing" : ""}${
                        occupied ? " conflict" : ""
                      }`}
                      disabled={table === null}
                      onClick={() =>
                        setCapturingId((prev) => (prev === action.id ? null : action.id))
                      }
                    >
                      {capturingId === action.id ? "按下新组合…" : combo}
                    </button>
                    {hint && hint.id === action.id && (
                      <span className={`shortcut-cell-hint${hint.ok ? " ok" : " bad"}`}>
                        {hint.text}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ))}

      <div className="shortcut-foot">
        <button type="button" className="settings-action-btn" onClick={handleReset} disabled={table === null}>
          恢复默认
        </button>
        {hint && hint.id === "reset" && (
          <span className={`settings-action-hint${hint.ok ? " ok" : " bad"}`}>{hint.text}</span>
        )}
        <span className="shortcut-note">
          点键位钮进入捕获，按下新组合即改键（Esc 取消）；改键立即生效并持久化，目标键被其他动作占用或系统保留时不改动
        </span>
      </div>
    </div>
  );
}
