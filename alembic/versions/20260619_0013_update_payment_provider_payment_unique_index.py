"""Update payment provider payment unique index.

Revision ID: 20260619_0013
Revises: 20260619_0012
Create Date: 2026-06-19 00:13:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0013"
down_revision: str | None = "20260619_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "payments"
OLD_COLUMN = "provider_payment_id"
NEW_INDEX_NAME = "uq_payments_provider_payment_id"
NEW_INDEX_COLUMNS = ["provider", OLD_COLUMN]


def _drop_legacy_provider_payment_unique_objects() -> None:
    """Drop legacy single-column uniqueness regardless of generated DB name."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for constraint in inspector.get_unique_constraints(TABLE_NAME):
        if constraint.get("column_names") == [OLD_COLUMN]:
            op.drop_constraint(constraint["name"], TABLE_NAME, type_="unique")

    for index in inspector.get_indexes(TABLE_NAME):
        if index.get("unique") and index.get("column_names") == [OLD_COLUMN]:
            op.drop_index(index["name"], table_name=TABLE_NAME)


def _has_index(index_name: str) -> bool:
    """Return whether the payments table already has the named index."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(TABLE_NAME))


def upgrade() -> None:
    """Upgrade schema."""
    _drop_legacy_provider_payment_unique_objects()
    if not _has_index(NEW_INDEX_NAME):
        op.create_index(
            NEW_INDEX_NAME,
            TABLE_NAME,
            NEW_INDEX_COLUMNS,
            unique=True,
            postgresql_where=sa.text(f"{OLD_COLUMN} IS NOT NULL"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    if _has_index(NEW_INDEX_NAME):
        op.drop_index(NEW_INDEX_NAME, table_name=TABLE_NAME)
    op.create_unique_constraint(None, TABLE_NAME, [OLD_COLUMN])
