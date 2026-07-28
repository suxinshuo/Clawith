"""Storing an OAuth grant must not depend on how many scopes it carries.

The OAuth callbacks write the provider's granted-scope string straight into
``*_external_credentials.scopes`` without passing through a request schema, so
that column is the only thing bounding it.  A Feishu app with a large permission
set answers with a scope list of several thousand characters, which overflowed
the original ``VARCHAR(500)`` and turned a successful authorization into an
opaque 500 for the end user.
"""

from __future__ import annotations

from importlib import util
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import Text

from app.models.user_external_credential import (
    AgentExternalCredential,
    TenantExternalCredential,
    UserExternalCredential,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607281000_widen_credential_scopes.py"
)

# Observed in production: the grant Feishu returned for an app holding the full
# docs/bitable/approval/calendar permission set.
OBSERVED_FEISHU_GRANT_LENGTH = 2250


def _load_migration():
    spec = util.spec_from_file_location("widen_credential_scopes", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "model",
    [UserExternalCredential, TenantExternalCredential, AgentExternalCredential],
    ids=["user", "tenant", "agent"],
)
def test_credential_scopes_column_is_unbounded(model) -> None:
    """A granted-scope list has no useful upper bound, so neither can the column."""
    column = model.__table__.c.scopes

    assert isinstance(column.type, Text)
    assert getattr(column.type, "length", None) is None, (
        f"{model.__tablename__}.scopes is capped, so a real Feishu grant "
        f"(~{OBSERVED_FEISHU_GRANT_LENGTH} chars) cannot be stored"
    )


def test_migration_widens_every_credential_scope_column() -> None:
    migration = _load_migration()

    assert migration.revision == "widen_credential_scopes"
    assert migration.down_revision == "merge_fork_v1112"
    assert migration.TABLES == (
        "user_external_credentials",
        "tenant_external_credentials",
        "agent_external_credentials",
    )


# ── The callback must stay legible when storage fails anyway ──────────────────


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """One ``async with async_session()`` block, with a scripted commit."""

    def __init__(self, value, *, commit_error: Exception | None = None) -> None:
        self._value = value
        self._commit_error = commit_error
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self._value)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self._commit_error is not None:
            raise self._commit_error
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _install_callback(monkeypatch, credential_db: _FakeDB, granted_scope: str):
    """Drive feishu_credential_callback down to the credential upsert."""
    from app.api import oauth_feishu
    from app.services import feishu_service as feishu_module

    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async def state(_state):
        return {
            "user_id": user_id,
            "tenant_id": uuid.uuid4(),
            "provider": f"feishu:{agent_id}",
            "agent_id": agent_id,
        }

    channel_db = _FakeDB(SimpleNamespace(app_id="cli_x", app_secret="secret"))
    sessions = iter([channel_db, credential_db])

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {
                        "access_token": "u-token",
                        "refresh_token": "r-token",
                        "expires_in": 7200,
                        "scope": granted_scope,
                    },
                },
            )

    async def app_token(*_args, **_kwargs):
        return "app-token"

    monkeypatch.setattr(oauth_feishu, "validate_oauth_state", state)
    monkeypatch.setattr(oauth_feishu, "async_session", lambda: next(sessions))
    monkeypatch.setattr(
        feishu_module.feishu_service, "get_tenant_access_token", app_token
    )
    monkeypatch.setattr("httpx.AsyncClient", Client)
    return oauth_feishu


@pytest.mark.asyncio
async def test_storage_failure_reports_an_actionable_page(monkeypatch) -> None:
    """The user already granted access — never answer them with a bare 500."""
    credential_db = _FakeDB(None, commit_error=RuntimeError("value too long"))
    oauth_feishu = _install_callback(
        monkeypatch, credential_db, "docx:document " * 200
    )

    response = await oauth_feishu.feishu_credential_callback(code="c", state="s")

    body = bytes(response.body).decode()
    assert response.status_code == 500
    assert "授权失败" in body
    # The grant itself worked; the page must say the saving step is what broke,
    # so the user knows re-authorizing on its own will not help.
    assert "保存" in body


@pytest.mark.asyncio
async def test_a_long_grant_is_stored_whole(monkeypatch) -> None:
    """The happy path keeps every granted scope, however many there are."""
    granted_scope = " ".join(f"docx:document:scope{i}" for i in range(120))
    assert len(granted_scope) > 500

    credential_db = _FakeDB(None)
    oauth_feishu = _install_callback(monkeypatch, credential_db, granted_scope)

    response = await oauth_feishu.feishu_credential_callback(code="c", state="s")

    assert response.status_code == 200
    assert "授权成功" in bytes(response.body).decode()
    assert credential_db.committed
    (stored,) = credential_db.added
    assert stored.scopes == granted_scope
