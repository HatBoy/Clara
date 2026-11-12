"""CLI 工具接入接口（前缀 /api/cli，2026-10-15 增，第四源 kind="cli"）。

薄壳层——调 service、按约定翻译状态码（ValueError→404/422 分语境、不存在→404）；
不含任何业务逻辑。全同步 def：run_cli 是同步阻塞函数，FastAPI 同步路由自动跑
线程池（同 mcp_servers.test_local_folder 先例），不占事件循环。

端点一览：
- GET    /api/cli/tools                       注册表精选 + 接入行 + 自定义行合并（零子进程）
- POST   /api/cli/tools/custom                窄口自定义只读命令（201；deny-list 命中 422）
- POST   /api/cli/tools/custom/test           表单态直测（不建行不落库；校验同保存闸）
- POST   /api/cli/tools/{id}/probe            安装+授权探测（失败也是 200；已接入行落库）
- POST   /api/cli/tools/{id}/test             自定义行命令测试（近 24h 窗真跑；失败也是 200；精选行 422）
- POST   /api/cli/tools/{id}/connect          显式接入（201；未装 422；未知适配器 404）
- PUT    /api/cli/tools/{id}                  启停 + 自定义行编辑（联动镜像；精选行编辑 422）
- DELETE /api/cli/tools/{id}                  移除（204；CLI 侧凭据保留）
- POST   /api/cli/tools/{id}/auth/start       发起授权（auto：URL / manual：终端命令）
- GET    /api/cli/tools/{id}/auth/status      授权状态轮询（结果落库）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    CliAuthStartOut,
    CliAuthStatusOut,
    CliCustomTestOut,
    CliProbeOut,
    CliToolOut,
    CliToolUpdate,
    CustomCliBody,
)
from services import cli_tools

router = APIRouter(prefix="/api/cli", tags=["cli"])

# 字面量路由：/tools/custom 必须先于任何 /tools/{adapter_id} 形态注册
# （Starlette 按注册序匹配；当前无单段 POST 冲突，防御性前置同 mcp_servers 惯例）


@router.get("/tools", response_model=list[CliToolOut])
def list_cli_tools(db: Session = Depends(get_db)):
    return cli_tools.list_tools(db)


@router.post("/tools/custom", response_model=CliToolOut, status_code=201)
def create_custom_cli(body: CustomCliBody, db: Session = Depends(get_db)):
    try:
        row = cli_tools.create_custom(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _row_to_out(db, row.adapter_id)


# 字面量路由：须先于 /tools/{adapter_id}/test 注册（Starlette 按注册序匹配）
@router.post("/tools/custom/test", response_model=CliCustomTestOut)
def test_custom_cli_draft(body: CustomCliBody):
    try:
        return cli_tools.test_custom_body(body)
    except ValueError as e:  # 校验闸与保存同源
        raise HTTPException(status_code=422, detail=str(e)) from e


def _row_to_out(db: Session, adapter_id: str) -> dict:
    """行 → CliToolOut 形状（复用 list_tools 的合并逻辑，单条取回）。"""
    return next(t for t in cli_tools.list_tools(db) if t["adapter_id"] == adapter_id)


@router.post("/tools/{adapter_id}/probe", response_model=CliProbeOut)
def probe_cli_tool(adapter_id: str, db: Session = Depends(get_db)):
    try:
        return cli_tools.probe(db, adapter_id)
    except ValueError as e:  # 未知适配器
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/tools/{adapter_id}/connect", response_model=CliToolOut, status_code=201)
def connect_cli_tool(adapter_id: str, db: Session = Depends(get_db)):
    if adapter_id not in cli_tools.ADAPTERS:  # 状态码翻译所需的最小判据
        raise HTTPException(status_code=404, detail=f"未知适配器：{adapter_id}")
    try:
        cli_tools.connect(db, adapter_id)  # ValueError 只剩「未安装」一种业务态
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _row_to_out(db, adapter_id)


@router.post("/tools/{adapter_id}/test", response_model=CliCustomTestOut)
def test_custom_cli(adapter_id: str, db: Session = Depends(get_db)):
    try:
        result = cli_tools.test_custom(db, adapter_id)
    except ValueError as e:  # 精选行——其「测试」走 probe 端点
        raise HTTPException(status_code=422, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail="该 CLI 工具未接入")
    return result


@router.put("/tools/{adapter_id}", response_model=CliToolOut)
def update_cli_tool(adapter_id: str, body: CliToolUpdate, db: Session = Depends(get_db)):
    try:
        row = cli_tools.update_tool(db, adapter_id, body)
    except ValueError as e:  # 精选行编辑 / 自定义行校验失败
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="该 CLI 工具未接入")
    return _row_to_out(db, adapter_id)


@router.delete("/tools/{adapter_id}", status_code=204)
def delete_cli_tool(adapter_id: str, db: Session = Depends(get_db)):
    if cli_tools.delete(db, adapter_id) is None:
        raise HTTPException(status_code=404, detail="该 CLI 工具未接入")


@router.post("/tools/{adapter_id}/auth/start", response_model=CliAuthStartOut)
def start_cli_auth(adapter_id: str, db: Session = Depends(get_db)):
    try:
        return cli_tools.start_auth(db, adapter_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/tools/{adapter_id}/auth/status", response_model=CliAuthStatusOut)
def cli_auth_status(adapter_id: str, db: Session = Depends(get_db)):
    try:
        return cli_tools.auth_status(db, adapter_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
