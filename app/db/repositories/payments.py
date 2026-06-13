"""Payment repository helpers."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Payment


class PaymentRepository:
    """Persist and query payments."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_manual_payment(self, provider_payment_id: str) -> Payment | None:
        """Load a manual payment with its user."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.provider == "manual", Payment.provider_payment_id == provider_payment_id)
        )
