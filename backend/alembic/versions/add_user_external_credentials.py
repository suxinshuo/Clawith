"""Add user_external_credentials and tenant_external_credentials tables.

Revision ID: add_user_ext_creds
Revises: user_refactor_v1
Create Date: 2026-04-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'add_user_ext_creds'
down_revision: Union[str, None] = 'increase_api_key_length'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── UserExternalCredential ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_external_credentials (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            provider VARCHAR(100) NOT NULL,
            credential_type VARCHAR(20) NOT NULL DEFAULT 'api_key',
            access_token_encrypted TEXT NOT NULL,
            refresh_token_encrypted TEXT,
            extra_encrypted TEXT,
            token_expires_at TIMESTAMPTZ,
            scopes VARCHAR(500),
            external_user_id VARCHAR(200),
            external_username VARCHAR(200),
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            display_name VARCHAR(200),
            last_used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_user_external_credential_provider UNIQUE (user_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_ext_cred_user_id ON user_external_credentials(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_ext_cred_tenant_id ON user_external_credentials(tenant_id)")

    # ── TenantExternalCredential ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS tenant_external_credentials (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            provider VARCHAR(100) NOT NULL,
            credential_type VARCHAR(20) NOT NULL DEFAULT 'api_key',
            access_token_encrypted TEXT NOT NULL,
            refresh_token_encrypted TEXT,
            extra_encrypted TEXT,
            token_expires_at TIMESTAMPTZ,
            scopes VARCHAR(500),
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            display_name VARCHAR(200),
            created_by UUID,
            last_used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_tenant_external_credential_provider UNIQUE (tenant_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_tenant_ext_cred_tenant_id ON tenant_external_credentials(tenant_id)")

    # ── Tool.required_credential_provider ──
    op.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS required_credential_provider VARCHAR(100)")


def downgrade() -> None:
    op.execute("ALTER TABLE tools DROP COLUMN IF EXISTS required_credential_provider")
    op.execute("DROP TABLE IF EXISTS tenant_external_credentials")
    op.execute("DROP TABLE IF EXISTS user_external_credentials")
