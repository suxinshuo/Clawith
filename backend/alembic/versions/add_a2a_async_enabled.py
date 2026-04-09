"""Add a2a_async_enabled column to agents table.

Revision ID: add_a2a_async_enabled
"""
from alembic import op


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS a2a_async_enabled BOOLEAN DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS a2a_async_enabled")
