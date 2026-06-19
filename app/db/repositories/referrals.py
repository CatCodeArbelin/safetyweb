"""Referral reward repository helpers."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferralReward, User


class ReferralRewardRepository:
    """Query referral bonus rewards."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_pending_bonus_days_by_telegram_id(self, telegram_id: int) -> int:
        """Return unapplied referral bonus days for a Telegram user."""
        pending_days = await self.session.scalar(
            select(func.coalesce(func.sum(ReferralReward.bonus_days), 0))
            .join(User, ReferralReward.recipient_user_id == User.id)
            .where(
                User.telegram_id == telegram_id,
                ReferralReward.applied_at.is_(None),
            )
        )
        return int(pending_days or 0)
