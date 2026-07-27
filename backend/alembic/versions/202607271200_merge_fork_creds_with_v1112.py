"""Merge the fork's external-credentials chain with upstream v1.11.2 head.

After merging tag v1.11.2 into the fork (merge/v1.9.3), the Alembic graph has
two heads that both descend from the shared trunk (`add_agent_focus_items`):

  * ``merge_v193_creds_focus`` — the fork's external-credentials / oauth chain
    (user/tenant/agent external credentials, oauth provider configs,
    agent_triggers.acting_user_id).
  * ``add_agent_model_deleted_at`` — upstream v1.11.2's head (user tenant
    onboarding, perf indexes, runtime group schema, experience revision
    drafts, agents/llm_models soft-delete markers).

Both heads only ADD new tables/columns/indexes and do not touch each other's
objects, so unifying them is a pure no-op merge with no schema changes.

Revision ID: merge_fork_v1112
Revises: merge_v193_creds_focus, add_agent_model_deleted_at
Create Date: 2026-07-27
"""

from typing import Sequence, Union


revision: str = "merge_fork_v1112"
down_revision: Union[str, Sequence[str], None] = (
    "merge_v193_creds_focus",
    "add_agent_model_deleted_at",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pure merge — no schema changes needed.
    pass


def downgrade() -> None:
    # Pure merge — no schema changes to revert.
    pass
