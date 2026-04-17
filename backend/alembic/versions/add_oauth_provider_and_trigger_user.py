"""Add oauth_provider_configs table and agent_triggers.acting_user_id column.

Revision ID: add_oauth_trigger
Revises: add_user_ext_creds
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'add_oauth_trigger'
down_revision: Union[str, None] = 'add_user_ext_creds'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── OAuthProviderConfig ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS oauth_provider_configs (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            provider VARCHAR(100) NOT NULL,
            client_id VARCHAR(500) NOT NULL,
            client_secret_encrypted TEXT NOT NULL,
            authorize_url VARCHAR(1000) NOT NULL,
            token_url VARCHAR(1000) NOT NULL,
            scopes VARCHAR(500) NOT NULL DEFAULT '',
            redirect_uri VARCHAR(1000) NOT NULL,
            created_by UUID,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_oauth_provider_config UNIQUE (tenant_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_oauth_provider_config_tenant ON oauth_provider_configs(tenant_id)")

    # ── AgentTrigger.acting_user_id ──
    op.execute("ALTER TABLE agent_triggers ADD COLUMN IF NOT EXISTS acting_user_id UUID REFERENCES users(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_triggers DROP COLUMN IF EXISTS acting_user_id")
    op.execute("DROP TABLE IF EXISTS oauth_provider_configs")
