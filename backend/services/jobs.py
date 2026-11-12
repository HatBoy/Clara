"""定时任务服务层（2026-10-20 增，「设置-定时任务」配套）。

两部分职责：
- 任务注册表（list_jobs）：内置六流 + 用户自定义简报逐行展开，静态文案后端
  单源（前端不硬编码 desc）；enabled / time_min / last_run_at 从设置与
  workflow_runs 实时推导。
- 自定义每日简报 CRUD：约束全在服务层——≤3 条、同 brief_min 唯一（编辑排除
  自身）、30 分钟粒度；违规 ValueError，routers 薄壳翻译 422（同 settings 惯例）。
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import CustomBrief, WorkflowRun
from services.settings import get_settings

# 自定义简报上限：LLM 成本 + 打扰度都是线性叠加，3 条够覆盖"早报/晚报/周报"心智
MAX_BRIEFS = 3


# —— 自定义每日简报 CRUD ————————————————————————————————————————————————


def list_briefs(db: Session) -> list[CustomBrief]:
    """全部简报行（含停用），按时刻升序（页面时间轴阅读序）。"""
    return list(db.scalars(select(CustomBrief).order_by(CustomBrief.brief_min)))


def _validate_brief_min(brief_min: int) -> None:
    if brief_min % 30 != 0:
        raise ValueError("简报时刻需为 30 分钟整点档")


def create_brief(db: Session, *, brief_min: int, instruction: str) -> CustomBrief:
    """新建简报：粒度 → 同刻唯一 → 上限，三闸顺序校验。"""
    _validate_brief_min(brief_min)
    if db.scalar(select(CustomBrief).where(CustomBrief.brief_min == brief_min)) is not None:
        raise ValueError("该时刻已有一条简报，请换一个时刻")
    if len(list_briefs(db)) >= MAX_BRIEFS:
        raise ValueError(f"最多 {MAX_BRIEFS} 条自定义简报")
    row = CustomBrief(brief_min=brief_min, instruction=instruction)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_brief(
    db: Session,
    brief_id: str,
    *,
    brief_min: int | None = None,
    instruction: str | None = None,
    enabled: bool | None = None,
) -> CustomBrief:
    """部分更新（键不出现 = 不动）；改时刻时粒度与同刻唯一照查（排除自身）。"""
    row = db.get(CustomBrief, brief_id)
    if row is None:
        raise LookupError(f"简报不存在：{brief_id}")
    if brief_min is not None:
        _validate_brief_min(brief_min)
        clash = db.scalar(
            select(CustomBrief).where(
                CustomBrief.brief_min == brief_min, CustomBrief.id != brief_id
            )
        )
        if clash is not None:
            raise ValueError("该时刻已有一条简报，请换一个时刻")
        row.brief_min = brief_min
    if instruction is not None:
        row.instruction = instruction
    if enabled is not None:
        row.enabled = enabled
    db.commit()
    db.refresh(row)
    return row


def delete_brief(db: Session, brief_id: str) -> None:
    """物理删除（硬删行：简报无历史引用，workflow_runs 留作运行痕迹不清理）。"""
    row = db.get(CustomBrief, brief_id)
    if row is None:
        raise LookupError(f"简报不存在：{brief_id}")
    db.delete(row)
    db.commit()


# —— 任务注册表 ————————————————————————————————————————————————————————


def _last_run_map(db: Session) -> dict[str, datetime]:
    """按工作流名聚合最近认领时刻（认领 ≠ 成功，页面口径是"最近运行"不谎报）。
    采集多槽（ingest@2/3/4，2026-10-23）并入主键 "ingest" 取最大值——页面只看
    采集流的最近一次，不逐槽展开。"""
    rows = db.execute(
        select(WorkflowRun.name, func.max(WorkflowRun.created_at)).group_by(WorkflowRun.name)
    ).all()
    runs = {name: ts for name, ts in rows}
    ingest_slots = [ts for n, ts in runs.items() if n == "ingest" or n.startswith("ingest@")]
    if ingest_slots:
        runs["ingest"] = max(ingest_slots)
    return runs


def list_jobs(db: Session) -> list[dict]:
    """定时任务总清单：内置六流注册表 + 自定义简报逐行展开，供 JobsSettings 页
    渲染。顺序即页面展示序：每日节奏组（晨间/采集/夜间）→ AI 自动增强组
    （会前准备/高难协助/自定义简报）→ 监控组（负荷）。"""
    settings = get_settings(db)
    last_runs = _last_run_map(db)

    jobs: list[dict] = [
        {
            "key": "morning",
            "title": "晨间对齐",
            "desc": "每天早上生成今日简报：日程总览、任务预警与防坑指南。",
            "enabled": settings.morning_enabled,
            "requires_ai": True,
            "time_min": settings.morning_brief_min,
            "time_note": None,
            "last_run_at": last_runs.get("morning"),
        },
        {
            "key": "ingest",
            "title": "数据采集",
            "desc": (
                f"每天 {settings.ingest_runs_per_day} 轮汇聚各数据源的新动态，提炼成"
                "收件箱建议与素材；无新数据时静默执行不打扰。"
            ),
            "enabled": settings.ingest_enabled,
            "requires_ai": True,
            "time_min": settings.ingest_brief_min,
            "time_note": "后续轮次当天顺延",
            "last_run_at": last_runs.get("ingest"),
        },
        {
            "key": "nightly",
            "title": "夜间重要整理",
            "desc": "到点后趁空闲按优先级挑出未排期待办（至多 5 件）预排时间草案供确认，并整理记忆画像。",
            "enabled": settings.nightly_enabled,
            "requires_ai": True,
            "time_min": settings.nightly_start_min,
            "time_note": "到点后空闲时运行",
            "last_run_at": last_runs.get("nightly"),
        },
        {
            "key": "prep",
            "title": "会前准备",
            "desc": "每晚扫描明日重要会议（优先级高 / 时长 ≥90 分钟 / 标题含会议、评审、汇报等），生成各会议准备清单。",
            "enabled": settings.prep_enabled,
            "requires_ai": True,
            "time_min": settings.nightly_start_min,
            "time_note": "与夜间重要整理同时刻",
            "last_run_at": last_runs.get("prep"),
        },
        {
            "key": "hardtask",
            "title": "高难协助",
            "desc": "每晚挑一件滞留最久的未排期难任务（时长 ≥90 分钟 / 优先级高 / 标题含方案、设计、报告等），给出分解步骤与建议。",
            "enabled": settings.hardtask_enabled,
            "requires_ai": True,
            "time_min": settings.nightly_start_min,
            "time_note": "会前准备之后",
            "last_run_at": last_runs.get("hardtask"),
        },
        {
            # 周报自动生成（2026-11-07 统计 120 分档）：周日晚随夜间报点，认领键
            # weekly_report（(name, run_date) 唯一 + weekday 门双保险每周一次）
            "key": "weekly_report",
            "title": "周报自动生成",
            "desc": "每周日晚间随夜间整理报点自动生成本周 AI 周报，留档统计页、全文进助手面板并推送开启推送的渠道。",
            "enabled": settings.weekly_report_enabled,
            "requires_ai": True,
            "time_min": settings.nightly_start_min,
            "time_note": "仅周日晚间运行",
            "last_run_at": last_runs.get("weekly_report"),
        },
        {
            # 会后纪要邀请（2026-11-10 批 3）：事件窗驱动（重要会议结束 15~120
            # 分钟内）而非报点——time_min 恒 None 由 time_note 说明节奏；认领键
            # minutes_invite（(name, run_date) 唯一 = 每日至多邀请一次）
            "key": "minutes_invite",
            "title": "会后纪要邀请",
            "desc": "重要会议（优先级高 / 时长 ≥90 分钟 / 标题含会议、评审等）结束后 15 分钟起询问补记纪要，粘贴原文即提炼行动项，每天至多一次。",
            "enabled": settings.minutes_enabled,
            "requires_ai": True,
            "time_min": None,
            "time_note": "重要会议结束后 15 分钟",
            "last_run_at": last_runs.get("minutes_invite"),
        },
        {
            # 对话上下文自动压缩（2026-10-26 增）：挂轮末 fire-and-forget 而非 cron
            # tick——同 heartbeat 先例无认领谓词不落 workflow_runs（每轮对话落行会
            # 撑爆台账），last_run_at 恒 None 由 time_note 副行说明节奏
            "key": "chat_autocompact",
            "title": "对话上下文压缩",
            "desc": "每轮对话结束后检查，较早对话超量（条数或字符）时自动并入滚动摘要，近期对话保持原文。",
            "enabled": settings.chat_autocompact_enabled,
            "requires_ai": True,
            "time_min": None,
            "time_note": "每轮对话结束后自动检查",
            "last_run_at": None,
        },
    ]
    for brief in list_briefs(db):
        jobs.append({
            "key": f"brief:{brief.id}",
            "title": "定时简报",
            "desc": brief.instruction,
            "enabled": brief.enabled,
            "requires_ai": True,
            "time_min": brief.brief_min,
            "time_note": None,
            "last_run_at": last_runs.get(f"brief:{brief.id}"),
        })
    # 局部导入防环（惯例同 settings.py；workflows 并不反向 import 本模块）
    from services.workflows import heartbeat_last_run

    jobs.append({
        "key": "heartbeat",
        "title": "负荷监控",
        "desc": "白天定时检查日程负荷，超过当日工时八成时提醒留白（纯规则，无需配置）。",
        "enabled": True,
        "requires_ai": False,
        "time_min": None,
        "time_note": "每 3 小时自动检查",
        # 纯规则不落 workflow_runs（每 3h 落行日增 8 行），最近检查时刻走内存口径
        "last_run_at": heartbeat_last_run(),
    })
    return jobs
