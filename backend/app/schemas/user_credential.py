# backend/app/schemas/user_credential.py
"""Pydantic schemas for user external credential API."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CredentialType = Literal["api_key", "oauth2", "basic_auth", "custom"]
CredentialStatus = Literal["active", "expired", "needs_reauth", "revoked"]


class UserCredentialCreate(BaseModel):
    """Request body for manually adding an API key credential."""

    provider: str = Field(..., min_length=1, max_length=100, description="Provider identifier, e.g. 'jira', 'github'")
    access_token: str = Field(..., min_length=1, description="API key or access token (will be encrypted)")
    credential_type: CredentialType = Field(default="api_key")
    display_name: str | None = Field(default=None, max_length=200)
    external_user_id: str | None = Field(default=None, max_length=200)
    external_username: str | None = Field(default=None, max_length=200)
    scopes: str | None = Field(default=None, max_length=500, description="Comma-separated scopes")


class UserCredentialResponse(BaseModel):
    """Response body — never exposes the actual token."""

    id: uuid.UUID
    provider: str
    credential_type: str
    status: str
    display_name: str | None
    external_user_id: str | None
    external_username: str | None
    scopes: str | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserCredentialUpdate(BaseModel):
    """Request body for updating a credential."""

    access_token: str | None = Field(default=None, min_length=1)
    display_name: str | None = None
    external_user_id: str | None = None
    external_username: str | None = None
    scopes: str | None = None
    status: CredentialStatus | None = None


class TenantCredentialCreate(BaseModel):
    """Request body for adding an org-level shared credential."""

    provider: str = Field(..., min_length=1, max_length=100)
    access_token: str = Field(..., min_length=1)
    credential_type: CredentialType = Field(default="api_key")
    display_name: str | None = Field(default=None, max_length=200)
    scopes: str | None = Field(default=None, max_length=500)


class TenantCredentialResponse(BaseModel):
    """Response body for org-level credentials."""

    id: uuid.UUID
    provider: str
    credential_type: str
    status: str
    display_name: str | None
    scopes: str | None
    created_by: uuid.UUID | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OneTimeTokenSubmit(BaseModel):
    """Request body for submitting a credential via one-time token link."""

    token: str = Field(..., min_length=1, description="One-time JWT token from the credential link")
    access_token: str = Field(..., min_length=1, description="API key or access token to store")
    external_user_id: str | None = Field(default=None, max_length=200)
    external_username: str | None = Field(default=None, max_length=200)
