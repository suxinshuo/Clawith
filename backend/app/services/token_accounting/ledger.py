"""Token 记账持久化：单事务、固定顺序、原子累加。

旧实现的三个问题在这里一并解决：
1. Agent 行是 Python 侧读改写，并发下丢更新，且与 DailyTokenUsage 的原子 upsert
   长期漂移 —— 改成同一事务内的 SQL 原子累加。
2. 日/月重置只在两个 API 端点里做，纯 cron 驱动的 Agent 过了午夜仍被旧计数卡死
   —— 改成记账路径上的条件 UPDATE，幂等且无竞态。
3. 失败被 try/except 吞掉只记 warning —— 改成有界重试后按 ERROR 记录完整载荷。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import load_only

from app.database import async_session
from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.tenant_token_counter import TenantTokenCounter
from app.services.token_accounting.normalize import TokenUsage
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)

SYSTEM_SCOPE_GROUP_COMPACT = "group_compact"
SYSTEM_SCOPE_PLANNING = "planning"
SYSTEM_SCOPE_MODEL_PROBE = "model_probe"
SYSTEM_SCOPES = (
    SYSTEM_SCOPE_GROUP_COMPACT,
    SYSTEM_SCOPE_PLANNING,
    SYSTEM_SCOPE_MODEL_PROBE,
)

LEDGER_MAX_RETRIES = 2

# Postgres SQLSTATE：40001 = serialization_failure，40P01 = deadlock_detected。
# 优先按这两个码判断，因为错误消息文本随 lc_messages / 驱动版本变化，同一类瞬时
# 失败换个语言环境就可能匹配不到子串，把本该重试的错误误判成硬失败。
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

# SQLSTATE 取不到时（例如测试里的裸 RuntimeError，或某些驱动没有暴露 .orig）的
# 兜底：仍按消息子串匹配，保底不比改动前更弱。
_RETRYABLE_MARKERS = (
    "could not serialize",
    "deadlock detected",
    "concurrent update",
)


def _is_retryable(error: Exception) -> bool:
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    if sqlstate is not None:
        return sqlstate in _RETRYABLE_SQLSTATES
    text = str(error).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


async def _reset_agent_counters_if_stale(db, agent, tz_name: str, now: datetime) -> None:
    # 用 getattr 兜底：真实 Agent 行这两列总是存在（可空），但轻量测试替身可能
    # 不设置它们；缺失视同"从未重置过"。
    last_daily_reset = getattr(agent, "last_daily_reset", None)
    last_monthly_reset = getattr(agent, "last_monthly_reset", None)
    if is_new_local_day(last_daily_reset, tz_name, now=now):
        day_start = local_day_start(tz_name, now=now)
        await db.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                (Agent.last_daily_reset.is_(None)) | (Agent.last_daily_reset < day_start),
            )
            .values(
                tokens_used_today=0,
                input_tokens_today=0,
                cache_read_tokens_today=0,
                cache_creation_tokens_today=0,
                last_daily_reset=now,
            )
        )
    if is_new_local_month(last_monthly_reset, tz_name, now=now):
        month_start = local_month_start(tz_name, now=now)
        await db.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                (Agent.last_monthly_reset.is_(None)) | (Agent.last_monthly_reset < month_start),
            )
            .values(
                tokens_used_month=0,
                input_tokens_month=0,
                cache_read_tokens_month=0,
                cache_creation_tokens_month=0,
                last_monthly_reset=now,
            )
        )


def _daily_upsert(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    agent_name: str | None,
    system_scope: str | None,
    date_anchor: datetime,
):
    values = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "agent_name_snapshot": agent_name,
        "system_scope": system_scope,
        "date": date_anchor,
        "tokens_used": usage.total_tokens,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "estimated_tokens": usage.estimated_tokens,
    }
    increments = {
        "tokens_used": DailyTokenUsage.tokens_used + usage.total_tokens,
        "input_tokens": DailyTokenUsage.input_tokens + usage.input_tokens,
        "output_tokens": DailyTokenUsage.output_tokens + usage.output_tokens,
        "cache_read_tokens": DailyTokenUsage.cache_read_tokens + usage.cache_read_tokens,
        "cache_creation_tokens": (DailyTokenUsage.cache_creation_tokens + usage.cache_creation_tokens),
        "reasoning_tokens": DailyTokenUsage.reasoning_tokens + usage.reasoning_tokens,
        "estimated_tokens": DailyTokenUsage.estimated_tokens + usage.estimated_tokens,
    }
    statement = insert(DailyTokenUsage).values(**values)
    # 两个部分唯一索引必须靠 index_where 精确指向，否则 ON CONFLICT 推断不出索引。
    if system_scope is None:
        return statement.on_conflict_do_update(
            index_elements=["agent_id", "date"],
            index_where=DailyTokenUsage.system_scope.is_(None),
            set_=increments,
        )
    return statement.on_conflict_do_update(
        index_elements=["tenant_id", "system_scope", "date"],
        index_where=DailyTokenUsage.system_scope.isnot(None),
        set_=increments,
    )


async def _write_once(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    system_scope: str | None,
    now: datetime,
) -> bool:
    """执行一次记账事务。返回 ``agent_missing``：True 表示 `agent_id` 非空但对应
    Agent 行已不存在（已被删除），这种情况下租户级计数照常写入，但明细行被跳过。
    """
    async with async_session() as db:
        try:
            # 固定顺序 tenant -> agent -> daily，避免并发事务互相死锁。
            # 只加载 timezone：effective_timezone()/tenant_timezone() 全程只读
            # Tenant.timezone 一列（见 get_agent_timezone_sync），load_only 让编译出
            # 的 SELECT 只取这一列。注意这是有代价的：其余列被声明为待加载（deferred），
            # 这个 tenant 实例之后不能交给会读取其他 Tenant 属性的代码——那样会触发
            # 懒加载，在异步 session 里同步执行而抛 MissingGreenlet。
            tenant_row = await db.execute(
                select(Tenant).options(load_only(Tenant.timezone)).where(Tenant.id == tenant_id)
            )
            tenant = tenant_row.scalar_one_or_none()
            tz_tenant = tenant_timezone(tenant)
            day_start_tenant = local_day_start(tz_tenant, now=now)

            # 租户计数器行可能在迁移之后才创建的租户上缺失；用 upsert 而非 UPDATE，
            # 否则新租户的 UPDATE 会静默匹配 0 行、天花板永远不累加。
            await db.execute(
                insert(TenantTokenCounter)
                .values(
                    tenant_id=tenant_id,
                    tokens_used_today=0,
                    tokens_used_total=0,
                    last_daily_reset=now,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )
            # 惰性重置：条件 UPDATE，在累加之前执行，构造上幂等。
            await db.execute(
                update(TenantTokenCounter)
                .where(
                    TenantTokenCounter.tenant_id == tenant_id,
                    (TenantTokenCounter.last_daily_reset.is_(None))
                    | (TenantTokenCounter.last_daily_reset < day_start_tenant),
                )
                .values(tokens_used_today=0, last_daily_reset=now)
            )
            await db.execute(
                update(TenantTokenCounter)
                .where(TenantTokenCounter.tenant_id == tenant_id)
                .values(
                    tokens_used_today=(TenantTokenCounter.tokens_used_today + usage.total_tokens),
                    tokens_used_total=(TenantTokenCounter.tokens_used_total + usage.total_tokens),
                )
            )

            agent_name: str | None = None
            date_anchor = day_start_tenant
            # agent_id 非空但 Agent 行已不存在：LLM 调用与记账之间的正常竞态（Agent
            # 被删除），daily_token_usage.agent_id 的 ondelete="SET NULL" 正是为
            # 让历史留存而设的。这些 token 仍真实消耗、仍属于该租户，所以租户计数
            # 照常累加；但明细行不能带着一个不存在的 agent_id 去 upsert —— FK 是
            # NO ACTION，插入会被拒绝、整个事务（连同刚写的租户计数）一起回滚；也不
            # 能改道系统开销分桶，那会把 Agent 自己的开销误记成共享开销。所以这种情
            # 况下明细行整条跳过，只留租户级计数，由调用方在日志里追溯。
            agent_missing = False
            if agent_id is not None:
                agent_row = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = agent_row.scalar_one_or_none()
                if agent is None:
                    agent_missing = True
                else:
                    agent_name = agent.name
                    tz_agent = effective_timezone(agent, tenant)
                    date_anchor = local_day_start(tz_agent, now=now)
                    await _reset_agent_counters_if_stale(db, agent, tz_agent, now)
                    await db.execute(
                        update(Agent)
                        .where(Agent.id == agent_id)
                        .values(
                            tokens_used_today=Agent.tokens_used_today + usage.total_tokens,
                            tokens_used_month=Agent.tokens_used_month + usage.total_tokens,
                            tokens_used_total=Agent.tokens_used_total + usage.total_tokens,
                            input_tokens_today=Agent.input_tokens_today + usage.input_tokens,
                            input_tokens_month=Agent.input_tokens_month + usage.input_tokens,
                            input_tokens_total=Agent.input_tokens_total + usage.input_tokens,
                            cache_read_tokens_today=Agent.cache_read_tokens_today + usage.cache_read_tokens,
                            cache_read_tokens_month=Agent.cache_read_tokens_month + usage.cache_read_tokens,
                            cache_read_tokens_total=Agent.cache_read_tokens_total + usage.cache_read_tokens,
                            cache_creation_tokens_today=(
                                Agent.cache_creation_tokens_today + usage.cache_creation_tokens
                            ),
                            cache_creation_tokens_month=(
                                Agent.cache_creation_tokens_month + usage.cache_creation_tokens
                            ),
                            cache_creation_tokens_total=(
                                Agent.cache_creation_tokens_total + usage.cache_creation_tokens
                            ),
                        )
                    )

            if not agent_missing:
                await db.execute(
                    _daily_upsert(
                        usage,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        agent_name=agent_name,
                        system_scope=system_scope,
                        date_anchor=date_anchor,
                    )
                )
            await db.commit()
            return agent_missing
        except Exception:
            await db.rollback()
            raise


async def record(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    system_scope: str | None = None,
    now: datetime | None = None,
) -> bool:
    """记一次 token 消耗。返回是否落库成功；不抛异常。"""
    if (agent_id is None) == (system_scope is None):
        raise ValueError(
            "record() requires exactly one of agent_id or system_scope, "
            f"got agent_id={agent_id!r} system_scope={system_scope!r}"
        )
    if system_scope is not None and system_scope not in SYSTEM_SCOPES:
        raise ValueError(f"unknown system_scope: {system_scope!r}")
    if usage.total_tokens <= 0:
        return True

    effective_now = now or datetime.now(UTC)
    last_error: Exception | None = None
    for attempt in range(LEDGER_MAX_RETRIES + 1):
        try:
            agent_missing = await _write_once(
                usage,
                tenant_id=tenant_id,
                agent_id=agent_id,
                system_scope=system_scope,
                now=effective_now,
            )
            if agent_missing:
                # 租户级计数已经落库成功；明细行被有意跳过（见 _write_once 里的
                # 注释），WARNING 而非 ERROR——这是预期内的删除竞态，不是记账故障。
                logger.warning(
                    "token_ledger_agent_missing_daily_row_skipped tenant_id={} agent_id={} "
                    "total={} input={} output={} cache_read={} cache_creation={} "
                    "reasoning={} estimated={}",
                    tenant_id,
                    agent_id,
                    usage.total_tokens,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_creation_tokens,
                    usage.reasoning_tokens,
                    usage.estimated_tokens,
                )
            return True
        except Exception as error:  # noqa: BLE001 - 记账失败必须捕获任何数据库异常以判断
            # 是否可重试，并在耗尽重试后仍能落 ERROR 日志；直接冒泡会绕过这条日志。
            last_error = error
            if attempt < LEDGER_MAX_RETRIES and _is_retryable(error):
                continue
            break

    # 载荷完整写进日志，使这条记录可从日志恢复、也能被告警抓到。
    logger.error(
        "token_ledger_write_failed tenant_id={} agent_id={} system_scope={} "
        "total={} input={} output={} cache_read={} cache_creation={} "
        "reasoning={} estimated={} error={!r}",
        tenant_id,
        agent_id,
        system_scope,
        usage.total_tokens,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        usage.reasoning_tokens,
        usage.estimated_tokens,
        last_error,
    )
    return False


__all__ = [
    "LEDGER_MAX_RETRIES",
    "SYSTEM_SCOPES",
    "SYSTEM_SCOPE_GROUP_COMPACT",
    "SYSTEM_SCOPE_MODEL_PROBE",
    "SYSTEM_SCOPE_PLANNING",
    "record",
]
