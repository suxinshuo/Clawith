"""Tests for OAuth service — token exchange, refresh, state management."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.services.oauth_service import (
    generate_oauth_state,
    validate_oauth_state,
    exchange_code_for_tokens,
    refresh_access_token,
    OAuthTokens,
)


class TestOAuthState:
    def test_generate_and_validate_state(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        state = generate_oauth_state(user_id, tenant_id, "github", flow="web")
        payload = validate_oauth_state(state)
        assert payload["user_id"] == user_id
        assert payload["tenant_id"] == tenant_id
        assert payload["provider"] == "github"
        assert payload["flow"] == "web"

    def test_validate_rejects_invalid_state(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_oauth_state("invalid-garbage")

    def test_validate_rejects_consumed_state(self):
        user_id = uuid.uuid4()
        state = generate_oauth_state(user_id, uuid.uuid4(), "github")
        validate_oauth_state(state)  # first use
        with pytest.raises(ValueError, match="already been used"):
            validate_oauth_state(state)  # second use


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_exchange_code_returns_tokens(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "read write",
        }

        with patch("app.services.oauth_service.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            tokens = await exchange_code_for_tokens(
                token_url="https://oauth.example.com/token",
                client_id="cid",
                client_secret="csec",
                code="auth_code_123",
                redirect_uri="https://app.com/callback",
            )

        assert tokens.access_token == "new_access"
        assert tokens.refresh_token == "new_refresh"
        assert tokens.expires_in == 3600
        assert tokens.scope == "read write"

    @pytest.mark.asyncio
    async def test_exchange_code_raises_on_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "invalid_grant"
        mock_response.json.return_value = {"error": "invalid_grant"}

        with patch("app.services.oauth_service.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            with pytest.raises(ValueError, match="Token exchange failed"):
                await exchange_code_for_tokens(
                    token_url="https://oauth.example.com/token",
                    client_id="cid",
                    client_secret="csec",
                    code="bad_code",
                    redirect_uri="https://app.com/callback",
                )


class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_refresh_returns_new_tokens(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "refreshed_access",
            "refresh_token": "refreshed_refresh",
            "expires_in": 7200,
        }

        with patch("app.services.oauth_service.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            tokens = await refresh_access_token(
                token_url="https://oauth.example.com/token",
                client_id="cid",
                client_secret="csec",
                refresh_token="old_refresh",
            )

        assert tokens.access_token == "refreshed_access"
