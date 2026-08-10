"""统一限额闸门 `gate.py` 的单元测试（任务 4.1）。

覆盖 `gate.check()` 的核心行为：命中限额 / 未命中 / 两级异常 fail-open / 软告警去重、
`BudgetClearance.not_applicable()` 的理由校验、`clearance_from()` 的包装，以及
`load_subjects()` 用会话替身验证查询写法。

测试风格沿用现有 43 个 token 测试的替身风格（`SimpleNamespace` 主体 + `monkeypatch`），
不引入新依赖。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.token_accounting import budget, gate
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    SCOPE_AGENT_DAY,
    SCOPE_TENANT_DAY,
    BudgetVerdict,
    reset_enforcement_mode_cache,
)
from app.services.token_accounting.gate import (
    LANE_BUSINESS_STEP,
    LANE_GROUP_COMPACT,
    BudgetClearance,
    BudgetSubjects,
    check,
    clearance_from,
    load_subjects,
)

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
RUN_ID = "run-123"


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3）。"""
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


@pytest.fixture(autouse=True)
def _force_enforce_mode(monkeypatch):
    """`check()` 不透传 `mode`，`evaluate()` 会自己走 `current_enforcement_mode()`。

    这里把配置读取钉在 `enforce`，让本文件的用例只关心 `check()` 自身的行为
    （异常分类、日志、软告警去重），不受本机是否能连上真实 `system_settings` 表影响
    （测试环境没有数据库连接，读取会失败并 fail-open 到 warn_only，把命中限额的
    verdict.allowed 变成 True，污染断言）。
    """

    async def fake_get_value(key, default=None):
        del key
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)


def _agent(**overrides) -> SimpleNamespace:
    base = {
        "id": AGENT_ID,
        "tenant_id": TENANT_ID,
        "timezone": None,
        "max_tokens_per_day": 100_000,
        "max_tokens_per_month": None,
        "tokens_used_today": 0,
        "tokens_used_month": 0,
        "last_daily_reset": NOW,
        "last_monthly_reset": NOW,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _tenant(**overrides) -> SimpleNamespace:
    base = {"id": TENANT_ID, "timezone": "Asia/Shanghai", "max_tokens_per_day": 500_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _counter(**overrides) -> SimpleNamespace:
    base = {"tenant_id": TENANT_ID, "tokens_used_today": 0, "last_daily_reset": NOW}
    base.update(overrides)
    return SimpleNamespace(**base)


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="TRACE",
    )
    return records, handler_id


# ---------------------------------------------------------------------------
# check(): allowed / blocked
# ---------------------------------------------------------------------------


async def test_check_returns_allowed_verdict_when_within_limits() -> None:
    subjects = BudgetSubjects(agent=_agent(), tenant=_tenant(), tenant_counter=_counter())

    verdict = await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)

    assert verdict.allowed is True
    assert verdict.blocked_scope is None


async def test_check_returns_blocked_verdict_and_logs_with_lane_when_limit_hit() -> None:
    subjects = BudgetSubjects(
        agent=_agent(tokens_used_today=100_000),
        tenant=_tenant(),
        tenant_counter=_counter(),
    )
    records, handler_id = _capture_logs()

    try:
        verdict = await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)
    finally:
        logger.remove(handler_id)

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.used == 100_000
    assert verdict.limit == 100_000

    warning_logs = [text for level, text in records if level == "WARNING" and "[TokenBudget]" in text]
    assert any("lane=business_step" in text for text in warning_logs), (
        "命中限额的 WARNING 日志必须带 lane= 字段"
    )
    # 其余字段名与顺序必须与 model_step_service._budget_gate 现有实现逐字段一致，
    # 使既有的日志告警规则继续匹配。
    hit_log = next(text for text in warning_logs if "scope=" in text)
    assert f"run_id={RUN_ID}" in hit_log
    assert f"agent_id={AGENT_ID}" in hit_log
    assert "scope=agent_day" in hit_log
    assert "used=100000" in hit_log
    assert "limit=100000" in hit_log
    assert f"mode={MODE_ENFORCE}" in hit_log
    assert "blocked=True" in hit_log


async def test_check_reports_no_agent_id_when_subjects_agent_is_none() -> None:
    """system_scope 链路没有 agent 主体，agent_id 字段应落 None，仍能判 tenant_day。"""
    subjects = BudgetSubjects(agent=None, tenant=_tenant(), tenant_counter=_counter(tokens_used_today=500_000))

    verdict = await check(lane=LANE_GROUP_COMPACT, subjects=subjects, now=NOW, run_id=RUN_ID)

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_TENANT_DAY


# ---------------------------------------------------------------------------
# check(): 两级异常 fail-open
# ---------------------------------------------------------------------------


async def test_check_fails_open_on_programming_error_and_logs_error_with_lane(monkeypatch) -> None:
    async def fake_evaluate(**kwargs):
        del kwargs
        raise TypeError("signature drift")

    monkeypatch.setattr(gate, "evaluate", fake_evaluate)
    subjects = BudgetSubjects(agent=_agent(), tenant=_tenant(), tenant_counter=_counter())
    records, handler_id = _capture_logs()

    try:
        verdict = await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)
    finally:
        logger.remove(handler_id)

    assert verdict.allowed is True
    assert verdict == BudgetVerdict(allowed=True)

    error_logs = [text for level, text in records if level == "ERROR"]
    assert any(
        "token_budget_enforcement_disabled_bug" in text and "lane=business_step" in text for text in error_logs
    )


