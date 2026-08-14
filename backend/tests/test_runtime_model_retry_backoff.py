"""Backoff policy for transient provider failures in the runtime model step.

The failure this pins down: Anthropic rate limits reset on a per-minute window,
but the blind exponential backoff only spanned ~7 seconds across four attempts,
so every attempt landed inside the same throttled window and the Run parked in a
`wait` that needs a human to resume. When the provider tells us how long to wait,
we wait that long instead of guessing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import uuid

import pytest

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.agent_runtime.model_step_service import (
    _DEFAULT_MODEL_RETRY_ATTEMPTS,
    _DEFAULT_MODEL_RETRY_BASE_DELAY_SECONDS,
    _DEFAULT_MODEL_RETRY_JITTER_RATIO,
    _DEFAULT_MODEL_RETRY_MAX_DELAY_SECONDS,
    _DEFAULT_MODEL_RETRY_PROVIDER_HINT_CAP_SECONDS,
    RuntimeModelStepService,
)
from app.services.llm.client import LLMHTTPError


@asynccontextmanager
async def _unused_session_factory():
    raise AssertionError("the retry loop must not touch the database")
    yield  # pragma: no cover


def _model() -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="anthropic",
        model="claude-sonnet-5",
        api_key_encrypted="encrypted",
        label="Retry Model",
        enabled=True,
        supports_vision=False,
        max_output_tokens=2048,
        max_input_tokens=100_000,
        context_window_tokens=None,
        supports_tool_calling=True,
    )


def _agent() -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Retry Agent",
        role_description="Solve the task",
        status="idle",
        is_expired=False,
    )


def _service(completion, sleeps: list[float], **overrides) -> RuntimeModelStepService:
    async def retry_sleep(delay: float) -> None:
        sleeps.append(delay)

    return RuntimeModelStepService(
        session_factory=_unused_session_factory,
        context_builder=object(),  # type: ignore[arg-type]
        completion=completion,
        tool_provider=lambda *a, **k: [],  # type: ignore[arg-type]
        prompt_builder=lambda *a, **k: ("", ""),  # type: ignore[arg-type]
        retry_sleep=retry_sleep,
        **overrides,
    )


def _rate_limited(retry_after: float | None) -> LLMHTTPError:
    return LLMHTTPError(
        "HTTP 429: rate_limit_error",
        status_code=429,
        retry_after_seconds=retry_after,
    )


async def _call(service: RuntimeModelStepService):
    return await service._call_prepared_with_retry(
        model=_model(),
        agent=_agent(),
        messages=[],
        tools=[],
    )


@pytest.mark.asyncio
async def test_provider_retry_after_hint_replaces_blind_backoff() -> None:
    """A 429 carrying `retry-after: 45` must sleep ~45s, not ~2s."""
    sleeps: list[float] = []
    attempts = 0

    async def completion(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _rate_limited(45.0)
        return "ok"

    assert await _call(_service(completion, sleeps)) == "ok"
    assert attempts == 2
    assert len(sleeps) == 1
    # Honoured, jittered upward only, and never past the policy ceiling.
    assert 45.0 <= sleeps[0] <= _DEFAULT_MODEL_RETRY_PROVIDER_HINT_CAP_SECONDS


@pytest.mark.asyncio
async def test_provider_hint_is_capped() -> None:
    """A hostile or absurd hint cannot park the worker indefinitely."""
    sleeps: list[float] = []

    async def completion(*args, **kwargs):
        raise _rate_limited(86_400.0)

    with pytest.raises(LLMHTTPError):
        await _call(_service(completion, sleeps))

    assert sleeps
    assert all(delay <= _DEFAULT_MODEL_RETRY_PROVIDER_HINT_CAP_SECONDS * 1.5 for delay in sleeps)


@pytest.mark.asyncio
async def test_zero_or_tiny_hint_never_undercuts_exponential_backoff() -> None:
    """`retry-after: 0` must not turn the retry loop into a tight spin."""
    sleeps: list[float] = []

    async def completion(*args, **kwargs):
        raise _rate_limited(0.0)

    with pytest.raises(LLMHTTPError):
        await _call(_service(completion, sleeps))

    assert len(sleeps) == _DEFAULT_MODEL_RETRY_ATTEMPTS
    assert all(delay >= _DEFAULT_MODEL_RETRY_BASE_DELAY_SECONDS for delay in sleeps)


@pytest.mark.asyncio
async def test_blind_backoff_spans_a_provider_rate_limit_window() -> None:
    """Without a hint, the default budget must not expire inside one window."""
    sleeps: list[float] = []

    async def completion(*args, **kwargs):
        raise _rate_limited(None)

    with pytest.raises(LLMHTTPError):
        await _call(_service(completion, sleeps))

    assert len(sleeps) == _DEFAULT_MODEL_RETRY_ATTEMPTS

    nominal = [
        min(_DEFAULT_MODEL_RETRY_BASE_DELAY_SECONDS * (2**i), _DEFAULT_MODEL_RETRY_MAX_DELAY_SECONDS)
        for i in range(_DEFAULT_MODEL_RETRY_ATTEMPTS)
    ]
    # 1s -> 2s -> 4s (~7s total) was the bug: it all fits inside one throttled
    # minute. The blind budget now has to reach into the next window. This is a
    # jitter-free assertion on the configured policy.
    assert sum(nominal) >= 25.0
    # And the observed sleeps track that policy, jitter included.
    assert sum(sleeps) >= sum(nominal) * (1.0 - _DEFAULT_MODEL_RETRY_JITTER_RATIO)
    assert max(sleeps) <= _DEFAULT_MODEL_RETRY_MAX_DELAY_SECONDS * (1.0 + _DEFAULT_MODEL_RETRY_JITTER_RATIO)


@pytest.mark.asyncio
async def test_non_retryable_error_is_not_slept_on() -> None:
    sleeps: list[float] = []

    async def completion(*args, **kwargs):
        raise LLMHTTPError("HTTP 401: invalid api key", status_code=401)

    with pytest.raises(LLMHTTPError):
        await _call(_service(completion, sleeps))

    assert sleeps == []
