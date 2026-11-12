"""MCP 服务接入接口（前缀 /api/mcp）。

薄壳层——调 service、按约定翻译状态码（ConflictError→409、ValueError→422、
不存在→404）；不含任何业务逻辑。字面量路由（/test 与三个 local-folder）声明
在 /{server_id} 之前——Starlette 按注册序匹配，POST /local-folder/test 会被
/{server_id}/test 吞掉。

端点一览：
- GET  /api/mcp                    服务全量（按创建序）
- POST /api/mcp                    新建（201；重名 409）
- PATCH /api/mcp/{id}              部分更新（键不出现不动；不开放测试三列）
- DELETE /api/mcp/{id}             硬删（204）
- POST /api/mcp/{id}/test          对已保存服务测试（结果落库；探测失败也是 200）
- POST /api/mcp/test               草稿参数纯探测（不读库不落库）
- GET  /api/mcp/local-folder       本地文件夹配置读（get_or_create 落行）
- PUT  /api/mcp/local-folder       保存目录列表 + 开关（多目录，422：相对路径/启用无路径/目录不存在/超 10 个）
- POST /api/mcp/local-folder/test  文件夹有效性测试（body 带 paths = 草稿探测；
                                   失败也是 200——失败是数据）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    LocalFolderOut,
    LocalFolderTestIn,
    LocalFolderTestOut,
    LocalFolderUpdate,
    McpServerCreate,
    McpServerOut,
    McpServerTestIn,
    McpServerTestOut,
    McpServerUpdate,
)
from services import local_folder, mcp_servers as mcp_service
from services.mcp_servers import ConflictError

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("", response_model=list[McpServerOut])
def list_servers(db: Session = Depends(get_db)):
    return mcp_service.list_servers(db)


@router.post("", response_model=McpServerOut, status_code=201)
def create_server(body: McpServerCreate, db: Session = Depends(get_db)):
    try:
        return mcp_service.create_server(db, body)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


# ---- 字面量路由：必须在 /{server_id} 之前注册 ----


@router.post("/test", response_model=McpServerTestOut)
async def test_server_draft(body: McpServerTestIn):
    # 草稿探测：表单"测试连通性"按钮用当前填写参数，纯函数调用即可（无 db）
    return await mcp_service.probe_server(
        body.transport, body.command, body.args, body.env, body.url, body.headers
    )


@router.get("/local-folder", response_model=LocalFolderOut)
def get_local_folder(db: Session = Depends(get_db)):
    return local_folder.get_local_folder(db)


@router.put("/local-folder", response_model=LocalFolderOut)
def save_local_folder(body: LocalFolderUpdate, db: Session = Depends(get_db)):
    try:
        return local_folder.save_local_folder(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/local-folder/test", response_model=LocalFolderTestOut)
def test_local_folder(
    body: LocalFolderTestIn | None = None, db: Session = Depends(get_db)
):
    # body 带 paths = 草稿探测（面板当前填写列表，不读库）；缺省测库中已保存路径
    return local_folder.test_local_folder(db, paths=body.paths if body else None)


# ---- 参数路由 ----


@router.patch("/{server_id}", response_model=McpServerOut)
def update_server(server_id: str, body: McpServerUpdate, db: Session = Depends(get_db)):
    try:
        row = mcp_service.update_server(db, server_id, body)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    return row


@router.delete("/{server_id}", status_code=204)
def delete_server(server_id: str, db: Session = Depends(get_db)):
    if not mcp_service.delete_server(db, server_id):
        raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.post("/{server_id}/test", response_model=McpServerTestOut)
async def test_server(server_id: str, db: Session = Depends(get_db)):
    result = await mcp_service.test_server(db, server_id)
    if result is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    return result
