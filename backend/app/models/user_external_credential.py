"""User and Tenant external credential models for accessing external systems."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserExternalCredential(Base):
    """Per-user encrypted credential for an external system provider.

    Credentials are resolved at MCP tool execution time and injected
    as HTTP headers — they never enter the LLM context.
    """

    __tablename__ = "user_external_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="api_key")

    # Encrypted storage (AES-256-CBC via encrypt_data/decrypt_data)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OAuth lifecycle
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # External system user identity
    external_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_username: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Metadata
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_external_credential_provider"),
    )


class TenantExternalCredential(Base):
    """Org-level shared credential for an external system provider.

    Used as fallback when the individual user has no credential for a provider.
    """

    __tablename__ = "tenant_external_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="api_key")

    # Encrypted storage
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OAuth lifecycle
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Metadata
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_external_credential_provider"),
    )


class AgentExternalCredential(Base):
    """Per-agent shared credential for an external system provider.

    Set by the agent creator. Used as middle-priority fallback:
    User > Agent > Tenant.
    """

    __tablename__ = "agent_external_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False, default="api_key")

    # Encrypted storage (AES-256-CBC via encrypt_data/decrypt_data)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OAuth lifecycle
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # External system user identity
    external_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_username: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Metadata
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "provider", name="uq_agent_external_credential_provider"),
    )
