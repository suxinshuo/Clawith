"""Resolve per-user or per-tenant external credentials for MCP tool execution."""

from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from app.config import get_settings


def async_session():
    """Lazy proxy for app.database.async_session (defers asyncpg import)."""
    from app.database import async_session as _session
    return _session()


def decrypt_data(ciphertext: str, key: str) -> str:
    """Thin wrapper — delegates to app.core.security.decrypt_data (lazy import)."""
    from app.core.security import decrypt_data as _decrypt
    return _decrypt(ciphertext, key)


class CredentialNotFoundError(Exception):
    """Raised when no credential is found for the given user/tenant + provider."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"No credential found for provider: {provider}")


@dataclass
class ResolvedCredential:
    """A decrypted, ready-to-use credential."""

    provider: str
    credential_type: str
    access_token: str
    external_user_id: str | None
    external_username: str | None
    scopes: list[str]
    source: str  # "user" | "tenant"
    credential_id: UUID


class CredentialResolver:
    """Resolve the correct external credential for a user + provider."""

    async def resolve(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider: str,
    ) -> ResolvedCredential | None:
        """Resolve credential with priority: user > tenant. Returns None if not found."""
        from app.models.user_external_credential import (
            UserExternalCredential,
            TenantExternalCredential,
        )

        settings = get_settings()

        async with async_session() as db:
            # 1. Try user-level credential
            result = await db.execute(
                select(UserExternalCredential).where(
                    UserExternalCredential.user_id == user_id,
                    UserExternalCredential.provider == provider,
                )
            )
            user_cred = result.scalar_one_or_none()

            if user_cred and user_cred.status == "active":
                try:
                    token = decrypt_data(user_cred.access_token_encrypted, settings.SECRET_KEY)
                except Exception:
                    logger.warning(f"[CredentialResolver] Failed to decrypt user credential for provider={provider}")
                    token = None

                if token:
                    return ResolvedCredential(
                        provider=provider,
                        credential_type=user_cred.credential_type,
                        access_token=token,
                        external_user_id=user_cred.external_user_id,
                        external_username=user_cred.external_username,
                        scopes=[s.strip() for s in user_cred.scopes.split(",") if s.strip()] if user_cred.scopes else [],
                        source="user",
                        credential_id=user_cred.id,
                    )

            # 2. Fallback to tenant-level credential
            result = await db.execute(
                select(TenantExternalCredential).where(
                    TenantExternalCredential.tenant_id == tenant_id,
                    TenantExternalCredential.provider == provider,
                )
            )
            tenant_cred = result.scalar_one_or_none()

            if tenant_cred and tenant_cred.status == "active":
                try:
                    token = decrypt_data(tenant_cred.access_token_encrypted, settings.SECRET_KEY)
                except Exception:
                    logger.warning(f"[CredentialResolver] Failed to decrypt tenant credential for provider={provider}")
                    return None

                return ResolvedCredential(
                    provider=provider,
                    credential_type=tenant_cred.credential_type,
                    access_token=token,
                    external_user_id=tenant_cred.external_user_id if hasattr(tenant_cred, 'external_user_id') else None,
                    external_username=tenant_cred.external_username if hasattr(tenant_cred, 'external_username') else None,
                    scopes=[s.strip() for s in tenant_cred.scopes.split(",") if s.strip()] if tenant_cred.scopes else [],
                    source="tenant",
                    credential_id=tenant_cred.id,
                )

        return None

    async def resolve_or_fail(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider: str,
    ) -> ResolvedCredential:
        """Same as resolve(), but raises CredentialNotFoundError if not found."""
        result = await self.resolve(user_id, tenant_id, provider)
        if result is None:
            raise CredentialNotFoundError(provider)
        return result
