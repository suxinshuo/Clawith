"""限额在活路径上的执行。

背景：限额判定原本全在 caller.py（无生产调用者），活路径 complete_once 零检查，
所以 token 限额此前在实际运行中完全不生效。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from loguru import logger
from test_agent_runtime_model_step_service import _agent as _real_agent
from test_agent_runtime_model_step_service import _build as _real_build
from test_agent_runtime_model_step_service import _context as _real_context
from test_agent_runtime_model_step_service import (
    _ContextBuilder as _RealContextBuilder,
)
from test_agent_runtime_model_step_service import _model as _real_model
from test_agent_runtime_model_step_service import _service as _real_service
from test_agent_runtime_model_step_service import _state as _real_state
from test_agent_runtime_node_executor import ModelService as _NodeModelService
from test_agent_runtime_node_executor import _context as _node_context
from test_agent_runtime_node_executor import _executor as _build_node_executor
from test_agent_runtime_node_executor import _state as _node_state

from app.services.agent_runtime import model_step_service, node_executor
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_TENANT_DAY,
    BudgetVerdict,
)
from app.services.token_tracker import TokenUsage

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _blocked_verdict() -> BudgetVerdict:
    return BudgetVerdict(
        allowed=False,
        blocked_scope=SCOPE_AGENT_DAY,
        used=100_000,
        limit=100_000,
        reset_at=datetime(2026, 8, 7, 16, 0, tzinfo=UTC),
        mode=MODE_ENFORCE,
    )


def _service() -> model_step_service.RuntimeModelStepService:
    return model_step_service.RuntimeModelStepService(
        session_factory=lambda: None,
        context_builder=SimpleNamespace(build=None),
    )


def _context():
    return SimpleNamespace(
        tenant_id=str(TENANT_ID),
        agent_id=str(AGENT_ID),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )


def _agent():
    return SimpleNamespace(id=AGENT_ID, name="Ada", tenant_id=TENANT_ID, timezone=None)


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    """订阅所有级别的 loguru 记录，返回 (records, handler_id)。"""
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="TRACE",
    )
    return records, handler_id


async def test_budget_gate_returns_an_error_step_when_blocked(monkeypatch) -> None:
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)

    async def fake_evaluate(**kwargs):
        return _blocked_verdict()

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)

    assert result is not None
    assert result.intent == "error"
    assert result.error["code"] == "token_budget_exceeded"
    assert "100,000" in result.error["message"]


async def test_budget_gate_returns_none_when_allowed(monkeypatch) -> None:
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    calls: list[dict[str, object]] = []

    async def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)

    assert result is None
    # 若判定内部抛异常，fail-open 处理同样返回 None——必须证明是判定真的放行了，
    # 不是判定崩溃后被兜底吞掉了。
    assert len(calls) == 1, "evaluate_budget 必须被真正调用，而不是走了 fail-open"


async def test_warn_only_breach_does_not_block(monkeypatch) -> None:
    """新口径数字变大，上线即硬拦会像一次大面积故障。"""
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    calls: list[dict[str, object]] = []

    async def fake_evaluate(**kwargs):
        calls.append(kwargs)
        return BudgetVerdict(
            allowed=True,
            blocked_scope=SCOPE_AGENT_DAY,
            used=100_000,
            limit=100_000,
            reset_at=NOW,
            mode=MODE_WARN_ONLY,
        )

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)

    assert result is None
    assert len(calls) == 1, "evaluate_budget 必须被真正调用，而不是走了 fail-open"


async def test_preflight_estimate_is_passed_through(monkeypatch) -> None:
    """阶段二必须把 prompt 估算值传给判定，否则预检形同虚设。"""
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    captured: dict[str, object] = {}

    async def fake_evaluate(**kwargs):
        captured.update(kwargs)
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=7_777)

    assert captured["estimated_next_round_tokens"] == 7_777


async def test_resolve_budget_subjects_returns_none_for_invalid_tenant_id() -> None:
    service = _service()
    context = SimpleNamespace(
        tenant_id="not-a-uuid",
        agent_id=str(AGENT_ID),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )

    assert await service._resolve_budget_subjects(context) is None


async def test_resolve_budget_subjects_fetches_exactly_once(monkeypatch) -> None:
    service = _service()
    calls: list[uuid.UUID] = []

    async def fake_subjects(tenant_id):
        calls.append(tenant_id)
        return SimpleNamespace(id=TENANT_ID, timezone="UTC"), SimpleNamespace(
            tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW
        )

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)

    result = await service._resolve_budget_subjects(_context())

    assert result is not None
    assert len(calls) == 1
    assert calls[0] == TENANT_ID


async def test_resolve_budget_subjects_programming_error_logs_at_error_and_disables_gating(
    monkeypatch,
) -> None:
    """签名漂移等编程错误必须吵得响，否则限额会像 caller.py 那次一样悄悄永久失效。"""
    service = _service()
    records, handler_id = _capture_logs()

    async def fake_subjects(tenant_id):
        raise TypeError("signature drift")

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)

    try:
        result = await service._resolve_budget_subjects(_context())
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any(level == "ERROR" and "token_budget_enforcement_disabled_bug" in text for level, text in records)


async def test_resolve_budget_subjects_transient_failure_logs_at_warning_and_disables_gating(
    monkeypatch,
) -> None:
    service = _service()
    records, handler_id = _capture_logs()

    async def fake_subjects(tenant_id):
        raise OSError("connection refused")

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)

    try:
        result = await service._resolve_budget_subjects(_context())
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any(level == "WARNING" and "token_budget_enforcement_disabled_transient" in text for level, text in records)


async def test_budget_gate_programming_error_logs_at_error_and_still_allows(monkeypatch) -> None:
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    records, handler_id = _capture_logs()

    async def fake_evaluate(**kwargs):
        raise TypeError("signature drift")

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    try:
        result = await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any(level == "ERROR" and "token_budget_enforcement_disabled_bug" in text for level, text in records)


async def test_budget_gate_transient_failure_logs_at_warning_and_still_allows(monkeypatch) -> None:
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    records, handler_id = _capture_logs()

    async def fake_evaluate(**kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    try:
        result = await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any(level == "WARNING" and "token_budget_enforcement_disabled_transient" in text for level, text in records)


async def test_soft_warning_dedup_uses_the_verdicts_own_scope_and_subject(monkeypatch) -> None:
    """去重键必须跟着触发软告警的 scope/subject 走，不能硬编码成 agent_day/agent.id。

    否则 tenant 级软告警会被错误地按 agent 记账去重：一旦某个 agent 已经因为自己
    的 agent_day 软告警写过这个键，租户级告警就永远发不出去；反过来也会互相压制。
    """
    service = _service()
    tenant = SimpleNamespace(id=TENANT_ID, timezone="UTC")
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=400_000, last_daily_reset=NOW)
    reset_at = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)
    captured: dict[str, object] = {}

    async def fake_evaluate(**kwargs):
        return BudgetVerdict(
            allowed=True,
            mode=MODE_ENFORCE,
            soft_warning=True,
            soft_warning_scope=SCOPE_TENANT_DAY,
            soft_warning_subject_id=TENANT_ID,
            reset_at=reset_at,
        )

    async def fake_should_emit(scope, subject_id, reset_at_arg):
        captured["scope"] = scope
        captured["subject_id"] = subject_id
        captured["reset_at"] = reset_at_arg
        return True

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)
    monkeypatch.setattr(model_step_service, "should_emit_soft_warning", fake_should_emit)

    await service._budget_gate(_context(), _agent(), (tenant, counter), estimated_next_round_tokens=0)

    assert captured["scope"] == SCOPE_TENANT_DAY
    assert captured["subject_id"] == TENANT_ID
    assert captured["reset_at"] == reset_at


async def test_budget_block_reason_reaches_the_lifecycle_as_token_budget_exceeded() -> None:
    """端到端驱动真正的 node_executor：超限的 run 必须落下 reason=token_budget_exceeded。

    超限的 run 若被记成 model_call_failed，会把排查的人带向错误方向。
    """
    run_id = uuid.uuid4()
    executor = _build_node_executor(
        _NodeModelService(
            node_executor.ModelStepResult(
                intent="error",
                error={"code": "token_budget_exceeded", "message": "budget exceeded"},
            )
        )
    )
    context = _node_context(run_id, executor, "command-budget-exceeded")

    update = await executor.execute("model", _node_state(run_id), context)

    assert update["lifecycle"]["status"] == "failed"
    assert update["lifecycle"]["reason"] == "token_budget_exceeded"


async def test_generic_model_failure_reason_reaches_the_lifecycle_as_model_call_failed() -> None:
    """镜像用例：一次真正的模型失败仍必须落下 reason=model_call_failed，不能被误标。"""
    run_id = uuid.uuid4()
    executor = _build_node_executor(
        _NodeModelService(
            node_executor.ModelStepResult(
                intent="error",
                error={"code": "model_call_failed", "message": "provider unavailable"},
            )
        )
    )
    context = _node_context(run_id, executor, "command-model-call-failed")

    update = await executor.execute("model", _node_state(run_id), context)

    assert update["lifecycle"]["status"] == "failed"
    assert update["lifecycle"]["reason"] == "model_call_failed"


def _completion_port() -> AsyncMock:
    return AsyncMock(
        return_value=LLMCompletionStep(
            content="Completed.",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=10),
        )
    )


async def test_complete_once_short_circuits_at_the_first_budget_gate(monkeypatch) -> None:
    """阶段一判定超限时必须直接短路，永远不该走到 provider 请求这一步。"""
    tenant_id = uuid.uuid4()
    model = _real_model(tenant_id)
    agent = _real_agent(tenant_id)
    state = _real_state(tenant_id, model, agent)
    completion = _completion_port()

    async def fake_evaluate(**kwargs):
        if kwargs["estimated_next_round_tokens"] == 0:
            return _blocked_verdict()
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    builder = _RealContextBuilder(_real_build())
    result = await _real_service(model, agent, builder, completion).complete_once(state, _real_context(state))

    assert result.intent == "error"
    assert result.error["code"] == "token_budget_exceeded"
    completion.assert_not_called()
    assert builder.calls == [], "阶段一短路后必须连上下文都不组装——这正是阶段一存在的意义：不做那些注定浪费的准备工作"


async def test_complete_once_short_circuits_at_the_second_budget_gate(monkeypatch) -> None:
    """阶段一放行、阶段二（用真实估算量）判定超限时，仍必须在发起请求前短路。"""
    tenant_id = uuid.uuid4()
    model = _real_model(tenant_id)
    agent = _real_agent(tenant_id)
    state = _real_state(tenant_id, model, agent)
    completion = _completion_port()

    async def fake_evaluate(**kwargs):
        if kwargs["estimated_next_round_tokens"] == 0:
            return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)
        return _blocked_verdict()

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await _real_service(model, agent, _RealContextBuilder(_real_build()), completion).complete_once(
        state, _real_context(state)
    )

    assert result.intent == "error"
    assert result.error["code"] == "token_budget_exceeded"
    completion.assert_not_called()


async def test_complete_once_calls_the_completion_port_when_both_gates_allow(monkeypatch) -> None:
    """镜像用例：两阶段都放行时，请求必须照常走到 provider。"""
    tenant_id = uuid.uuid4()
    model = _real_model(tenant_id)
    agent = _real_agent(tenant_id)
    state = _real_state(tenant_id, model, agent)
    completion = _completion_port()

    async def fake_evaluate(**kwargs):
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await _real_service(model, agent, _RealContextBuilder(_real_build()), completion).complete_once(
        state, _real_context(state)
    )

    assert result.intent == "finish"
    completion.assert_called_once()
