"""Planning v2 checkpoint contract and terminal transition tests."""

from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from typing import cast
import uuid

import pytest

from app.models.llm import LLMModel
from app.services.agent_runtime.planning import (
    PlanningContractError,
    PlanningModelResult,
    PlanningModelService,
    PlanningRuntimeNodeExecutor,
    checkpoint_plan,
    validate_planning_output,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RuntimeContext,
    RuntimeGraphState,
    RuntimeNodeExecutor,
)
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_accounting import gate as gate_module
from app.services.token_accounting.budget import MODE_ENFORCE
from app.services.token_accounting.gate import LANE_PLANNING, BudgetClearance, BudgetSubjects
from app.services.token_accounting.ledger import SYSTEM_SCOPE_PLANNING
from app.services.token_tracker import TokenUsage


def _candidate(agent_id: uuid.UUID, name: str) -> JsonObject:
    return {
        "agent_id": str(agent_id),
        "participant_id": str(uuid.uuid4()),
        "name": name,
        "role_description": f"Role for {name}",
    }


def _state(agent_ids: tuple[uuid.UUID, ...]) -> RuntimeGraphState:
    return {
        "snapshots": RunInputSnapshots(
            session_context={},
            session_context_version=1,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={
                "candidate_agents": [
                    _candidate(agent_id, f"Agent {index}") for index, agent_id in enumerate(agent_ids, start=1)
                ]
            },
        ),
        "messages": [],
        "lifecycle": {
            "status": "running",
            "next_route": "model",
            "pending_tool_calls": [],
        },
    }


