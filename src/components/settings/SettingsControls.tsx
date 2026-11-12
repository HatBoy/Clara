// "通用偏好"设置行的五个基础控件集中（类前缀 set-）：SetToggle 开关 / SetSegmented
// 分段单选 / SetSelect 下拉单选 / SetSlider 滑条 / SetTimeRangeSelect 时间对。
// 规格复刻既有惯例：开关 = .inbox-toggle（32×18 胶囊）、弹层 = .quick-add-pop
// （surface/border/圆角10/阴影）。
// 弹层关闭三路 = 点外 / Esc（capture + stopPropagation 只关弹层不连锁关设置页）/ 滚动
// （capture——scroll 不冒泡，capture 阶段才能截获任意滚动容器），口径同 QuickAdd/ConfirmPopover。
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import "./SettingsControls.css";

// —— 开关 ——
export function SetToggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean; // 总闸关闭等前置条件不满足时置灰禁点（灰态口径同 .set-lunch-group.off）
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`set-toggle${checked ? " on" : ""}`}
      onClick={() => onChange(!checked)}
      disabled={disabled}
    />
  );
}

// —— 文本输入（失焦/Enter 提交：草稿与已存值分治，onChange 不在每次击键上报；
//    Escape 弃改复位。视觉基调同 set-select-trigger）——
export function SetInput({
  value,
  placeholder,
  onChange,
}: {
  value: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]); // 外部回填（首载/别处变更）同步草稿
  const commit = () => {
    if (draft.trim() !== value) onChange(draft.trim());
    else setDraft(value); // 无实际变化复位（去掉手滑空格）
  };
  return (
    <input
      className="set-input"
      value={draft}
      placeholder={placeholder}
      maxLength={50}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur(); // Enter 即提交
        if (e.key === "Escape") {
          setDraft(value);
          e.currentTarget.blur();
        }
      }}
    />
  );
}

// —— 多行文本（自定义哲学等长文本）：同 SetInput 草稿/提交分治，失焦提交 / Escape
//    弃改；Enter 换行不提交（textarea 语义天然如此，不需要 blur 技巧）——
export function SetTextarea({
  value,
  placeholder,
  maxLength,
  rows = 3,
  onChange,
}: {
  value: string | null;
  placeholder?: string;
  maxLength?: number;
  rows?: number;
  onChange: (v: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  useEffect(() => setDraft(value ?? ""), [value]); // 外部回填（PATCH 响应后）同步草稿
  const commit = () => {
    const next = draft.trim() || null; // 纯空白归一 null（后端同口径，双保险）
    if (next !== value) onChange(next);
    else setDraft(value ?? ""); // 无实际变化复位
  };
  return (
    <div className="set-textarea-wrap">
      <textarea
        className="set-textarea"
        value={draft}
        placeholder={placeholder}
        maxLength={maxLength}
        rows={rows}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setDraft(value ?? "");
            e.currentTarget.blur();
          }
        }}
      />
      {/* 右下角字数实时计数（随草稿即时刷新，非提交值）——有 maxLength 才渲染 */}
      {maxLength != null && (
        <span className="set-textarea-count">
          {draft.length}/{maxLength}
        </span>
      )}
    </div>
  );
}

// —— 分段单选：灰底轨道 + 选中块白底凸起（icon 可选，外观模式三段带图标）——
export type SegmentOption<V extends string> = { value: V; label: string; icon?: ReactNode };

