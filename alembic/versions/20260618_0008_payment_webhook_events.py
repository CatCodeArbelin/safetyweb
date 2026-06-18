"""Add payment webhook events.

Revision ID: 20260618_0008
Revises: 20260618_0007
Create Date: 2026-06-18 00:08:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260618_0008"
down_revision: str | None = "20260618_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("event_status", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=128), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("handling_state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "handling_state IN ('pending', 'processed', 'failed')",
            name="ck_payment_webhook_events_handling_state",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "payload_hash",
            name="uq_payment_webhook_events_provider_payload_hash",
        ),
    )
    op.create_index(
        "ix_payment_webhook_events_provider_payment_id",
        "payment_webhook_events",
        ["provider_payment_id"],
        unique=False,
    )
    op.create_index(
        "ix_payment_webhook_events_handling_state",
        "payment_webhook_events",
        ["handling_state"],
        unique=False,
    )
    op.create_index(
        "ix_payment_webhook_events_provider_handling_state",
        "payment_webhook_events",
        ["provider", "handling_state"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_payment_webhook_events_provider_handling_state",
        table_name="payment_webhook_events",
    )
    op.drop_index(
        "ix_payment_webhook_events_handling_state",
        table_name="payment_webhook_events",
    )
    op.drop_index(
        "ix_payment_webhook_events_provider_payment_id",
        table_name="payment_webhook_events",
    )
    op.drop_table("payment_webhook_events")
