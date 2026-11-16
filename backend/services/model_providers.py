"""模型接入业务逻辑层：CRUD、激活互斥与连通性探测。

连通性测试（probe_model）由后端统一执行——前端不直接请求模型服务（密钥
不暴露给浏览器网络面板、口径单一）。探测经 LiteLLM 统一协议调用 100+ 供应
商（同步阻塞），挂在 FastAPI sync 端点上由线程池承载（最长 _TEST_TIMEOUT_S
秒，不堵事件循环）。
"""

import logging
import os
import time
from datetime import datetime

# 跳过 litellm 首次 import 的联网拉价格表（连不上 GitHub 会阻塞数十秒），改读
# 包内备份；必须发生在 import litellm 之前（阶段二 agents 经 services 链先触发此处）
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models import ModelProvider
from schemas import (
    ModelProviderCreate,
    ModelProviderTestOut,
    ModelProviderUpdate,
)

logger = logging.getLogger(__name__)


class ConflictError(Exception):
    """业务冲突（显示名称重名），router 翻译为 409。"""


# Optional 放行显式 null 后由 service 拦（NOT NULL 列）：键 → 中文标签
# （base_url 已可选——显式 null = 清回官方路由，合法操作不拦）
_NOT_NULL_FIELDS = {"name": "显示名称", "model_name": "模型名称"}

# 连通性探测超时（秒）：兼顾跨境网关慢连接与前端等待体验
_TEST_TIMEOUT_S = 10.0


def list_models(db: Session) -> list[ModelProvider]:
    rows = list(db.scalars(select(ModelProvider).order_by(ModelProvider.created_at)))
    logger.info("读取模型列表：%d 条", len(rows))
    return rows


def get_model(db: Session, model_id: str) -> ModelProvider | None:
    row = db.get(ModelProvider, model_id)
    if row is None:
        logger.info("模型不存在: %s", model_id)
    return row


def create_model(db: Session, body: ModelProviderCreate) -> ModelProvider:
    dup = db.scalar(select(ModelProvider).where(ModelProvider.name == body.name))
    if dup is not None:
        raise ConflictError(f"显示名称已存在: {body.name}")
    row = ModelProvider(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)  # refresh 才能拿到 server_default 的 created_at
    logger.info("新建模型 %s (%s)", row.name, row.id)
    return row


def update_model(db: Session, model_id: str, patch: ModelProviderUpdate) -> ModelProvider | None:
    row = get_model(db, model_id)
    if row is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return row  # 空 patch 短路：不发 UPDATE、不误抬 updated_at
    for key, label in _NOT_NULL_FIELDS.items():
        if key in data and data[key] is None:
            raise ValueError(f"{label}不允许置空")
    if "name" in data and data["name"] != row.name:
        dup = db.scalar(
            select(ModelProvider).where(
                ModelProvider.name == data["name"], ModelProvider.id != model_id
            )
        )
        if dup is not None:
            raise ConflictError(f"显示名称已存在: {data['name']}")
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    logger.info("更新模型 %s：字段 %s", row.name, sorted(data))
    return row


def delete_model(db: Session, model_id: str) -> bool:
    row = get_model(db, model_id)
    if row is None:
        return False
    db.delete(row)  # 硬删：非业务数据 + 含 api_key 不留墓碑（口径见 ORM docstring）
    db.commit()
    logger.info("删除模型 %s (%s)", row.name, row.id)
    return True


def activate_model(db: Session, model_id: str) -> ModelProvider | None:
    """激活指定模型：同事务把其余激活行置 False——"全局至多一行激活"的唯一写入口。"""
    row = get_model(db, model_id)
    if row is None:
        return None
    if row.is_active:
        return row  # 已激活：幂等直接返回，不重复 UPDATE 不抬 updated_at
    db.execute(
        update(ModelProvider)
        .where(ModelProvider.is_active.is_(True), ModelProvider.id != model_id)
        .values(is_active=False)
    )
    row.is_active = True
    db.commit()
    db.refresh(row)
    logger.info("激活模型 %s (%s)", row.name, row.id)
    return row


def get_active_model(db: Session) -> ModelProvider | None:
    """当前激活模型（阶段二 agent 的读入口；库可处于零激活态）。"""
    return db.scalar(select(ModelProvider).where(ModelProvider.is_active.is_(True)))


def get_aux_model(db: Session) -> ModelProvider | None:
    """轻量助手模型（2026-11-01 优化批 9，解禁 7）：小调用白名单的目标行；
    全局至多一行 is_aux（set_aux_model 互斥维护），未配置返回 None（调用方
    init_llm 回落激活模型——行为不变）。"""
    return db.scalar(select(ModelProvider).where(ModelProvider.is_aux.is_(True)))


