"""Token 限额判定。

判定顺序由最具体到最宽：agent_day -> agent_month -> tenant_day，第一个命中者写进
verdict，使错误能说清究竟哪一档天花板起了作用。

能力边界：预检基于估算，且 provider 真实用量要等响应返回才知道，所以"一个 token
都不超"做不到。设计目标是超限幅度有界 —— 超出部分不超过一轮的消耗量。
"""

from __future__ import annotations

import time
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
# 调用者的死路径上，从未真正生效过，也从未有人发现）。
#
# 划这条分界线的判据只有一条：读取动作是否成功。
#   - 读取动作本身失败（这个元组）→ 连"管理员配的是什么"都不知道，模式未知，按
#     3.6 fail-open 退回 warn_only，且必须吵到 ERROR——这类异常基本必然是代码 bug
#     （签名漂移、属性改名、变量名打错），必须有人立刻看到，不能被静默吞掉。
#   - 读取动作成功、但读到的值不可用（行缺失 / 缺 mode 键 / 值不在 KNOWN_MODES /
#     下面 _CONFIG_DIRT_TYPES 描述的脏 JSON 反序列化失败）→ 属于"配置层缺省"，
#     安全默认值是 enforce：enforce 的误判代价被限额自身的作用域限住了——只影响
#     已经超过管理员设定上限的主体，而这正是管理员设上限时要求的结果；warn_only
#     的误判代价则是无上界的超额消耗，两者不对称，所以"值不可用"时偏向 enforce。
#     这一类仍然只落 WARNING，不升级为 ERROR——它不是代码 bug，是数据问题。
#
# ValueError/KeyError 故意不在这个元组里——它们是脏数据（例如损坏的
# system_settings.value JSON）的惯用异常类型，落进这个元组会把一次配置脏数据误记
# 成代码 bug，带着堆栈吵到 ERROR 级，让排查者去找一个不存在的 bug。它们被分类进
# 下面的 _CONFIG_DIRT_TYPES：日志级别仍是 WARNING（这一点没变——不吵到 ERROR），
# 但生效模式从 warn_only 改成了 enforce——因为"反序列化失败"本质上是"读取动作
# 成功返回了内容，只是这份内容不可用"，属于配置层缺省，不是"读取动作本身失败、
# 模式未知"。三类异常都仍然是 fail-open 的（宁可暂时不强制执行限额，也不能让模
# 型调用级联失败），但日志级别、关键词与生效模式必须能区分，才 grep 得到、才能
# 分别落到 enforce / warn_only 两种安全默认值。
PROGRAMMING_ERROR_TYPES = (TypeError, AttributeError, NameError)

# 脏 JSON 反序列化失败的惯用异常类型：system_settings.value 列的内容本身被数据库
# 成功返回（读取动作没有失败），但反序列化 / 解析成预期形状时失败。归类为"配置层
# 缺省"（见上面 PROGRAMMING_ERROR_TYPES 的注释），生效模式是 enforce，日志级别是
# WARNING、关键字 reason=unparsable——跟"行缺失"“脏值”是同一类问题的不同成因，
# 不是代码 bug。
_CONFIG_DIRT_TYPES = (ValueError, KeyError)

# 进程内执行模式缓存：读多写少，竞态条件的后果只是短暂的重复查询，不影响正确性，
# 因此不加锁，用简单的模块级变量 + 单调时钟时间戳即可（`time.monotonic()` 不受
# 系统时钟被拨动影响，比 `datetime.now()` 更适合做 TTL 判定）。
#
# `_MODE_TTL_SECONDS`：缓存新鲜期。命中新鲜缓存直接返回，不查 DB —— 这是 3.3
# （未达上限时不引入额外 DB 往返）的实现手段：今天每个模型步的两阶段判定各自调一次
# `current_enforcement_mode()`，稳态下缓存生效后降为 0 次额外 SELECT。
# `_MODE_STALE_TOLERANCE_SECONDS`：读取失败（DB 抖动等基础设施故障）时，允许沿用
# 缓存值的过期上限。读取失败但缓存年龄在这个容忍期内 -> 用缓存值（stale-if-error），
# 比直接 fail-open 退回 warn_only 更贴近真实配置；超出容忍期 -> 按 3.6 fail-open
# 退回 warn_only（模式已经太旧，不能再信）。
_MODE_TTL_SECONDS = 30.0
_MODE_STALE_TOLERANCE_SECONDS = 600.0

