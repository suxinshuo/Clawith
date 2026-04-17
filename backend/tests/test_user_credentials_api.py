"""Tests for user external credential API endpoints."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import HTTPException

from app.api.user_credentials import router
from app.schemas.user_credential import UserCredentialCreate, OneTimeTokenSubmit


# ── Helpers ──

def _make_user(role="member"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        is_active=True,
    )


def _make_cred_row(user_id, provider="jira", status="active"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        tenant_id=uuid.uuid4(),
        provider=provider,
        credential_type="api_key",
        access_token_encrypted="encrypted_xxx",
        refresh_token_encrypted=None,
        extra_encrypted=None,
        token_expires_at=None,
        scopes="read:issues",
        external_user_id="ext-1",
        external_username="john",
        status=status,
        display_name="My Jira",
        last_used_at=None,
        created_at="2026-04-16T00:00:00Z",
        updated_at="2026-04-16T00:00:00Z",
    )


class DummyResult:
    def __init__(self, values=None):
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class FakeDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []

    async def execute(self, _stmt):
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def delete(self, obj):
        pass


# ── Import the actual endpoint functions ──
from app.api import user_credentials as api_mod


@pytest.mark.asyncio
async def test_list_my_credentials():
    """GET /credentials/me returns current user's credentials."""
    user = _make_user()
    cred = _make_cred_row(user.id)
    db = FakeDB(responses=[DummyResult([cred])])

    result = await api_mod.list_my_credentials(current_user=user, db=db)
    assert len(result) == 1
    assert result[0]["provider"] == "jira"
    # Token must never be exposed
    assert "access_token_encrypted" not in result[0]


@pytest.mark.asyncio
async def test_create_credential_encrypts_token():
    """POST /credentials/manual encrypts the access_token before storage."""
    user = _make_user()
    db = FakeDB()
    data = UserCredentialCreate(provider="github", access_token="ghp_abc123")

    with patch("app.api.user_credentials.encrypt_data", return_value="encrypted_ghp") as mock_enc:
        with patch("app.api.user_credentials.get_settings") as mock_settings:
            mock_settings.return_value = SimpleNamespace(SECRET_KEY="test-key")
            result = await api_mod.create_credential(data=data, current_user=user, db=db)

    mock_enc.assert_called_once_with("ghp_abc123", "test-key")
    assert len(db.added) == 1
    assert db.added[0].access_token_encrypted == "encrypted_ghp"


@pytest.mark.asyncio
async def test_submit_via_one_time_token():
    """POST /credentials/submit creates a credential using one-time token."""
    db = FakeDB(responses=[DummyResult()])  # no existing credential
    body = OneTimeTokenSubmit(token="jwt-token", access_token="my-api-key")

    token_payload = {
        "user_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "provider": "internal_erp",
        "credential_mode": "manual",
    }

    with patch("app.api.user_credentials.validate_one_time_token", return_value=token_payload):
        with patch("app.api.user_credentials.encrypt_data", return_value="encrypted_key"):
            with patch("app.api.user_credentials.get_settings") as mock_settings:
                mock_settings.return_value = SimpleNamespace(SECRET_KEY="test-key")
                result = await api_mod.submit_credential_via_token(data=body, db=db)

    assert result["provider"] == "internal_erp"
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_submit_rejects_consumed_token():
    """POST /credentials/submit rejects an already-consumed one-time token."""
    db = FakeDB()
    body = OneTimeTokenSubmit(token="jwt-token", access_token="key")

    with patch("app.api.user_credentials.validate_one_time_token", side_effect=ValueError("Token has already been used")):
        with pytest.raises(HTTPException) as exc:
            await api_mod.submit_credential_via_token(data=body, db=db)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_credential():
    """DELETE /credentials/{id} deletes the credential."""
    user = _make_user()
    cred = _make_cred_row(user.id)
    db = FakeDB(responses=[DummyResult([cred])])

    await api_mod.delete_credential(credential_id=cred.id, current_user=user, db=db)
    # Should not raise
