"""OAuth authorization flow endpoints for credential creation.

Two flows:
  1. Web users (logged in): GET /credentials/oauth/authorize → callback
  2. Channel users (no login): GET /credentials/oauth/start?token=<ott> → callback
"""

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import encrypt_data, get_current_user
from app.database import get_db
from app.models.oauth_provider_config import OAuthProviderConfig
from app.models.user import User
from app.models.user_external_credential import UserExternalCredential
from app.services.oauth_service import (
    generate_oauth_state,
    validate_oauth_state,
    exchange_code_for_tokens,
)
from app.services.one_time_token import validate_one_time_token

router = APIRouter(prefix="/credentials/oauth", tags=["oauth-credentials"])


async def _get_oauth_config(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str,
) -> OAuthProviderConfig:
    """Load and validate OAuth provider config."""
    result = await db.execute(
        select(OAuthProviderConfig).where(
            OAuthProviderConfig.tenant_id == tenant_id,
            OAuthProviderConfig.provider == provider,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail=f"OAuth provider '{provider}' not configured for this tenant")
    return config


# ── Flow 1: Web users (logged in) ──

@router.get("/authorize")
async def oauth_authorize(
    provider: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiate OAuth authorization for a logged-in web user.

    Returns JSON with the authorize URL. The frontend navigates the browser.
    (Cannot use RedirectResponse because this endpoint requires Bearer auth
    which browser redirects don't carry.)
    """
    config = await _get_oauth_config(db, current_user.tenant_id, provider)
    state = generate_oauth_state(current_user.id, current_user.tenant_id, provider, flow="web")

    from app.services.audit_logger import write_audit_log
    import asyncio
    asyncio.create_task(write_audit_log(
        action="credential_oauth_start",
        details={"provider": provider, "flow": "web"},
        user_id=current_user.id,
    ))

    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if config.scopes:
        params["scope"] = config.scopes

    authorize_url = f"{config.authorize_url}?{urlencode(params)}"
    return {"authorize_url": authorize_url}


# ── Flow 2: Channel users (no login, one-time token) ──

@router.get("/start")
async def oauth_start_via_token(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Initiate OAuth for a channel user via one-time token link.

    Validates the one-time token, then redirects to the external OAuth page.
    """
    try:
        payload = validate_one_time_token(token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if payload.get("credential_mode") != "oauth":
        raise HTTPException(status_code=400, detail="Token is not for OAuth flow")

    user_id = payload["user_id"]
    tenant_id = payload["tenant_id"]
    provider = payload["provider"]

    config = await _get_oauth_config(db, tenant_id, provider)
    state = generate_oauth_state(user_id, tenant_id, provider, flow="channel")

    from app.services.audit_logger import write_audit_log
    import asyncio
    asyncio.create_task(write_audit_log(
        action="credential_oauth_start",
        details={"provider": provider, "flow": "channel"},
        user_id=user_id,
    ))

    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if config.scopes:
        params["scope"] = config.scopes

    authorize_url = f"{config.authorize_url}?{urlencode(params)}"
    return RedirectResponse(url=authorize_url)


# ── Shared callback (handles both flows) ──

@router.get("/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """OAuth callback — exchange code for tokens and store credential.

    Handles both web-user and channel-user flows (distinguished by state payload).
    """
    try:
        state_payload = validate_oauth_state(state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {e}")

    user_id = state_payload["user_id"]
    tenant_id = state_payload["tenant_id"]
    provider = state_payload["provider"]

    config = await _get_oauth_config(db, tenant_id, provider)
    settings = get_settings()

    # Decrypt client_secret
    from app.core.security import decrypt_data
    client_secret = decrypt_data(config.client_secret_encrypted, settings.SECRET_KEY)

    # Exchange code for tokens
    try:
        tokens = await exchange_code_for_tokens(
            token_url=config.token_url,
            client_id=config.client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=config.redirect_uri,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    # Upsert credential
    result = await db.execute(
        select(UserExternalCredential).where(
            UserExternalCredential.user_id == user_id,
            UserExternalCredential.provider == provider,
        )
    )
    existing = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None

    if existing:
        existing.access_token_encrypted = encrypt_data(tokens.access_token, settings.SECRET_KEY)
        existing.credential_type = "oauth2"
        existing.status = "active"
        existing.token_expires_at = expires_at
        if tokens.refresh_token:
            existing.refresh_token_encrypted = encrypt_data(tokens.refresh_token, settings.SECRET_KEY)
        if tokens.scope:
            existing.scopes = tokens.scope
        existing.updated_at = now
    else:
        cred = UserExternalCredential(
            user_id=user_id,
            tenant_id=tenant_id,
            provider=provider,
            credential_type="oauth2",
            access_token_encrypted=encrypt_data(tokens.access_token, settings.SECRET_KEY),
            refresh_token_encrypted=encrypt_data(tokens.refresh_token, settings.SECRET_KEY) if tokens.refresh_token else None,
            token_expires_at=expires_at,
            scopes=tokens.scope,
            status="active",
        )
        db.add(cred)

    await db.commit()

    # Audit
    import asyncio
    from app.services.audit_logger import write_audit_log
    asyncio.create_task(write_audit_log(
        action="credential_oauth_complete",
        details={"provider": provider},
        user_id=user_id,
    ))

    # Redirect based on flow: web users → settings page, channel users → standalone success page
    base_url = settings.PUBLIC_BASE_URL.rstrip("/") if settings.PUBLIC_BASE_URL else ""
    flow = state_payload.get("flow", "web")
    if flow == "web":
        return RedirectResponse(url=f"{base_url}/external-connections?oauth_success=1&provider={provider}")
    else:
        return RedirectResponse(url=f"{base_url}/credentials/connect?oauth_success=1&provider={provider}")
