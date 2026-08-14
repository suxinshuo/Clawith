"""MCP tools must forward per-user identity as ``X-Clawith-*`` headers.

Authenticated MCP servers derive the acting user from these headers.  When the
Durable Runtime MCP dispatch path was rewritten the credential wiring was lost
in a merge, so every request reached the provider anonymously and the provider
rejected it (e.g. ``user_id is required for TiDB connection``).  These tests
pin the contract so a future refactor cannot silently drop it again.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import agent_tools
from app.services.credential_resolver import ResolvedCredential
from app.services.mcp_client import MCPClient

TENANT_ID = uuid.uuid4()


def _credential(
    *,
    external_user_id: str | None = "ext-user-1",
    scopes: list[str] | None = None,
) -> ResolvedCredential:
    return ResolvedCredential(
        provider="tidb",
        credential_type="api_key",
        access_token="tok-abc",
        external_user_id=external_user_id,
        external_username="alice",
        scopes=[] if scopes is None else scopes,
        source="user",
        credential_id=uuid.uuid4(),
    )


def _target(
    *,
    provider: str | None = "tidb",
    required_scopes: str = "",
) -> dict:
    return {
        "full_name": "mcp_data_execute_tidb_sql",
        "raw_name": "execute_tidb_sql",
        "server_url": "https://data.example/mcp",
        "server_name": "data",
        "config": {},
        "async_completion": None,
        "required_credential_provider": provider,
        "required_scopes": required_scopes,
    }


def _ok_response() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": '{"rows":[]}'}]},
    }


def _install_common(monkeypatch, *, tenant_id_str: str | None = str(TENANT_ID)) -> list:
    """Stub tenant lookup, audit logging and guidance; return the audit sink."""
    audited: list = []

    async def tenant(_agent_id):
        return tenant_id_str

    async def audit(*, action, details, agent_id, user_id):
        audited.append((action, details, agent_id, user_id))

    async def guidance(*, provider, user_id, tenant_id, session_id, agent_id):
        del user_id, tenant_id, session_id, agent_id
        return f"请先授权 {provider}"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant)
    monkeypatch.setattr(agent_tools, "write_audit_log", audit)
    monkeypatch.setattr(agent_tools, "_build_credential_guidance", guidance)
    return audited


def _install_resolver(monkeypatch, cred, *, raises: bool = False) -> None:
    class _Resolver:
        async def resolve(self, user_id, tenant_id, provider, *, agent_id=None):
            del user_id, agent_id
            if raises:
                raise RuntimeError("resolver exploded")
            assert tenant_id == TENANT_ID
            assert provider == "tidb"
            return cred

    monkeypatch.setattr(agent_tools, "CredentialResolver", _Resolver)


def _capture_headers(monkeypatch) -> dict:
    captured: dict = {}

    async def raw_call(self, raw_name, arguments):
        del arguments
        captured["raw_name"] = raw_name
        captured["headers"] = dict(self._user_headers)
        # Assert the headers survive into the wire-level header builder too,
        # not merely into the constructor.
        captured["wire_headers"] = dict(self._headers())
        return _ok_response()

    monkeypatch.setattr(MCPClient, "call_tool_result", raw_call)
    return captured


def _forbid_dispatch(monkeypatch) -> None:
    async def raw_call(self, raw_name, arguments):
        raise AssertionError(
            "MCP must not be dispatched when the credential is unusable"
        )

    monkeypatch.setattr(MCPClient, "call_tool_result", raw_call)


@pytest.mark.asyncio
async def test_required_credential_is_sent_as_clawith_identity_headers(
    monkeypatch,
) -> None:
    audited = _install_common(monkeypatch)
    _install_resolver(
        monkeypatch, _credential(scopes=["read:data", "write:data"])
    )
    captured = _capture_headers(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "succeeded"
    assert captured["headers"] == {
        "X-Clawith-User-Token": "tok-abc",
        "X-Clawith-User-Id": "ext-user-1",
        "X-Clawith-User-Scopes": "read:data,write:data",
    }
    # The header builder must merge them alongside the MCP protocol headers.
    assert captured["wire_headers"]["X-Clawith-User-Id"] == "ext-user-1"
    assert [entry[0] for entry in audited] == ["credential_resolve"]


@pytest.mark.asyncio
async def test_optional_identity_headers_are_omitted_when_credential_lacks_them(
    monkeypatch,
) -> None:
    _install_common(monkeypatch)
    _install_resolver(
        monkeypatch, _credential(external_user_id=None, scopes=[])
    )
    captured = _capture_headers(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "succeeded"
    assert captured["headers"] == {"X-Clawith-User-Token": "tok-abc"}


@pytest.mark.asyncio
async def test_tool_without_required_provider_sends_no_identity_headers(
    monkeypatch,
) -> None:
    def _resolver_forbidden():
        raise AssertionError("credential resolution must not run")

    monkeypatch.setattr(agent_tools, "CredentialResolver", _resolver_forbidden)
    captured = _capture_headers(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(provider=None),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "succeeded"
    assert captured["headers"] == {}


@pytest.mark.asyncio
async def test_legacy_target_without_credential_keys_still_dispatches(
    monkeypatch,
) -> None:
    """Targets built before this contract existed must not start failing."""
    captured = _capture_headers(monkeypatch)
    legacy = _target()
    del legacy["required_credential_provider"]
    del legacy["required_scopes"]

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        legacy,
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "succeeded"
    assert captured["headers"] == {}


@pytest.mark.asyncio
async def test_missing_credential_fails_with_guidance_and_never_dispatches(
    monkeypatch,
) -> None:
    audited = _install_common(monkeypatch)
    _install_resolver(monkeypatch, None)
    _forbid_dispatch(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_credential_missing"
    assert "请先授权 tidb" in (outcome.result_summary or "")
    assert [entry[0] for entry in audited] == ["credential_resolve_fail"]


@pytest.mark.asyncio
async def test_insufficient_scopes_fail_before_dispatch(monkeypatch) -> None:
    _install_common(monkeypatch)
    _install_resolver(monkeypatch, _credential(scopes=["read:data"]))
    _forbid_dispatch(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(required_scopes="read:data,write:data"),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_credential_scope_insufficient"
    assert "write:data" in (outcome.result_summary or "")


@pytest.mark.asyncio
async def test_anonymous_call_to_credentialed_tool_never_dispatches(
    monkeypatch,
) -> None:
    """A call with no user identity must fail, not silently go unauthenticated."""
    _install_common(monkeypatch)
    _forbid_dispatch(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=None,
        session_id="sess-1",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_credential_user_missing"


@pytest.mark.asyncio
async def test_agent_without_tenant_fails_before_dispatch(monkeypatch) -> None:
    _install_common(monkeypatch, tenant_id_str=None)
    _forbid_dispatch(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_credential_tenant_missing"


@pytest.mark.asyncio
async def test_resolver_error_fails_before_dispatch(monkeypatch) -> None:
    _install_common(monkeypatch)
    _install_resolver(monkeypatch, None, raises=True)
    _forbid_dispatch(monkeypatch)

    outcome = await agent_tools._execute_resolved_mcp_target_outcome(
        _target(),
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_credential_resolution_failed"


@pytest.mark.asyncio
async def test_legacy_text_path_also_forwards_identity_headers(
    monkeypatch,
) -> None:
    """The bare-name text wrapper must authenticate identically."""
    _install_common(monkeypatch)
    _install_resolver(monkeypatch, _credential(scopes=["read:data"]))
    captured = _capture_headers(monkeypatch)

    async def resolve(tool_name, _agent_id, *, allow_legacy_bare_name=False):
        assert allow_legacy_bare_name is True
        assert tool_name == "execute_tidb_sql"
        return _target()

    monkeypatch.setattr(agent_tools, "_resolve_mcp_execution_target", resolve)

    result = await agent_tools._execute_mcp_tool(
        "execute_tidb_sql",
        {"sql": "select 1"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="sess-1",
    )

    assert not result.startswith("❌")
    assert captured["headers"]["X-Clawith-User-Id"] == "ext-user-1"
