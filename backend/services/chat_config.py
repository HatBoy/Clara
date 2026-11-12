"""对话自然语言设置（2026-10-22 批 3，「日程助手对话智能」加强版）：chat 分支
settings 意图的执行权威——偏好规则增删 / 数值设置 / 撤销反演三动作在此落地，
agents.nodes.settings_handler 只做抽取与回执拼装（delete_confirmer 形态：LLM
理解语义、services 执行写库，节点零直接 ORM）。

设计原因：痛点是「以后周五下午都别排事」这类配置表达曾空转落 smalltalk/
coach，最快当晚反思链才以 ai_reflect、confidence≤0.55 入画像、永不成
user_manual——而它本就是"用户明说"（与手写同信任级），值得一条直达通道：
source="chat"、confidence=1.0、evidence 留对话原话。

三类动作（与 prompts.SETTINGS_PROMPT 契约一一对应）：
- prefer：经公共仲裁者 agents.arbiter（2026-11-03 记忆重构批 2，signal="user"
  可与既有规则 merge/replace 消歧——「下午不排会」与「周三下午开放讨论」矛盾
  共存根治；keep=印证既有行升 source=chat/1.0）；仲裁失败/无动作降级直写
  upsert_memory_rule(source="chat")——用户意图必须落；反思链让位硬闸白名单
  已扩 chat（夜间归纳不得动对话写入的规则，与 user_manual 同保护级）
- unprefer：difflib ≥0.6 对 content 匹配（单命中直删先例=删除预定位），
  多命中歧义列清单让用户点名、零命中如实回；删除落纠偏账（批 2 起）
- setting：白名单 _CHAT_SETTINGS（键→值域，执行权威）→ update_settings
  复用既有校验闸（档位/跨字段/粒度 ValueError 向上转追问回复，白得）
- undo：反演最近一条 kind="settings_change" proposal（创建即置 accepted、
  payload 记 actions journal；撤销后 outcome 改 "rejected" 防二次反演）。
  proposal 落库 → 撤销天然跨重启。撤销粒度=一轮（一轮多动作一条 journal
  原子回滚）。

层级红线：本模块在 services 层，不 import agents（prompt 在 nodes 侧组段）。
"""

import difflib
import re
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import MemoryRule, Proposal
from schemas import SettingsUpdate
from services.ai_data import create_proposal, record_proposal_outcome, upsert_memory_rule
from services.settings import get_settings, update_settings

logger = logging.getLogger(__name__)

# 对话可改设置白名单（执行权威；prompts.build_settings_prompt 组段同源）。
# 收敛原则：只开放"值域一句话说得清、改错代价低"的高频键——API key / 采集
# 配置 / 自动收录资格类不入列（复杂决策不塞进一句对话）。分钟口径键的粒度
# 与跨字段校验由 update_settings 统一把关，这里不重复
_CHAT_SETTINGS: dict[str, str] = {
    "reminder_lead_min": "提醒提前量（分钟）：0/5/10/15/30 之一；null = 不提醒（仅提示音）",
    "sound": "提示音：chime 铃声 / bowl 颂钵 / water 水滴 / off 关闭",
    "notify_style": "日程提醒形式（仅作用于日程提醒，助手消息在日程助手聊天框查看）：floating 居中悬浮框 / corner 右下角悬浮框 / system 系统通知",
    "theme_mode": "外观：light 亮色 / dark 暗色 / system 跟随系统",
    "work_minutes_per_day": "每日工时上限（分钟）：60-720",
    "lunch_enabled": "午休避让开关：true / false",
    "lunch_start_min": "午休开始（分钟，0-1440，30 分钟粒度）",
    "lunch_end_min": "午休结束（分钟，0-1440，30 分钟粒度）",
    "calendar_start_min": "日历每日可视开始（分钟，0-1440，30 分钟粒度）",
    "calendar_end_min": "日历每日可视结束（分钟，0-1440，30 分钟粒度）",
    "morning_brief_min": "晨间简报时刻（分钟，0-1440，30 分钟粒度）",
    "nightly_start_min": "夜间整理报点（分钟，0-1440，30 分钟粒度）",
    "weather_city": "排期天气对照城市（中文城市名；null = 停用）",
    "ai_style": "回复风格：balanced 默认 / concise 简洁 / detailed 详尽 / rigorous 严谨",
}

# 偏好匹配阈值（与 nodes._locate_by_title 删除预定位同 0.6 口径）
_SIM_THRESHOLD = 0.6