def set_aux_model(db: Session, model_id: str) -> ModelProvider:
    """设/取消轻量助手模型（toggle 语义，同事务互斥）：目标行已是 aux → 取消
    （回落未配置态）；否则清掉旧 aux 行并打上目标行。与激活互斥正交（同一行
    可既激活又兼任助手）。"""
    row = db.get(ModelProvider, model_id)
    if row is None:
        raise ValueError("模型不存在")
    if row.is_aux:
        row.is_aux = False
    else:
        for other in db.scalars(select(ModelProvider).where(
                ModelProvider.is_aux.is_(True), ModelProvider.id != model_id)):
            other.is_aux = False
        row.is_aux = True
    db.commit()
    db.refresh(row)
    return row


def describe_llm_error(exc: Exception) -> str:
    """LLM 异常 → 用户可读中文（2026-11-16 优化批 2 从 probe_model 抽出）：
    探测/对话/统计问答共用一套分类，聊天错误帧不再只显示「详见后端日志」。
    捕获链注意：litellm 各异常是兄弟关系非父子嵌套，逐个显式判定。
    固定文案不读 str(e)（防泄露 key 片段），只有裸 APIError 回显状态码。"""
    if isinstance(exc, litellm.AuthenticationError):
        return "鉴权失败（401）：API Key 无效或无权限，请到设置 → 模型接入检查"
    if isinstance(exc, litellm.NotFoundError):
        return "模型或接口不存在（404）：检查模型标识与接口地址"
    if isinstance(exc, litellm.RateLimitError):
        return "触发限流（429）：稍后重试或换用其他模型"
    if isinstance(exc, litellm.BadRequestError):
        return "请求被拒绝（400）：检查模型标识格式（需 provider/模型名，如 deepseek/deepseek-chat）"
    if isinstance(exc, litellm.Timeout):
        return "响应超时：服务未及时返回"
    if isinstance(exc, litellm.APIConnectionError):
        if "No module named" in str(exc):
            return ("该供应商未随 Clara 打包：请换用已内置前缀（deepseek/、openai/、ollama/ 等），"
                    "或用 openai/ 前缀 + Base URL 接入任意 OpenAI 兼容端点")
        return "无法连接：检查网络与接口地址"
    if isinstance(exc, litellm.InternalServerError):
        return "模型服务错误（500）：服务端异常或代理无法到达目标地址"
    if isinstance(exc, litellm.APIError):
        return f"模型服务返回 HTTP {getattr(exc, 'status_code', '')}".rstrip()
    if type(exc).__name__ == "ContextOverflowError":
        # 不 import llm（避免环）：按类名判定，文案给可操作建议
        return "上下文超出模型容量：请压缩会话或换用更大上下文档位的模型"
    if isinstance(exc, ModuleNotFoundError):
        return ("该供应商未随 Clara 打包：请换用已内置前缀（deepseek/、openai/、ollama/ 等），"
                "或用 openai/ 前缀 + Base URL 接入任意 OpenAI 兼容端点")
    return f"模型调用异常：{type(exc).__name__}"


def probe_model(model_name: str, api_key: str | None, base_url: str | None) -> ModelProviderTestOut:
    """连通性探测（纯函数无 db，永不抛出——探测失败是结果不是异常）。

    经 LiteLLM 统一入口发一条最小 chat 请求：不抛异常即视为连通，往返耗时作
    测速值；失败按 litellm 统一异常分类为中文摘要。模型标识须为 LiteLLM 格式
    "provider/模型名"。api_key 空时传占位符——OpenAI SDK 构造期要求非空 key，
    免鉴权本地端点（Ollama/vLLM）靠占位符通过构造，鉴权由服务端忽略。
    捕获链注意：litellm 各异常是兄弟关系非父子嵌套（Timeout 不是
    APIConnectionError 子类、AuthenticationError 不是 APIError 子类），
    必须逐个显式排列，不能靠父子链短路。max_tokens=1 控制耗时与 token 消耗。
    """
    start = time.perf_counter()
    try:
        litellm.completion(
            model=model_name,
            messages=[{"role": "user", "content": "ping"}],
            # 探测预算 16 而非 1（2026-11-16 优化批 2）：推理类模型在极小输出
            # 预算下可能直接 400/空返回，被误判为不可用；16 仍几乎零成本。
            max_tokens=16,
            timeout=_TEST_TIMEOUT_S,
            api_key=api_key or "EMPTY",
            base_url=base_url or None,  # None = LiteLLM 按 provider/ 前缀官方路由
        )
    except Exception as e:  # 探测永不向上抛（固定文案不读 str(e)，防泄露 key 片段）
        return ModelProviderTestOut(ok=False, error=describe_llm_error(e))
    return ModelProviderTestOut(ok=True, latency_ms=round((time.perf_counter() - start) * 1000))


def test_model(db: Session, model_id: str) -> ModelProviderTestOut | None:
    """对已保存模型执行连通性测试并记录结果（last_test_* 三列落库）。"""
    row = get_model(db, model_id)
    if row is None:
        return None
    result = probe_model(row.model_name, row.api_key, row.base_url)
    row.last_test_ok = result.ok
    row.last_test_ms = result.latency_ms
    row.last_test_at = datetime.now()
    db.commit()
    logger.info("模型测速 %s (%s)：ok=%s latency=%sms", row.name, row.id, result.ok, result.latency_ms)
    return result
