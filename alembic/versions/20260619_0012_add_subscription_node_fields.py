"""Add subscription node fields.

Revision ID: 20260619_0012
Revises: 20260619_0011
Create Date: 2026-06-19 00:12:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0012"
down_revision: str | None = "20260619_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "subscriptions",
        sa.Column("node_key", sa.String(length=255), server_default="default", nullable=False),
    )
    op.add_column("subscriptions", sa.Column("node_label", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("subscriptions", "node_label")
    op.drop_column("subscriptions", "node_key")
