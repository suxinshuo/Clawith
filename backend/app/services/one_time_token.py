"""One-time JWT tokens for credential configuration links.

Used by channel users (Feishu/DingTalk/WeCom) who don't have a Clawith Web login
to configure credentials via a standalone browser page.
"""

import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from loguru import logger

from app.config import get_settings

# In-memory jti store for consumed tokens.
# Production: replace with Redis SET NX or DB table for multi-process deployments.
_consumed_jtis: set[str] = set()


def generate_one_time_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    provider: str,
    credential_mode: str = "manual",
    ttl_minutes: int = 10,
) -> str:
    """Generate a short-lived, one-time-use JWT for credential configuration.

    Args:
        user_id: Clawith user who will own the credential.
        tenant_id: Tenant for isolation.
        provider: Target provider (e.g. "jira", "internal_erp").
        credential_mode: "manual" (API key form) or "oauth" (redirect to OAuth).
        ttl_minutes: Token lifetime in minutes.

    Returns:
        Signed JWT string.
    """
    settings = get_settings()
    jti = uuid.uuid4().hex
    payload = {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "provider": provider,
        "credential_mode": credential_mode,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        "jti": jti,
        "type": "credential_connect",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def validate_one_time_token(token: str) -> dict:
    """Validate and consume a one-time credential token.

    Args:
        token: The JWT string to validate.

    Returns:
        Decoded payload dict with user_id, tenant_id, provider, credential_mode.

    Raises:
        ValueError: If token is invalid, expired, or already consumed.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError as e:
        raise ValueError(f"Invalid or expired token: {e}")

    if payload.get("type") != "credential_connect":
        raise ValueError("Invalid token type")

    jti = payload.get("jti")
    if not jti:
        raise ValueError("Token missing jti")

    # Check if already consumed
    if jti in _consumed_jtis:
        raise ValueError("Token has already been used")

    # Mark as consumed
    _consumed_jtis.add(jti)

    return {
        "user_id": uuid.UUID(payload["user_id"]),
        "tenant_id": uuid.UUID(payload["tenant_id"]),
        "provider": payload["provider"],
        "credential_mode": payload.get("credential_mode", "manual"),
    }
