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
    benefits: Mapped[list["CustomerBenefit"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    referral_code: Mapped["ReferralCode | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    referrals_made: Mapped[list["Referral"]] = relationship(
        back_populates="referrer",
        cascade="all, delete-orphan",
        foreign_keys="Referral.referrer_user_id",
    )
    referral: Mapped["Referral | None"] = relationship(
        back_populates="referred",
        cascade="all, delete-orphan",
        foreign_keys="Referral.referred_user_id",
    )
    referral_rewards: Mapped[list["ReferralReward"]] = relationship(
        back_populates="recipient", cascade="all, delete-orphan"
    )


class ReferralCode(Base):
    """Shareable referral code owned by a Telegram user."""

    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="referral_code")


class Referral(Base):
    """Referral relationship between an inviting user and a referred user."""

    __tablename__ = "referrals"

    __table_args__ = (
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    referral_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("referral_codes.id", ondelete="SET NULL"), nullable=True
    )
    first_paid_months: Mapped[int | None] = mapped_column(Integer)
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    referrer: Mapped[User] = relationship(
        back_populates="referrals_made", foreign_keys=[referrer_user_id]
    )
    referred: Mapped[User] = relationship(
        back_populates="referral", foreign_keys=[referred_user_id]
    )
    referral_code: Mapped[ReferralCode | None] = relationship()
    rewards: Mapped[list["ReferralReward"]] = relationship(
        back_populates="referral", cascade="all, delete-orphan"
    )


class ReferralReward(Base):
    """Granted referral subscription extension reward."""

    __tablename__ = "referral_rewards"

    __table_args__ = (
        UniqueConstraint("referral_id", "reward_type", name="uq_referral_rewards_once"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referral_id: Mapped[int] = mapped_column(
        ForeignKey("referrals.id", ondelete="CASCADE"), nullable=False
    )
    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reward_type: Mapped[str] = mapped_column(String(64), nullable=False)
    bonus_days: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    referral: Mapped[Referral] = relationship(back_populates="rewards")
    recipient: Mapped[User] = relationship(back_populates="referral_rewards")


class CustomerBenefit(Base):
    """Customer-specific promotional benefit."""

    __tablename__ = "customer_benefits"

    __table_args__ = (
        UniqueConstraint("user_id", "benefit_type", name="uq_customer_benefits_user_type"),
        CheckConstraint(
            "discount_percent BETWEEN 0 AND 100",
            name="ck_customer_benefits_discount_percent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    benefit_type: Mapped[str] = mapped_column(String(64), nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="benefits")


class VpnNode(Base):
    """Protected access node managed by the application."""

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
    """User protected access subscription."""

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
            "period_expires_at",
            name="uq_subscription_notifications_once_per_period",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[SubscriptionNotificationType] = mapped_column(
        String(64), nullable=False
    )
    period_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    subscription: Mapped[Subscription] = relationship(back_populates="notifications")


class Payment(Base):
    """Payment for a protected access subscription."""

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
