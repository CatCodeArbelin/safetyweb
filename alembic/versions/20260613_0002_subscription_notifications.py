"""Add subscription notification events.

Revision ID: 20260613_0002
Revises: 20260613_0001
Create Date: 2026-06-13 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260613_0002"
down_revision: str | None = "20260613_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "subscription_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "notification_type IN "
            "('expires_in_3_days', 'expires_in_1_day', 'expires_today', 'expired')",
            name="ck_subscription_notifications_type",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "notification_type",
            name="uq_subscription_notifications_once",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("subscription_notifications")
