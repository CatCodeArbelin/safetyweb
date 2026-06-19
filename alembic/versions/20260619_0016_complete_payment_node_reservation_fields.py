"""Complete payment node reservation fields.

Revision ID: 20260619_0016
Revises: 20260619_0015
Create Date: 2026-06-19 00:15:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0016"
down_revision: str | None = "20260619_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "payments"


def _has_column(column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(TABLE_NAME)
    )


def _has_index(index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(TABLE_NAME))


def upgrade() -> None:
    """Upgrade schema."""
    if not _has_column("reserved_node_name"):
        op.add_column(
            TABLE_NAME,
            sa.Column("reserved_node_name", sa.String(length=128), nullable=True),
        )
    else:
        op.alter_column(
            TABLE_NAME,
            "reserved_node_name",
            existing_type=sa.String(length=255),
            type_=sa.String(length=128),
            existing_nullable=True,
        )

    if not _has_column("node_reserved_at"):
        op.add_column(
            TABLE_NAME,
            sa.Column("node_reserved_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.alter_column(
        TABLE_NAME,
        "reserved_node_key",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    if _has_index("ix_payments_node_reservation"):
        op.drop_index("ix_payments_node_reservation", table_name=TABLE_NAME)
    if not _has_index("ix_payments_node_reservation_capacity"):
        op.create_index(
            "ix_payments_node_reservation_capacity",
            TABLE_NAME,
            ["reserved_node_key", "status", "node_reservation_expires_at"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    if _has_index("ix_payments_node_reservation_capacity"):
        op.drop_index("ix_payments_node_reservation_capacity", table_name=TABLE_NAME)
    if not _has_index("ix_payments_node_reservation"):
        op.create_index(
            "ix_payments_node_reservation",
            TABLE_NAME,
            ["status", "reserved_node_key", "node_reservation_expires_at"],
        )
    op.alter_column(
        TABLE_NAME,
        "reserved_node_key",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    if _has_column("reserved_node_name"):
        op.alter_column(
            TABLE_NAME,
            "reserved_node_name",
            existing_type=sa.String(length=128),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
