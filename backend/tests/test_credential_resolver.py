"""Tests for CredentialResolver — credential lookup, priority, and error handling."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.credential_resolver import CredentialResolver, ResolvedCredential, CredentialNotFoundError


# ── Helpers ──

def _make_user_cred(provider="jira", status="active", access_token_encrypted="encrypted_token_abc",
                    external_user_id="ext-123", scopes="read:issues", token_expires_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider=provider,
        credential_type="api_key",
        access_token_encrypted=access_token_encrypted,
        refresh_token_encrypted=None,
        external_user_id=external_user_id,
        external_username="john",
        scopes=scopes,
        status=status,
        token_expires_at=token_expires_at,
        last_used_at=None,
    )


def _make_tenant_cred(provider="jira", status="active", access_token_encrypted="encrypted_tenant_token"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider=provider,
        credential_type="api_key",
        access_token_encrypted=access_token_encrypted,
        refresh_token_encrypted=None,
        external_user_id=None,
        external_username=None,
        scopes=None,
        status=status,
        token_expires_at=None,
        last_used_at=None,
    )


class DummyResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.committed = False

    async def execute(self, _stmt):
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── Tests ──

@pytest.mark.asyncio
async def test_resolve_user_credential():
    """User credential is returned when it exists and is active."""
    user_cred = _make_user_cred()
    db = FakeDB(responses=[DummyResult(user_cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="decrypted_token"):
            result = await resolver.resolve(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")

    assert result is not None
    assert isinstance(result, ResolvedCredential)
    assert result.access_token == "decrypted_token"
    assert result.source == "user"
    assert result.external_user_id == "ext-123"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_tenant_credential():
    """When no user credential exists, resolver falls back to tenant credential."""
    tenant_cred = _make_tenant_cred()
    db = FakeDB(responses=[DummyResult(None), DummyResult(tenant_cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="decrypted_tenant_token"):
            result = await resolver.resolve(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")

    assert result is not None
    assert result.source == "tenant"
    assert result.access_token == "decrypted_tenant_token"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_credential():
    """Returns None when neither user nor tenant credential exists."""
    db = FakeDB(responses=[DummyResult(None), DummyResult(None)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        result = await resolver.resolve(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_skips_inactive_user_credential():
    """User credentials with status != 'active' are skipped."""
    expired_cred = _make_user_cred(status="expired")
    tenant_cred = _make_tenant_cred()
    db = FakeDB(responses=[DummyResult(expired_cred), DummyResult(tenant_cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="tenant_token"):
            result = await resolver.resolve(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")

    assert result is not None
    assert result.source == "tenant"


@pytest.mark.asyncio
async def test_resolve_or_fail_raises_when_not_found():
    """resolve_or_fail raises CredentialNotFoundError when no credential exists."""
    db = FakeDB(responses=[DummyResult(None), DummyResult(None)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with pytest.raises(CredentialNotFoundError) as exc:
            await resolver.resolve_or_fail(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")
        assert exc.value.provider == "jira"


@pytest.mark.asyncio
async def test_resolve_parses_scopes():
    """Scopes are split from comma-separated string into a list."""
    user_cred = _make_user_cred(scopes="read:issues,write:comments,admin")
    db = FakeDB(responses=[DummyResult(user_cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="token"):
            result = await resolver.resolve(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="jira")

    assert result.scopes == ["read:issues", "write:comments", "admin"]
