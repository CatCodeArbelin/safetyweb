"""Make subscription notifications expiration-period aware.

Revision ID: 20260618_0005
Revises: 20260618_0004
Create Date: 2026-06-18 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260618_0005"
down_revision: str | None = "20260618_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "subscription_notifications",
        sa.Column("period_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE subscription_notifications AS sn
        SET period_expires_at = s.expires_at
        FROM subscriptions AS s
        WHERE sn.subscription_id = s.id
          AND sn.period_expires_at IS NULL
        """
    )
    op.alter_column("subscription_notifications", "period_expires_at", nullable=False)
    op.drop_constraint(
        "uq_subscription_notifications_once",
        "subscription_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_subscription_notifications_once_per_period",
        "subscription_notifications",
        ["subscription_id", "notification_type", "period_expires_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_subscription_notifications_once_per_period",
        "subscription_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_subscription_notifications_once",
        "subscription_notifications",
        ["subscription_id", "notification_type"],
    )
    op.drop_column("subscription_notifications", "period_expires_at")
