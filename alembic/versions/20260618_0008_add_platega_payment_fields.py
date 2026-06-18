"""Add Platega payment fields.

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


OLD_PAYMENT_STATUSES = "status IN ('pending', 'paid', 'failed', 'refunded')"
NEW_PAYMENT_STATUSES = "status IN ('pending', 'paid', 'failed', 'refunded', 'expired')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("ck_payments_status", "payments", type_="check")
    op.add_column("payments", sa.Column("tariff_months", sa.Integer(), nullable=True))
    op.add_column(
        "payments",
        sa.Column("provider_redirect_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provider_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provider_payment_method", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provider_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("payments", sa.Column("status_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_payments_status",
        "payments",
        NEW_PAYMENT_STATUSES,
    )
    op.create_index(
        "ix_payments_provider_status_provider_expires_at",
        "payments",
        ["provider", "status", "provider_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_payments_provider_status_provider_expires_at",
        table_name="payments",
    )
    op.drop_constraint("ck_payments_status", "payments", type_="check")
    op.drop_column("payments", "status_reason")
    op.drop_column("payments", "provider_data")
    op.drop_column("payments", "provider_payment_method")
    op.drop_column("payments", "provider_expires_at")
    op.drop_column("payments", "provider_redirect_url")
    op.drop_column("payments", "tariff_months")
    op.create_check_constraint(
        "ck_payments_status",
        "payments",
        OLD_PAYMENT_STATUSES,
    )
