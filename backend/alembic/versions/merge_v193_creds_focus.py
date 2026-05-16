"""Merge agent_external_credentials with agent_focus_items.

Resolves the dual-head produced when v1.9.3 (add_agent_focus_items) was
merged with the merge/v1.9.0 branch (add_agent_ext_creds). Both heads
only add new columns/tables, so this is a pure merge with no schema
changes.

Revision ID: merge_v193_creds_focus
Revises: add_agent_ext_creds, add_agent_focus_items
"""

from alembic import op  # noqa: F401

# revision identifiers
revision = "merge_v193_creds_focus"
down_revision = ("add_agent_ext_creds", "add_agent_focus_items")
branch_labels = None
depends_on = None


def upgrade():
    # Pure merge — no schema changes needed.
    pass


def downgrade():
    # Pure merge — no schema changes to revert.
    pass
