"""User External Credential API — manage per-user credentials for external systems.

Endpoints:
  GET    /credentials/me              List current user's credentials
  POST   /credentials/manual          Add a credential manually (API key)
  DELETE /credentials/{id}            Delete a credential
  PATCH  /credentials/{id}            Update a credential
  POST   /credentials/submit          Submit via one-time token (no login required)
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import encrypt_data, get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_external_credential import UserExternalCredential, TenantExternalCredential
from app.schemas.user_credential import (
    UserCredentialCreate,
    UserCredentialResponse,
    UserCredentialUpdate,
    TenantCredentialCreate,
    TenantCredentialResponse,
    OneTimeTokenSubmit,
)
from app.services.one_time_token import validate_one_time_token

router = APIRouter(prefix="/credentials", tags=["user-credentials"])


def _to_response(cred: UserExternalCredential) -> dict:
    """Convert credential to safe response dict — never expose tokens."""
    return {
        "id": cred.id,
        "provider": cred.provider,
        "credential_type": cred.credential_type,
        "status": cred.status,
        "display_name": getattr(cred, "display_name", None),
        "external_user_id": cred.external_user_id,
        "external_username": cred.external_username,
        "scopes": cred.scopes,
        "last_used_at": cred.last_used_at,
        "created_at": cred.created_at,
        "updated_at": cred.updated_at,
    }


@router.get("/me")
async def list_my_credentials(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all external credentials for the current user."""
    result = await db.execute(
        select(UserExternalCredential)
        .where(UserExternalCredential.user_id == current_user.id)
        .order_by(UserExternalCredential.created_at.desc())
    )
    credentials = result.scalars().all()
    return [_to_response(c) for c in credentials]


@router.post("/manual", status_code=status.HTTP_201_CREATED)
async def create_credential(
    data: UserCredentialCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually add an API key credential."""
    settings = get_settings()

    cred = UserExternalCredential(
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        provider=data.provider,
        credential_type=data.credential_type,
        access_token_encrypted=encrypt_data(data.access_token, settings.SECRET_KEY),
        display_name=data.display_name,
        external_user_id=data.external_user_id,
        external_username=data.external_username,
        scopes=data.scopes,
        status="active",
    )

    db.add(cred)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "uq_user_external_credential_provider" in str(e):
            raise HTTPException(status_code=409, detail=f"Credential for provider '{data.provider}' already exists")
        raise
    await db.refresh(cred)

    from app.services.audit_logger import write_audit_log
    import asyncio
    asyncio.create_task(write_audit_log(
        action="credential_create",
        details={"provider": data.provider, "credential_type": data.credential_type},
        user_id=current_user.id,
    ))
    return _to_response(cred)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete one of the current user's credentials."""
    result = await db.execute(
        select(UserExternalCredential).where(
            UserExternalCredential.id == credential_id,
            UserExternalCredential.user_id == current_user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    await db.delete(cred)
    await db.commit()

    from app.services.audit_logger import write_audit_log
    import asyncio
    asyncio.create_task(write_audit_log(
        action="credential_delete",
        details={"provider": cred.provider, "credential_id": str(cred.id)},
        user_id=current_user.id,
    ))


@router.patch("/{credential_id}")
async def update_credential(
    credential_id: uuid.UUID,
    data: UserCredentialUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing credential."""
    result = await db.execute(
        select(UserExternalCredential).where(
            UserExternalCredential.id == credential_id,
            UserExternalCredential.user_id == current_user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    settings = get_settings()
    update_data = data.model_dump(exclude_unset=True)

    if "access_token" in update_data and update_data["access_token"]:
        cred.access_token_encrypted = encrypt_data(update_data.pop("access_token"), settings.SECRET_KEY)

    for field in ("display_name", "external_user_id", "external_username", "scopes", "status"):
        if field in update_data:
            setattr(cred, field, update_data[field])

    cred.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cred)

    return _to_response(cred)


@router.post("/submit")
async def submit_credential_via_token(
    data: OneTimeTokenSubmit,
    db: AsyncSession = Depends(get_db),
):
    """Submit a credential via one-time token link (no login required).

    Used by channel users (Feishu/DingTalk/WeCom) who don't have a Clawith Web login.
    """
    try:
        payload = validate_one_time_token(data.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    settings = get_settings()
    user_id = payload["user_id"]
    tenant_id = payload["tenant_id"]
    provider = payload["provider"]

    # Check if credential already exists for this user+provider
    result = await db.execute(
        select(UserExternalCredential).where(
            UserExternalCredential.user_id == user_id,
            UserExternalCredential.provider == provider,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Update existing credential
        existing.access_token_encrypted = encrypt_data(data.access_token, settings.SECRET_KEY)
        existing.status = "active"
        if data.external_user_id:
            existing.external_user_id = data.external_user_id
        if data.external_username:
            existing.external_username = data.external_username
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return {"status": "updated", "provider": provider}
    else:
        # Create new credential
        cred = UserExternalCredential(
            user_id=user_id,
            tenant_id=tenant_id,
            provider=provider,
            credential_type="api_key",
            access_token_encrypted=encrypt_data(data.access_token, settings.SECRET_KEY),
            external_user_id=data.external_user_id,
            external_username=data.external_username,
            status="active",
        )
        db.add(cred)
        await db.commit()
        return {"status": "created", "provider": provider}
