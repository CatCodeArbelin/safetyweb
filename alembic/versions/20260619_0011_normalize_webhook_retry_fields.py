"""Normalize webhook retry fields.

Revision ID: 20260619_0011
Revises: 20260619_0010
Create Date: 2026-06-19 00:11:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260619_0011"
down_revision: str | None = "20260619_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "payment_webhook_events",
        "retry_count",
        new_column_name="attempt_count",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )
    op.alter_column(
        "payment_webhook_events",
        "processing_error",
        new_column_name="last_error",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.add_column(
        "payment_webhook_events",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payment_webhook_events",
        sa.Column("last_http_status", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("payment_webhook_events", "last_http_status")
    op.drop_column("payment_webhook_events", "dead_lettered_at")
    op.alter_column(
        "payment_webhook_events",
        "last_error",
        new_column_name="processing_error",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "payment_webhook_events",
        "attempt_count",
        new_column_name="retry_count",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )
