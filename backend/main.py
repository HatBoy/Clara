"""FastAPI 入口：装配 app、CORS、挂载 routers、启动时建表与 seed。仅此职责。

业务逻辑在 services/，HTTP 编解码在 routers/——本文件不再实现任何端点。

日志约定：业务动作与数据量（seed 灌入/查询命中条数/将来的增删改）由各 service
经 logging 打 INFO；HTTP 请求行（方法/路径/状态码）由 uvicorn 自带 access log 输出。
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import HOST, PORT
from database import SessionLocal, init_db
from routers import about, ai_coach, chat_channels, cli_tools, ingest, integrations, jobs, mcp_servers, model_providers, projects, reminders, search, settings, stats, tasks
from services import chat_channels as chat_channel_workers
from services import local_folder
from services.ai_data import seed_memory_rules_if_empty
from services.backup import apply_pending_restore
from services.config_backup import maybe_restore_and_snapshot
from services.reminders import reminder_loop
from services.seed import refill_seed_proposals, seed_if_empty
from services.tasks import purge_expired_soft_deletes
from services.workflows import agent_loop, note_activity

# 应用日志格式（区别于 uvicorn 请求行，便于快速定位业务动作）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _purge_loop() -> None:
    """每 24h 清理一次过期软删（场景 2 的 TTL 兜底）。桌面应用重启频繁，启动时的
    清理已覆盖主场景，此循环只兜长开不重启的会话——不为此引 APScheduler。"""
    while True:
        await asyncio.sleep(24 * 3600)
        with SessionLocal() as db:
            purge_expired_soft_deletes(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时按 models 建表（幂等）→ 空库灌 seed（幂等）→ 清理
    过期软删；随后 24h 周期清理。seed/purge 逻辑在 service 层，此处只是薄调用点
    （便于测试直调）。"""
    # 备份恢复第二阶段（2026-11-06 批 A）：宿主重启后端进程后，staging 里有
    # PENDING 标记则原子替换库/配置——须先于 init_db 与一切数据库访问（此刻
    # 本进程尚未连接库文件，无锁可直覆；详见 services/backup.py 模块头）
    if apply_pending_restore():
        logger.info("备份恢复已应用（restore_staging → 数据目录替换完成）")
    init_db()
    logger.info("数据库建表完成（幂等 create_all）")
    with SessionLocal() as db:
        # 配置快照自愈（2026-10-22）：删库重灌 seed 不会再连坐清空模型/数据
        # 接入等配置——启动时检测库刚重建特征则从 data/config_snapshot.json 恢复，
        # 随后无条件覆盖写新快照（services/config_backup.py 模块头有机制说明）
        maybe_restore_and_snapshot(db)
        seed_if_empty(db)
        # dev 专属：测试建议卡是可消耗数据，消耗完（无未决 seed 卡）重启在此
        # 自动补一批——打包形态函数内 no-op（services/seed.py 有机制说明）
        refill_seed_proposals(db)
        seed_memory_rules_if_empty(db)
        purge_expired_soft_deletes(db)
    purge_task = asyncio.create_task(_purge_loop())
    reminder_task = asyncio.create_task(reminder_loop())
    agent_task = asyncio.create_task(agent_loop())
    # 聊天通讯 worker（2026-10-23 批 5）：捕获主循环引用 + 按 enabled 行启动
    # SDK 长连接线程——须在快照恢复之后（恢复带回的凭据才是 worker 读到的最终态）。
    # 后台化（2026-11-04 真机批）：create_task 仍在快照恢复之后创建（顺序保证
    # 不破），只是不再 await 阻塞 startup complete——旧版飞书 ws 握手秒级把前端
    # 首个可受理请求推迟同样时长（uvicorn 在 lifespan 完成前不服务请求，页面
    # 出现慢几秒的真正元凶）；worker 晚 1-3 秒连上无感（远端消息晚几秒到达）
    chat_task = asyncio.create_task(chat_channel_workers.start_all())
    # FastAPI mount 不自动触发子 app 的 lifespan——本地文件夹 MCP 服务的会话管理器
    # 须在主 lifespan 显式运行。恒运行（run() 不可重入，挂载不随开关启停），启用
    # 判定在工具调用层（未启用时工具返回中文错误文本，见 services/local_folder.py）
    async with local_folder.mcp.session_manager.run():
        yield
    chat_channel_workers.shutdown()  # 关闭标志：不再启动新 worker，在途投递丢弃
    chat_task.cancel()  # 仍在建连中的 start_all 一并终止（已完成的 no-op）
    purge_task.cancel()
    reminder_task.cancel()
    agent_task.cancel()


app = FastAPI(title="Clara Local Engine", lifespan=lifespan)

# 允许本地 React 前端跨域访问（Tauri dev 模式在 1420 端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境可限制为 tauri://localhost
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _track_activity(request, call_next):
    """夜间空闲判定的活动信号：任意请求完成即刷新（用户在用的代理信号，判定在
    services/workflows._is_idle）。"""
    response = await call_next(request)
    note_activity()
    return response

# 内置本地文件夹 MCP 服务（/mcp/local/mcp）：mount 须先于任何请求进来，且
# mcp_http_app() 只调一次 streamable_http_app()（session_manager 惰性建于该调用，
# 此后 lifespan 里 session_manager.run() 访问到的是同一实例）
app.mount(local_folder.MOUNT_PATH, local_folder.mcp_http_app())

app.include_router(tasks.router)
app.include_router(projects.router)
app.include_router(settings.router)
app.include_router(reminders.router)
app.include_router(model_providers.router)
app.include_router(integrations.router)
app.include_router(mcp_servers.router)
app.include_router(cli_tools.router)
app.include_router(ai_coach.router)
app.include_router(ingest.router)
app.include_router(jobs.router)
app.include_router(chat_channels.router)
app.include_router(search.router)
app.include_router(about.router)
app.include_router(stats.router)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "Clara Python Engine is running!"}


if __name__ == "__main__":
    # 作为 Sidecar 运行时，通过 uvicorn 启动在固定的本地端口
    uvicorn.run(app, host=HOST, port=PORT)
