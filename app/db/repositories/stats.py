"""Administrative statistics repository helpers."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CustomerBenefit,
    Payment,
    PaymentStatus,
    Referral,
    ReferralReward,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services.benefit_service import EARLY_BUYER_BENEFIT_TYPE


class StatsRepository:
    """Read aggregate counters for administrative bot statistics."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def total_users_count(self) -> int:
        """Return the total number of registered users."""
        return int(await self.session.scalar(select(func.count()).select_from(User)) or 0)

    async def active_subscriptions_count(self) -> int:
        """Return the number of active subscriptions."""
        return int(
            await self.session.scalar(
                select(func.count()).select_from(Subscription).where(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
            )
            or 0
        )

    async def paid_payments_count_for_period(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> int:
        """Return paid payments count for the half-open period [start_at, end_at)."""
        return int(
            await self.session.scalar(
                select(func.count()).select_from(Payment).where(
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= start_at,
                    Payment.paid_at < end_at,
                )
            )
            or 0
        )

    async def paid_payments_sum_for_period(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> Decimal:
        """Return paid payments amount sum for the half-open period [start_at, end_at)."""
        return Decimal(
            await self.session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= start_at,
                    Payment.paid_at < end_at,
                )
            )
            or 0
        )

    async def paid_payments_count_all_time(self) -> int:
        """Return all-time paid payments count."""
        return int(
            await self.session.scalar(
                select(func.count()).select_from(Payment).where(
                    Payment.status == PaymentStatus.PAID,
                )
            )
            or 0
        )

    async def paid_payments_sum_all_time(self) -> Decimal:
        """Return all-time paid payments amount sum."""
        return Decimal(
            await self.session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == PaymentStatus.PAID,
                )
            )
            or 0
        )

    async def active_early_buyer_benefits_count(self) -> int:
        """Return the number of active early-buyer benefits."""
        return int(
            await self.session.scalar(
                select(func.count()).select_from(CustomerBenefit).where(
                    CustomerBenefit.benefit_type == EARLY_BUYER_BENEFIT_TYPE,
                    CustomerBenefit.is_active.is_(True),
                )
            )
            or 0
        )

    async def referrals_count(self) -> int:
        """Return the total number of registered referrals."""
        return int(await self.session.scalar(select(func.count()).select_from(Referral)) or 0)

    async def rewarded_referrals_count(self) -> int:
        """Return the number of referrals with at least one granted reward."""
        return int(
            await self.session.scalar(select(func.count(distinct(ReferralReward.referral_id))))
            or 0
        )