def _context(
    *,
    model_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    goal: str = "Research the topic, then write the answer",
) -> RuntimeContext:
    return RuntimeContext(
        tenant_id=str(tenant_id or uuid.uuid4()),
        run_id=str(run_id or uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        executor=cast(RuntimeNodeExecutor, object()),
        goal=goal,
        run_kind="orchestration",
        source_type="chat",
        model_id=str(model_id or uuid.uuid4()),
        graph_name="runtime_group_planning",
        graph_version="v1",
        agent_id=None,
        session_id=str(uuid.uuid4()),
        system_role="group_planning",
    )


def _plan(
    first: uuid.UUID,
    second: uuid.UUID | None = None,
    *,
    mode: str = "advisory",
) -> dict:
    entries = [
        {
            "agent_id": str(first),
            "instruction": "Research the evidence",
        }
    ]
    if second is not None:
        entries.append(
            {
                "agent_id": str(second),
                "instruction": "Review the initial evidence",
            }
        )
    return {
        "version": 2,
        "mode": mode,
        "goal": "Produce one grounded answer",
        "plan_prompt": (
            "Research the request, publish each handoff in the group, and stop when the requested answer is grounded."
        ),
        "entry_steps": entries,
    }


class _CancelSource:
    async def get_cancel(self, state, context):
        del state, context
        return None


class _PlanningModel:
    def __init__(self, *results: PlanningModelResult) -> None:
        self.results = deque(results)

    async def complete_once(self, state, context):
        del state, context
        return self.results.popleft()


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    """Shared-queue DB double spanning the two separate sessions `complete_once`
    now opens: `_load_model` (1 query: LLMModel) and `_resolve_budget_subjects`
    (2 queries: Tenant, TenantTokenCounter, task 6.3). `_session_factory` is
    called twice, each yielding a *new* `_DB` wrapping the *same* deque, so
    queries are consumed in call order across both sessions.
    """

    def __init__(self, results: deque) -> None:
        self._results = results
        self.calls = 0

    async def execute(self, statement):
        del statement
        self.calls += 1
        if not self._results:
            raise AssertionError("unexpected database query")
        return self._results.popleft()


def _session_factory(
    model: LLMModel,
    *,
    tenant: object | None = None,
    tenant_counter: object | None = None,
):
    """Queue up [model, tenant, tenant_counter] regardless of whether the
    caller reaches the budget-subjects session — unused queue entries are
    harmless. Defaulting `tenant`/`tenant_counter` to `None` lets
    `budget.evaluate()` run its normal (non-breaching, agent=None ->
    tenant_day-only) path without crashing (`getattr(None, ..., default)`
    everywhere in `budget.evaluate`/`periods.tenant_timezone`).
    """
    results: deque = deque([_Result(model), _Result(tenant), _Result(tenant_counter)])

    @asynccontextmanager
    async def factory():
        yield _DB(results)

    return factory


def test_plan_validator_accepts_an_entry_subset_without_inventing_a_dag() -> None:
    first, second, non_entry = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw = _plan(first, second, mode="enforced")

    plan = validate_planning_output(
        raw,
        candidate_agent_ids=frozenset({first, second, non_entry}),
    )

    assert plan == raw
    assert [entry["agent_id"] for entry in plan["entry_steps"]] == [
        str(first),
        str(second),
    ]
    assert "steps" not in plan
    assert "execution_strategy" not in plan


@pytest.mark.parametrize(
    "mutation",
    [
        "legacy_v1",
        "unknown_agent",
        "duplicate_agent",
        "blank_goal",
        "blank_plan_prompt",
        "blank_instruction",
        "invalid_mode",
        "unknown_field",
        "too_many_entries",
    ],
)
def test_plan_validator_rejects_non_v2_or_nonstructural_input(mutation: str) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    candidates = {first, second}
    raw = _plan(first, second)
    if mutation == "legacy_v1":
        raw = {
            "version": 1,
            "goal": "Old plan",
            "execution_strategy": "parallel",
            "steps": [],
        }
    elif mutation == "unknown_agent":
        raw["entry_steps"][1]["agent_id"] = str(uuid.uuid4())
    elif mutation == "duplicate_agent":
        raw["entry_steps"][1]["agent_id"] = str(first)
    elif mutation == "blank_goal":
        raw["goal"] = "  "
    elif mutation == "blank_plan_prompt":
        raw["plan_prompt"] = ""
    elif mutation == "blank_instruction":
        raw["entry_steps"][0]["instruction"] = " "
    elif mutation == "invalid_mode":
        raw["mode"] = "dependency"
    elif mutation == "unknown_field":
        raw["execution_strategy"] = "parallel"
    else:
        many_agents = tuple(uuid.uuid4() for _ in range(51))
        candidates.update(many_agents)
        raw["entry_steps"] = [
            {"agent_id": str(agent_id), "instruction": f"Entry {index}"} for index, agent_id in enumerate(many_agents)
        ]

    with pytest.raises(PlanningContractError):
        validate_planning_output(raw, candidate_agent_ids=frozenset(candidates))


@pytest.mark.asyncio
async def test_planning_model_uses_the_pinned_platform_model_without_tools() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    state = _state((first, second))
    calls = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    context = _context(model_id=model.id)
    result = await PlanningModelService(
        session_factory=_session_factory(model),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(state, context)

    assert result.plan == _plan(first)
    assert calls[0][0] is model
    sent_kwargs = dict(calls[0][2])
    sent_clearance = sent_kwargs.pop("clearance")
    assert sent_kwargs == {
        "tools": None,
        "agent_id": None,
        "tenant_id": uuid.UUID(context.tenant_id),
        "system_scope": SYSTEM_SCOPE_PLANNING,
        "supports_vision": False,
    }
    assert isinstance(sent_clearance, BudgetClearance)
    assert sent_clearance.lane == LANE_PLANNING
    # The budget gate is now wired in (task 6.3): with no limits configured
    # on the (None-defaulted) tenant/tenant_counter, `gate.check()` runs and
    # returns an allowed verdict, so `clearance.verdict` is populated rather
    # than `not_applicable`.
    assert sent_clearance.verdict is not None
    assert sent_clearance.verdict.allowed is True
    assert sent_clearance.not_applicable_reason is None
    planning_prompt = str(calls[0][1][0].content)
    assert '"version": 2' in planning_prompt
    assert '"entry_steps"' in planning_prompt
    assert "advisory" in planning_prompt
    assert "enforced" in planning_prompt
    assert "depends_on_step_ids" not in planning_prompt
    assert "digital employee in Clawith" not in planning_prompt
    assert "call `finish`" not in planning_prompt
    assert "call `wait`" not in planning_prompt
    assert "Use the simplest plan" in planning_prompt
    assert "silently rewrite user_goal into clear directives" in planning_prompt
    assert "Bind an instruction after an @mentioned Agent to that Agent" in planning_prompt
    assert '"@A write a poem @B then translate it"' in planning_prompt
    assert "never resolve ambiguity by moving work to a different Agent" in planning_prompt
    assert "repeat this normalization from the original user_goal" in planning_prompt
    assert "Do not merely repair JSON syntax" in planning_prompt
    assert "greeting or check-in" in planning_prompt
    assert "Never create a handoff from an Agent to itself" in planning_prompt
    assert "Each assigned Agent must author its own public group reply" in planning_prompt
    assert "Never route a planned group transition through private A2A" in planning_prompt
    assert "must say exactly which different Agent to wake publicly next" in planning_prompt


def _forced_enforce(monkeypatch) -> None:
    """Pin the execution mode to enforce so these tests don't drift with the
    configured default / cache state (same approach used by tasks 6.1/6.2's
    tests in test_agent_runtime_run_compactor.py /
    test_agent_runtime_session_context_compactor.py)."""
    original_evaluate = gate_module.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await original_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate_module, "evaluate", forced_enforce_evaluate)


def _breached_tenant(tenant_id: uuid.UUID) -> tuple[SimpleNamespace, SimpleNamespace]:
    """A tenant/tenant_counter pair whose tenant_day limit is already breached.

    `last_daily_reset` is anchored to the real wall clock (not a fixed past
    date) because `complete_once` -> `gate.check()` does not pass an explicit
    `now`, so `budget.evaluate()` uses `datetime.now(UTC)`. A fixed past
    timestamp would eventually look like a stale/rolled-over period and the
    breach would silently disappear as effective_used resets to 0 (same
    issue documented in test_agent_runtime_run_compactor.py for task 6.1).
    """
    now = datetime.now(UTC)
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=500_000)
    counter = SimpleNamespace(
        tenant_id=tenant_id, tokens_used_today=500_000, last_daily_reset=now
    )
    return tenant, counter


