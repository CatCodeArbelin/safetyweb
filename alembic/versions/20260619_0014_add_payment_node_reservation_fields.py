"""Add payment node reservation fields.

Revision ID: 20260619_0014
Revises: 20260619_0013
Create Date: 2026-06-19 00:14:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0014"
down_revision: str | None = "20260619_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column("reserved_node_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column(
            "node_reservation_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_payments_node_reservation",
        "payments",
        ["status", "reserved_node_key", "node_reservation_expires_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_payments_node_reservation", table_name="payments")
    op.drop_column("payments", "node_reservation_expires_at")
    op.drop_column("payments", "reserved_node_key")
