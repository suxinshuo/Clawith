"""Pydantic schemas for OAuth provider config management."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class OAuthProviderCreate(BaseModel):
    """Request body for registering an OAuth provider."""

    provider: str = Field(..., min_length=1, max_length=100)
    client_id: str = Field(..., min_length=1, max_length=500)
    client_secret: str = Field(..., min_length=1, description="Will be encrypted before storage")
    authorize_url: str = Field(..., min_length=1, max_length=1000)
    token_url: str = Field(..., min_length=1, max_length=1000)
    scopes: str = Field(default="", max_length=500, description="Space-separated default scopes")
    redirect_uri: str = Field(..., min_length=1, max_length=1000)


class OAuthProviderResponse(BaseModel):
    """Response body — never exposes client_secret."""

    id: uuid.UUID
    provider: str
    client_id: str
    authorize_url: str
    token_url: str
    scopes: str
    redirect_uri: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OAuthProviderUpdate(BaseModel):
    """Request body for updating an OAuth provider config."""

    client_id: str | None = Field(default=None, max_length=500)
    client_secret: str | None = Field(default=None, description="Will be encrypted if provided")
    authorize_url: str | None = Field(default=None, max_length=1000)
    token_url: str | None = Field(default=None, max_length=1000)
    scopes: str | None = Field(default=None, max_length=500)
    redirect_uri: str | None = Field(default=None, max_length=1000)
