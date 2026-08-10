"""限额判定：三档顺序、预算预检、执行模式、软告警去重。

背景：现存的限额逻辑全在 caller.py 这条无生产调用者的死路径上，活路径
complete_llm_once 零检查。本模块是新的唯一判定实现。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.token_accounting import budget
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_AGENT_MONTH,
    SCOPE_TENANT_DAY,
    BudgetVerdict,
    budget_exceeded_message,
    evaluate,
    reset_enforcement_mode_cache,
)

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3）。

    这提前处理了任务 3.5 里提到的 autouse fixture 需求的一部分——本文件的这个
    fixture 覆盖了任务 3.5 清单里本文件相关的用例，任务 3.5 不需要重复添加。
    """
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


def _agent(**overrides):
    base = {
        "id": AGENT_ID,
        "name": "Ada",
        "tenant_id": TENANT_ID,
        "timezone": None,
        "max_tokens_per_day": 100_000,
        "max_tokens_per_month": 1_000_000,
        "tokens_used_today": 0,
        "tokens_used_month": 0,
        "last_daily_reset": NOW,
        "last_monthly_reset": NOW,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _tenant(**overrides):
    base = {"id": TENANT_ID, "timezone": "Asia/Shanghai", "max_tokens_per_day": 500_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _counter(**overrides):
    base = {"tenant_id": TENANT_ID, "tokens_used_today": 0, "last_daily_reset": NOW}
    base.update(overrides)
    return SimpleNamespace(**base)


async def _evaluate(**kwargs):
    defaults = {
        "agent": _agent(),
        "tenant": _tenant(),
        "tenant_counter": _counter(),
        "now": NOW,
        "mode": MODE_ENFORCE,
    }
    defaults.update(kwargs)
    return await evaluate(**defaults)


async def test_within_all_limits_is_allowed() -> None:
    verdict = await _evaluate()

    assert verdict.allowed is True
    assert verdict.blocked_scope is None


async def test_agent_daily_limit_blocks_first() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.used == 100_000
    assert verdict.limit == 100_000


async def test_agent_monthly_limit_blocks_when_daily_is_fine() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=10, tokens_used_month=1_000_000))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_MONTH


async def test_tenant_daily_limit_blocks_when_agent_is_fine() -> None:
    verdict = await _evaluate(tenant_counter=_counter(tokens_used_today=500_000))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_TENANT_DAY
    assert verdict.limit == 500_000


async def test_the_most_specific_scope_wins_when_several_are_breached() -> None:
    """错误信息必须说清是哪一档卡的，否则运维无从下手。"""
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=100_000, tokens_used_month=1_000_000),
        tenant_counter=_counter(tokens_used_today=500_000),
    )

    assert verdict.blocked_scope == SCOPE_AGENT_DAY


async def test_null_limit_means_unlimited() -> None:
    verdict = await _evaluate(
        agent=_agent(
            max_tokens_per_day=None,
            max_tokens_per_month=None,
            tokens_used_today=10**9,
            tokens_used_month=10**9,
        ),
        tenant=_tenant(max_tokens_per_day=None),
        tenant_counter=_counter(tokens_used_today=10**9),
    )

    assert verdict.allowed is True


async def test_zero_limit_blocks_everything() -> None:
    """limit=0 是管理员显式要求的"禁止一切"，不能被真值判断误当成"无限额"。"""
    verdict = await _evaluate(agent=_agent(max_tokens_per_day=0, tokens_used_today=0))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.used == 0
    assert verdict.limit == 0


async def test_preflight_blocks_when_remaining_is_below_the_estimate() -> None:
    """不发一个必然超支的请求 —— 单轮长上下文可能就烧掉几十万 token。"""
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=99_000),
        estimated_next_round_tokens=5_000,
    )

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY


async def test_preflight_allows_when_remaining_covers_the_estimate() -> None:
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=90_000),
        estimated_next_round_tokens=5_000,
    )

    assert verdict.allowed is True


async def test_stale_counters_are_treated_as_reset_for_the_new_period() -> None:
    """日计数器不重置曾让纯 cron 驱动的 Agent 永久卡死，判定必须自己看周期。"""
    stale = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000, last_daily_reset=stale))

    assert verdict.allowed is True


async def test_warn_only_mode_reports_the_breach_without_blocking() -> None:
    """新口径数字变大，上线即硬拦会像一次大面积故障。"""
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000), mode=MODE_WARN_ONLY)

    assert verdict.allowed is True
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.mode == MODE_WARN_ONLY


