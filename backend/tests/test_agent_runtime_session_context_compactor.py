"""Strict Session Compact model selection and batching tests."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from app.config import Settings
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.services.agent_runtime import session_context_compactor as compactor_module
from app.services.agent_runtime.session_context_compactor import (
    CompactModelSelection,
    LLMSessionContextCompactor,
    SessionContextCompactorError,
)
from app.services.agent_runtime.session_context_completion import SessionCompactRequest
from app.services.agent_runtime.session_context_service import (
    SessionContextDelta,
    SessionContextSnapshot,
)
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_accounting import gate as gate_module
from app.services.token_accounting.budget import MODE_ENFORCE
from app.services.token_accounting.gate import BudgetSubjects
from app.services.token_accounting.ledger import SYSTEM_SCOPE_GROUP_COMPACT
from app.services.token_tracker import TokenUsage


def _model(tenant_id: uuid.UUID, *, name: str, input_tokens: int = 100_000) -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model=name,
        label=name,
        api_key_encrypted="encrypted",
        enabled=True,
        max_input_tokens=input_tokens,
        max_output_tokens=256,
    )


def _request(*, messages: tuple[dict, ...] = ()) -> SessionCompactRequest:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    return SessionCompactRequest(
        tenant_id=tenant_id,
        session_id=uuid.uuid4(),
        source_agent_id=uuid.uuid4(),
        checkpoint_id="checkpoint-terminal",
        snapshot=SessionContextSnapshot(
            version=2,
            summary="old summary",
            requirements=("keep wording",),
            decisions=(),
            open_items=("old question",),
            evidence_refs=(),
            workspace_refs=(),
            covered_through_message_id=None,
        ),
        messages=messages,
        delta=SessionContextDelta(
            source_run_id=run_id,
            new_requirements=(),
            new_decisions=("use checkpoint",),
            resolved_open_items=("old question",),
            new_open_items=("ship",),
            evidence_refs=("checkpoint://terminal",),
            workspace_refs=("workspace://runtime",),
            result_summary="answer completed",
        ),
    )


def _step(summary: str = "compacted") -> LLMCompletionStep:
    arguments = {
        "summary": summary,
        "requirements": ["keep wording"],
        "decisions": ["use checkpoint"],
        "open_items": ["ship"],
        "evidence_refs": ["checkpoint://terminal"],
        "workspace_refs": ["workspace://runtime"],
    }
    return LLMCompletionStep(
        content="",
        tool_calls=(
            {
                "id": "compact-call",
                "type": "function",
                "function": {
                    "name": "commit_session_context",
                    "arguments": json.dumps(arguments),
                },
            },
        ),
        reasoning_content=None,
        retry_instruction=None,
        usage=TokenUsage(total_tokens=10),
    )


def _resolver(selection: CompactModelSelection):
    async def resolve(request: SessionCompactRequest) -> CompactModelSelection:
        del request
        return selection

    return resolve


class _UnusedSessionFactory:
    def __call__(self):
        raise AssertionError("injected model resolver must avoid database access")


class _Result:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.value) if isinstance(self.value, (list, tuple)) else []


class _DB:
    def __init__(self, *values) -> None:
        self.results = deque(_Result(value) for value in values)
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected database query")
        return self.results.popleft()


def _session_factory(db):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


@pytest.mark.asyncio
async def test_compact_accepts_only_the_commit_tool_and_sets_code_owned_watermark() -> None:
    message_id = uuid.uuid4()
    request = _request(
        messages=(
            {
                "id": str(message_id),
                "role": "assistant",
                "content": "done",
            },
        )
    )
    model = _model(request.tenant_id, name="compact-primary")
    calls = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=request.source_agent_id,
            )
        ),
        completion=complete,
    )

    candidate = await compactor.compact(request)

    assert candidate.summary == "compacted"
    assert candidate.covered_through_message_id == message_id
    assert len(calls) == 1
    assert calls[0][2]["agent_id"] == request.source_agent_id
    assert calls[0][2]["tools"][0]["function"]["name"] == "commit_session_context"
    # Direct (per-agent) Session Compact must still attribute to the Agent,
    # not to the tenant-level system-overhead scope.
    assert calls[0][2]["tenant_id"] == request.tenant_id
    assert calls[0][2]["system_scope"] is None


@pytest.mark.asyncio
async def test_group_compact_resolves_the_tenant_scoped_context_model(monkeypatch) -> None:
    request = _request()
    group_session = ChatSession(
        id=request.session_id,
        tenant_id=request.tenant_id,
        session_type="group",
        group_id=uuid.uuid4(),
        title="Group",
        source_channel="web",
        is_group=True,
        is_primary=True,
    )
    platform_model = _model(request.tenant_id, name="group-compact")
    platform_model.tenant_id = None
    settings = Settings(MULTI_AGENT_COMPACT_MODEL_ID=platform_model.id)
    tenant_stub = SimpleNamespace(id=request.tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter_stub = SimpleNamespace(
        tenant_id=request.tenant_id, tokens_used_today=0, last_daily_reset=None
    )
    # `_resolve_models` now also calls `gate.load_subjects(db, ...)` in the same
    # session (task 6.2), which issues two more SELECTs (Tenant, TenantTokenCounter)
    # after the ChatSession lookup.
    db = _DB(group_session, tenant_stub, counter_stub)
    resolver_calls = []

    async def resolve(db_arg, settings_arg, *, tenant_id):
        resolver_calls.append((db_arg, settings_arg, tenant_id))
        return platform_model

    monkeypatch.setattr(
        compactor_module,
        "resolve_multi_agent_compact_model",
        resolve,
    )
    compactor = LLMSessionContextCompactor(
        session_factory=_session_factory(db),  # type: ignore[arg-type]
        settings=settings,
    )

    selection = await compactor._resolve_models(request)  # type: ignore[attr-defined]

    assert selection.primary is platform_model
    assert selection.usage_agent_id is None
    assert selection.system_scope == SYSTEM_SCOPE_GROUP_COMPACT
    assert resolver_calls == [(db, settings, request.tenant_id)]
    assert selection.subjects is not None
    assert selection.subjects.agent is None
    assert selection.subjects.tenant is tenant_stub
    assert selection.subjects.tenant_counter is counter_stub
    assert db.calls == 3
    assert not db.results


@pytest.mark.asyncio
async def test_direct_compact_resolves_active_model_candidates() -> None:
    request = _request()
    primary = _model(request.tenant_id, name="current-primary")
    agent = Agent(
        id=request.source_agent_id,
        tenant_id=request.tenant_id,
        creator_id=uuid.uuid4(),
        name="Direct Agent",
        status="idle",
        is_expired=False,
        primary_model_id=primary.id,
        fallback_model_id=uuid.uuid4(),
    )
    direct_session = ChatSession(
        id=request.session_id,
        tenant_id=request.tenant_id,
        session_type="direct",
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        title="Direct",
        source_channel="web",
        is_primary=True,
    )
    tenant_stub = SimpleNamespace(id=request.tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter_stub = SimpleNamespace(
        tenant_id=request.tenant_id, tokens_used_today=0, last_daily_reset=None
    )
    # After resolving the primary model, `_resolve_models` now also calls
    # `gate.load_subjects(db, ..., agent=agent)` in the same session (task 6.2),
    # which issues two more SELECTs (Tenant, TenantTokenCounter).
    db = _DB(direct_session, agent, None, [primary], tenant_stub, counter_stub)
    compactor = LLMSessionContextCompactor(
        session_factory=_session_factory(db),  # type: ignore[arg-type]
    )

    selection = await compactor._resolve_models(  # type: ignore[attr-defined]
        replace(request, source_agent_id=agent.id)
    )

    assert selection.primary is primary
    assert selection.usage_agent_id == agent.id
    assert selection.system_scope is None
    assert selection.subjects is not None
    assert selection.subjects.agent is agent
    assert selection.subjects.tenant is tenant_stub
    assert selection.subjects.tenant_counter is counter_stub
    assert db.calls == 6
    assert not db.results


@pytest.mark.asyncio
async def test_group_compact_attributes_usage_to_the_tenant_scope_not_an_agent() -> None:
    """群聊压缩此前把 usage_agent_id=None 直接传给 complete_llm_once,零记录.

    现在必须带上 tenant_id 与 system_scope=group_compact,且绝不能带 agent_id.
    """
    request = _request()
    model = _model(request.tenant_id, name="group-compact")
    calls = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=None,
                system_scope=SYSTEM_SCOPE_GROUP_COMPACT,
            )
        ),
        completion=complete,
    )

    await compactor.compact(request)

    assert len(calls) == 1
    assert calls[0][2]["agent_id"] is None
    assert calls[0][2]["tenant_id"] == request.tenant_id
    assert calls[0][2]["system_scope"] == SYSTEM_SCOPE_GROUP_COMPACT


@pytest.mark.asyncio
async def test_oversized_session_is_compacted_in_complete_message_batches() -> None:
    message_ids = [uuid.uuid4(), uuid.uuid4()]
    request = _request(
        messages=tuple(
            {
                "id": str(message_id),
                "role": "user",
                "content": character * 4_000,
            }
            for message_id, character in zip(message_ids, ("a", "b"), strict=True)
        )
    )
    model = _model(request.tenant_id, name="small-compact", input_tokens=3_000)
    payloads: list[dict] = []

    async def complete(_model, messages, **_kwargs):
        payloads.append(json.loads(messages[1].content))
        return _step(summary=f"batch-{len(payloads)}")

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(CompactModelSelection(primary=model, usage_agent_id=request.source_agent_id)),
        completion=complete,
    )

    candidate = await compactor.compact(request)

    assert len(payloads) == 2
    assert [len(payload["new_messages"]) for payload in payloads] == [1, 1]
    assert payloads[0]["terminal_delta"] is not None
    assert payloads[1]["terminal_delta"] is None
    assert candidate.covered_through_message_id == message_ids[-1]


@pytest.mark.asyncio
async def test_retryable_failure_does_not_switch_session_compact_models() -> None:
    request = _request()
    primary = _model(request.tenant_id, name="primary")
    called: list[uuid.UUID] = []

    async def complete(model, _messages, **_kwargs):
        called.append(model.id)
        raise TimeoutError("provider timeout")

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=primary,
                usage_agent_id=request.source_agent_id,
            )
        ),
        completion=complete,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "session_compact_model_failed"
    assert called == [primary.id]


@pytest.mark.asyncio
async def test_non_retryable_compact_failure_keeps_the_previous_context() -> None:
    request = _request()
    primary = _model(request.tenant_id, name="primary")
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("invalid API key")

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(CompactModelSelection(primary=primary, usage_agent_id=request.source_agent_id)),
        completion=complete,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "session_compact_model_failed"
    assert calls == 1


@pytest.mark.asyncio
async def test_free_text_compact_output_is_rejected_without_repair_loop() -> None:
    request = _request()
    model = _model(request.tenant_id, name="primary")

    async def complete(*_args, **_kwargs):
        return LLMCompletionStep(
            content="looks good",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=2),
        )

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(CompactModelSelection(primary=model, usage_agent_id=request.source_agent_id)),
        completion=complete,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "invalid_session_compact_output"


def _breached_agent_subjects(*, tenant_id: uuid.UUID) -> BudgetSubjects:
    """A subjects triple whose agent day limit is already breached.

    `last_daily_reset` is anchored to the real wall clock (not a fixed past
    date) because `_compact_with_model` -> `gate.check()` does not pass an
    explicit `now`, so `budget.evaluate()` uses `datetime.now(UTC)`. A fixed
    past timestamp would eventually look like a stale/rolled-over period and
    the breach would silently disappear as effective_used resets to 0 (same
    issue documented in test_agent_runtime_run_compactor.py for task 6.1).
    """
    now = datetime.now(UTC)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        timezone=None,
        max_tokens_per_day=100_000,
        max_tokens_per_month=None,
        tokens_used_today=200_000,
        tokens_used_month=0,
        last_daily_reset=now,
        last_monthly_reset=now,
    )
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=now)
    return BudgetSubjects(agent=agent, tenant=tenant, tenant_counter=counter)


def _breached_tenant_subjects(*, tenant_id: uuid.UUID) -> BudgetSubjects:
    """A subjects triple with no agent (group_compact) whose tenant day limit is breached."""
    now = datetime.now(UTC)
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=500_000)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=500_000, last_daily_reset=now)
    return BudgetSubjects(agent=None, tenant=tenant, tenant_counter=counter)


def _clear_agent_subjects(*, tenant_id: uuid.UUID) -> BudgetSubjects:
    """A subjects triple with no limits configured (never breached), agent-scoped."""
    now = datetime.now(UTC)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        timezone=None,
        max_tokens_per_day=None,
        max_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        last_daily_reset=now,
        last_monthly_reset=now,
    )
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=now)
    return BudgetSubjects(agent=agent, tenant=tenant, tenant_counter=counter)


def _clear_tenant_subjects(*, tenant_id: uuid.UUID) -> BudgetSubjects:
    """A subjects triple with no limits configured (never breached), group-scoped."""
    now = datetime.now(UTC)
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=now)
    return BudgetSubjects(agent=None, tenant=tenant, tenant_counter=counter)


def _forced_enforce(monkeypatch) -> None:
    """Pin the execution mode to enforce so these tests don't drift with the
    configured default / cache state (same approach as task 6.1's tests)."""
    original_evaluate = gate_module.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await original_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate_module, "evaluate", forced_enforce_evaluate)


