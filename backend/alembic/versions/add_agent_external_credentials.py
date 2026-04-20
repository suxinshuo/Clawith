"""Add agent_external_credentials table.

Revision ID: add_agent_ext_creds
Revises: add_user_ext_creds
Create Date: 2026-04-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'add_agent_ext_creds'
down_revision: Union[str, None] = 'add_oauth_trigger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_external_credentials (
            id UUID PRIMARY KEY,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
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
            created_by UUID,
            last_used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_agent_external_credential_provider UNIQUE (agent_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_ext_cred_agent_id ON agent_external_credentials(agent_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_external_credentials")
