"""Customer benefit and discount service."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.db.models import CustomerBenefit, User
from app.db.session import async_session_maker

EARLY_BUYER_BENEFIT_TYPE: Final = "early_buyer_discount"


class BenefitService:
    """Coordinate customer discounts and promotional benefits."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    async def get_active_discount_percent(self, telegram_id: int) -> int:
        """Return the highest active discount percent for a Telegram user."""
        async with async_session_maker() as session:
            discount_percent = await session.scalar(
                select(func.max(CustomerBenefit.discount_percent))
                .join(User, CustomerBenefit.user_id == User.id)
                .where(
                    User.telegram_id == telegram_id,
                    CustomerBenefit.is_active.is_(True),
                )
            )
            return int(discount_percent or 0)

    async def apply_price_discount(
        self, telegram_id: int, base_price: Decimal | int | str
    ) -> Decimal:
        """Apply the user's best active discount to a base price."""
        discount_percent = await self.get_active_discount_percent(telegram_id)
        price = Decimal(str(base_price))
        discount_multiplier = Decimal(100 - discount_percent) / Decimal(100)
        return (price * discount_multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async def grant_early_buyer_discount_if_eligible(self, telegram_id: int) -> bool:
        """Grant early buyer discount if the promotion is enabled and has capacity."""
        if not self.settings.early_buyer_discount_enabled:
            return False

        async with async_session_maker() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
                await session.flush()

            existing = await session.scalar(
                select(CustomerBenefit).where(
                    CustomerBenefit.user_id == user.id,
                    CustomerBenefit.benefit_type == EARLY_BUYER_BENEFIT_TYPE,
                )
            )
            if existing is not None:
                return False

            granted_count = await session.scalar(
                select(func.count()).select_from(CustomerBenefit).where(
                    CustomerBenefit.benefit_type == EARLY_BUYER_BENEFIT_TYPE,
                )
            )
            if int(granted_count or 0) >= self.settings.early_buyer_limit:
                return False

            statement = (
                insert(CustomerBenefit)
                .values(
                    user_id=user.id,
                    benefit_type=EARLY_BUYER_BENEFIT_TYPE,
                    discount_percent=self.settings.early_buyer_discount_percent,
                    is_active=True,
                )
                .on_conflict_do_nothing(
                    constraint="uq_customer_benefits_user_type",
                )
            )
            result = await session.execute(statement)
            await session.commit()
            return result.rowcount == 1
