"""Add payment node reservation metadata.

Revision ID: 20260619_0015
Revises: 20260619_0014
Create Date: 2026-06-19 00:15:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0015"
down_revision: str | None = "20260619_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column("reserved_node_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("node_reserved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("payments", "node_reserved_at")
    op.drop_column("payments", "reserved_node_name")
