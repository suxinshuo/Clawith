"""Provider throttling hints must survive the HTTP -> exception boundary.

A 429 from Anthropic (and from every other provider we speak to) carries a
`Retry-After` header saying when the rate-limit window resets. If the client
collapses the response into a bare `LLMError`, that header is lost and the
runtime retry loop can only guess -- which is how four attempts end up landing
inside the same throttled minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.services.llm.client import (
    AnthropicClient,
    LLMError,
    LLMHTTPError,
    LLMMessage,
    OpenAICompatibleClient,
    parse_retry_after,
)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_parse_retry_after_accepts_delta_seconds() -> None:
    assert parse_retry_after("42") == 42.0
    assert parse_retry_after(" 7.5 ") == 7.5


def test_parse_retry_after_accepts_http_date() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=45)
    parsed = parse_retry_after(format_datetime(deadline, usegmt=True))
    assert parsed is not None
    # HTTP-date has second granularity, so allow the truncation slack.
    assert 43.0 <= parsed <= 46.0


def test_parse_retry_after_clamps_past_dates_to_zero() -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0.0


def test_parse_retry_after_rejects_garbage() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None


@pytest.mark.asyncio
async def test_anthropic_complete_preserves_retry_after_on_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "37"},
            json={"type": "error", "error": {"type": "rate_limit_error"}},
        )

    client = AnthropicClient(api_key="k", model="claude-sonnet-5")
    client._client = _mock_client(handler)
    try:
        with pytest.raises(LLMHTTPError) as excinfo:
            await client.complete(messages=[LLMMessage(role="user", content="hi")])
    finally:
        await client.close()

    error = excinfo.value
    assert isinstance(error, LLMError)
    assert error.status_code == 429
    assert error.retry_after_seconds == 37.0
    # The message shape is load-bearing: classify_error and the runtime's
    # http_status log field both scrape "HTTP 429" out of str(error).
    assert "HTTP 429" in str(error)


@pytest.mark.asyncio
async def test_openai_compatible_complete_preserves_retry_after_on_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, text="slow down")

    client = OpenAICompatibleClient(api_key="k", model="gpt-x")
    client._client = _mock_client(handler)
    try:
        with pytest.raises(LLMHTTPError) as excinfo:
            await client.complete(messages=[LLMMessage(role="user", content="hi")])
    finally:
        await client.close()

    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after_seconds == 12.0


@pytest.mark.asyncio
async def test_missing_retry_after_leaves_hint_unset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    client = AnthropicClient(api_key="k", model="claude-sonnet-5")
    client._client = _mock_client(handler)
    try:
        with pytest.raises(LLMHTTPError) as excinfo:
            await client.complete(messages=[LLMMessage(role="user", content="hi")])
    finally:
        await client.close()

    assert excinfo.value.status_code == 503
    assert excinfo.value.retry_after_seconds is None
