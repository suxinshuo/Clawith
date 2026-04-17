"""Tests for CredentialResolver token auto-refresh and locking."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.services.credential_resolver import CredentialResolver, ResolvedCredential


# ── Helpers ──

def _make_oauth_cred(
    provider="github",
    status="active",
    token_expires_at=None,
    has_refresh=True,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider=provider,
        credential_type="oauth2",
        access_token_encrypted="encrypted_access",
        refresh_token_encrypted="encrypted_refresh" if has_refresh else None,
        external_user_id="ext-1",
        external_username="john",
        scopes="repo,read:org",
        status=status,
        token_expires_at=token_expires_at,
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
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── Tests ──

@pytest.mark.asyncio
async def test_fresh_oauth_token_returned_as_is():
    """OAuth token that hasn't expired is returned without refresh."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    cred = _make_oauth_cred(token_expires_at=future)
    db = FakeDB(responses=[DummyResult(cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="valid_token"):
            result = await resolver.resolve(
                user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="github"
            )

    assert result is not None
    assert result.access_token == "valid_token"


@pytest.mark.asyncio
async def test_expired_token_without_refresh_marks_needs_reauth():
    """Expired OAuth token with no refresh_token is marked needs_reauth."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    cred = _make_oauth_cred(token_expires_at=past, has_refresh=False)
    # Two DB contexts: one for resolve query, one for update in _ensure_token_fresh
    db_resolve = FakeDB(responses=[DummyResult(cred)])
    db_update = FakeDB()

    resolver = CredentialResolver()
    call_count = [0]
    def mock_session():
        call_count[0] += 1
        if call_count[0] == 1:
            return db_resolve
        return db_update

    with patch("app.services.credential_resolver.async_session", side_effect=mock_session):
        with patch("app.services.credential_resolver.decrypt_data", return_value="expired_token"):
            result = await resolver.resolve(
                user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="github"
            )

    # Token was expired and no refresh available — should return None
    assert result is None


@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reauth():
    """When OAuth refresh fails, credential is marked needs_reauth instead of returning expired token."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    cred = _make_oauth_cred(token_expires_at=past, has_refresh=True)

    oauth_config = SimpleNamespace(
        token_url="https://oauth.example.com/token",
        client_id="cid",
        client_secret_encrypted="encrypted_secret",
    )

    resolver = CredentialResolver()

    db_calls = []
    class MultiDB:
        """Fake that returns different results per call."""
        def __init__(self):
            self.call_idx = 0
            self.results = [
                DummyResult(cred),        # resolve user cred
                DummyResult(oauth_config), # load oauth config
                DummyResult(),             # update needs_reauth
            ]
        def __call__(self):
            return self
        async def execute(self, stmt):
            db_calls.append(stmt)
            idx = self.call_idx
            self.call_idx += 1
            if idx < len(self.results):
                return self.results[idx]
            return DummyResult()
        async def commit(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    multi_db = MultiDB()
    with patch("app.services.credential_resolver.async_session", return_value=multi_db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="some_token"):
            with patch("app.services.oauth_service.refresh_access_token", side_effect=ValueError("Refresh failed")):
                result = await resolver.resolve(
                    user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="github"
                )

    # Should return None (not the expired token)
    assert result is None


@pytest.mark.asyncio
async def test_non_oauth_credential_skips_refresh():
    """api_key credentials bypass the refresh logic entirely."""
    cred = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="internal_erp",
        credential_type="api_key",
        access_token_encrypted="encrypted_key",
        refresh_token_encrypted=None,
        external_user_id=None,
        external_username=None,
        scopes=None,
        status="active",
        token_expires_at=None,
        last_used_at=None,
    )
    db = FakeDB(responses=[DummyResult(cred)])

    resolver = CredentialResolver()
    with patch("app.services.credential_resolver.async_session", return_value=db):
        with patch("app.services.credential_resolver.decrypt_data", return_value="my_api_key"):
            result = await resolver.resolve(
                user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="internal_erp"
            )

    assert result is not None
    assert result.access_token == "my_api_key"
    assert result.credential_type == "api_key"
