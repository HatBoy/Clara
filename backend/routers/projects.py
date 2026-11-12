"""阶段一：项目读/写接口。

薄壳层——调 service、按约定翻译状态码（ConflictError→409、ValueError→422、
不存在→404）；不含任何业务逻辑。

端点一览：
- GET /api/projects            项目全量（按 sort_order）
- POST /api/projects           新建（201；重名 409）
- PATCH /api/projects/{id}     部分更新（键不出现不动，显式 null 清空/置空拦截）
- DELETE /api/projects/{id}    软删（归属任务 project_id 置空）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import ProjectCreate, ProjectOut, ProjectUpdate
from services import projects as project_service
from services.projects import ConflictError

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return project_service.list_projects(db)


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    try:
        return project_service.create_project(db, body.name, body.color)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, body: ProjectUpdate, db: Session = Depends(get_db)):
    try:
        project = project_service.update_project(db, project_id, body)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, db: Session = Depends(get_db)):
    if not project_service.delete_project(db, project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