async def test_soft_warning_fires_at_eighty_percent() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=80_000))

    assert verdict.allowed is True
    assert verdict.soft_warning is True


async def test_no_soft_warning_below_the_threshold() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=79_999))

    assert verdict.soft_warning is False


async def test_reset_at_uses_the_agent_effective_timezone() -> None:
    """提示要如实说明额度何时释放。北京 8/7 00:30 的下一个日边界是 8/7 16:00Z。"""
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000))

    assert verdict.reset_at == datetime(2026, 8, 7, 16, 0, tzinfo=UTC)


async def test_agent_none_only_checks_tenant_day_and_does_not_raise() -> None:
    """system_scope 链路（group_compact / planning / model_probe）没有 agent 主体。

    `agent=None` 时必须只判 tenant_day 一档，不能调 effective_timezone(None, tenant)
    （会走到 get_agent_timezone_sync 访问 agent.timezone 而抛 AttributeError）。
    `reset_at` 必须用租户时区计算，与 test_tenant_daily_limit_blocks_when_agent_is_fine
    等既有测试里 tenant_day 档的计算方式一致。
    """
    verdict = await _evaluate(
        agent=None,
        tenant_counter=_counter(tokens_used_today=500_000),
    )

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_TENANT_DAY
    assert verdict.used == 500_000
    assert verdict.limit == 500_000
    # 租户时区 Asia/Shanghai，北京 8/7 00:30 的下一个日边界是 8/7 16:00Z。
    # `reset_at` 必须经 tenant_timezone() 算出（agent=None 时不能调 effective_timezone）。
    assert verdict.reset_at == datetime(2026, 8, 7, 16, 0, tzinfo=UTC)


async def test_agent_none_is_unaffected_by_agent_day_or_month_limits() -> None:
    """agent=None 时即使传入的 tenant_counter 未击穿，也不会因为跳过了 agent 档而误判。

    这里没有 agent 可供击穿 agent_day / agent_month，只验证 tenant_day 未击穿时
    正常放行、且不抛异常。
    """
    verdict = await _evaluate(agent=None, tenant_counter=_counter(tokens_used_today=0))

    assert verdict.allowed is True
    assert verdict.blocked_scope is None


async def test_enforcement_mode_defaults_to_enforce_when_setting_absent(
    monkeypatch,
) -> None:
    """行缺失属于配置层缺省（读取动作成功，只是值不可用），安全默认值是 enforce。"""

    async def fake_get_value(key, default=None):
        return default

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)
    records, handler_id = _capture_logs()

    try:
        mode = await budget.current_enforcement_mode()
    finally:
        logger.remove(handler_id)

    assert mode == MODE_ENFORCE
    assert any(
        level == "WARNING" and "token_budget_enforcement_mode_defaulted reason=row_absent" in text
        for level, text in records
    )


async def test_enforcement_mode_reads_the_dict_shaped_setting(monkeypatch) -> None:
    """system_settings 的既有约定是 dict 形状的 value。"""

    async def fake_get_value(key, default=None):
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_ENFORCE
    # value 里没有 grace_until 时不进入 grace（任务 3.4）：effective_mode 直接
    # 等于 configured_mode，不会被 grace 覆写成 warn_only。
    reset_enforcement_mode_cache()
    state = await budget.current_enforcement_state()
    assert state.configured_mode == MODE_ENFORCE
    assert state.grace_until is None
    assert state.effective_mode == MODE_ENFORCE


async def test_unknown_mode_value_falls_back_to_enforce(monkeypatch) -> None:
    """脏值也是"读到了值但值不可用"，属于配置层缺省，安全默认值是 enforce。"""

    async def fake_get_value(key, default=None):
        return {"mode": "whatever"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)
    records, handler_id = _capture_logs()

    try:
        mode = await budget.current_enforcement_mode()
    finally:
        logger.remove(handler_id)

    assert mode == MODE_ENFORCE
    assert any(
        level == "WARNING" and "token_budget_enforcement_mode_defaulted reason=dirty_value" in text
        for level, text in records
    )


async def test_enforcement_mode_falls_back_to_warn_only_when_lookup_raises(
    monkeypatch,
) -> None:
    """设置读取本身失败（例如 DB 抖动）也不该意外变成硬拦或让调用方崩溃。

    这条判定现在挂在每一次模型调用的活路径上（Task 8），配置读取的瞬时故障绝不能
    级联成全平台模型调用失败。
    """

    async def fake_get_value(key, default=None):
        raise OSError("connection refused")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_WARN_ONLY


