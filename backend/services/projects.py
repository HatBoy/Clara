"""项目业务层：读全量 + 写路径 CRUD（新建/更新/删除）。

分层红线：router 只翻译状态码（ConflictError→409、ValueError→422、None/False→404），
本层不知道 HTTP；业务规则与日志都在这里。删除是软删（对齐全库口径），且同事务把
归属任务的 project_id 置空——用户侧语义"项目没了，任务回默认收件箱"。
"""

import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from models import Project, Task
from schemas import ProjectUpdate

logger = logging.getLogger(__name__)


class ConflictError(Exception):
    """业务冲突（active 项目重名），router 翻译为 409。"""


# Optional 入参会放行显式 null，但这些是 NOT NULL 列——直接 setattr 会打 IntegrityError 500，service 拦为 422
_NOT_NULL_FIELDS = {"name": "项目名", "starred": "星标", "color": "项目色"}


def list_projects(db: Session) -> list[Project]:
    """项目全量（恒过滤软删，按 sort_order 稳定排序 = 侧栏渲染序）。"""
    stmt = select(Project).where(Project.is_deleted.is_(False)).order_by(Project.sort_order)
    projects = list(db.scalars(stmt))
    logger.info("查询项目：%d 个", len(projects))
    return projects


def get_project(db: Session, project_id: str) -> Project | None:
    """单条；软删视为不存在（业务规则在 service，不下沉 router）。"""
    project = db.get(Project, project_id)
    result = project if project is not None and not project.is_deleted else None
    if result is None:
        logger.info("读取单条项目：id=%s 未命中（不存在或已软删）", project_id)
    else:
        logger.info("读取单条项目：id=%s 命中「%s」", project_id, result.name)
    return result


def create_project(db: Session, name: str, color: str) -> Project:
    """新建项目：重名冲突 409（active 口径，软删墓碑名不占）；sort_order 排全表末位。"""
    dup = db.scalar(select(Project).where(Project.name == name, Project.is_deleted.is_(False)))
    if dup is not None:
        raise ConflictError(f"项目名已存在: {name}")
    sort_order = db.scalar(select(func.coalesce(func.max(Project.sort_order), -1))) + 1
    project = Project(name=name, color=color, sort_order=sort_order)
    db.add(project)
    db.commit()
    db.refresh(project)  # created_at/updated_at 是 server_default，refresh 才能进响应体
    logger.info("新建项目：「%s」id=%s sort_order=%d", name, project.id, sort_order)
    return project


def update_project(db: Session, project_id: str, patch: ProjectUpdate) -> Project | None:
    """部分更新：patch.exclude_unset 取数，键不出现不动；返回 None = 不存在（router 404）。"""
    project = get_project(db, project_id)
    if project is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return project  # 空 patch 短路：不发 UPDATE、不误抬 updated_at
    for key, label in _NOT_NULL_FIELDS.items():
        if key in data and data[key] is None:
            raise ValueError(f"{label}不允许置空")
    if "name" in data and data["name"] != project.name:
        dup = db.scalar(
            select(Project).where(
                Project.name == data["name"],
                Project.is_deleted.is_(False),
                Project.id != project_id,
            )
        )
        if dup is not None:
            raise ConflictError(f"项目名已存在: {data['name']}")
    for key, value in data.items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    logger.info("更新项目 id=%s「%s」：字段 %s", project_id, project.name, sorted(data))
    return project


def delete_project(db: Session, project_id: str) -> bool:
    """软删项目；同事务把归属任务（未软删口径）的 project_id 置空。

    name 重写为墓碑（原名截断 + "#del#" + id 前缀，恒 ≤20）：软删行仍占 name 唯一
    约束，不改名则之后无法新建同名项目。返回 False = 不存在（router 404）。
    """
    project = get_project(db, project_id)
    if project is None:
        return False
    detached = db.execute(
        update(Task)
        .where(Task.project_id == project_id, Task.is_deleted.is_(False))
        .values(project_id=None)
    )
    old_name = project.name
    project.is_deleted = True
    project.name = old_name[:7] + f"#del#{project_id[:8]}"
    db.commit()
    logger.info("删除项目「%s」id=%s：归属任务 %d 条置空", old_name, project_id, detached.rowcount)
    return True
