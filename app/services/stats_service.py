"""Administrative statistics service."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.db.repositories.stats import StatsRepository
from app.db.session import async_session_maker


@dataclass(frozen=True)
class AdminStats:
    """Aggregate metrics shown in the admin statistics command."""

    total_users_count: int
    active_subscriptions_count: int
    paid_payments_count_current_month: int
    paid_payments_sum_current_month: Decimal
    paid_payments_count_all_time: int
    paid_payments_sum_all_time: Decimal
    active_early_buyer_benefits_count: int
    referrals_count: int
    rewarded_referrals_count: int


class StatsService:
    """Collect administrative statistics."""

    async def get_admin_stats(self) -> AdminStats:
        """Return all metrics required by the admin statistics command."""
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if month_start.month == 12:
            next_month_start = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month_start = month_start.replace(month=month_start.month + 1)

        async with async_session_maker() as session:
            repository = StatsRepository(session)
            return AdminStats(
                total_users_count=await repository.total_users_count(),
                active_subscriptions_count=await repository.active_subscriptions_count(),
                paid_payments_count_current_month=(
                    await repository.paid_payments_count_for_period(
                        month_start,
                        next_month_start,
                    )
                ),
                paid_payments_sum_current_month=(
                    await repository.paid_payments_sum_for_period(
                        month_start,
                        next_month_start,
                    )
                ),
                paid_payments_count_all_time=await repository.paid_payments_count_all_time(),
                paid_payments_sum_all_time=await repository.paid_payments_sum_all_time(),
                active_early_buyer_benefits_count=(
                    await repository.active_early_buyer_benefits_count()
                ),
                referrals_count=await repository.referrals_count(),
                rewarded_referrals_count=await repository.rewarded_referrals_count(),
            )