@pytest.mark.asyncio
async def test_breached_agent_budget_blocks_session_compact_before_completion(
    monkeypatch,
) -> None:
    """Task 6.2: a breached Agent must block session_compact before any provider call.

    This is Counterexample 4 (session_compact half) from task 1 turned into a
    positive assertion: `_compact_with_model` now judges the budget before the
    first batch, so a breached Agent must raise
    `SessionContextCompactorError("token_budget_exceeded")` and never call the
    completion port.
    """
    _forced_enforce(monkeypatch)
    request = _request()
    model = _model(request.tenant_id, name="compact-primary")
    calls: list[tuple] = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=request.source_agent_id,
                subjects=_breached_agent_subjects(tenant_id=request.tenant_id),
            )
        ),
        completion=complete,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "token_budget_exceeded"
    assert calls == [], (
        "completion port must not be called once the budget gate blocks the "
        "session_compact request"
    )


@pytest.mark.asyncio
async def test_breached_tenant_budget_blocks_group_compact_before_completion(
    monkeypatch,
) -> None:
    """Task 6.2: a breached tenant_day limit must block group_compact.

    This is Counterexample 4 (group_compact half) from task 1 turned into a
    positive assertion. `agent=None` and `system_scope=group_compact` select
    the `LANE_GROUP_COMPACT` lane, which only judges `tenant_day` (task 3.1).
    """
    _forced_enforce(monkeypatch)
    request = _request()
    model = _model(request.tenant_id, name="group-compact")
    calls: list[tuple] = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=None,
                system_scope=SYSTEM_SCOPE_GROUP_COMPACT,
                subjects=_breached_tenant_subjects(tenant_id=request.tenant_id),
            )
        ),
        completion=complete,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "token_budget_exceeded"
    assert calls == [], (
        "completion port must not be called once the budget gate blocks the "
        "group_compact request"
    )