async def test_cache_hit_within_ttl_does_not_re_query(monkeypatch) -> None:
    """TTL 内连续两次调用，中间无时间流逝，DB 只查一次。"""
    calls = 0

    async def counting_get_value(key, default=None):
        nonlocal calls
        calls += 1
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", counting_get_value)

    first = await budget.current_enforcement_mode()
    second = await budget.current_enforcement_mode()

    assert first == MODE_ENFORCE
    assert second == MODE_ENFORCE
    assert calls == 1, "缓存命中时不应重新查库"


async def test_cache_expires_after_ttl_and_re_queries(monkeypatch) -> None:
    """TTL 过期后重读：用 monkeypatch 操纵单调时钟，模拟时间流逝。"""
    calls = 0

    async def counting_get_value(key, default=None):
        nonlocal calls
        calls += 1
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", counting_get_value)

    fake_now = [1_000.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: fake_now[0])

    first = await budget.current_enforcement_mode()
    assert first == MODE_ENFORCE
    assert calls == 1

    # TTL 内，不应重读。
    fake_now[0] += budget._MODE_TTL_SECONDS - 1
    second = await budget.current_enforcement_mode()
    assert second == MODE_ENFORCE
    assert calls == 1

    # 超过 TTL，应重读。
    fake_now[0] += 2
    third = await budget.current_enforcement_mode()
    assert third == MODE_ENFORCE
    assert calls == 2, "TTL 过期后必须重新查库"


