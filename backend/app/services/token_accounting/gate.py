"""统一限额闸门：把散在各条链路里的"取判定主体 -> 调 evaluate() -> 两级异常分类 ->
带 lane 标签落日志"这套逻辑收敛成一个可复用的模块。

背景（design.md 变更 4）：`business_step` 链路今天在 `model_step_service._budget_gate`
里有一份完整实现（调 `evaluate()`、两级异常分类、命中/软告警日志），但 `run_compact` /
`session_compact` / `group_compact` / `planning` / `model_probe` 五条链路完全没有限额
判定，`group_handoff` 又自己手写了一套无视执行模式的硬拦。本模块是这些链路日后统一接入
的唯一入口——新增一条链路时只需要 `load_subjects()` + `check()`，不需要重新发明一遍
异常分类与日志格式。

本任务（4.1）只新增这个模块本身，不改 `model_step_service.py`：`business_step` 收敛到
`gate.check()` 是任务 4.2 的范围。

日志格式对照 `model_step_service._budget_gate` 逐字段复刻，只新增 `lane=` 字段，使既有
的日志告警规则（grep `[TokenBudget] ... blocked=` 之类）继续匹配。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.tenant_token_counter import TenantTokenCounter
from app.services.token_accounting.budget import (
    PROGRAMMING_ERROR_TYPES,
    BudgetVerdict,
    evaluate,
    should_emit_soft_warning,
)

# 七条链路（lane）。business_step 是今天唯一已接闸门的链路；其余六条是本次修复要
# 补齐（run_compact / session_compact / group_compact / planning / model_probe）
# 或收敛（group_handoff）的对象。
LANE_BUSINESS_STEP = "business_step"
LANE_RUN_COMPACT = "run_compact"
LANE_SESSION_COMPACT = "session_compact"
LANE_GROUP_COMPACT = "group_compact"
LANE_PLANNING = "planning"
LANE_MODEL_PROBE = "model_probe"
LANE_GROUP_HANDOFF = "group_handoff"


@dataclass(frozen=True, slots=True)
class BudgetSubjects:
    """一次限额判定需要的三个主体：Agent（可为 None，见下）、Tenant、TenantTokenCounter。

    `agent=None` 对应没有 Agent 主体的 system_scope 链路（`group_compact` /
    `planning` / `model_probe`）——`budget.evaluate(agent=None, ...)` 只判 `tenant_day`
    一档（任务 3.1 已支持）。
    """

    agent: Agent | None
    tenant: Tenant | None
    tenant_counter: TenantTokenCounter | None


@dataclass(frozen=True, slots=True)
class BudgetClearance:
    """一次模型调用的限额表态凭证。

    只能由两种方式产生：
      - `gate.clearance_from(lane, verdict)`：已经跑过 `gate.check()`，`verdict` 是
        判定结果（可能 `allowed=True` 或 `False`——调用方必须先检查 `verdict.allowed`
        再决定是否继续，`BudgetClearance` 本身不做这个判断）。
      - `BudgetClearance.not_applicable(lane, reason=...)`：显式声明"这次调用不适用
        限额判定"（例如平台管理员没有归属租户），`verdict=None`，`reason` 必须非空——
        不允许无理由地跳过判定，否则这个逃生舱口会变成新的"静默不表态"缺口。
    """

    lane: str
    verdict: BudgetVerdict | None
    not_applicable_reason: str | None = None

    @classmethod
    def not_applicable(cls, lane: str, reason: str) -> "BudgetClearance":
        if not reason:
            raise ValueError("BudgetClearance.not_applicable requires a non-empty reason")
        return cls(lane=lane, verdict=None, not_applicable_reason=reason)


async def load_subjects(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: Agent | None = None,
) -> BudgetSubjects:
    """一次会话内取齐限额判定需要的租户与租户计数器，与传入的 agent 一起打包。

    查询写法与 `model_step_service._load_budget_subjects` 一致（两条独立 SELECT，
    分别取 `Tenant` 与 `TenantTokenCounter`）。不做异常处理——调用方（未来的各条链路
    接入代码）负责按自己的场景决定加载失败时如何降级，本函数只负责查询本身。
    """
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    counter_result = await db.execute(select(TenantTokenCounter).where(TenantTokenCounter.tenant_id == tenant_id))
    return BudgetSubjects(
        agent=agent,
        tenant=tenant_result.scalar_one_or_none(),
        tenant_counter=counter_result.scalar_one_or_none(),
    )


async def check(
    *,
    lane: str,
    subjects: BudgetSubjects,
    estimated_next_round_tokens: int = 0,
    run_id: str | None = None,
    now: datetime | None = None,
) -> BudgetVerdict:
    """执行一次限额判定，承载今天散在 `model_step_service._budget_gate` 里的三件事。

    1. 调 `evaluate()`；
    2. 两级异常分类：`PROGRAMMING_ERROR_TYPES` -> ERROR 日志 + fail-open 放行；
       其余异常 -> WARNING 日志 + fail-open 放行。两者都返回 `BudgetVerdict(allowed=True)`——
       拦不住比拦错代价小（3.6）；
    3. 命中限额时的 WARNING 日志、软告警的 WARNING 日志 + `should_emit_soft_warning` 去重
       （去重键完全复用 `verdict.soft_warning_scope` / `soft_warning_subject_id` /
       `reset_at`，不改）。

    日志格式对照 `_budget_gate` 现有实现逐字段复刻，只新增 `lane=` 字段（末尾），既有的
    日志告警规则（按字段名 grep）继续匹配。

    `subjects.agent` 可以是 None（system_scope 链路），此时 `agent_id` 字段落 `None`——
    这与 `evaluate(agent=None, ...)` 只判 `tenant_day` 一档的语义是一致的。
    """
    agent_id = getattr(subjects.agent, "id", None)

    try:
        verdict = await evaluate(
            agent=subjects.agent,
            tenant=subjects.tenant,
            tenant_counter=subjects.tenant_counter,
            estimated_next_round_tokens=estimated_next_round_tokens,
            now=now,
        )
    except PROGRAMMING_ERROR_TYPES as exc:
        logger.opt(exception=True).error(
            "[TokenBudget] token_budget_enforcement_disabled_bug run_id={} agent_id={} error={!r} lane={}",
            run_id,
            agent_id,
            exc,
            lane,
        )
        return BudgetVerdict(allowed=True)
    except Exception as exc:  # noqa: BLE001 - 判定失败不能拖垮模型调用（基础设施/瞬时故障）
        logger.warning(
            "[TokenBudget] token_budget_enforcement_disabled_transient run_id={} agent_id={} error={!r} lane={}",
            run_id,
            agent_id,
            exc,
            lane,
        )
        return BudgetVerdict(allowed=True)

    if verdict.blocked_scope is not None:
        logger.warning(
            "[TokenBudget] run_id={} agent_id={} scope={} used={} limit={} mode={} blocked={} lane={}",
            run_id,
            agent_id,
            verdict.blocked_scope,
            verdict.used,
            verdict.limit,
            verdict.mode,
            not verdict.allowed,
            lane,
        )

    if (
        verdict.soft_warning
        and verdict.reset_at is not None
        and verdict.soft_warning_scope is not None
        and verdict.soft_warning_subject_id is not None
        and await should_emit_soft_warning(
            verdict.soft_warning_scope,
            verdict.soft_warning_subject_id,
            verdict.reset_at,
        )
    ):
        logger.warning(
            "[TokenBudget] soft warning run_id={} scope={} subject_id={} lane={}",
            run_id,
            verdict.soft_warning_scope,
            verdict.soft_warning_subject_id,
            lane,
        )

    return verdict


def clearance_from(lane: str, verdict: BudgetVerdict) -> BudgetClearance:
    """把一次 `check()` 的结果包装成 `BudgetClearance`。"""
    return BudgetClearance(lane=lane, verdict=verdict)


__all__ = [
    "LANE_BUSINESS_STEP",
    "LANE_GROUP_COMPACT",
    "LANE_GROUP_HANDOFF",
    "LANE_MODEL_PROBE",
    "LANE_PLANNING",
    "LANE_RUN_COMPACT",
    "LANE_SESSION_COMPACT",
    "BudgetClearance",
    "BudgetSubjects",
    "check",
    "clearance_from",
    "load_subjects",
]