@pytest.mark.asyncio
async def test_unbreached_agent_budget_allows_session_compact_to_call_completion(
    monkeypatch,
) -> None:
    """Guard rail: the new gate must not block the normal session_compact path."""
    _forced_enforce(monkeypatch)
    message_id = uuid.uuid4()
    request = _request(
        messages=(
            {"id": str(message_id), "role": "assistant", "content": "done"},
        )
    )
    model = _model(request.tenant_id, name="compact-primary")
    calls: list[tuple] = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=request.source_agent_id,
                subjects=_clear_agent_subjects(tenant_id=request.tenant_id),
            )
        ),
        completion=complete,
    )

    candidate = await compactor.compact(request)

    assert candidate.summary == "compacted"
    assert len(calls) == 1, "completion port must still be called when the gate allows"


@pytest.mark.asyncio
async def test_unbreached_tenant_budget_allows_group_compact_to_call_completion(
    monkeypatch,
) -> None:
    """Guard rail: the new gate must not block the normal group_compact path."""
    _forced_enforce(monkeypatch)
    request = _request()
    model = _model(request.tenant_id, name="group-compact")
    calls: list[tuple] = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=model,
                usage_agent_id=None,
                system_scope=SYSTEM_SCOPE_GROUP_COMPACT,
                subjects=_clear_tenant_subjects(tenant_id=request.tenant_id),
            )
        ),
        completion=complete,
    )

    await compactor.compact(request)

    assert len(calls) == 1, "completion port must still be called when the gate allows"


@pytest.mark.asyncio
async def test_missing_subjects_does_not_block_session_compact() -> None:
    """Backward compatibility: `subjects=None` (test doubles that construct
    `CompactModelSelection` directly) must not fail closed — same rationale as
    `RunCompactInputs.subjects is None` from task 6.1."""
    request = _request()
    primary = _model(request.tenant_id, name="primary")

    async def complete(*_args, **_kwargs):
        return _step()

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=_resolver(
            CompactModelSelection(
                primary=primary,
                usage_agent_id=request.source_agent_id,
                subjects=None,
            )
        ),
        completion=complete,
    )

    candidate = await compactor.compact(request)

    assert candidate.summary == "compacted"