async def test_stale_cache_used_when_lookup_fails_within_tolerance(monkeypatch) -> None:
    """读取失败时使用 stale 缓存（缓存年龄在 600 秒容忍期内）。"""
    fake_now = [2_000.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: fake_now[0])

    async def succeeding_get_value(key, default=None):
        return {"mode": "warn_only"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", succeeding_get_value)

    # 先成功写入缓存。
    primed = await budget.current_enforcement_mode()
    assert primed == MODE_WARN_ONLY

    # 让 TTL 过期，但仍在 stale 容忍期内，读取失败。
    fake_now[0] += budget._MODE_TTL_SECONDS + 1

    async def failing_get_value(key, default=None):
        raise OSError("connection refused")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", failing_get_value)

    result = await budget.current_enforcement_mode()

    assert result == MODE_WARN_ONLY, "读取失败但缓存仍在 stale 容忍期内，应沿用缓存值"


async def test_fail_open_when_stale_tolerance_exceeded(monkeypatch) -> None:
    """超出 stale 容忍期后仍然 fail-open 到 warn_only（不使用过旧的缓存值）。"""
    fake_now = [3_000.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: fake_now[0])

    async def succeeding_get_value(key, default=None):
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", succeeding_get_value)

    primed = await budget.current_enforcement_mode()
    assert primed == MODE_ENFORCE

    # 超出 stale 容忍期（TTL 之外再加 stale 容忍期，再加一点余量）。
    fake_now[0] += budget._MODE_TTL_SECONDS + budget._MODE_STALE_TOLERANCE_SECONDS + 1

    async def failing_get_value(key, default=None):
        raise OSError("connection refused")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", failing_get_value)

    result = await budget.current_enforcement_mode()

    assert result == MODE_WARN_ONLY, "缓存已超出 stale 容忍期，不得再被信任，必须 fail-open"


async def test_reset_enforcement_mode_cache_forces_immediate_re_read(monkeypatch) -> None:
    """`reset_enforcement_mode_cache()` 后立即重读，不受 TTL 影响。"""
    calls = 0

    async def counting_get_value(key, default=None):
        nonlocal calls
        calls += 1
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", counting_get_value)

    first = await budget.current_enforcement_mode()
    assert first == MODE_ENFORCE
    assert calls == 1

    # 仍在 TTL 内，未重置时不应重读。
    second = await budget.current_enforcement_mode()
    assert calls == 1

    reset_enforcement_mode_cache()

    third = await budget.current_enforcement_mode()
    assert third == MODE_ENFORCE
    assert calls == 2, "reset_enforcement_mode_cache() 之后必须强制重新查库"


async def test_grace_missing_does_not_activate_grace(monkeypatch) -> None:
    """value 里没有 grace_until 键 -> 不进入 grace，effective_mode 等于 configured_mode。"""

    async def fake_get_value(key, default=None):
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    state = await budget.current_enforcement_state(now=NOW)

    assert state.configured_mode == MODE_ENFORCE
    assert state.grace_until is None
    assert state.effective_mode == MODE_ENFORCE
    assert state.source == "row_present"


async def test_grace_expired_does_not_activate_grace(monkeypatch) -> None:
    """grace_until 已经过期（now >= grace_until）-> 不进入 grace。"""
    past = "2026-08-01T00:00:00+00:00"  # 早于 NOW（2026-08-06）

    async def fake_get_value(key, default=None):
        return {"mode": "enforce", "grace_until": past}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    state = await budget.current_enforcement_state(now=NOW)

    assert state.configured_mode == MODE_ENFORCE
    assert state.grace_until == datetime.fromisoformat(past)
    assert state.effective_mode == MODE_ENFORCE, "grace 已过期，effective_mode 应等于 configured_mode"


async def test_grace_unparsable_does_not_activate_grace(monkeypatch) -> None:
    """grace_until 格式错误（不可解析）-> 不进入 grace，不抛异常。"""

    async def fake_get_value(key, default=None):
        return {"mode": "enforce", "grace_until": "not-a-valid-timestamp"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    state = await budget.current_enforcement_state(now=NOW)

    assert state.configured_mode == MODE_ENFORCE
    assert state.grace_until is None
    assert state.effective_mode == MODE_ENFORCE


async def test_grace_active_forces_warn_only_and_logs_once_per_ttl(monkeypatch) -> None:
    """grace_until 在未来 -> grace 生效，effective_mode 恒为 warn_only（无论 configured_mode）。

    同时验证节流：TTL 内命中缓存的后续调用不重复记 INFO 日志——这是"每进程每 TTL
    一次，不逐调用刷屏"的实现手段（复用任务 3.3 的缓存写入时机作为节流依据）。
    """
    future = "2026-08-13T00:00:00+00:00"  # 晚于 NOW（2026-08-06）

    async def fake_get_value(key, default=None):
        return {"mode": "enforce", "grace_until": future}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)
    records, handler_id = _capture_logs()

    try:
        first = await budget.current_enforcement_state(now=NOW)
        second = await budget.current_enforcement_state(now=NOW)  # 命中新鲜缓存
    finally:
        logger.remove(handler_id)

    assert first.configured_mode == MODE_ENFORCE
    assert first.grace_until == datetime.fromisoformat(future)
    assert first.effective_mode == MODE_WARN_ONLY, "grace 生效时 effective_mode 恒为 warn_only"
    assert first.source == "row_present"

    assert second.effective_mode == MODE_WARN_ONLY
    assert second.source == "cache", "第二次调用应命中缓存，不重新查库"

    grace_logs = [text for level, text in records if level == "INFO" and "token_budget_enforcement_grace_active" in text]
    assert len(grace_logs) == 1, "grace 生效日志每进程每 TTL 只记一次，不逐调用刷屏"
    assert future in grace_logs[0]


async def test_current_enforcement_mode_matches_effective_mode_during_grace(monkeypatch) -> None:
    """`current_enforcement_mode()` 的薄封装行为：grace 生效时同样返回 warn_only。"""
    future = "2026-08-13T00:00:00+00:00"

    async def fake_get_value(key, default=None):
        return {"mode": "enforce", "grace_until": future}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_WARN_ONLY


async def test_soft_warning_is_deduplicated_per_period(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeRedis:
        async def set(self, key, value, *, nx=False, exat=None, ex=None):
            calls.append((key, nx, exat, ex))
            return len(calls) == 1  # 第二次 NX set 返回 None/False

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)
    reset_at = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)

    first = await budget.should_emit_soft_warning(SCOPE_AGENT_DAY, AGENT_ID, reset_at)
    second = await budget.should_emit_soft_warning(SCOPE_AGENT_DAY, AGENT_ID, reset_at)

    assert first is True
    assert second is False
    assert calls[0][1] is True, "必须用 NX 才能保证只发一次"
    assert calls[0][2] == int(reset_at.timestamp()), "TTL 必须对齐周期重置时刻，否则去重键永不过期"


async def test_soft_warning_is_skipped_when_redis_is_down(monkeypatch) -> None:
    """告警只是提示性的，绝不能影响正确性路径或阻塞一次运行。"""

    async def fake_get_redis():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)

    result = await budget.should_emit_soft_warning(SCOPE_AGENT_DAY, AGENT_ID, datetime(2026, 8, 7, 16, 0, tzinfo=UTC))

    assert result is False


async def test_soft_warning_verdict_carries_its_own_reset_at_and_agent_subject() -> None:
    """agent 档软告警必须能自证是谁触发的，去重键不能靠调用方猜。"""
    verdict = await _evaluate(agent=_agent(tokens_used_today=80_000))

    assert verdict.soft_warning is True
    assert verdict.soft_warning_scope == SCOPE_AGENT_DAY
    assert verdict.soft_warning_subject_id == AGENT_ID
    assert verdict.reset_at == datetime(2026, 8, 7, 16, 0, tzinfo=UTC)


