"""Add referral reward applied timestamp.

Revision ID: 20260618_0006
Revises: 20260618_0005
Create Date: 2026-06-18 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260618_0006"
down_revision: str | None = "20260618_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "referral_rewards",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("referral_rewards", "applied_at")
