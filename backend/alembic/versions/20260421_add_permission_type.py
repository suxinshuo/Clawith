"""Add permission_type to AgentPermission, dev_tools_access_mode to Agent.

Revision ID: 20260421_perm_type
"""
from typing import Union
from alembic import op


revision: str = "20260421_perm_type"
down_revision: Union[str, None] = "20260421_dev_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_permissions ADD COLUMN IF NOT EXISTS permission_type VARCHAR(20) DEFAULT 'general'")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS dev_tools_access_mode VARCHAR(20) DEFAULT 'all'")


def downgrade() -> None:
    pass
