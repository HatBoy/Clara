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
            max_tokens=1,
            timeout=_TEST_TIMEOUT_S,
            api_key=api_key or "EMPTY",
            base_url=base_url or None,  # None = LiteLLM 按 provider/ 前缀官方路由
        )
    except litellm.AuthenticationError:
        return ModelProviderTestOut(ok=False, error="鉴权失败（401）：API Key 无效或无权限")
    except litellm.NotFoundError:
        return ModelProviderTestOut(ok=False, error="模型或接口不存在（404）：检查模型标识与接口地址")
    except litellm.RateLimitError:
        return ModelProviderTestOut(ok=False, error="触发限流（429）：稍后重试")
    except litellm.BadRequestError:
        return ModelProviderTestOut(ok=False, error="请求被拒绝（400）：检查模型标识格式（需 provider/模型名，如 deepseek/deepseek-chat）")
    except litellm.Timeout:
        return ModelProviderTestOut(ok=False, error="响应超时：服务未在 10 秒内返回")
    except litellm.APIConnectionError as e:
        # 打包裁剪（2026-11-11）：被裁供应商 handler 加载的 ImportError 会被
        # litellm 包装成本类（消息含 "No module named"）——先于连接文案特判，
        # 指路 openai/ + Base URL 逃生通道。仅作子串检查不回显消息（固定文案
        # 纪律不变）
        if "No module named" in str(e):
            return ModelProviderTestOut(ok=False, error="该供应商未随 Clara 打包：请换用已内置前缀（deepseek/、openai/、ollama/ 等），或用 openai/ 前缀 + Base URL 接入任意 OpenAI 兼容端点")
        return ModelProviderTestOut(ok=False, error="无法连接：检查网络与接口地址")
    except litellm.InternalServerError:
        # litellm 把 5xx 响应（含经代理不可达——代理会返回 500）归为此类，
        # 它继承 openai.APIError 而非 litellm.APIError，须显式排列
        return ModelProviderTestOut(ok=False, error="模型服务错误（500）：服务端异常或代理无法到达目标地址")
    except litellm.APIError as e:
        return ModelProviderTestOut(ok=False, error=f"模型服务返回 HTTP {getattr(e, 'status_code', '')}".rstrip())
    except ModuleNotFoundError:
        # 打包裁剪纵深防御（2026-11-11）：实测被裁供应商的 import 失败多被
        # litellm 包装成 APIConnectionError（上方特判），此分支兜住少数可能
        # 裸抛的路径——固定文案不读 str(e)
        return ModelProviderTestOut(ok=False, error="该供应商未随 Clara 打包：请换用已内置前缀（deepseek/、openai/、ollama/ 等），或用 openai/ 前缀 + Base URL 接入任意 OpenAI 兼容端点")
    except Exception as e:  # 兜底：探测永不向上抛（固定文案不读 str(e)，防泄露 key 片段）
        return ModelProviderTestOut(ok=False, error=f"探测异常：{type(e).__name__}")
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