# 缓存状态本身（任务 3.4 从"只缓存 mode 字符串"扩展为"缓存完整的配置层状态"，
# 使 `current_enforcement_state()` 也能从缓存快速返回，不必每次都重新查库）：
#   - `_state_cache_configured_mode`：配置层的原始模式（不含 grace 影响）。
#   - `_state_cache_grace_until`：配置里的 grace 窗口截止时刻（可能是 None）。
#   - `_state_cache_written_at`：写入时刻（`time.monotonic()`）。
# 三者要么全部为 None（冷启动，或 `reset_enforcement_mode_cache()` 刚清空过），
# 要么全部有值——不存在"只写了一半"的中间状态，因为它们只通过 `_remember_state()`
# 一起写入。缓存本身不记"这份值最初是从哪条分支来的"——命中新鲜缓存时
# `EnforcementState.source` 统一记为 "cache"（见 `current_enforcement_state()`），
# 沿用 stale 缓存时统一记为 "stale_cache"；只有真正触发了一次新查询时，source
# 才是具体的分支名（row_absent / dirty_value / unparsable / row_present /
# programming_error / transient_error）。
#
# 注意：grace 是否生效必须每次调用都用**当前**的挂钟时间（`datetime.now(UTC)` 或
# 调用方传入的 `now`）重新判断，不能缓存"effective_mode"这个已经算好的结论——
# 否则 grace 窗口过期后，缓存 TTL 内的调用仍会继续放行，直到 TTL 自然过期才更新，
# 这在 TTL 较短（30s）时问题不大，但语义上是错的：grace 只应该在"当下确实还没到
# grace_until"时生效。因此只缓存 configured_mode / grace_until 这两个原始输入，
# effective_mode 每次调用都现算。
_state_cache_configured_mode: str | None = None
_state_cache_grace_until: datetime | None = None
_state_cache_written_at: float | None = None


def reset_enforcement_mode_cache() -> None:
    """清空进程内执行模式缓存，使下一次 `current_enforcement_mode()` 强制重读。

    供测试（避免用例间通过 30 秒 TTL 缓存互相污染）与写入端点（任务 8.2 的
    `PUT /token-budget-enforcement`）调用：写入后立即失效，使同进程内立刻生效；
    跨 worker 最长 30 秒生效。
    """
    global _state_cache_configured_mode, _state_cache_grace_until, _state_cache_written_at
    _state_cache_configured_mode = None
    _state_cache_grace_until = None
    _state_cache_written_at = None


def _stale_state_or_fail_open(now_monotonic: float, *, fallback_source: str) -> tuple[str, datetime | None, str]:
    """读取动作本身失败时的兜底：缓存新鲜度尚可就用缓存的配置状态，否则 fail-open 到 warn_only。

    不更新 `_state_cache_written_at`——沿用一个已经过期的缓存值不代表它变新鲜了，
    避免"假新鲜"：下一次调用仍应基于原始写入时刻判断是否还在 stale 容忍期内。
    命中 stale 缓存时 source 统一记为 "stale_cache"，方便按日志关键字区分
    "这次判定用的是刚查出来的值"还是"沿用了一份旧值"；超出容忍期后彻底
    fail-open，source 沿用调用方传入的 `fallback_source`（"programming_error" /
    "transient_error"，与本次异常的分类保持一致），此时不再知道 grace_until 是
    什么形状，`configured_mode` 与 `effective_mode` 都退回 MODE_WARN_ONLY（grace
    逻辑对已经是 warn_only 的 configured_mode 是无操作的，不需要特殊处理）。
    """
    if _state_cache_written_at is not None:
        age = now_monotonic - _state_cache_written_at
        if age <= _MODE_STALE_TOLERANCE_SECONDS:
            return _state_cache_configured_mode or MODE_WARN_ONLY, _state_cache_grace_until, "stale_cache"
    return MODE_WARN_ONLY, None, fallback_source


