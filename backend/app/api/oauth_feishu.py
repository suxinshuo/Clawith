"""Feishu OAuth credential callback — exchanges code for user token and stores credential.

Unlike the generic OAuth flow (oauth_credentials.py), this endpoint handles
Feishu's OIDC-specific token exchange which requires app_access_token as
Bearer auth instead of client_id/client_secret in the body.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import async_session
from app.models.user_external_credential import UserExternalCredential
from app.services.oauth_service import validate_oauth_state

router = APIRouter(prefix="/oauth/feishu", tags=["oauth-feishu"])


@router.get("/credential-callback")
async def feishu_credential_callback(
    code: str = Query(..., min_length=1, max_length=4096),
    state: str = Query(..., min_length=1),
):
    """Feishu OAuth callback — exchange code for user_access_token and store credential.

    Feishu OIDC token exchange:
      POST /open-apis/authen/v1/oidc/access_token
      Authorization: Bearer <app_access_token>
      Body: {"grant_type": "authorization_code", "code": "<code>"}
    """
    import httpx

    # 1. Validate state
    try:
        state_payload = await validate_oauth_state(state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {e}")

    user_id = state_payload["user_id"]
    tenant_id = state_payload["tenant_id"]
    provider = state_payload["provider"]  # "feishu:{agent_id}"
    agent_id = state_payload.get("agent_id")

    if not agent_id:
        raise HTTPException(status_code=400, detail="Missing agent_id in OAuth state")

    # 2. Get app credentials from ChannelConfig
    from app.models.channel_config import ChannelConfig
    async with async_session() as db:
        r = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        config = r.scalar_one_or_none()
        if not config or not config.app_id or not config.app_secret:
            raise HTTPException(status_code=400, detail="No Feishu channel config found for this agent")
        app_id = config.app_id
        app_secret = config.app_secret

    # 3. Get app_access_token (needed as Bearer for OIDC exchange)
    from app.services.feishu_service import feishu_service
    app_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # 4. Exchange code for user tokens via Feishu OIDC
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
            json={"grant_type": "authorization_code", "code": code},
            headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        )

    data = resp.json()
    if data.get("code") != 0:
        msg = data.get("msg", "unknown error")
        logger.warning(f"[FeishuOAuth] Token exchange failed: code={data.get('code')}, msg={msg}")
        return HTMLResponse(
            content=f"<html><body><h2>授权失败</h2><p>{msg}</p>"
            "<p>请关闭此页面并重试。如果该飞书应用未开启用户授权功能，请联系应用管理员在飞书开发者后台配置。</p>"
            "</body></html>",
            status_code=400,
        )

    token_data = data.get("data", {})
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 7200)
    scope = token_data.get("scope", "")
    open_id = token_data.get("open_id", "")
    name = token_data.get("name", "")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access_token in Feishu response")

    # 5. Upsert UserExternalCredential
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in) if expires_in else None

    async with async_session() as db:
        result = await db.execute(
            select(UserExternalCredential).where(
                UserExternalCredential.user_id == user_id,
                UserExternalCredential.tenant_id == tenant_id,
                UserExternalCredential.provider == provider,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.access_token_encrypted = encrypt_data(access_token, settings.SECRET_KEY)
            existing.credential_type = "oauth2"
            existing.status = "active"
            existing.token_expires_at = expires_at
            existing.external_user_id = open_id or existing.external_user_id
            existing.external_username = name or existing.external_username
            if refresh_token:
                existing.refresh_token_encrypted = encrypt_data(refresh_token, settings.SECRET_KEY)
            if scope:
                existing.scopes = scope
            existing.updated_at = now
        else:
            cred = UserExternalCredential(
                user_id=user_id,
                tenant_id=tenant_id,
                provider=provider,
                credential_type="oauth2",
                access_token_encrypted=encrypt_data(access_token, settings.SECRET_KEY),
                refresh_token_encrypted=encrypt_data(refresh_token, settings.SECRET_KEY) if refresh_token else None,
                token_expires_at=expires_at,
                scopes=scope,
                external_user_id=open_id,
                external_username=name,
                status="active",
            )
            db.add(cred)

        await db.commit()

    # Audit
    from app.services.audit_logger import write_audit_log
    asyncio.create_task(write_audit_log(
        action="credential_feishu_oauth_complete",
        details={"provider": provider, "agent_id": str(agent_id)},
        user_id=user_id,
    ))

    logger.info(f"[FeishuOAuth] Credential stored for user={user_id}, provider={provider}")

    # 6. Success page
    return HTMLResponse(
        content=(
            "<html><body style='font-family: sans-serif; text-align: center; padding: 60px;'>"
            "<h2>✅ 飞书授权成功</h2>"
            "<p>您的飞书账号已成功授权，请返回对话继续操作。</p>"
            "<p style='color: #888;'>此页面可以安全关闭。</p>"
            "</body></html>"
        ),
        status_code=200,
    )
