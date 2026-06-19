"""Customer benefit repository helpers."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CustomerBenefit, User


class CustomerBenefitRepository:
    """Query customer discounts and promotional benefits."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_discount_percent_by_telegram_id(self, telegram_id: int) -> int:
        """Return the highest active discount percent for a Telegram user."""
        discount_percent = await self.session.scalar(
            select(func.max(CustomerBenefit.discount_percent))
            .join(User, CustomerBenefit.user_id == User.id)
            .where(
                User.telegram_id == telegram_id,
                CustomerBenefit.is_active.is_(True),
            )
        )
        return int(discount_percent or 0)