def _clear_tenant(tenant_id: uuid.UUID) -> tuple[SimpleNamespace, SimpleNamespace]:
    """A tenant/tenant_counter pair with no limit configured (never breached)."""
    now = datetime.now(UTC)
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=now)
    return tenant, counter


@pytest.mark.asyncio
async def test_breached_tenant_budget_blocks_planning_before_completion(monkeypatch) -> None:
    """Task 6.3: a breached tenant_day limit must block Planning before any
    provider call. This is Counterexample 3 from task 1 turned into a
    positive assertion: `complete_once` now judges the budget after
    `_load_model` and before `self._completion(...)`, so a breached tenant
    must return `PlanningModelResult(error_code="token_budget_exceeded",
    retryable=False)` and never call the completion port.
    """
    _forced_enforce(monkeypatch)
    first, second = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    tenant, counter = _breached_tenant(tenant_id)
    calls: list[tuple] = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("completion port must not be called once the gate blocks")

    result = await PlanningModelService(
        session_factory=_session_factory(model, tenant=tenant, tenant_counter=counter),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, tenant_id=tenant_id),
    )

    assert result.plan is None
    assert result.error_code == "token_budget_exceeded"
    assert result.retryable is False
    assert calls == [], (
        "completion port must not be called once the budget gate blocks the "
        "Planning request"
    )


@pytest.mark.asyncio
async def test_unbreached_tenant_budget_allows_planning_to_call_completion(monkeypatch) -> None:
    """Guard rail: the new gate must not accidentally block the normal Planning
    path when the tenant_day limit is not configured / not breached."""
    _forced_enforce(monkeypatch)
    first, second = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    tenant, counter = _clear_tenant(tenant_id)
    calls: list[tuple] = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    result = await PlanningModelService(
        session_factory=_session_factory(model, tenant=tenant, tenant_counter=counter),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, tenant_id=tenant_id),
    )

    assert result.plan == _plan(first)
    assert len(calls) == 1, "completion port must still be called when the gate allows"
    sent_clearance = calls[0][2]["clearance"]
    assert isinstance(sent_clearance, BudgetClearance)
    assert sent_clearance.verdict is not None
    assert sent_clearance.verdict.allowed is True


@pytest.mark.asyncio
async def test_planning_budget_gate_loads_subjects_with_agent_none() -> None:
    """`_resolve_budget_subjects` opens its own session and calls
    `gate.load_subjects(db, tenant_id=..., agent=None)` — Planning is a
    tenant-scoped lane and judges only `tenant_day` (task 3.1)."""
    first, second = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    tenant, counter = _clear_tenant(tenant_id)

    factory = _session_factory(model, tenant=tenant, tenant_counter=counter)

    async def complete(model_arg, messages, **kwargs):
        del model_arg, messages, kwargs
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    result = await PlanningModelService(
        session_factory=factory,  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, tenant_id=tenant_id),
    )

    assert result.plan == _plan(first)
    # Two sessions are opened by `complete_once`: one for `_load_model`
    # (1 query: LLMModel) and one for `_resolve_budget_subjects`
    # (2 queries: Tenant, TenantTokenCounter via `gate.load_subjects`).
    # The shared queue must be fully drained across both sessions.