export function SetSegmented<V extends string>({
  value,
  options,
  onChange,
}: {
  value: V;
  options: readonly SegmentOption<V>[];
  onChange: (v: V) => void;
}) {
  return (
    <div className="set-seg" role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          className={`set-seg-item${value === o.value ? " on" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.icon}
          <span>{o.label}</span>
        </button>
      ))}
    </div>
  );
}

// —— 下拉单选：trigger（当前值 + ChevronDown）+ 右对齐弹层；value 支持 null（"不提醒"档）——
export type SelectOption<V extends string | number | null> = {
  value: V;
  label: string;
  disabled?: boolean;
};

export function SetSelect<V extends string | number | null>({
  value,
  options,
  onChange,
  width,
  disabled,
}: {
  value: V;
  options: readonly SelectOption<V>[];
  onChange: (v: V) => void;
  width?: number; // trigger 定宽（时间下拉 76px），缺省随内容
  disabled?: boolean; // 前置条件不满足时置灰禁点（同 SetToggle）
}) {
  const [open, setOpen] = useState(false);
  const [popStyle, setPopStyle] = useState<CSSProperties | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  // 打开瞬间按 trigger 视口矩形算 fixed 弹层坐标：弹层必须脱离 .settings-body
  // 滚动容器——absolute 弹层向下超出容器底缘会撑大其 scrollHeight，容器随即发生
  // 程序性滚动（贴底调整 + focus 滚动），scroll 事件又被"滚动关闭"监听捕获，弹层
  // 刚开即关（页面底部行必现）；fixed 不参与滚动容器的溢出计算，根因消除，弹层
  // 也不被容器底缘裁剪。向下空间不足时翻上方（空间感知口径同 ConfirmPopover），
  // max-height 按可用空间 clamp（120 下限兜底极矮窗口）。
  const togglePop = () => {
    if (open) {
      setOpen(false);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const vh = window.innerHeight;
      const below = vh - rect.bottom - 6;
      const above = rect.top - 6;
      const down = below >= 160 || below >= above;
      setPopStyle({
        ...(down ? { top: rect.bottom + 6 } : { bottom: vh - rect.top + 6 }),
        right: window.innerWidth - rect.right,
        minWidth: rect.width,
        maxHeight: Math.max(120, Math.min(260, (down ? below : above) - 4)),
      });
    }
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    // 滚动关闭只针对弹层外部的滚动（锚点漂移）；弹层自身内滚（时间 49 档超出
    // max-height）不关——scroll 事件 target 是发生滚动的元素，据此区分
    const onScroll = (e: Event) => {
      if (rootRef.current && e.target instanceof Node && rootRef.current.contains(e.target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  // 档外值兜底（2026-10-21 复查批 D2）：库值不在档内（档位调整后的存量值）时显示
  // 原值格式化而非空白——用户看得见现状，点选档内项即覆盖
  const label =
    options.find((o) => o.value === value)?.label ??
    (value === null ? "未设置" : String(value));
  return (
    <div className="set-select" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`set-select-trigger${open ? " open" : ""}`}
        style={width !== undefined ? { width } : undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={togglePop}
        disabled={disabled}
      >
        <span className="set-select-value">{label}</span>
        <ChevronDown
          size={14}
          strokeWidth={2}
          className={`set-select-chevron${open ? " flip" : ""}`}
        />
      </button>
      {open && popStyle && (
        <div className="set-select-pop" role="listbox" style={popStyle}>
          {options.map((o) => (
            <button
              key={String(o.value)}
              type="button"
              role="option"
              aria-selected={o.value === value}
              disabled={o.disabled}
              className={`set-select-option${o.value === value ? " on" : ""}`}
              onClick={() => {
                setOpen(false);
                if (!o.disabled && o.value !== value) onChange(o.value);
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// —— 滑条（数值偏好，如小部件透明度）：拖动中只更本地 draft（UI 即时预览数值），
// pointerup / 键盘 keyup / 失焦三路才提交 onCommit——滑条 onChange 拖动中连发，
// 逐次上抛会打出一串 PATCH 请求风暴（松手一发，同栏宽 300ms 防抖的节拍哲学）。
// committedRef 记最近已提交值：PATCH 回填前的重复触发（提交后未等回填即失焦）不重发
export function SetSlider({
  value,
  min,
  max,
  step = 1,
  format,
  onCommit,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  format: (v: number) => string; // 值显示（如 "38%"）
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(value);
  const committedRef = useRef(value);
  // 外部值变化（PATCH 回填 / 双窗同步）同步进 draft 与提交基线
  useEffect(() => {
    setDraft(value);
    committedRef.current = value;
  }, [value]);

  const commit = () => {
    if (draft !== committedRef.current) {
      committedRef.current = draft;
      onCommit(draft);
    }
  };

  const pct = ((draft - min) / (max - min)) * 100;
  return (
    <div className="set-slider">
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={draft}
        style={{ "--fill": `${pct}%` } as CSSProperties}
        onChange={(e) => setDraft(Number(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
      />
      <span className="set-slider-value">{format(draft)}</span>
    </div>
  );
}

// —— 时间对：两个时间下拉并排（30 分钟粒度）。minSpan 联动禁用不可行组合（可视范围
// 行 240 分钟 / 午休行 30 分钟），保证前端不发必被后端 422 的请求；跨行约束
// （午休 ⊆ 可视范围）交后端修复回填（PATCH 响应带修复后全量），不在前端连带 clamp。
const TIME_STEPS = Array.from({ length: 48 }, (_, i) => i * 30); // 0:00–23:30

const fmtMinute = (m: number) =>
  `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

export function SetTimeRangeSelect({
  start,
  end,
  minSpan,
  onStartChange,
  onEndChange,
}: {
  start: number; // 小时小数（11.5），与 Settings 口径一致
  end: number;
  minSpan: number; // 分钟
  onStartChange: (hour: number) => void;
  onEndChange: (hour: number) => void;
}) {
  const startMin = Math.round(start * 60);
  const endMin = Math.round(end * 60);
  const startOptions: SelectOption<number>[] = TIME_STEPS.map((m) => ({
    value: m,
    label: fmtMinute(m),
    disabled: m > endMin - minSpan,
  }));
  // 结束档多一档 24:00（1440，仅结束侧有意义）
  const endOptions: SelectOption<number>[] = [...TIME_STEPS, 1440].map((m) => ({
    value: m,
    label: fmtMinute(m),
    disabled: m < startMin + minSpan,
  }));
  return (
    <div className="set-time-range">
      <SetSelect
        value={startMin}
        options={startOptions}
        onChange={(m) => onStartChange(m / 60)}
        width={76}
      />
      <span className="set-time-range-sep">–</span>
      <SetSelect
        value={endMin}
        options={endOptions}
        onChange={(m) => onEndChange(m / 60)}
        width={76}
      />
    </div>
  );
}
