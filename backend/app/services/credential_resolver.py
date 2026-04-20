"""Resolve per-user or per-tenant external credentials for MCP tool execution."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.models.user_external_credential import AgentExternalCredential, TenantExternalCredential, UserExternalCredential

_CredentialTable = type[UserExternalCredential] | type[TenantExternalCredential] | type[AgentExternalCredential]


def parse_credential_scopes(s: str | None) -> list[str]:
    """Normalize comma or space-separated scope strings into a list.

    Shared by CredentialResolver and agent_tools scope validation.
    """
    if not s:
        return []
    return [p.strip() for p in s.replace(",", " ").split() if p.strip()]

# In-memory lock fallback when Redis is unavailable.
_refresh_locks: dict[str, asyncio.Lock] = {}


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
    source: str  # "user" | "agent" | "tenant"
    credential_id: UUID


class CredentialResolver:
    """Resolve the correct external credential for a user + provider."""

    async def resolve(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider: str,
        *,
        agent_id: UUID | None = None,
    ) -> ResolvedCredential | None:
        """Resolve credential with priority: user > agent > tenant. Returns None if not found."""
        from app.models.user_external_credential import (
            UserExternalCredential,
            AgentExternalCredential,
            TenantExternalCredential,
        )

        async with async_session() as db:
            # 1. Try user-level credential (tenant_id for defense-in-depth)
            result = await db.execute(
                select(UserExternalCredential).where(
                    UserExternalCredential.user_id == user_id,
                    UserExternalCredential.tenant_id == tenant_id,
                    UserExternalCredential.provider == provider,
                )
            )
            user_cred = result.scalar_one_or_none()

            if user_cred and user_cred.status == "active":
                try:
                    token = await self._ensure_token_fresh(
                        credential_id=user_cred.id,
                        credential_type=user_cred.credential_type,
                        provider=provider,
                        tenant_id=tenant_id,
                        access_token_encrypted=user_cred.access_token_encrypted,
                        refresh_token_encrypted=getattr(user_cred, 'refresh_token_encrypted', None),
                        token_expires_at=getattr(user_cred, 'token_expires_at', None),
                        table_class=UserExternalCredential,
                    )
                except Exception:
                    logger.warning(f"[CredentialResolver] Failed to resolve user credential for provider={provider}")
                    token = None

                if token:
                    resolved = ResolvedCredential(
                        provider=provider,
                        credential_type=user_cred.credential_type,
                        access_token=token,
                        external_user_id=user_cred.external_user_id,
                        external_username=user_cred.external_username,
                        scopes=parse_credential_scopes(user_cred.scopes),
                        source="user",
                        credential_id=user_cred.id,
                    )
                    # Fire-and-forget: _update_last_used creates its own DB session,
                    # so it is safe to run detached from the current context manager.
                    asyncio.create_task(self._update_last_used(user_cred.id, UserExternalCredential))
                    return resolved

            # 2. Fallback to agent-level credential
            if agent_id:
                result = await db.execute(
                    select(AgentExternalCredential).where(
                        AgentExternalCredential.agent_id == agent_id,
                        AgentExternalCredential.provider == provider,
                    )
                )
                agent_cred = result.scalar_one_or_none()

                if agent_cred and agent_cred.status == "active":
                    try:
                        token = await self._ensure_token_fresh(
                            credential_id=agent_cred.id,
                            credential_type=agent_cred.credential_type,
                            provider=provider,
                            tenant_id=tenant_id,
                            access_token_encrypted=agent_cred.access_token_encrypted,
                            refresh_token_encrypted=getattr(agent_cred, 'refresh_token_encrypted', None),
                            token_expires_at=getattr(agent_cred, 'token_expires_at', None),
                            table_class=AgentExternalCredential,
                        )
                    except Exception:
                        logger.warning(f"[CredentialResolver] Failed to resolve agent credential for provider={provider}")
                        token = None

                    if token:
                        resolved = ResolvedCredential(
                            provider=provider,
                            credential_type=agent_cred.credential_type,
                            access_token=token,
                            external_user_id=agent_cred.external_user_id,
                            external_username=agent_cred.external_username,
                            scopes=parse_credential_scopes(agent_cred.scopes),
                            source="agent",
                            credential_id=agent_cred.id,
                        )
                        asyncio.create_task(self._update_last_used(agent_cred.id, AgentExternalCredential))
                        return resolved

            # 3. Fallback to tenant-level credential
            result = await db.execute(
                select(TenantExternalCredential).where(
                    TenantExternalCredential.tenant_id == tenant_id,
                    TenantExternalCredential.provider == provider,
                )
            )
            tenant_cred = result.scalar_one_or_none()

            if tenant_cred and tenant_cred.status == "active":
                try:
                    token = await self._ensure_token_fresh(
                        credential_id=tenant_cred.id,
                        credential_type=tenant_cred.credential_type,
                        provider=provider,
                        tenant_id=tenant_id,
                        access_token_encrypted=tenant_cred.access_token_encrypted,
                        refresh_token_encrypted=getattr(tenant_cred, 'refresh_token_encrypted', None),
                        token_expires_at=getattr(tenant_cred, 'token_expires_at', None),
                        table_class=TenantExternalCredential,
                    )
                except Exception:
                    logger.warning(f"[CredentialResolver] Failed to resolve tenant credential for provider={provider}")
                    return None

                if token:
                    resolved = ResolvedCredential(
                        provider=provider,
                        credential_type=tenant_cred.credential_type,
                        access_token=token,
                        external_user_id=tenant_cred.external_user_id if hasattr(tenant_cred, 'external_user_id') else None,
                        external_username=tenant_cred.external_username if hasattr(tenant_cred, 'external_username') else None,
                        scopes=parse_credential_scopes(tenant_cred.scopes),
                        source="tenant",
                        credential_id=tenant_cred.id,
                    )
                    # Fire-and-forget: _update_last_used creates its own DB session.
                    asyncio.create_task(self._update_last_used(tenant_cred.id, TenantExternalCredential))
                    return resolved

        return None

    async def _update_last_used(self, credential_id: UUID, table_class: _CredentialTable) -> None:
        """Update last_used_at timestamp for a resolved credential."""
        try:
            from datetime import datetime, timezone
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(table_class)
                    .where(table_class.id == credential_id)
                    .values(last_used_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception:
            logger.debug(f"[CredentialResolver] Failed to update last_used_at for {credential_id}")

    async def _acquire_refresh_lock(self, credential_id: UUID, timeout: int = 10) -> bool:
        """Acquire a distributed lock for token refresh.

        Tries Redis SET NX first; falls back to asyncio.Lock per credential_id.
        Returns True if lock acquired.
        """
        lock_key = f"cred_refresh:{credential_id}"
        try:
            from app.core.events import get_redis
            redis = await get_redis()
            return bool(await redis.set(lock_key, "1", nx=True, ex=timeout))
        except Exception:
            # Redis unavailable — use in-memory asyncio.Lock
            logger.debug(f"[CredentialResolver] Redis unavailable for refresh lock {lock_key}, using in-memory lock")
            return True  # in-memory lock acquired via _get_memory_lock context

    async def _release_refresh_lock(self, credential_id: UUID) -> None:
        """Release the distributed refresh lock."""
        lock_key = f"cred_refresh:{credential_id}"
        try:
            from app.core.events import get_redis
            redis = await get_redis()
            await redis.delete(lock_key)
        except Exception:
            logger.debug(f"[CredentialResolver] Redis unavailable for releasing refresh lock {lock_key}")

    def _get_memory_lock(self, credential_id: UUID) -> asyncio.Lock:
        """Get or create an in-memory asyncio.Lock for a credential."""
        key = str(credential_id)
        if key not in _refresh_locks:
            _refresh_locks[key] = asyncio.Lock()
        return _refresh_locks[key]

    async def _ensure_token_fresh(
        self,
        *,
        credential_id: UUID,
        credential_type: str,
        provider: str,
        tenant_id: UUID,
        access_token_encrypted: str,
        refresh_token_encrypted: str | None,
        token_expires_at: datetime | None,
        table_class: _CredentialTable,
    ) -> str | None:
        """Check if an OAuth token is expired and attempt to refresh it.

        All credential fields are passed as scalar parameters (not ORM objects)
        to avoid detached-session issues. Uses a distributed lock (Redis) or
        in-memory lock to prevent concurrent refresh of the same credential.

        Returns the valid access token (possibly refreshed), or None if
        refresh failed and credential was marked needs_reauth.
        """
        from datetime import datetime, timedelta, timezone

        settings = get_settings()

        # Not an OAuth credential or no expiry set — return as-is
        if credential_type != "oauth2" or not token_expires_at:
            return decrypt_data(access_token_encrypted, settings.SECRET_KEY)

        # Check if token is still fresh (5-minute buffer)
        now = datetime.now(timezone.utc)
        expires_at = token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at > now + timedelta(minutes=5):
            # Token still valid
            return decrypt_data(access_token_encrypted, settings.SECRET_KEY)

        # Token expired or about to expire — try refresh
        if not refresh_token_encrypted:
            # No refresh token — mark as needs_reauth
            logger.info(f"[CredentialResolver] Token expired for provider={provider}, no refresh token")
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(table_class)
                    .where(table_class.id == credential_id)
                    .values(status="needs_reauth")
                )
                await db.commit()
            return None

        # Acquire lock to prevent concurrent refresh of the same credential.
        # Many OAuth providers invalidate the old refresh_token on use,
        # so a second concurrent refresh would fail and incorrectly mark needs_reauth.
        lock = self._get_memory_lock(credential_id)
        async with lock:
            acquired = await self._acquire_refresh_lock(credential_id)
            if not acquired:
                # Another process is refreshing — return current token and let caller retry
                logger.info(f"[CredentialResolver] Refresh lock contention for provider={provider}, returning current token")
                try:
                    return decrypt_data(access_token_encrypted, settings.SECRET_KEY)
                except Exception:
                    logger.warning(f"[CredentialResolver] Failed to decrypt token during lock contention for provider={provider}, credential_id={credential_id}")
                    return None

            try:
                return await self._do_refresh(
                    credential_id=credential_id,
                    provider=provider,
                    tenant_id=tenant_id,
                    access_token_encrypted=access_token_encrypted,
                    refresh_token_encrypted=refresh_token_encrypted,
                    table_class=table_class,
                )
            finally:
                await self._release_refresh_lock(credential_id)

    async def _do_refresh(
        self,
        *,
        credential_id: UUID,
        provider: str,
        tenant_id: UUID,
        access_token_encrypted: str,
        refresh_token_encrypted: str,
        table_class: _CredentialTable,
    ) -> "str | None":
        """Execute the actual OAuth token refresh (called under lock)."""
        from datetime import datetime, timedelta, timezone

        settings = get_settings()
        now = datetime.now(timezone.utc)

        try:
            # ── Feishu-specific refresh ──────────────────────────────────
            if provider.startswith("feishu:"):
                return await self._do_refresh_feishu(
                    credential_id=credential_id,
                    provider=provider,
                    tenant_id=tenant_id,
                    refresh_token_encrypted=refresh_token_encrypted,
                    table_class=table_class,
                )

            # ── Generic OAuth refresh (existing logic) ──────────────────
            from app.models.oauth_provider_config import OAuthProviderConfig
            from sqlalchemy import select as sa_select

            async with async_session() as db:
                result = await db.execute(
                    sa_select(OAuthProviderConfig).where(
                        OAuthProviderConfig.tenant_id == tenant_id,
                        OAuthProviderConfig.provider == provider,
                    )
                )
                oauth_config = result.scalar_one_or_none()
                if not oauth_config:
                    logger.warning(f"[CredentialResolver] No OAuth config for provider={provider}")
                    return decrypt_data(access_token_encrypted, settings.SECRET_KEY)

                # Extract scalar values before session closes to avoid detached-object issues
                config_token_url = oauth_config.token_url
                config_client_id = oauth_config.client_id
                config_client_secret_encrypted = oauth_config.client_secret_encrypted

            # Decrypt secrets
            client_secret = decrypt_data(config_client_secret_encrypted, settings.SECRET_KEY)
            refresh_token = decrypt_data(refresh_token_encrypted, settings.SECRET_KEY)

            # Call refresh
            from app.services.oauth_service import refresh_access_token
            tokens = await refresh_access_token(
                token_url=config_token_url,
                client_id=config_client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
            )

            # Update stored tokens
            from app.core.security import encrypt_data
            from sqlalchemy import update
            async with async_session() as db:
                update_values = {
                    "access_token_encrypted": encrypt_data(tokens.access_token, settings.SECRET_KEY),
                    "token_expires_at": now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None,
                    "status": "active",
                }
                if tokens.refresh_token:
                    update_values["refresh_token_encrypted"] = encrypt_data(tokens.refresh_token, settings.SECRET_KEY)
                if tokens.scope:
                    update_values["scopes"] = tokens.scope

                await db.execute(
                    update(table_class)
                    .where(table_class.id == credential_id)
                    .values(**update_values)
                )
                await db.commit()

            # Audit log
            from app.services.audit_logger import write_audit_log
            asyncio.create_task(write_audit_log(
                action="credential_token_refresh",
                details={"provider": provider, "credential_id": str(credential_id)},
            ))

            logger.info(f"[CredentialResolver] Token refreshed for provider={provider}")
            return tokens.access_token

        except Exception as e:
            logger.exception(f"[CredentialResolver] Token refresh failed for provider={provider}")
            # Audit log refresh failure
            from app.services.audit_logger import write_audit_log
            asyncio.create_task(write_audit_log(
                action="credential_token_refresh_fail",
                details={"provider": provider, "error": str(e)[:200]},
            ))
            # mark credential as needs_reauth instead of returning expired token
            try:
                from sqlalchemy import update
                async with async_session() as db:
                    await db.execute(
                        update(table_class)
                        .where(table_class.id == credential_id)
                        .values(status="needs_reauth")
                    )
                    await db.commit()
            except Exception:
                logger.debug(f"[CredentialResolver] Failed to mark needs_reauth for {credential_id}")
            return None

    async def _do_refresh_feishu(
        self,
        *,
        credential_id: UUID,
        provider: str,
        tenant_id: UUID,
        refresh_token_encrypted: str,
        table_class: _CredentialTable,
    ) -> str | None:
        """Feishu-specific token refresh using OIDC refresh endpoint.

        Feishu requires app_access_token as Bearer auth (not client_id/secret in body).
        The agent_id is extracted from the provider string "feishu:{agent_id}".
        """
        import httpx
        from datetime import datetime, timedelta, timezone

        settings = get_settings()
        now = datetime.now(timezone.utc)

        try:
            # Extract agent_id from provider
            agent_id_str = provider.split(":", 1)[1]
            agent_id = UUID(agent_id_str)

            # Get app credentials from ChannelConfig
            from app.models.channel_config import ChannelConfig
            async with async_session() as db:
                result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "feishu",
                    )
                )
                config = result.scalar_one_or_none()
                if not config or not config.app_id or not config.app_secret:
                    logger.warning(f"[CredentialResolver] No Feishu config for agent={agent_id}")
                    return None

                app_id = config.app_id
                app_secret = config.app_secret

            # Get app_access_token
            from app.services.feishu_service import feishu_service
            app_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

            # Decrypt refresh token
            refresh_token = decrypt_data(refresh_token_encrypted, settings.SECRET_KEY)

            # Call Feishu OIDC refresh
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
                    json={"grant_type": "refresh_token", "refresh_token": refresh_token},
                    headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
                )

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"Feishu refresh failed: {data.get('msg', 'unknown')}")

            token_data = data.get("data", {})
            new_access_token = token_data["access_token"]
            new_refresh_token = token_data.get("refresh_token", "")
            expires_in = token_data.get("expires_in", 7200)

            # Update stored tokens
            from app.core.security import encrypt_data
            from sqlalchemy import update
            async with async_session() as db:
                update_values = {
                    "access_token_encrypted": encrypt_data(new_access_token, settings.SECRET_KEY),
                    "token_expires_at": now + timedelta(seconds=expires_in) if expires_in else None,
                    "status": "active",
                }
                if new_refresh_token:
                    update_values["refresh_token_encrypted"] = encrypt_data(new_refresh_token, settings.SECRET_KEY)

                await db.execute(
                    update(table_class)
                    .where(table_class.id == credential_id)
                    .values(**update_values)
                )
                await db.commit()

            # Audit
            from app.services.audit_logger import write_audit_log
            asyncio.create_task(write_audit_log(
                action="credential_token_refresh",
                details={"provider": provider, "credential_id": str(credential_id), "method": "feishu_oidc"},
            ))

            logger.info(f"[CredentialResolver] Feishu token refreshed for provider={provider}")
            return new_access_token

        except Exception as e:
            logger.exception(f"[CredentialResolver] Feishu token refresh failed for provider={provider}")
            from app.services.audit_logger import write_audit_log
            asyncio.create_task(write_audit_log(
                action="credential_token_refresh_fail",
                details={"provider": provider, "error": str(e)[:200], "method": "feishu_oidc"},
            ))
            # Mark as needs_reauth
            try:
                from sqlalchemy import update
                async with async_session() as db:
                    await db.execute(
                        update(table_class)
                        .where(table_class.id == credential_id)
                        .values(status="needs_reauth")
                    )
                    await db.commit()
            except Exception:
                logger.debug(f"[CredentialResolver] Failed to mark needs_reauth for {credential_id}")
            return None

    async def resolve_or_fail(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider: str,
        *,
        agent_id: UUID | None = None,
    ) -> ResolvedCredential:
        """Same as resolve(), but raises CredentialNotFoundError if not found."""
        result = await self.resolve(user_id, tenant_id, provider, agent_id=agent_id)
        if result is None:
            raise CredentialNotFoundError(provider)
        return result