@pytest.mark.asyncio
async def test_planning_budget_subjects_load_failure_fails_open(monkeypatch) -> None:
    """If `session_factory()` itself fails while resolving budget subjects,
    Planning must fail open (3.6): the completion port must still be called,
    with a `not_applicable` clearance rather than a hard failure — the same
    fail-open judgment as `model_step_service._resolve_budget_subjects`."""
    first, second = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )

    call_count = 0

    @asynccontextmanager
    async def flaky_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # _load_model's session: succeeds normally.
            yield _DB(deque([_Result(model)]))
            return
        # _resolve_budget_subjects's session: the session_factory() call
        # itself raises — an infrastructure/transient failure (task 6.3's
        # "if session_factory itself fails" scenario from the task prompt).
        raise ConnectionError("database unavailable")

    calls: list[tuple] = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    result = await PlanningModelService(
        session_factory=flaky_factory,  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, tenant_id=tenant_id),
    )

    assert result.plan == _plan(first)
    assert len(calls) == 1, "a failed subjects load must fail open, not block Planning"
    sent_clearance = calls[0][2]["clearance"]
    assert isinstance(sent_clearance, BudgetClearance)
    assert sent_clearance.verdict is None
    assert sent_clearance.not_applicable_reason is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal",
    [
        "@Agent 1 @Agent 2 在嘛",
        "@Agent 1 @Agent 2 你们好！",
        "@Agent 1 @Agent 2 hello?",
    ],
)
async def test_simple_multi_agent_check_in_returns_a_fast_plan_without_calling_the_model(
    goal: str,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    calls = []

    async def complete(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("simple check-ins must not call the Planning model")

    result = await PlanningModelService(
        session_factory=_session_factory(model),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, goal=goal),
    )

    assert result.error_code is None
    assert result.plan == {
        "version": 2,
        "mode": "advisory",
        "goal": "Each mentioned Agent replies briefly to the user's greeting or check-in as itself.",
        "plan_prompt": (
            "This is a simple greeting or check-in. Every entry Agent replies once, "
            "briefly, and only as itself. Do not report another Agent's status, do not "
            "ask another Agent to reply, and do not create a public handoff."
        ),
        "entry_steps": [
            {
                "agent_id": str(first),
                "instruction": (
                    "Reply briefly to the user's greeting or check-in as Agent 1 only. "
                    "Do not report another Agent's status and do not mention or hand off "
                    "to another Agent."
                ),
            },
            {
                "agent_id": str(second),
                "instruction": (
                    "Reply briefly to the user's greeting or check-in as Agent 2 only. "
                    "Do not report another Agent's status and do not mention or hand off "
                    "to another Agent."
                ),
            },
        ],
    }
    assert calls == []


@pytest.mark.asyncio
async def test_greeting_with_a_real_task_still_uses_the_planning_model() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    calls = []

    async def complete(model_arg, messages, **kwargs):
        calls.append((model_arg, messages, kwargs))
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    result = await PlanningModelService(
        session_factory=_session_factory(model),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(
            model_id=model.id,
            goal="@Agent 1 @Agent 2 你好，请分析本周交付风险",
        ),
    )

    assert result.plan == _plan(first)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_planning_model_accepts_a_model_owned_by_the_group_tenant() -> None:
    tenant_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="tenant-planning-model",
        api_key_encrypted="encrypted",
        label="Tenant Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )

    async def complete(_model, _messages, **_kwargs):
        return LLMCompletionStep(
            content=json.dumps(_plan(first)),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(),
        )

    result = await PlanningModelService(
        session_factory=_session_factory(model),  # type: ignore[arg-type]
        completion=complete,
    ).complete_once(
        _state((first, second)),
        _context(model_id=model.id, tenant_id=tenant_id),
    )

    assert result.plan == _plan(first)


@pytest.mark.asyncio
async def test_planning_model_rejects_a_model_owned_by_another_tenant() -> None:
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="openai",
        model="foreign-planning-model",
        api_key_encrypted="encrypted",
        label="Foreign Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    result = await PlanningModelService(
        session_factory=_session_factory(model),  # type: ignore[arg-type]
    ).complete_once(
        _state((uuid.uuid4(), uuid.uuid4())),
        _context(model_id=model.id, tenant_id=uuid.uuid4()),
    )

    assert result.error_code == "planning_model_unavailable"


