"""Token 限额判定。

判定顺序由最具体到最宽：agent_day -> agent_month -> tenant_day，第一个命中者写进
verdict，使错误能说清究竟哪一档天花板起了作用。

能力边界：预检基于估算，且 provider 真实用量要等响应返回才知道，所以"一个 token
都不超"做不到。设计目标是超限幅度有界 —— 超出部分不超过一轮的消耗量。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger

from app.core.events import get_redis
from app.dao.system_setting_dao import system_setting_dao
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)

SCOPE_AGENT_DAY = "agent_day"
SCOPE_AGENT_MONTH = "agent_month"
SCOPE_TENANT_DAY = "tenant_day"

MODE_WARN_ONLY = "warn_only"
MODE_ENFORCE = "enforce"
KNOWN_MODES = frozenset({MODE_WARN_ONLY, MODE_ENFORCE})

SETTING_ENFORCEMENT_MODE = "token_budget_enforcement_mode"
SETTING_CALIBRATION_SWITCHED_AT = "token_accounting_calibration_switched_at"

SOFT_WARNING_RATIO = 0.8

# 这个元组划出"编程错误"（签名漂移、属性改名、变量名打错）与其它一切异常
# （基础设施/瞬时故障、脏数据）之间的分界：前者是 bug，静默吞掉会让限额永久失效却
# 没人注意到——这正是本计划要修的历史教训（旧的限额实现挂在 caller.py 一条无生产
# 调用者的死路径上，从未真正生效过，也从未有人发现）。ValueError/KeyError 故意不在
# 这里——它们也是脏数据（例如损坏的 system_settings.value JSON）的惯用异常类型，
# 落进这个元组会把一次配置脏数据误记成代码 bug，带着堆栈吵到 ERROR 级，让排查者去
# 找一个不存在的 bug；脏数据应该走下面的 transient 分支，安静地退回默认值。两类
# 异常都仍选择 fail-open（宁可暂时不强制执行限额，也不能让模型调用级联失败），但
# 日志级别和关键词必须能区分，才 grep 得到。
PROGRAMMING_ERROR_TYPES = (TypeError, AttributeError, NameError)

_SCOPE_LABELS = {
    SCOPE_AGENT_DAY: "Agent 当日",
    SCOPE_AGENT_MONTH: "Agent 当月",
    SCOPE_TENANT_DAY: "企业当日",
}


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """一次限额判定的结果。

    `reset_at` 复用于两种场景：命中限额时是被拦截档位的周期重置时刻；未命中但触发
    软告警时，是被临近的那个档位的重置时刻（既未命中限额也未触发软告警时才恒为
    None——命中限额时 `soft_warning` 仍是 False，但 `reset_at` 不是 None）。
    `soft_warning_scope` / `soft_warning_subject_id` 标出软告警具体是哪个 scope、
    哪个主体（agent 或 tenant）触发的——去重键必须用这两个字段，不能靠调用方猜。
    """

    allowed: bool
    blocked_scope: str | None = None
    used: int | None = None
    limit: int | None = None
    reset_at: datetime | None = None
    mode: str = MODE_WARN_ONLY
    soft_warning: bool = False
    soft_warning_scope: str | None = None
    soft_warning_subject_id: uuid.UUID | None = None


async def current_enforcement_mode() -> str:
    """读执行模式。缺失、脏配置或读取本身失败，一律退回 warn_only —— 不能意外变成硬拦。

    这条判定挂在活路径的每一次模型调用上（Task 8），配置存储的瞬时故障（例如数据
    库连接抖动）绝不能级联成全平台模型调用失败；宁可暂时不强制执行限额。读取失败
    本身按异常类型分类记日志（见 PROGRAMMING_ERROR_TYPES），编程错误要吵，基础设
    施故障可以安静，但两条路径都必须退回 warn_only。
    """
    try:
        value = await system_setting_dao.get_value(SETTING_ENFORCEMENT_MODE, {})
    except PROGRAMMING_ERROR_TYPES as error:
        logger.opt(exception=True).error(
            "token_budget_enforcement_disabled_bug scope=enforcement_mode error={!r}",
            error,
        )
        return MODE_WARN_ONLY
    except Exception as error:  # noqa: BLE001 - 基础设施/瞬时故障也不能级联成硬拦
        logger.warning(
            "token_budget_enforcement_disabled_transient scope=enforcement_mode error={!r}",
            error,
        )
        return MODE_WARN_ONLY
    mode = value.get("mode") if isinstance(value, dict) else None
    if mode in KNOWN_MODES:
        return mode
    return MODE_WARN_ONLY


def _next_day_boundary(tz_name: str, now: datetime) -> datetime:
    """下一个本地日边界。用当前边界 + 26h 再取边界，跨 DST 也稳。"""
    return local_day_start(tz_name, now=local_day_start(tz_name, now=now) + timedelta(hours=26))


def _next_month_boundary(tz_name: str, now: datetime) -> datetime:
    month_start = local_month_start(tz_name, now=now)
    return local_month_start(tz_name, now=month_start + timedelta(days=32))


def _effective_used(
    used: int | None,
    last_reset: datetime | None,
    tz_name: str,
    *,
    now: datetime,
    monthly: bool,
) -> int:
    """周期已翻页时把计数视为 0。

    计数器不自动重置曾让纯 cron 驱动的 Agent 永久卡死，所以判定不能盲信存量数字。
    """
    stale = (
        is_new_local_month(last_reset, tz_name, now=now) if monthly else is_new_local_day(last_reset, tz_name, now=now)
    )
    if stale:
        return 0
    return int(used or 0)


def _breach(
    *,
    used: int,
    limit: int | None,
    estimated: int,
) -> bool:
    # limit 为 None 表示未设上限；0 表示管理员要求禁止一切，二者语义不同，
    # 不能用真值判断合并（0 是 falsy）。前端设置页现在会拒绝把 0 提交为限额（非正数
    # 一律转成 null），迁移也把历史遗留的 0 值改成了 NULL，所以这里能读到的 0 只会
    # 来自有人主动设置（例如直接调用 API）以彻底封锁该 agent/租户的用量。
    if limit is None:
        return False
    return used + max(0, estimated) >= limit


async def evaluate(
    *,
    agent,
    tenant,
    tenant_counter,
    estimated_next_round_tokens: int = 0,
    now: datetime | None = None,
    mode: str | None = None,
) -> BudgetVerdict:
    """判定本轮是否可以发起模型请求。"""
    effective_now = now or datetime.now(UTC)
    if mode is not None:
        if mode in KNOWN_MODES:
            effective_mode = mode
        else:
            logger.warning("token_budget_unknown_mode_override mode={!r} fallback=warn_only", mode)
            effective_mode = MODE_WARN_ONLY
    else:
        effective_mode = await current_enforcement_mode()

    tz_agent = effective_timezone(agent, tenant)
    tz_tenant = tenant_timezone(tenant)

    checks = (
        (
            SCOPE_AGENT_DAY,
            _effective_used(
                getattr(agent, "tokens_used_today", 0),
                getattr(agent, "last_daily_reset", None),
                tz_agent,
                now=effective_now,
                monthly=False,
            ),
            getattr(agent, "max_tokens_per_day", None),
            _next_day_boundary(tz_agent, effective_now),
        ),
        (
            SCOPE_AGENT_MONTH,
            _effective_used(
                getattr(agent, "tokens_used_month", 0),
                getattr(agent, "last_monthly_reset", None),
                tz_agent,
                now=effective_now,
                monthly=True,
            ),
            getattr(agent, "max_tokens_per_month", None),
            _next_month_boundary(tz_agent, effective_now),
        ),
        (
            SCOPE_TENANT_DAY,
            _effective_used(
                getattr(tenant_counter, "tokens_used_today", 0),
                getattr(tenant_counter, "last_daily_reset", None),
                tz_tenant,
                now=effective_now,
                monthly=False,
            ),
            getattr(tenant, "max_tokens_per_day", None),
            _next_day_boundary(tz_tenant, effective_now),
        ),
    )

    soft_warning = False
    soft_warning_scope: str | None = None
    soft_warning_subject_id: uuid.UUID | None = None
    soft_warning_reset_at: datetime | None = None
    for scope, used, limit, reset_at in checks:
        if _breach(used=used, limit=limit, estimated=estimated_next_round_tokens):
            return BudgetVerdict(
                allowed=effective_mode == MODE_WARN_ONLY,
                blocked_scope=scope,
                used=used,
                limit=limit,
                reset_at=reset_at,
                mode=effective_mode,
            )
        # 同上：None 才是无限额，0 要参与阈值判断，不能用真值判断合并。
        # 只记最具体（第一个命中）的档位，跟 breach 分支的优先级规则保持一致，
        # 否则 tenant_day 会在循环末尾把更具体的 agent_day 软告警信息覆盖掉。
        if not soft_warning and limit is not None and used >= int(limit * SOFT_WARNING_RATIO):
            soft_warning = True
            soft_warning_scope = scope
            soft_warning_subject_id = (
                getattr(agent, "id", None)
                if scope in (SCOPE_AGENT_DAY, SCOPE_AGENT_MONTH)
                else getattr(tenant, "id", None)
            )
            soft_warning_reset_at = reset_at

    return BudgetVerdict(
        allowed=True,
        mode=effective_mode,
        soft_warning=soft_warning,
        soft_warning_scope=soft_warning_scope,
        soft_warning_subject_id=soft_warning_subject_id,
        reset_at=soft_warning_reset_at,
    )


async def should_emit_soft_warning(scope: str, subject_id, reset_at) -> bool:
    """每周期每 scope 只告警一次。Redis 不可用就跳过 —— 告警不影响正确性路径。"""
    try:
        client = await get_redis()
        key = f"token_budget_soft_warning:{scope}:{subject_id}"
        acquired = await client.set(key, "1", nx=True, exat=int(reset_at.timestamp()))
        return bool(acquired)
    except Exception as error:  # noqa: BLE001 - 告警是提示性的，任何异常都不能影响正确性路径
        logger.debug("token_soft_warning_dedup_unavailable error={!r}", error)
        return False


def budget_exceeded_message(verdict: BudgetVerdict) -> str:
    label = _SCOPE_LABELS.get(verdict.blocked_scope or "", verdict.blocked_scope or "")
    used = f"{verdict.used:,}" if verdict.used is not None else "?"
    limit = f"{verdict.limit:,}" if verdict.limit is not None else "?"
    reset = verdict.reset_at.isoformat(timespec="minutes") if verdict.reset_at is not None else "下一个周期"
    return (
        f"{label} token 用量已达上限（{used}/{limit}，scope={verdict.blocked_scope}）。"
        f"额度将在 {reset} 释放，或请管理员调高上限。"
    )


__all__ = [
    "KNOWN_MODES",
    "MODE_ENFORCE",
    "MODE_WARN_ONLY",
    "PROGRAMMING_ERROR_TYPES",
    "SCOPE_AGENT_DAY",
    "SCOPE_AGENT_MONTH",
    "SCOPE_TENANT_DAY",
    "SETTING_CALIBRATION_SWITCHED_AT",
    "SETTING_ENFORCEMENT_MODE",
    "SOFT_WARNING_RATIO",
    "BudgetVerdict",
    "budget_exceeded_message",
    "current_enforcement_mode",
    "evaluate",
    "should_emit_soft_warning",
]
