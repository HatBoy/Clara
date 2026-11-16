// AI 用量与成本卡（设置 → 智能体，2026-11-17 批 3）：本设备遥测聚合展示。
// 数据只读（GET /api/stats/ai-usage）；用量表不参与同步，换设备数字从零开始。
import { useEffect, useState } from "react";
import { Coins } from "lucide-react";
import EmptyState from "../EmptyState";
import { fetchAiUsage, type AiUsageSummary } from "../../lib/api";
import "./AiUsageCard.css";

const RANGES = [7, 30, 90] as const;
type RangeDays = (typeof RANGES)[number];

const fmtTokens = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;

/** 成本展示：金额极小时保留 4 位（0.0123 而非 0.01）；完全无已知成本给「—」 */
const fmtCost = (n: number) => `$${n > 0 && n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;

export default function AiUsageCard() {
  const [days, setDays] = useState<RangeDays>(30);
  const [data, setData] = useState<AiUsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    fetchAiUsage(days)
      .then((d) => alive && setData(d))
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : "用量加载失败");
      });
    return () => {
      alive = false;
    };
  }, [days]);

  // 按日补齐零值（by_day 只有有调用的日子；7/30/90 天轴要连续才可比）
  const bars = (() => {
    if (!data) return [];
    const map = new Map(data.by_day.map((b) => [b.date, b]));
    return Array.from({ length: days }, (_, i) => {
      const d = new Date();
      d.setDate(d.getDate() - (days - 1 - i));
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      return { date: key, calls: map.get(key)?.calls ?? 0, tokens: map.get(key)?.tokens ?? 0 };
    });
  })();
  const maxCalls = Math.max(1, ...bars.map((b) => b.calls));

  return (
    <div className="settings-group">
      <span className="settings-group-title">AI 用量与成本</span>
      <div className="settings-card ai-usage-card">
        <div className="ai-usage-head">
          <span className="settings-row-name">
            <Coins size={13} strokeWidth={2} className="ai-usage-ico" />
            近 {days} 天调用统计
          </span>
          <div className="ai-usage-range">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                className={`ai-usage-range-btn${days === r ? " active" : ""}`}
                onClick={() => setDays(r)}
              >
                {r}天
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <EmptyState compact tone="error" title="用量加载失败" description={error} />
        ) : !data ? (
          <div className="ai-usage-loading">加载中…</div>
        ) : data.calls === 0 ? (
          <EmptyState
            compact
            title="还没有 AI 调用记录"
            description="对话、排期、简报等每次调用模型都会记一条（仅本设备，不参与同步）"
          />
        ) : (
          <>
            <div className="ai-usage-kpis">
              <div className="ai-usage-kpi">
                <span className="ai-usage-kpi-value">{data.calls}</span>
                <span className="ai-usage-kpi-label">调用次数</span>
              </div>
              <div className="ai-usage-kpi">
                <span className="ai-usage-kpi-value">{fmtTokens(data.total_tokens)}</span>
                <span
                  className="ai-usage-kpi-label"
                  title={`输入 ${fmtTokens(data.prompt_tokens)} / 输出 ${fmtTokens(data.completion_tokens)}`}
                >
                  Token 总量
                </span>
              </div>
              <div className="ai-usage-kpi">
                <span className="ai-usage-kpi-value">
                  {data.unknown_cost_calls === data.calls ? "—" : fmtCost(data.cost_usd)}
                </span>
                <span className="ai-usage-kpi-label">估算成本</span>
              </div>
              <div className="ai-usage-kpi">
                <span className="ai-usage-kpi-value">{(data.calls / days).toFixed(1)}</span>
                <span className="ai-usage-kpi-label">日均调用</span>
              </div>
            </div>

            <div className="ai-usage-bars" aria-label="按日调用次数">
              {bars.map((b) => (
                <span
                  key={b.date}
                  className={`ai-usage-bar${b.calls === 0 ? " zero" : ""}`}
                  style={{ height: `${Math.max(4, (b.calls / maxCalls) * 100)}%` }}
                  title={`${b.date} · ${b.calls} 次 · ${fmtTokens(b.tokens)} token`}
                />
              ))}
            </div>

            <div className="ai-usage-table">
              <div className="ai-usage-row ai-usage-row-head">
                <span>功能</span>
                <span>次数</span>
                <span>Token</span>
                <span>成本</span>
              </div>
              {data.by_feature.map((f) => (
                <div key={f.feature} className="ai-usage-row">
                  <span title={f.feature}>{f.feature}</span>
                  <span>{f.calls}</span>
                  <span>{fmtTokens(f.tokens)}</span>
                  <span>{fmtCost(f.cost_usd)}</span>
                </div>
              ))}
            </div>

            <div className="ai-usage-models">
              {data.by_model.map((m) => (
                <span key={m.model_name} className="ai-usage-model-chip">
                  {m.model_name || "（未知模型）"}
                  <em>{m.calls} 次</em>
                </span>
              ))}
            </div>

            {(data.unknown_usage_calls > 0 || data.unknown_cost_calls > 0) && (
              <span className="ai-usage-note">
                {data.unknown_usage_calls > 0 &&
                  `${data.unknown_usage_calls} 次调用供应商未返回用量；`}
                {data.unknown_cost_calls > 0 &&
                  `${data.unknown_cost_calls} 次无法估算成本（模型不在价格表）——成本为下限值。`}
              </span>
            )}
          </>
        )}

        <span className="ai-usage-note">本设备统计，不参与同步备份；成本按 LiteLLM 价格表估算。</span>
      </div>
    </div>
  );
}
