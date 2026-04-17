"""OAuth provider configuration model — per-tenant OAuth app registration."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OAuthProviderConfig(Base):
    """Admin-registered OAuth provider for a tenant.

    Stores client_id, client_secret (encrypted), and OAuth endpoint URLs
    so users in this tenant can authorize via OAuth to get credentials.
    """

    __tablename__ = "oauth_provider_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    client_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    authorize_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    token_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    scopes: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    redirect_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_oauth_provider_config"),
    )
