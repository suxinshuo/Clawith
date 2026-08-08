"""兼容转发层 —— 真实实现在 app.services.token_accounting。

保留这一层的理由有两个：让 caller.py 与 single_step.py 的既有 import 零改动继续可用
（caller.py 本身是死代码，但仍有一批测试在跑它），以及保证平台上只有一套记账实现 ——
TokenUsage 在此只做再导出，绝不重新定义。新代码请直接用 token_accounting。

注意：node_executor.py 与本模块无关，它一个符号都不从这里导入。它导入的
WRITE_FILE_PROTOCOL_* 常量来自 app.services.llm.caller（见 node_executor.py:24-26），
那是不能删 caller.py 的原因，不是不能删本模块的原因。
"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.services.token_accounting.ledger import record as ledger_record
from app.services.token_accounting.normalize import (
    PROTOCOL_OPENAI_COMPATIBLE,
    TokenUsage,
    estimate_token_usage_from_chars,
    estimate_tokens_from_chars,
    normalize,
)


def extract_token_usage(usage: dict | None) -> TokenUsage | None:
    """已废弃：按 openai_compatible 协议解释 usage。

    新代码请用 normalize(protocol, usage) 并显式传入协议 —— 键嗅探会把 Anthropic
    的 usage 误判成 OpenAI 语义，那正是要修的 bug。
    """
    return normalize(PROTOCOL_OPENAI_COMPATIBLE, usage)


def extract_usage_tokens(usage: dict | None) -> int | None:
    parsed = extract_token_usage(usage)
    return parsed.total_tokens if parsed else None


async def _resolve_tenant_id(agent_id: uuid.UUID) -> uuid.UUID | None:
    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        return result.scalar_one_or_none()


async def record_token_usage(
    agent_id: uuid.UUID,
    tokens: int | TokenUsage,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    estimated_tokens: int = 0,
) -> None:
    """记一次 Agent 的 token 消耗（兼容签名）。

    `tokens` 传裸 int 的那条分支不校验统一口径不变式
    （total_tokens == input_tokens + output_tokens）：只给总量、不给细分时会写入
    total=N, input=0, output=0。它纯粹为兼容旧签名而存在，现有调用方（caller.py 六处、
    single_step.py 一处）全部传 TokenUsage 实例，无人走裸 int。新代码不要用这条分支。
    """
    if isinstance(tokens, TokenUsage):
        usage = tokens
    else:
        # 只给了总量、没有细分：按全部估算处理，避免伪装成 provider 权威数据。
        usage = TokenUsage(
            total_tokens=tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_tokens=estimated_tokens or tokens,
        )
    if usage.total_tokens <= 0:
        return

    tenant_id = await _resolve_tenant_id(agent_id)
    if tenant_id is None:
        logger.warning(
            "token_usage_dropped_unknown_tenant agent_id={} total={}",
            agent_id,
            usage.total_tokens,
        )
        return

    # 刻意丢弃 ledger_record 的 bool 返回值：兼容签名是 -> None，无法向上传递成败。
    # 这不会让失败变得不可见 —— record() 内部在最终失败时已按 ERROR 记下完整 payload。
    await ledger_record(usage, tenant_id=tenant_id, agent_id=agent_id)


__all__ = [
    "TokenUsage",
    "estimate_token_usage_from_chars",
    "estimate_tokens_from_chars",
    "extract_token_usage",
    "extract_usage_tokens",
    "record_token_usage",
]
