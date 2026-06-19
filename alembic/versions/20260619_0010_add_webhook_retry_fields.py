"""Add webhook retry fields.

Revision ID: 20260619_0010
Revises: 20260618_0009
Create Date: 2026-06-19 00:10:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0010"
down_revision: str | None = "20260618_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payment_webhook_events",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "payment_webhook_events",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payment_webhook_events",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_payment_webhook_events_handling_state",
        "payment_webhook_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_webhook_events_handling_state",
        "payment_webhook_events",
        "handling_state IN ('pending', 'processed', 'failed', 'dead')",
    )
    op.create_index(
        "ix_payment_webhook_events_provider_state_next_retry",
        "payment_webhook_events",
        ["provider", "handling_state", "next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_payment_webhook_events_provider_state_next_retry",
        table_name="payment_webhook_events",
    )
    op.drop_constraint(
        "ck_payment_webhook_events_handling_state",
        "payment_webhook_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_webhook_events_handling_state",
        "payment_webhook_events",
        "handling_state IN ('pending', 'processed', 'failed')",
    )
    op.drop_column("payment_webhook_events", "next_retry_at")
    op.drop_column("payment_webhook_events", "last_attempt_at")
    op.drop_column("payment_webhook_events", "retry_count")
