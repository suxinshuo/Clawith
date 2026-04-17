"""Tests for one-time JWT token service — generation, validation, replay prevention."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.one_time_token import (
    generate_one_time_token,
    validate_one_time_token,
    _consumed_jtis,
    _consume_jti,
)


@pytest.fixture(autouse=True)
def clear_consumed_jtis():
    """Clear in-memory jti store between tests."""
    _consumed_jtis.clear()
    yield
    _consumed_jtis.clear()


@pytest.mark.asyncio
async def test_generate_and_validate():
    """A generated token can be validated once."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token = generate_one_time_token(user_id, tenant_id, "jira")

    payload = await validate_one_time_token(token)

    assert payload["user_id"] == user_id
    assert payload["tenant_id"] == tenant_id
    assert payload["provider"] == "jira"
    assert payload["credential_mode"] == "manual"


@pytest.mark.asyncio
async def test_replay_rejected():
    """Second use of the same token is rejected."""
    token = generate_one_time_token(uuid.uuid4(), uuid.uuid4(), "github")

    await validate_one_time_token(token)  # first use OK

    with pytest.raises(ValueError, match="already been used"):
        await validate_one_time_token(token)  # replay rejected


@pytest.mark.asyncio
async def test_invalid_token_rejected():
    """Garbage tokens are rejected."""
    with pytest.raises(ValueError, match="Invalid"):
        await validate_one_time_token("not-a-jwt")


@pytest.mark.asyncio
async def test_oauth_mode_token():
    """Token with credential_mode=oauth is correctly parsed."""
    token = generate_one_time_token(
        uuid.uuid4(), uuid.uuid4(), "feishu", credential_mode="oauth"
    )

    payload = await validate_one_time_token(token)

    assert payload["credential_mode"] == "oauth"


@pytest.mark.asyncio
async def test_consume_jti_first_returns_true():
    """_consume_jti returns True on first consumption."""
    result = await _consume_jti("test-jti-1")
    assert result is True


@pytest.mark.asyncio
async def test_consume_jti_replay_returns_false():
    """_consume_jti returns False on replay."""
    await _consume_jti("test-jti-2")
    result = await _consume_jti("test-jti-2")
    assert result is False


@pytest.mark.asyncio
async def test_consumed_jtis_has_timestamp():
    """In-memory store records timestamp for TTL-based cleanup."""
    await _consume_jti("test-jti-3")
    assert "test-jti-3" in _consumed_jtis
    assert isinstance(_consumed_jtis["test-jti-3"], float)
