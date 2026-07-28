"""Widen external-credential ``scopes`` from VARCHAR(500) to TEXT.

The OAuth callbacks store the provider's granted-scope string verbatim, without
a request schema to bound it: ``oauth_feishu.feishu_credential_callback`` and
``oauth_credentials.oauth_callback`` both assign the raw ``scope`` field of the
token response.  A Feishu app holding a large permission set answers with a
grant of several thousand characters, which overflowed VARCHAR(500) and made the
callback fail with StringDataRightTruncationError *after* the user had already
authorized — they saw an opaque 500 and their credential was never stored.

A granted-scope list has no useful upper bound, so the column must not impose
one.  On PostgreSQL widening varchar(n) to text is a metadata-only change: no
table rewrite, no lock beyond the brief ACCESS EXCLUSIVE, no data loss.

SQLite ignores VARCHAR length entirely, so the columns there already accept any
length and this migration has nothing to do.

Revision ID: widen_credential_scopes
Revises: merge_fork_v1112
Create Date: 2026-07-28 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "widen_credential_scopes"
down_revision: str | None = "merge_fork_v1112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES: tuple[str, ...] = (
    "user_external_credentials",
    "tenant_external_credentials",
    "agent_external_credentials",
)


def _is_sqlite() -> bool:
    # get_context() works in both online and offline (--sql) mode, unlike an
    # inspector, which has no offline equivalent.
    return op.get_context().dialect.name == "sqlite"


def _change_scopes_type(new_type: sa.types.TypeEngine, existing_type: sa.types.TypeEngine) -> None:
    for table in TABLES:
        op.alter_column(
            table,
            "scopes",
            type_=new_type,
            existing_type=existing_type,
            existing_nullable=True,
        )


def upgrade() -> None:
    if _is_sqlite():
        # VARCHAR length is not enforced here, so the column already accepts
        # any grant and there is nothing to widen.
        return
    _change_scopes_type(sa.Text(), sa.String(length=500))


def downgrade() -> None:
    if _is_sqlite():
        return

    # Narrowing again would fail on any grant already stored above 500 chars, so
    # truncate first — the stored grant is a cache of what the provider told us,
    # not a source of truth, and it is rewritten on the next authorization.
    for table in TABLES:
        op.execute(
            sa.text(
                f"UPDATE {table} SET scopes = LEFT(scopes, 500) "  # noqa: S608 - fixed table list above
                "WHERE scopes IS NOT NULL AND LENGTH(scopes) > 500"
            )
        )

    _change_scopes_type(sa.String(length=500), sa.Text())