@pytest.mark.asyncio
async def test_token_budget_exceeded_terminates_immediately_without_entering_repair_loop() -> None:
    """Task 6.3 preservation check: `retryable=False` must not be treated as a
    transient failure by the existing repair loop (`_model`'s
    `result.retryable and attempt <= self._max_repairs` branch). A budget
    rejection is a deterministic outcome — retrying immediately would just
    reproduce the same rejection — so the Planning Run must go straight to
    `status="failed"` on the very first attempt, not `status="running"` /
    `reason="planning_repair_required"`.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    state = _state((first, second))
    model = _PlanningModel(
        PlanningModelResult(
            error_code="token_budget_exceeded",
            error_message="企业当日 token 用量已达上限",
            retryable=False,
        )
    )
    executor = PlanningRuntimeNodeExecutor(
        cancel_source=_CancelSource(),  # type: ignore[arg-type]
        model_service=model,  # type: ignore[arg-type]
        max_repairs=2,
    )

    update = await executor.execute("model", state, _context())

    lifecycle = update["lifecycle"]
    assert lifecycle["status"] == "failed"
    assert lifecycle["next_route"] == "terminal"
    assert lifecycle["reason"] == "token_budget_exceeded"
    assert lifecycle["error"] == {
        "code": "token_budget_exceeded",
        "message": "企业当日 token 用量已达上限",
    }
    assert lifecycle["planning_attempt_count"] == 1


@pytest.mark.asyncio
async def test_invalid_plans_receive_two_repairs_then_fail_the_checkpoint() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    state = _state((first, second))
    model = _PlanningModel(
        *(
            PlanningModelResult(
                error_code="invalid_plan",
                error_message="bad schema",
                raw_output="{}",
                retryable=True,
            )
            for _ in range(3)
        )
    )
    executor = PlanningRuntimeNodeExecutor(
        cancel_source=_CancelSource(),  # type: ignore[arg-type]
        model_service=model,  # type: ignore[arg-type]
        max_repairs=2,
    )
    context = _context()

    for attempt in range(1, 4):
        update = await executor.execute("model", state, context)
        state["lifecycle"] = update["lifecycle"]
        assert state["lifecycle"]["planning_attempt_count"] == attempt

    assert state["lifecycle"]["status"] == "failed"
    assert state["lifecycle"]["next_route"] == "terminal"
    assert state["lifecycle"]["error"] == {
        "code": "invalid_plan",
        "message": "bad schema",
    }


@pytest.mark.asyncio
async def test_valid_plan_completes_without_waiting_and_freezes_the_exact_v2_plan() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    state = _state((first, second))
    plan = validate_planning_output(
        _plan(first, mode="enforced"),
        candidate_agent_ids=frozenset({first, second}),
    )
    executor = PlanningRuntimeNodeExecutor(
        cancel_source=_CancelSource(),  # type: ignore[arg-type]
        model_service=_PlanningModel(PlanningModelResult(plan=plan)),  # type: ignore[arg-type]
    )

    update = await executor.execute("model", state, _context())

    assert update["lifecycle"]["status"] == "completed"
    assert update["lifecycle"]["next_route"] == "terminal"
    assert update["lifecycle"]["planning"] == plan
    assert update["lifecycle"]["waiting_request"] is None
    assert update["lifecycle"]["error"] is None


def test_checkpoint_plan_revalidates_the_frozen_candidate_scope() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    state = _state((first, second))
    state["lifecycle"]["planning"] = _plan(first)

    assert checkpoint_plan(state) == _plan(first)

    state["lifecycle"]["planning"] = _plan(uuid.uuid4())
    with pytest.raises(PlanningContractError, match="candidate"):
        checkpoint_plan(state)


@pytest.mark.asyncio
async def test_planning_executor_has_no_child_resume_path() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    state = _state((first, second))
    plan = validate_planning_output(
        _plan(first),
        candidate_agent_ids=frozenset({first, second}),
    )
    state["lifecycle"].update(
        {
            "status": "completed",
            "next_route": "terminal",
            "planning": plan,
            "waiting_request": None,
        }
    )
    executor = PlanningRuntimeNodeExecutor(
        cancel_source=_CancelSource(),  # type: ignore[arg-type]
        model_service=_PlanningModel(),  # type: ignore[arg-type]
    )

    with pytest.raises(PlanningContractError, match="cannot execute wait"):
        await executor.execute(
            "wait",
            state,
            _context(),
            resume_value={
                "resume_type": "agent_result",
                "correlation_id": "planning:legacy",
                "payload": {},
            },
        )
