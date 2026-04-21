"""Add agent dev config fields (allowed_repos, dev_approval_mode).

Revision ID: 20260421_dev_config
Revises: add_agent_ext_creds
Create Date: 2026-04-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260421_dev_config"
down_revision: Union[str, None] = "add_agent_ext_creds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS allowed_repos JSON DEFAULT '[]'")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS dev_approval_mode VARCHAR(20) DEFAULT 'confirm'")


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS allowed_repos")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dev_approval_mode")