def apply_preference(db: Session, *, raw: str, category: str, content: str) -> dict:
    """写入长期偏好（prefer）。返回 {ok, reply, journal}——journal 由调用方
    累积进 settings_change proposal（undo 反演依据）。

    2026-11-03 记忆重构批 2：改走公共仲裁者（agents.arbiter，signal="user" 可
    与既有规则 merge/replace 消歧，杜绝「下午不排会」与「周三下午开放讨论」矛盾
    共存）；仲裁失败或无动作 → 降级为原直写（用户意图必须落，老行为兜底）。
    keep 动作 = 既有行被用户亲口印证 → 升 source=chat / 1.0（最强印证）。"""
    actions = None
    try:  # 延迟 import 防 services→agents 环（chat_config 模块头红线）
        from agents.arbiter import arbitrate_rules

        actions = arbitrate_rules(db, candidates=[{
            "category": category, "content": content,
            "evidence": f"对话明示：{raw}", "confidence": 1.0, "signal": "user"}])
    except Exception as exc:  # 仲裁链任何故障（LLM 不可用/解析失败）不挡用户意图
        logger.warning("[settings] 偏好仲裁失败，降级直写：%s", exc)
        actions = None
    if actions:
        return _apply_user_actions(db, actions, raw=raw, category=category,
                                   content=content)
    rule = upsert_memory_rule(
        db, category=category, content=content, confidence=1.0,
        evidence=f"对话明示：{raw}", source="chat",
    )
    reply = (
        f"记住了：{content}\n已写入长期偏好（设置 → 记忆画像可见），排期、简报与建议都会遵守。"
    )
    logger.info("[settings] 对话写入偏好 [%s] %s", category, rule.id)
    return {"ok": True, "reply": reply,
            "journal": {"type": "prefer", "rule_id": rule.id,
                        "category": category, "content": content}}


def _apply_user_actions(db: Session, actions: list[dict], *, raw: str,
                        category: str, content: str) -> dict:
    """执行仲裁动作（user 信号语义：落库恒 source=chat / 1.0——用户明示即铁律级）。

    new 直接落；replace/merge 先删旧行再落合成终态；keep = 印证既有行升级
    （source=chat / 1.0 / last_confirmed_at=now）。回执把合并/取代结果讲给用户
    （知情权），journal 记 rule_id 供 undo。"""
    from datetime import datetime as _dt

    from services.ai_data import delete_memory_rule

    first = actions[0]
    note = ""
    if first["action"] == "keep" and first.get("rule_id"):
        row = db.get(MemoryRule, first["rule_id"])
        if row is not None:
            row.source = "chat"
            row.confidence = 1.0
            row.last_confirmed_at = _dt.now()
            row.evidence = f"对话明示：{raw}"
            db.commit()
            logger.info("[settings] 对话印证既有偏好并升级 %s", row.id)
            return {"ok": True,
                    "reply": f"记住了：{row.content}\n（与已有偏好一致，已确认为你的明确设定）",
                    "journal": {"type": "prefer", "rule_id": row.id,
                                "category": row.category, "content": row.content}}
    if first["action"] in ("replace", "merge") and first.get("rule_id"):
        delete_memory_rule(db, first["rule_id"])  # 合并非纠偏：不落纠偏账
        note = "（已与你此前的相近偏好合并更新）"
    rule = upsert_memory_rule(
        db, category=first.get("category") or category,
        content=first.get("content") or content, confidence=1.0,
        evidence=f"对话明示：{raw}", source="chat",
    )
    logger.info("[settings] 对话写入偏好 [%s] %s（仲裁动作 %s）",
                rule.category, rule.id, first["action"])
    return {"ok": True,
            "reply": f"记住了：{rule.content}\n已写入长期偏好{note}，排期、简报与建议都会遵守。",
            "journal": {"type": "prefer", "rule_id": rule.id,
                        "category": rule.category, "content": rule.content}}


def remove_preference(db: Session, *, rule_ref: str) -> dict:
    """撤销一条偏好（unprefer）。rule_ref 是用户话里指的规则描述——LLM 契约给
    规则原文口径，这里 difflib 单命中直删（全 source 可撤——用户有权否决 AI
    归纳的规则）；多条歧义列清单、零命中如实回，绝不猜。2026-11-03 批 2 起
    走 delete_memory_rule(dismiss=True) 落纠偏账（删了就别再推）。"""
    from services.ai_data import delete_memory_rule

    rules = list(db.scalars(select(MemoryRule)))
    # 精确匹配短路（2026-11-05 真机批）：用户按助手列出的原文逐字指认
    # （「撤掉这条：每周四上午不安排任何任务」）仍被 difflib 判成两条歧义
    # ——「每周四上午…」与「每周五下午…」仅差 3 字相似度 ~0.7 双双过阈，
    # 永远删不掉。归一化（去空白/标点）后全等的规则直取，绝不进歧义分支
    def _canon(s: str) -> str:
        return re.sub(r"[\W_]+", "", s or "")

    exact = [r for r in rules if _canon(r.content) == _canon(rule_ref)]
    if exact:
        hits = exact[:1]
    else:
        scored = sorted(
            ((difflib.SequenceMatcher(None, rule_ref, r.content).ratio(), r)
             for r in rules),
            key=lambda p: -p[0],
        )
        hits = [r for score, r in scored if score >= _SIM_THRESHOLD]
    if not hits:
        return {"ok": False, "reply": "没找到对应的偏好规则——可到设置 → 记忆画像"
                                      "查看现有规则原文后再指认。", "journal": None}
    if len(hits) > 1:
        listing = "；".join(f"「{r.content[:30]}」" for r in hits[:3])
        return {"ok": False, "reply": f"有 {len(hits)} 条偏好都像你说的这条：{listing}……"
                                      f"请说出更完整的那条原文。", "journal": None}
    target = hits[0]
    snapshot = {"category": target.category, "content": target.content,
                "source": target.source, "confidence": target.confidence,
                "evidence": target.evidence}
    delete_memory_rule(db, target.id, dismiss=True)
    logger.info("[settings] 对话删除偏好 %s", target.id)
    return {"ok": True, "reply": f"已撤销偏好：「{target.content}」",
            "journal": {"type": "unprefer", "snapshot": snapshot}}


