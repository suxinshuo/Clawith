"""Tests for credential scope validation and channel-specific guidance."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.services.credential_resolver import parse_credential_scopes


# ── parse_credential_scopes ──

class TestParseCredentialScopes:
    def test_comma_separated(self):
        assert parse_credential_scopes("read:issues,write:comments") == [
            "read:issues", "write:comments"
        ]

    def test_space_separated(self):
        assert parse_credential_scopes("repo read:org") == ["repo", "read:org"]

    def test_mixed_separators(self):
        result = parse_credential_scopes("repo, read:org write:comments")
        assert set(result) == {"repo", "read:org", "write:comments"}

    def test_none_returns_empty(self):
        assert parse_credential_scopes(None) == []

    def test_empty_string_returns_empty(self):
        assert parse_credential_scopes("") == []

    def test_whitespace_trimmed(self):
        assert parse_credential_scopes("  repo , read:org  ") == ["repo", "read:org"]


# ── _parse_credential_scopes (agent_tools wrapper) ──

class TestAgentToolsScopeParser:
    def test_returns_set(self):
        from app.services.agent_tools import _parse_credential_scopes
        result = _parse_credential_scopes("read:issues,write:comments")
        assert isinstance(result, set)
        assert result == {"read:issues", "write:comments"}


# ── _build_credential_guidance ──

class DummyResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])

    async def execute(self, _stmt):
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_guidance_web_user():
    """Web users get directed to settings page."""
    from app.services.agent_tools import _build_credential_guidance

    db = FakeDB(responses=[DummyResult("web")])
    with patch("app.services.agent_tools.async_session", return_value=db):
        msg = await _build_credential_guidance(
            provider="jira",
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            session_id="web_session_123",
        )

    assert "个人设置" in msg
    assert "jira" in msg


@pytest.mark.asyncio
async def test_guidance_channel_user_gets_link():
    """Channel users (feishu) get a one-time token link."""
    from app.services.agent_tools import _build_credential_guidance

    db = FakeDB(responses=[DummyResult("feishu"), DummyResult(None)])
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    with patch("app.services.agent_tools.async_session", return_value=db):
        with patch("app.services.agent_tools.get_settings") as mock_settings:
            mock_settings.return_value = SimpleNamespace(
                SECRET_KEY="test-key",
                PUBLIC_BASE_URL="https://clawith.example.com",
            )
            msg = await _build_credential_guidance(
                provider="erp",
                user_id=user_id,
                tenant_id=tenant_id,
                session_id="feishu_session_456",
            )

    assert "credentials/connect?token=" in msg
    assert "10 分钟" in msg


@pytest.mark.asyncio
async def test_guidance_no_session_defaults_to_web():
    """When session_id is empty, defaults to web guidance."""
    from app.services.agent_tools import _build_credential_guidance

    msg = await _build_credential_guidance(
        provider="jira",
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id="",
    )

    assert "个人设置" in msg