async def test_tenant_daily_soft_warning_is_keyed_by_tenant_not_agent() -> None:
    """tenant_day 档软告警必须挂 tenant.id，不能被硬编码成 agent.id。"""
    verdict = await _evaluate(tenant_counter=_counter(tokens_used_today=400_000))

    assert verdict.soft_warning is True
    assert verdict.soft_warning_scope == SCOPE_TENANT_DAY
    assert verdict.soft_warning_subject_id == TENANT_ID
    assert verdict.soft_warning_subject_id != AGENT_ID


async def test_agent_and_tenant_soft_warnings_from_separate_evaluations_use_distinct_dedup_keys(
    monkeypatch,
) -> None:
    """两次**各自独立**的评估各命中一档软告警时，去重键必须落在两个不同的 Redis 键上。

    注意：这不是同一次评估里两档同时命中的场景——那种场景见下面
    `test_simultaneous_soft_warnings_report_only_the_most_specific_scope`，两者行为不同。
    """
    calls: list[str] = []

    class FakeRedis:
        async def set(self, key, value, *, nx=False, exat=None, ex=None):
            calls.append(key)
            return True

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)

    agent_verdict = await _evaluate(agent=_agent(tokens_used_today=80_000))
    tenant_verdict = await _evaluate(tenant_counter=_counter(tokens_used_today=400_000))

    assert agent_verdict.soft_warning_scope == SCOPE_AGENT_DAY
    assert tenant_verdict.soft_warning_scope == SCOPE_TENANT_DAY

    await budget.should_emit_soft_warning(
        agent_verdict.soft_warning_scope,
        agent_verdict.soft_warning_subject_id,
        agent_verdict.reset_at,
    )
    await budget.should_emit_soft_warning(
        tenant_verdict.soft_warning_scope,
        tenant_verdict.soft_warning_subject_id,
        tenant_verdict.reset_at,
    )

    assert len(calls) == 2
    assert calls[0] != calls[1], "两档的去重键必须不同，否则会互相压制"


async def test_simultaneous_soft_warnings_report_only_the_most_specific_scope() -> None:
    """同一次评估里两档都达到软告警阈值时，只报最具体的那一档，不是两档都报。

    这是刻意的行为，跟 breach 分支"最具体档位优先"的规则保持一致（见 evaluate 里的
    注释）——不要把它当缺陷去改。这里把它钉成一个明确的回归测试，免得下一个读者
    以为两档软告警会同时触发。
    """
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=80_000),
        tenant_counter=_counter(tokens_used_today=400_000),
    )

    assert verdict.soft_warning is True
    assert verdict.soft_warning_scope == SCOPE_AGENT_DAY
    assert verdict.soft_warning_subject_id == AGENT_ID


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="TRACE",
    )
    return records, handler_id


async def test_enforcement_mode_lookup_programming_error_logs_at_error(monkeypatch) -> None:
    """签名漂移等编程错误必须吵得响，不能跟瞬时故障用同一条安静日志盖过去。"""

    async def fake_get_value(key, default=None):
        raise TypeError("signature drift")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)
    records, handler_id = _capture_logs()

    try:
        result = await budget.current_enforcement_mode()
    finally:
        logger.remove(handler_id)

    assert result == MODE_WARN_ONLY
    assert any(level == "ERROR" and "token_budget_enforcement_disabled_bug" in text for level, text in records)


async def test_enforcement_mode_lookup_transient_error_logs_at_warning(monkeypatch) -> None:
    async def fake_get_value(key, default=None):
        raise OSError("connection refused")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)
    records, handler_id = _capture_logs()

    try:
        result = await budget.current_enforcement_mode()
    finally:
        logger.remove(handler_id)

    assert result == MODE_WARN_ONLY
    assert any(level == "WARNING" and "token_budget_enforcement_disabled_transient" in text for level, text in records)


def test_message_names_the_scope_and_the_reset_time() -> None:
    reset_at = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)
    verdict = BudgetVerdict(
        allowed=False,
        blocked_scope=SCOPE_TENANT_DAY,
        used=500_000,
        limit=500_000,
        reset_at=reset_at,
        mode=MODE_ENFORCE,
    )

    message = budget_exceeded_message(verdict)

    assert "500,000" in message
    assert "tenant_day" in message or "租户" in message
    assert reset_at.isoformat(timespec="minutes") in message