async def test_check_fails_open_on_transient_error_and_logs_warning_with_lane(monkeypatch) -> None:
    async def fake_evaluate(**kwargs):
        del kwargs
        raise OSError("connection refused")

    monkeypatch.setattr(gate, "evaluate", fake_evaluate)
    subjects = BudgetSubjects(agent=_agent(), tenant=_tenant(), tenant_counter=_counter())
    records, handler_id = _capture_logs()

    try:
        verdict = await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)
    finally:
        logger.remove(handler_id)

    assert verdict.allowed is True
    assert verdict == BudgetVerdict(allowed=True)

    warning_logs = [text for level, text in records if level == "WARNING"]
    assert any(
        "token_budget_enforcement_disabled_transient" in text and "lane=business_step" in text
        for text in warning_logs
    )


# ---------------------------------------------------------------------------
# check(): 软告警去重
# ---------------------------------------------------------------------------


async def test_check_soft_warning_logs_and_calls_dedup_with_unchanged_key(monkeypatch) -> None:
    dedup_calls: list[tuple] = []

    async def fake_should_emit_soft_warning(scope, subject_id, reset_at):
        dedup_calls.append((scope, subject_id, reset_at))
        return True

    monkeypatch.setattr(gate, "should_emit_soft_warning", fake_should_emit_soft_warning)

    subjects = BudgetSubjects(agent=_agent(tokens_used_today=80_000), tenant=_tenant(), tenant_counter=_counter())
    records, handler_id = _capture_logs()

    try:
        verdict = await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)
    finally:
        logger.remove(handler_id)

    assert verdict.allowed is True
    assert verdict.soft_warning is True

    assert dedup_calls == [(verdict.soft_warning_scope, verdict.soft_warning_subject_id, verdict.reset_at)]

    soft_warning_logs = [text for level, text in records if level == "WARNING" and "soft warning" in text]
    assert any("lane=business_step" in text for text in soft_warning_logs)
    assert any(f"run_id={RUN_ID}" in text for text in soft_warning_logs)


async def test_check_soft_warning_not_logged_when_dedup_declines(monkeypatch) -> None:
    """去重认为"这个周期已经告过警"时，不应再落第二条软告警日志。"""

    async def fake_should_emit_soft_warning(scope, subject_id, reset_at):
        del scope, subject_id, reset_at
        return False

    monkeypatch.setattr(gate, "should_emit_soft_warning", fake_should_emit_soft_warning)

    subjects = BudgetSubjects(agent=_agent(tokens_used_today=80_000), tenant=_tenant(), tenant_counter=_counter())
    records, handler_id = _capture_logs()

    try:
        await check(lane=LANE_BUSINESS_STEP, subjects=subjects, now=NOW, run_id=RUN_ID)
    finally:
        logger.remove(handler_id)

    soft_warning_logs = [text for level, text in records if "soft warning" in text]
    assert soft_warning_logs == []


# ---------------------------------------------------------------------------
# BudgetClearance
# ---------------------------------------------------------------------------


def test_budget_clearance_not_applicable_requires_a_reason() -> None:
    clearance = BudgetClearance.not_applicable(LANE_BUSINESS_STEP, reason="platform_admin_no_tenant")

    assert clearance.lane == LANE_BUSINESS_STEP
    assert clearance.verdict is None
    assert clearance.not_applicable_reason == "platform_admin_no_tenant"


def test_budget_clearance_not_applicable_rejects_empty_reason() -> None:
    with pytest.raises(ValueError):
        BudgetClearance.not_applicable(LANE_BUSINESS_STEP, reason="")


# ---------------------------------------------------------------------------
# clearance_from()
# ---------------------------------------------------------------------------


def test_clearance_from_wraps_verdict() -> None:
    verdict = BudgetVerdict(allowed=True)

    clearance = clearance_from(LANE_BUSINESS_STEP, verdict)

    assert clearance.lane == LANE_BUSINESS_STEP
    assert clearance.verdict is verdict
    assert clearance.not_applicable_reason is None


# ---------------------------------------------------------------------------
# load_subjects(): 会话替身
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """记录每次 execute() 的调用，按调用顺序依次返回预置的结果。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.executed_statements: list = []

    async def execute(self, statement):
        self.executed_statements.append(statement)
        return self._results.pop(0)


async def test_load_subjects_packs_tenant_counter_and_passed_in_agent() -> None:
    tenant = _tenant()
    counter = _counter()
    session = _FakeSession([_FakeScalarResult(tenant), _FakeScalarResult(counter)])
    agent = _agent()

    subjects = await load_subjects(session, tenant_id=TENANT_ID, agent=agent)

    assert subjects.agent is agent
    assert subjects.tenant is tenant
    assert subjects.tenant_counter is counter
    assert len(session.executed_statements) == 2


async def test_load_subjects_defaults_agent_to_none() -> None:
    session = _FakeSession([_FakeScalarResult(None), _FakeScalarResult(None)])

    subjects = await load_subjects(session, tenant_id=TENANT_ID)

    assert subjects.agent is None
    assert subjects.tenant is None
    assert subjects.tenant_counter is None
