"""Node2 全域上下文聚合器（批 5 正式化，2026-10-21）。

intake 基础快照（busy/负荷/设置/天气/近期对话）之上按 Node1 剖析条件聚合：
① 背靠背会议链深析——≥3h 长链的链尾清单（Node3 缓冲闸数据源：链尾后
30 分钟不宜紧贴新任务，真机"连开三小时会还要立刻写方案"的秘书教训）；
② 医疗频率历史——待排条目命中频率防呆表键 → last_similar_occurrence
365 天窗取最近同类时刻（洗牙 180d / 体检 365d 防呆判据）。

天气保持 intake 取数段（2026-10-18 起 forecast TTL 6h 缓存 + 城市未配置
恒 None 零成本）——条件化收益小于 profiler payload 契约破坏，取舍登记
docs/tech-debt.md。通勤常量表静态取用（finder.py），无需运行时聚合。
"""

import logging
import re
from datetime import datetime, time, timedelta

from database import SessionLocal

from .state import SchedulerState

logger = logging.getLogger(__name__)

# 恢复窗活动（2026-10-22 批 5 场景用例）：busy 区间标题命中 → 活动结束次晨
# 0:00-10:00 并入 busy——马拉松/高强度运动后次日恢复、晚宴团建聚餐后次晨
# 保护，早会/重活不再紧贴排入（纯规则零 LLM；窗口口径与 PROFILER 常识条款
# "高强度运动次日宜安排恢复日"互为表里——条款管 LLM 建议，此处管硬避让）
_RECOVERY_RE = re.compile(r"马拉松|徒步|爬山|越野|晚宴|团建|聚餐")
_RECOVERY_END = time(10)  # 次晨保护上界 10:00

# 背靠背链判定：区间间隔 ≤ 此值视为同一会议链（分钟）
_CHAIN_GAP_MIN = 10
# 长会链阈值：链总长 ≥ 此值的链尾进缓冲清单（分钟，= 3 小时）
_LONG_CHAIN_MIN = 180


def _long_chain_ends(busy: list[tuple[datetime, datetime]]) -> list[datetime]:
    """busy 区间 → 排序后 gap≤10min 合并为链，返回 ≥3h 链的链尾时刻清单。

    纯函数可单测；链可跨午夜（晚间会+次日早会同链——间隔判定按时刻不按日）。
    """
    chains: list[list[datetime]] = []
    for b0, b1 in sorted(busy):
        if chains and (b0 - chains[-1][1]).total_seconds() <= _CHAIN_GAP_MIN * 60:
            chains[-1][1] = max(chains[-1][1], b1)  # 重叠区间取更晚终点
        else:
            chains.append([b0, b1])
    return [end for start, end in chains
            if (end - start).total_seconds() >= _LONG_CHAIN_MIN * 60]


def _recovery_windows(busy_meta: list[dict]) -> list[tuple[datetime, datetime]]:
    """busy_meta（区间→任务映射，intake 同循环产物）→ 恢复窗区间清单：标题
    命中恢复类正则的区间，其结束日次日 0:00-10:00 一条。纯函数可单测；同日
    多场活动产重复区间无害（busy 避让只判重叠）。"""
    out: list[tuple[datetime, datetime]] = []
    for m in busy_meta:
        end = m.get("end")
        if not isinstance(end, datetime) or not _RECOVERY_RE.search(m.get("title") or ""):
            continue
        d = end.date() + timedelta(days=1)
        out.append((datetime.combine(d, time(0)),
                    datetime.combine(d, _RECOVERY_END)))
    return out


def context_node(state: SchedulerState) -> dict:
    """聚合节点：长链尾清单 + 医疗频率历史 + 任务依赖 + 恢复窗并入 context
    （浅拷贝扩展，不改 intake 快照的其余键）。零 LLM；读库仅医疗条目/带依赖
    条目存在时各一次。"""
    from services.tasks import last_similar_occurrence

    from .finder import FREQ_GUARD_DAYS

    ctx = dict(state["context"])
    ctx["long_chain_ends"] = _long_chain_ends(ctx["busy"])

    # 频率防呆历史：待排条目标题命中防呆表键才查（365 天窗最近一次同类；
    # None=没做过/超出窗——不构成防呆理由，finder 见 None 直接放行）
    pending_titles = [e.get("title") or "" for e in
                      (state.get("agent_entries") or []) + (state.get("conflict_queue") or [])]
    guard_keys = [k for k in FREQ_GUARD_DAYS if any(k in t for t in pending_titles)]
    if guard_keys:
        with SessionLocal() as db:
            last = {k: last_similar_occurrence(db, k) for k in guard_keys}
        ctx["last_medical"] = {k: v.isoformat() if v is not None else None
                               for k, v in last.items()}

    # 恢复窗（2026-10-22 批 5）：高强度活动/晚宴的次晨并入 busy（写回 ctx——
    # finder/adjudicator 消费的 busy 都来自 ctx["busy"]，一处写入两处生效）
    recovery = _recovery_windows(ctx.get("busy_meta") or [])
    if recovery:
        ctx["busy"] = list(ctx["busy"]) + recovery
    logger.info("[scheduler] context：长链尾 %d / 频率防呆查 %s / 恢复窗 %d",
                len(ctx["long_chain_ends"]), guard_keys or "无", len(recovery))
    return {"context": ctx}
