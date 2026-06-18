"""Add user trial fields.

Revision ID: 20260618_0007
Revises: 20260618_0006
Create Date: 2026-06-18 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260618_0007"
down_revision: str | None = "20260618_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("trial_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("trial_subscription_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_trial_subscription_id_subscriptions",
        "users",
        "subscriptions",
        ["trial_subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_users_trial_subscription_id_subscriptions",
        "users",
        type_="foreignkey",
    )
    op.drop_column("users", "trial_subscription_id")
    op.drop_column("users", "trial_used_at")
