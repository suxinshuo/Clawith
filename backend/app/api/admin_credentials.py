"""Admin API for OAuth provider management and org-wide credential overview.

All endpoints require org_admin or platform_admin role.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import encrypt_data, get_current_user
from app.database import get_db
from app.models.oauth_provider_config import OAuthProviderConfig
from app.models.user import User
from app.models.user_external_credential import UserExternalCredential, TenantExternalCredential
from app.schemas.oauth_provider import OAuthProviderCreate, OAuthProviderResponse, OAuthProviderUpdate

router = APIRouter(prefix="/admin/credentials", tags=["admin-credentials"])


def _require_admin(user: User):
    if user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


# ── OAuth Provider Config CRUD ──

@router.get("/oauth-providers")
async def list_oauth_providers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all OAuth provider configs for current tenant."""
    _require_admin(current_user)
    result = await db.execute(
        select(OAuthProviderConfig)
        .where(OAuthProviderConfig.tenant_id == current_user.tenant_id)
        .order_by(OAuthProviderConfig.provider)
    )
    configs = result.scalars().all()
    return [OAuthProviderResponse.model_validate(c) for c in configs]


@router.post("/oauth-providers", status_code=status.HTTP_201_CREATED)
async def create_oauth_provider(
    data: OAuthProviderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a new OAuth provider for this tenant."""
    _require_admin(current_user)
    settings = get_settings()

    config = OAuthProviderConfig(
        tenant_id=current_user.tenant_id,
        provider=data.provider,
        client_id=data.client_id,
        client_secret_encrypted=encrypt_data(data.client_secret, settings.SECRET_KEY),
        authorize_url=data.authorize_url,
        token_url=data.token_url,
        scopes=data.scopes,
        redirect_uri=data.redirect_uri,
        created_by=current_user.id,
    )
    db.add(config)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "uq_oauth_provider_config" in str(e):
            raise HTTPException(status_code=409, detail=f"OAuth provider '{data.provider}' already configured for this tenant")
        raise
    await db.refresh(config)
    return OAuthProviderResponse.model_validate(config)


@router.patch("/oauth-providers/{provider_id}")
async def update_oauth_provider(
    provider_id: uuid.UUID,
    data: OAuthProviderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an OAuth provider config."""
    _require_admin(current_user)
    result = await db.execute(
        select(OAuthProviderConfig).where(
            OAuthProviderConfig.id == provider_id,
            OAuthProviderConfig.tenant_id == current_user.tenant_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="OAuth provider config not found")

    settings = get_settings()
    update_data = data.model_dump(exclude_unset=True)

    if "client_secret" in update_data and update_data["client_secret"]:
        config.client_secret_encrypted = encrypt_data(update_data.pop("client_secret"), settings.SECRET_KEY)

    for field in ("client_id", "authorize_url", "token_url", "scopes", "redirect_uri"):
        if field in update_data:
            setattr(config, field, update_data[field])

    config.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(config)
    return OAuthProviderResponse.model_validate(config)


@router.delete("/oauth-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_oauth_provider(
    provider_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an OAuth provider config."""
    _require_admin(current_user)
    result = await db.execute(
        select(OAuthProviderConfig).where(
            OAuthProviderConfig.id == provider_id,
            OAuthProviderConfig.tenant_id == current_user.tenant_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="OAuth provider config not found")
    await db.delete(config)
    await db.commit()


# ── Org-wide Credential Overview (masked) ──

@router.get("/overview")
async def credential_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get org-wide credential usage overview (masked, no tokens exposed)."""
    _require_admin(current_user)
    tid = current_user.tenant_id

    # Count user credentials by provider and status
    result = await db.execute(
        select(
            UserExternalCredential.provider,
            UserExternalCredential.status,
            sa_func.count().label("count"),
        )
        .where(UserExternalCredential.tenant_id == tid)
        .group_by(UserExternalCredential.provider, UserExternalCredential.status)
    )
    user_stats = [{"provider": r[0], "status": r[1], "count": r[2]} for r in result.all()]

    # Tenant-level credentials
    result = await db.execute(
        select(TenantExternalCredential)
        .where(TenantExternalCredential.tenant_id == tid)
    )
    tenant_creds = [
        {
            "id": str(c.id),
            "provider": c.provider,
            "credential_type": c.credential_type,
            "status": c.status,
            "display_name": c.display_name,
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in result.scalars().all()
    ]

    return {
        "user_credentials_by_provider": user_stats,
        "tenant_credentials": tenant_creds,
    }
