"""Add payment finalization columns.

Revision ID: 20260620_0017
Revises: 20260619_0016
Create Date: 2026-06-20 00:17:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260620_0017"
down_revision: str | None = "20260619_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column("finalization_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("finalization_finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("finalization_attempt_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provisioning_blocked_reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provisioning_blocked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("payments", "provisioning_blocked_at")
    op.drop_column("payments", "provisioning_blocked_reason")
    op.drop_column("payments", "finalization_attempt_key")
    op.drop_column("payments", "finalization_finished_at")
    op.drop_column("payments", "finalization_started_at")
