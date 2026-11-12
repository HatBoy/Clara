"""阶段一：任务读/写接口。

薄壳层——解析请求（查询参数时区归一经 schemas）、调 service、按约定返回；
不含任何业务逻辑（body 内联时间的时区归一在 schemas 契约层 validator 做）。

端点一览：
- GET /api/tasks?start=&end=   按时间范围查（窗口内单次 ∪ 全部重复系列；无参数全量）
- GET /api/tasks?unscheduled=true   只返回未排期待办（忽略 start/end）
- POST /api/tasks              新建（快捷录入框；业务校验失败 422）
- GET /api/tasks/{task_id}     单条
- PATCH /api/tasks/{task_id}?mode=&date=   部分更新（键不出现不动，显式 null 清空；
  mode=this/following 需 date，走 Clone & Override 分流——例外实体化 / 系列拆分，
  见 services/tasks.py 模块头；业务校验失败 422）
- DELETE /api/tasks/{task_id}?mode=&date=  删除（service 层 6 场景判定：物理删 /
  TTL 软删 / 永久软删 / 墓碑 / UNTIL 截断；mode=this/following 需 date）
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from agents.engine import auto_schedule_run  # routers→agents 先例（ai_coach 同款）
from database import get_db
from schemas import TaskCreate, TaskOut, TaskUpdate, _to_naive_local
from services import tasks as task_service
from services.tasks import AlreadyScheduledError, NoSlotError

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
def list_tasks(
    start: datetime | None = Query(default=None, description="范围起点（含），ISO 8601；与任务区间取交集，重复系列恒返回（对 unscheduled=true 无效）"),
    end: datetime | None = Query(default=None, description="范围终点（不含），ISO 8601（对 unscheduled=true 无效）"),
    unscheduled: bool = Query(default=False, description="只返回未排期待办；为 true 时忽略 start/end"),
    db: Session = Depends(get_db),
):
    return task_service.list_tasks(
        db,
        start=_to_naive_local(start) if start is not None else None,
        end=_to_naive_local(end) if end is not None else None,
        unscheduled=unscheduled,
    )


@router.post("", response_model=TaskOut, status_code=201)
def create_task(body: TaskCreate, db: Session = Depends(get_db)):
    try:
        return task_service.create_task(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/estimate-hint")
def get_estimate_hint(title: str = Query(min_length=1, max_length=30), db: Session = Depends(get_db)):
    """同类任务历史时长提示（批 E1）：近 90 天相似标题已完成任务的实际时长均值
    与样本数（count≥2 前端才展示）。收件箱编辑区估时行旁「同类均值 X · 采纳」。"""
    return task_service.estimate_hint(db, title)


@router.post("/{task_id}/auto-schedule", response_model=TaskOut)
def auto_schedule(task_id: str, db: Session = Depends(get_db)):
    """单卡智能安排（收件箱待办卡按钮）：纯规则找空档直落（毫秒级；多任务
    批量排期全走对话草案卡——2026-10-20 用户决策），统一引擎定时间后落库，
    返回更新后的任务。

    异常翻译：不存在 → 404；已排期 → 409；无空档 → 422（文案可直显）。"""
    try:
        decision = auto_schedule_run(db, task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="任务不存在") from e
    except AlreadyScheduledError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except NoSlotError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    scheduled, _failed = task_service.apply_slot_decisions(db, [decision])
    if not scheduled:  # 决策与落库之间被并发占用（正常链路不出现）的兜底
        raise HTTPException(status_code=422, detail="未来 7 天没有可用空档")
    return scheduled[0]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: str,
    body: TaskUpdate,
    mode: Literal["all", "this", "following"] = Query(
        default="all", description="编辑范围：all 直接更新任务本体（默认）；this 仅此日程、following 此日程及后续（后两者仅重复系列、需 date，实体化例外 / 拆分系列，返回受影响的行）"
    ),
    date: str | None = Query(default=None, description="被编辑实例日 YYYY-MM-DD（mode=this/following 必填）"),
    db: Session = Depends(get_db),
):
    try:
        task = task_service.update_task(db, task_id, body, mode=mode, date=date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    mode: Literal["auto", "this", "following", "all"] = Query(
        default="auto", description="删除模式：auto=all 整任务删除；this 仅删本次、following 删及后续（后两者仅重复系列、需 date）"
    ),
    date: str | None = Query(default=None, description="被删实例日 YYYY-MM-DD（mode=this/following 必填）"),
    db: Session = Depends(get_db),
):
    try:
        deleted = task_service.delete_task(db, task_id, mode=mode, date=date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
