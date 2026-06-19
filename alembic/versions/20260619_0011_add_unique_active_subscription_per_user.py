"""Add unique active subscription per user.

Revision ID: 20260619_0011
Revises: 20260619_0010
Create Date: 2026-06-19 00:11:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260619_0011"
down_revision: str | None = "20260619_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_subscriptions_one_active_per_user "
        "ON subscriptions (user_id) WHERE status = 'active';"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS uq_subscriptions_one_active_per_user;")