def apply_setting(db: Session, *, key: str, value) -> dict:
    """改一项设置（setting）。白名单外键直接拒（引导设置页）；值合法性全交
    update_settings 既有校验闸——ValueError 文案原样回给用户（档位/粒度/跨
    字段约束的报错本身就是追问话术）。"""
    if key not in _CHAT_SETTINGS:
        return {"ok": False, "reply": "这个设置暂不支持对话修改，请到设置页调整。",
                "journal": None}
    row = get_settings(db)
    old = getattr(row, key)
    try:
        update_settings(db, SettingsUpdate(**{key: value}))
    except ValueError as e:
        return {"ok": False, "reply": f"没改成：{e}", "journal": None}
    logger.info("[settings] 对话改设置 %s: %r → %r", key, old, value)
    return {"ok": True, "reply": f"已生效：{_setting_reply_text(key, old, value)}",
            "journal": {"type": "setting", "key": key, "old": old, "new": value}}


def _setting_reply_text(key: str, old, new) -> str:
    """回执里的旧→新对照（分钟口径键换算 HH:MM 更可读；其余原样）。"""
    if key.endswith("_min") and isinstance(old, (int, float)) and isinstance(new, (int, float)):
        fmt = lambda m: f"{int(m) // 60:02d}:{int(m) % 60:02d}"
        return f"{key} {fmt(old)} → {fmt(new)}"
    if isinstance(old, str) and len(old) > 20:
        old = old[:20] + "…"  # weather_city 长名截断
    return f"{key} {old!r} → {new!r}"


def undo_last_change(db: Session) -> dict:
    """撤销最近一轮对话设置变更（undo）：反演 settings_change proposal 的
    journal——setting 回写旧值 / prefer 删本轮新建行 / unprefer 按快照重建。
    已撤销的（outcome=rejected）跳过取更早一条；没有可撤销的如实回。"""
    rows = db.scalars(
        select(Proposal).where(Proposal.kind == "settings_change")
        .order_by(Proposal.created_at.desc()).limit(10)
    )
    target = next((r for r in rows if r.outcome != "rejected"), None)
    if target is None:
        return {"ok": False, "reply": "最近没有可撤销的对话设置变更。"}
    undone = []
    for act in target.payload.get("actions") or []:
        kind = act.get("type")
        if kind == "setting":
            update_settings(db, SettingsUpdate(**{act["key"]: act["old"]}))
            undone.append(f"{act['key']} 已回 {act['old']!r}")
        elif kind == "prefer":
            rule = db.get(MemoryRule, act.get("rule_id"))
            if rule is None and act.get("content"):
                # id 兜底：该规则可能被"unprefer → 再撤销恢复"链换过新 id——
                # 按 upsert 同款 category+content 定位（journal 记的是写入时刻 id）
                rule = db.scalar(select(MemoryRule).where(
                    MemoryRule.category == act.get("category"),
                    MemoryRule.content == act["content"]))
            if rule is not None:
                db.delete(rule)
                undone.append(f"偏好「{act['content'][:20]}」已删")
        elif kind == "unprefer":
            snap = act.get("snapshot") or {}
            from services.ai_data import slot_of

            db.add(MemoryRule(**snap, last_confirmed_at=datetime.now(),
                              slot=slot_of(snap.get("category") or "",
                                           snap.get("content") or "")))
            db.flush()
            undone.append(f"偏好「{(snap.get('content') or '')[:20]}」已恢复")
    record_proposal_outcome(db, target.id, "rejected", reason="对话撤销")
    db.commit()
    logger.info("[settings] 反演 settings_change %s（%d 项）", target.id, len(undone))
    body = "；".join(undone) if undone else "（该轮变更已无对应数据，仅作标记）"
    return {"ok": True, "reply": f"已撤销最近一轮变更：{body}。"}


def journal_change(db: Session, actions: list[dict]) -> None:
    """把本轮成功动作的 journal 落成 settings_change proposal 并立即置
    accepted（applied 直落先例——变更已生效，proposal 只作撤销锚与审计行，
    不是待确认卡；前端面板对该 kind 无渲染分支零打扰）。"""
    if not actions:
        return
    row = create_proposal(db, kind="settings_change", payload={"actions": actions})
    record_proposal_outcome(db, row.id, "accepted", reason="对话设置已生效")
