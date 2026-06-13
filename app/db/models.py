"""Database models."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


class SubscriptionStatus(StrEnum):
    """Available subscription lifecycle statuses."""

    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class SubscriptionNotificationType(StrEnum):
    """Subscription notification event types."""

    EXPIRES_IN_3_DAYS = "expires_in_3_days"
    EXPIRES_IN_1_DAY = "expires_in_1_day"
    EXPIRES_TODAY = "expires_today"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    """Available payment processing statuses."""

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class User(Base):
    """Telegram bot user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class VpnNode(Base):
    """VPN node managed by the application."""

    __tablename__ = "vpn_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    panel_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    inbound_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Subscription(Base):
    """User VPN subscription."""

    __tablename__ = "subscriptions"

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'disabled')",
            name="ck_subscriptions_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        String(32),
        default=SubscriptionStatus.ACTIVE,
        server_default=SubscriptionStatus.ACTIVE.value,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    xui_client_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    xui_email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    inbound_id: Mapped[int] = mapped_column(Integer, nullable=False)
    traffic_limit_gb: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    vpn_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    payments: Mapped[list["Payment"]] = relationship(back_populates="subscription")
    notifications: Mapped[list["SubscriptionNotification"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class SubscriptionNotification(Base):
    """Notification event emitted for a subscription."""

    __tablename__ = "subscription_notifications"

    __table_args__ = (
        CheckConstraint(
            "notification_type IN "
            "('expires_in_3_days', 'expires_in_1_day', 'expires_today', 'expired')",
            name="ck_subscription_notifications_type",
        ),
        UniqueConstraint(
            "subscription_id",
            "notification_type",
            name="uq_subscription_notifications_once",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[SubscriptionNotificationType] = mapped_column(
        String(64), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    subscription: Mapped[Subscription] = relationship(back_populates="notifications")


class Payment(Base):
    """Payment for a VPN subscription."""

    __tablename__ = "payments"

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'refunded')",
            name="ck_payments_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[PaymentStatus] = mapped_column(
        String(32),
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), default="RUB", server_default="RUB", nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="payments")
    subscription: Mapped[Subscription | None] = relationship(back_populates="payments")