def _remember_state(configured_mode: str, grace_until: datetime | None, now_monotonic: float) -> None:
    """把一次成功解析出的配置状态写入缓存（无论是正常值还是配置层缺省的安全默认值）。"""
    global _state_cache_configured_mode, _state_cache_grace_until, _state_cache_written_at
    _state_cache_configured_mode = configured_mode
    _state_cache_grace_until = grace_until
    _state_cache_written_at = now_monotonic


def _parse_grace_until(raw: object) -> datetime | None:
    """把 value["grace_until"] 解析成带时区的 datetime；缺失/类型不对/格式错误都返回 None。

    调用方按"返回 None 即不进入 grace"处理，不需要区分"缺失"与"不可解析"——
    二者在 grace 判定这一层的效果相同，区别只体现在上游是否落缺失相关的日志
    （目前设计不要求为不可解析的 grace_until 单独落一条 WARNING，只要求它不生效）。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _grace_active(grace_until: datetime | None, now: datetime) -> bool:
    return grace_until is not None and now < grace_until


def _resolve_state(
    configured_mode: str,
    grace_until: datetime | None,
    source: str,
    now: datetime,
    *,
    log_grace: bool,
) -> "EnforcementState":
    """按 grace 是否生效计算 effective_mode，生效时按需落一条 INFO 日志。

    `log_grace=False` 用在"直接命中 TTL 内新鲜缓存"这一条路径——那是稳态下绝大多数
    调用会走的路径，如果每次都记 INFO 会刷屏；`log_grace=True` 用在"这次调用触发了
    一次真正的配置读取（或 stale-if-error 兜底）"，是"每进程每 TTL 至多一次"这个
    节流目标实际落地的地方：只要缓存还新鲜，后续调用统统走 `log_grace=False`。
    """
    if _grace_active(grace_until, now):
        effective_mode = MODE_WARN_ONLY
        if log_grace:
            logger.info(
                "token_budget_enforcement_grace_active grace_until={} configured_mode={}",
                grace_until.isoformat(),
                configured_mode,
            )
    else:
        effective_mode = configured_mode
    return EnforcementState(
        configured_mode=configured_mode,
        grace_until=grace_until,
        effective_mode=effective_mode,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class EnforcementState:
    """执行模式的完整状态，供 API 与看板显示（`current_enforcement_mode()` 只是它的薄封装）。

    `configured_mode` 是管理员/迁移配置的原始模式，不含 grace 影响；`effective_mode`
    是判定实际采用的模式——grace 生效时恒为 `warn_only`，否则等于 `configured_mode`。
    `source` 说明这份状态是从哪来的，取值与 `current_enforcement_mode()` 内部的六条
    分支日志关键字保持一致，便于 grep：
      - "row_absent"：`system_settings` 无此行
      - "dirty_value"：有行但缺 `mode` 键，或值不在 `KNOWN_MODES`
      - "unparsable"：value 列 JSON 反序列化/解析失败（`_CONFIG_DIRT_TYPES`）
      - "row_present"：读到了合法的 `mode` 值
      - "programming_error"：读取动作本身抛 `PROGRAMMING_ERROR_TYPES`
      - "transient_error"：读取动作本身抛其它异常（基础设施/瞬时故障）
      - "cache"：命中 TTL 内的新鲜缓存，未触发本次查询
      - "stale_cache"：读取失败但沿用了 stale 容忍期内的缓存值
      - "fail_open"：读取失败且无可用缓存，彻底 fail-open
    """

    configured_mode: str
    grace_until: datetime | None
    effective_mode: str
    source: str


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
    estimated: int | None = None
    reset_at: datetime | None = None
    mode: str = MODE_WARN_ONLY
    soft_warning: bool = False
    soft_warning_scope: str | None = None
    soft_warning_subject_id: uuid.UUID | None = None


async def current_enforcement_state(now: datetime | None = None) -> EnforcementState:
    """读执行模式的完整状态（configured_mode + grace_until + effective_mode + source）。

    这是本模块读取 `token_budget_enforcement_mode` 的核心实现；`current_enforcement_mode()`
    只是它的薄封装，只返回 `effective_mode`，供 `advanced.py`、`model_step_service`
    等既有调用方在不改签名的前提下继续使用。

    按"读取动作是否成功"分两条截然不同的路径兜底：

    - 读取动作成功、但读到的值不可用（行缺失 / 缺 mode 键 / 值不在 KNOWN_MODES /
      value 列 JSON 反序列化失败）→ 配置层缺省，安全默认值是 MODE_ENFORCE，按
      WARNING + `token_budget_enforcement_mode_defaulted reason=...` 记日志。
    - 读取动作本身失败（这条判定挂在活路径的每一次模型调用上，配置存储的瞬时故
      障——例如数据库连接抖动——绝不能级联成全平台模型调用失败）→ 若有新鲜度尚可
      的缓存（见 `_MODE_STALE_TOLERANCE_SECONDS`）就用缓存的配置状态，否则模式
      未知，fail-open 退回 MODE_WARN_ONLY（且不再有 grace_until）；按异常类型分级
      （见 PROGRAMMING_ERROR_TYPES 的注释），编程错误吵到 ERROR，基础设施故障安
      静地落 WARNING，但两者的生效模式（缓存命中或 warn_only）都不受这条分级影响。

    grace 窗口：value 形状扩展为 `{"mode", "grace_until", "set_by"}`。`grace_until`
    是 ISO8601 字符串时，`now < grace_until` 则 grace 生效、`effective_mode` 恒为
    `warn_only`（无论 `configured_mode` 是什么）；`grace_until` 缺失、已过期、或
    不可解析（格式错误）→ grace 不生效，`effective_mode = configured_mode`。grace
    生效时落一条 INFO `token_budget_enforcement_grace_active grace_until=…`——只在
    这次调用触发了真正的配置读取（或 stale-if-error 兜底）时才记，命中 TTL 内新鲜
    缓存的调用不重复记，做到"每进程每 TTL 至多一次，不逐调用刷屏"。

    进程内缓存（TTL + stale-if-error）：命中新鲜缓存（未超过 `_MODE_TTL_SECONDS`）
    直接返回，不查 DB——这是 3.3（未达上限时不引入额外 DB 往返）的实现手段。缓存
    是模块级状态，读多写少，不加锁：竞态条件的后果只是短暂的重复查询，不影响正确性。
    """
    effective_now = now or datetime.now(UTC)
    now_monotonic = time.monotonic()
    if _state_cache_written_at is not None and now_monotonic - _state_cache_written_at <= _MODE_TTL_SECONDS:
        # 命中新鲜缓存：不查 DB，也不重复记 grace 生效日志（`log_grace=False`）。
        configured_mode = _state_cache_configured_mode or MODE_WARN_ONLY
        return _resolve_state(configured_mode, _state_cache_grace_until, "cache", effective_now, log_grace=False)

    try:
        value = await system_setting_dao.get_value(SETTING_ENFORCEMENT_MODE, {})
    except PROGRAMMING_ERROR_TYPES as error:
        logger.opt(exception=True).error(
            "token_budget_enforcement_disabled_bug scope=enforcement_mode error={!r}",
            error,
        )
        configured_mode, grace_until, source = _stale_state_or_fail_open(
            now_monotonic, fallback_source="programming_error"
        )
        return _resolve_state(configured_mode, grace_until, source, effective_now, log_grace=True)
    except _CONFIG_DIRT_TYPES as error:
        # 读取动作本身成功——只是 value 列的内容反序列化/解析失败，属于配置层
        # 缺省而非模式未知，安全默认值是 enforce（见 _CONFIG_DIRT_TYPES 的注释）。
        logger.warning(
            "token_budget_enforcement_mode_defaulted reason=unparsable scope=enforcement_mode error={!r}",
            error,
        )
        _remember_state(MODE_ENFORCE, None, now_monotonic)
        return _resolve_state(MODE_ENFORCE, None, "unparsable", effective_now, log_grace=True)
    except Exception as error:  # noqa: BLE001 - 基础设施/瞬时故障也不能级联成硬拦
        logger.warning(
            "token_budget_enforcement_disabled_transient scope=enforcement_mode error={!r}",
            error,
        )
        configured_mode, grace_until, source = _stale_state_or_fail_open(
            now_monotonic, fallback_source="transient_error"
        )
        return _resolve_state(configured_mode, grace_until, source, effective_now, log_grace=True)

    if not value:
        # get_value 按约定在行缺失时退回调用方传入的 default（这里是 {}）。
        logger.warning("token_budget_enforcement_mode_defaulted reason=row_absent scope=enforcement_mode")
        _remember_state(MODE_ENFORCE, None, now_monotonic)
        return _resolve_state(MODE_ENFORCE, None, "row_absent", effective_now, log_grace=True)

    mode = value.get("mode") if isinstance(value, dict) else None
    if mode in KNOWN_MODES:
        grace_until = _parse_grace_until(value.get("grace_until")) if isinstance(value, dict) else None
        _remember_state(mode, grace_until, now_monotonic)
        return _resolve_state(mode, grace_until, "row_present", effective_now, log_grace=True)

    # 有行，但缺 mode 键，或值不在 KNOWN_MODES 里——同属配置层缺省。
    logger.warning(
        "token_budget_enforcement_mode_defaulted reason=dirty_value scope=enforcement_mode mode={!r}",
        mode,
    )
    _remember_state(MODE_ENFORCE, None, now_monotonic)
    return _resolve_state(MODE_ENFORCE, None, "dirty_value", effective_now, log_grace=True)


async def current_enforcement_mode() -> str:
    """薄封装：只返回 `effective_mode`，保持既有调用方（`advanced.py`、
    `model_step_service` 等）的签名与返回类型不变。核心实现见 `current_enforcement_state()`。
    """
    state = await current_enforcement_state()
    return state.effective_mode


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
            # 显式覆盖为未知值：调用方确实传了 mode，只是值不在 KNOWN_MODES 里，
            # 属于"读到了值但值不可用"这一类配置层缺省，判据与 current_enforcement_mode()
            # 的兜底分支一致，安全默认值同样是 enforce（不是模式未知的 fail-open 场景）。
            logger.warning("token_budget_unknown_mode_override mode={!r} fallback=enforce", mode)
            effective_mode = MODE_ENFORCE
    else:
        effective_mode = await current_enforcement_mode()

    tz_tenant = tenant_timezone(tenant)

    tenant_day_check = (
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
    )

    if agent is None:
        # 系统开销链路（group_compact / planning / model_probe）没有 agent 主体，
        # 只判 tenant_day 一档。不能调 effective_timezone(None, tenant)——它会走到
        # get_agent_timezone_sync 里访问 agent.timezone 而抛 AttributeError，被
        # PROGRAMMING_ERROR_TYPES 捕获后 fail-open，等于「接了闸门但永远放行」。
        checks = (tenant_day_check,)
    else:
        tz_agent = effective_timezone(agent, tenant)
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
            tenant_day_check,
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
                estimated=estimated_next_round_tokens if estimated_next_round_tokens > 0 else None,
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
    if verdict.estimated is not None and verdict.estimated > 0:
        return (
            f"{label} token 用量已达上限（已用 {used} + 本轮预估 {verdict.estimated:,} ≥ 上限 {limit}，"
            f"scope={verdict.blocked_scope}）。"
            f"额度将在 {reset} 释放，或请管理员调高上限。"
        )
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
    "EnforcementState",
    "budget_exceeded_message",
    "current_enforcement_mode",
    "current_enforcement_state",
    "evaluate",
    "reset_enforcement_mode_cache",
    "should_emit_soft_warning",
]
