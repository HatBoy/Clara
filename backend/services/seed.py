"""seed 数据灌入（2026-11-15 起 lifespan 不再调用——任何模式启动一律零灌入，
用户拍板：启动时重灌测试数据对程序测试干扰大；需要测试数据时手动执行
backend/gen_test_data.py 随机生成。本模块仅供 pytest 的 seeded_db fixture
与一次性场景使用）。

以运行当天 0 点为基准把 seed_data 的相对偏移（dateOffset / 小时小数）换算为
绝对 naive 本地时间——保证灌入当天，日/周/月三视图都有数据可看。
refill_seed_proposals（dev 再生建议卡）已随 lifespan 调用点一同退役删除。
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import IS_PACKAGED
from models import Project, Task
from schemas import PRIORITY_INT  # priority 字符串 → 库内 INT（与写路径 service 共用单一来源）
from services.ai_data import create_proposal
from services.seed_data import SEED_PROJECTS, SEED_PROPOSALS, SEED_TASKS

logger = logging.getLogger(__name__)

# ISO 星期序号 1..7 → RFC 5545 星期码（BYDAY 取值）
_WEEKDAY_CODES = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}


def _to_rrule(rec: dict | None) -> str | None:
    """前端判别 dict → RRULE 字符串（本项目支持的 RFC 5545 子集）。

    daily → FREQ=DAILY；monthly → FREQ=MONTHLY；weekly → FREQ=WEEKLY;BYDAY=MO,WE,FR
    （weekdays 排序后映射，保证同规则字符串稳定）。None 透传。
    """
    if rec is None:
        return None
    if rec["freq"] == "daily":
        return "FREQ=DAILY"
    if rec["freq"] == "monthly":
        return "FREQ=MONTHLY"
    return "FREQ=WEEKLY;BYDAY=" + ",".join(_WEEKDAY_CODES[n] for n in sorted(rec["weekdays"]))


def _ingest_seed_proposals(db: Session) -> None:
    """灌入数据源页测试建议卡（SEED_PROPOSALS，seed_if_empty 尾部同批调用）。

    payload 与真实采集轮同构（from_ingest 标记，前端 isIngestProposal 命中），
    另加 seed 标记标识"这批是可再生的测试数据"；schedule 条目的相对偏移在
    此换算绝对 naive ISO。
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)  # 当天 0 点（naive 本地）
    for p in SEED_PROPOSALS:
        if p["kind"] == "inbox":
            payload = {"from_ingest": True, "seed": True, "items": p["items"]}
        else:
            payload = {
                "from_ingest": True,
                "seed": True,
                "draft_plan": [
                    {
                        "task_id": None,
                        "title": e["title"],
                        "start": (today + timedelta(days=e["dayOffset"], hours=e["hour"])).strftime("%Y-%m-%dT%H:%M:%S"),
                        "duration": e["duration"],
                        "origin": "agent",
                        **({"priority": e["priority"]} if "priority" in e else {}),
                    }
                    for e in p["entries"]
                ],
                "evidence": p["evidence"],
            }
        create_proposal(db, kind=p["kind"], payload=payload)


def seed_if_empty(db: Session) -> bool:
    """空库灌入 seed 数据；已灌过返回 False 跳过。

    判定口径是 projects 表空（seed 必含项目）。注意：未来若经写路径清空全部
    项目会被误判为首启——正式清数据应删库文件重启（本期无写路径，无碍）。

    打包形态恒 no-op（2026-11-06 真机批修复）：测试数据只属 dev——此前无此闸，
    打包版全新安装空库照灌全套测试任务/项目/建议卡，夜间整理流又对测试待办
    自动出排期草案（用户见「助手里冒出草案」即此）；与 refill_seed_proposals
    同口径。
    """
    if IS_PACKAGED:
        return False
    if db.scalar(select(func.count()).select_from(Project)) > 0:
        logger.info("数据库已有数据，跳过 seed")
        return False

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)  # 当天 0 点（naive 本地）

    projects = [Project(name=p["name"], color=p["color"], sort_order=i) for i, p in enumerate(SEED_PROJECTS)]
    db.add_all(projects)
    db.flush()  # 拿回模型层 default 生成的 UUID，任务的外键引用需要

    pid_by_name = {p.name: p.id for p in projects}
    for i, t in enumerate(SEED_TASKS):
        offset = t.get("dateOffset")  # 无 dateOffset = 未排期待办，三时间字段全空
        db.add(Task(
            title=t["title"],
            priority=PRIORITY_INT[t["priority"]],
            duration_mins=t.get("minutes"),
            notes=t.get("note"),
            start_time=None if offset is None else today + timedelta(days=offset, hours=t["start"]),
            end_time=None if offset is None or t.get("end") is None
            else today + timedelta(days=offset, hours=t["end"]),
            recurrence_rule=_to_rrule(t.get("recurrence")),
            # seed 直接入库不经 update 置位；已排期任务从灌入起即满足"曾经排期过"
            ever_scheduled=offset is not None,
            project_id=pid_by_name.get(t.get("project")),
            sort_order=i,  # 列表稳定序 = mock 数组序
        ))
    db.commit()

    # 数据源页测试建议卡与任务同批灌入（首启一次）；此后 dev 下的再生由
    # refill_seed_proposals 独立闸门负责（见其 docstring）
    _ingest_seed_proposals(db)

    logger.info(
        "灌入 seed 测试数据：%d 个任务 + %d 个项目 + %d 张采集建议卡（基准日 %s）",
        len(SEED_TASKS), len(projects), len(SEED_PROPOSALS), today.date(),
    )
    return True
