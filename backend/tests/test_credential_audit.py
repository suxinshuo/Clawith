"""Tests for credential audit logging integration."""

import uuid
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.services.audit_logger import AuditAction


def test_credential_audit_actions_exist():
    """Verify all credential audit action types are defined."""
    assert AuditAction.CREDENTIAL_CREATE == "credential_create"
    assert AuditAction.CREDENTIAL_DELETE == "credential_delete"
    assert AuditAction.CREDENTIAL_RESOLVE == "credential_resolve"
    assert AuditAction.CREDENTIAL_RESOLVE_FAIL == "credential_resolve_fail"
    assert AuditAction.CREDENTIAL_OAUTH_START == "credential_oauth_start"
    assert AuditAction.CREDENTIAL_OAUTH_COMPLETE == "credential_oauth_complete"
    assert AuditAction.CREDENTIAL_TOKEN_REFRESH == "credential_token_refresh"
    assert AuditAction.CREDENTIAL_TOKEN_REFRESH_FAIL == "credential_token_refresh_fail"
    assert AuditAction.CREDENTIAL_EXPIRED == "credential_expired"


@pytest.mark.asyncio
async def test_write_audit_log_does_not_raise():
    """write_audit_log must never raise, even on DB failure."""
    from app.services.audit_logger import write_audit_log
    with patch("app.services.audit_logger.async_session", side_effect=Exception("DB down")):
        # Should not raise
        await write_audit_log(
            action="credential_resolve",
            details={"provider": "jira", "tool_name": "query_jira"},
            user_id=uuid.uuid4(),
        )
