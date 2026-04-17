"""OAuth service — token exchange, refresh, and state management.

Handles the OAuth2 authorization code flow for external system credentials.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from jose import JWTError, jwt
from loguru import logger

from app.config import get_settings

# In-memory state store for consumed OAuth states with timestamps.
# States are cleaned up after 15 minutes to prevent unbounded growth.
# Production: replace with Redis for multi-process deployments.
_consumed_states: dict[str, float] = {}  # jti -> timestamp
_CONSUMED_STATE_TTL = 900  # 15 minutes


def _cleanup_consumed_states() -> None:
    """Remove expired entries from the consumed states store."""
    import time
    cutoff = time.time() - _CONSUMED_STATE_TTL
    expired = [k for k, v in _consumed_states.items() if v < cutoff]
    for k in expired:
        del _consumed_states[k]


@dataclass
class OAuthTokens:
    """Tokens returned from an OAuth token endpoint."""

    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None


def generate_oauth_state(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    provider: str,
    one_time_token: str | None = None,
    flow: str = "web",
) -> str:
    """Generate a signed state parameter for OAuth authorization.

    Args:
        user_id: Clawith user.
        tenant_id: Tenant for isolation.
        provider: OAuth provider name.
        one_time_token: If set, this is a channel-user flow (no web login).
        flow: "web" or "channel" — determines callback redirect target.
    """
    settings = get_settings()
    jti = uuid.uuid4().hex
    payload = {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "provider": provider,
        "flow": flow,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "jti": jti,
        "type": "oauth_state",
    }
    if one_time_token:
        payload["ott"] = one_time_token  # carry the one-time-token for channel flow
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def validate_oauth_state(state: str) -> dict:
    """Validate and consume an OAuth state parameter.

    Returns:
        Dict with user_id (UUID), tenant_id (UUID), provider (str).

    Raises:
        ValueError: If state is invalid, expired, or already consumed.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError as e:
        raise ValueError(f"Invalid or expired OAuth state: {e}")

    if payload.get("type") != "oauth_state":
        raise ValueError("Invalid state type")

    jti = payload.get("jti")
    if not jti:
        raise ValueError("State missing jti")

    if jti in _consumed_states:
        raise ValueError("OAuth state has already been used")

    import time
    _consumed_states[jti] = time.time()
    # Periodic cleanup to prevent unbounded growth
    if len(_consumed_states) > 100:
        _cleanup_consumed_states()

    return {
        "user_id": uuid.UUID(payload["user_id"]),
        "tenant_id": uuid.UUID(payload["tenant_id"]),
        "provider": payload["provider"],
        "flow": payload.get("flow", "web"),
    }


async def exchange_code_for_tokens(
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    Raises:
        ValueError: If the token exchange fails.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        logger.warning(f"[OAuth] Token exchange failed: {resp.status_code} {resp.text[:200]}")
        raise ValueError(f"Token exchange failed: {resp.status_code}")

    data = resp.json()
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scope=data.get("scope"),
    )


async def refresh_access_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> OAuthTokens:
    """Refresh an expired access token using a refresh token.

    Raises:
        ValueError: If the refresh fails.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        logger.warning(f"[OAuth] Token refresh failed: {resp.status_code} {resp.text[:200]}")
        raise ValueError(f"Token refresh failed: {resp.status_code}")

    data = resp.json()
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scope=data.get("scope"),
    )
