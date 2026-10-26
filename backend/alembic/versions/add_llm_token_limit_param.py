"""Add per-model Chat Completions token limit parameter strategy."""

from alembic import op
import sqlalchemy as sa

revision = "add_llm_token_limit_param"
down_revision = "merge_v193_creds_focus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Initial schema uses live ORM metadata, so fresh installs already have this column.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS token_limit_param VARCHAR(32) NOT NULL DEFAULT 'auto'")
    elif "token_limit_param" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("llm_models")}:
        op.add_column("llm_models", sa.Column("token_limit_param", sa.String(32), nullable=False, server_default="auto"))


def downgrade() -> None:
    op.drop_column("llm_models", "token_limit_param")
