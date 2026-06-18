"""Payment repository helpers."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Payment, PaymentStatus


class PaymentRepository:
    """Persist and query payments."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_provider_payment_id(
        self, provider: str, provider_payment_id: str
    ) -> Payment | None:
        """Load a payment for a provider with its user."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(
                Payment.provider == provider,
                Payment.provider_payment_id == provider_payment_id,
            )
        )

    async def get_manual_payment(self, provider_payment_id: str) -> Payment | None:
        """Load a manual payment with its user."""
        return await self.get_by_provider_payment_id("manual", provider_payment_id)

    async def get_by_provider_payment_id_any_provider(
        self, provider_payment_id: str
    ) -> Payment | None:
        """Load a payment by provider payment id regardless of provider."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.provider_payment_id == provider_payment_id)
        )

    async def attach_subscription(
        self,
        provider_payment_id: str,
        subscription_id: int,
        provider: str | None = None,
    ) -> Payment | None:
        """Attach a subscription to a payment."""
        payment = await self._get_payment(provider_payment_id, provider)
        if payment is None:
            return None
        payment.subscription_id = subscription_id
        await self.session.flush()
        return payment

    async def set_status(
        self,
        provider_payment_id: str,
        status: PaymentStatus,
        provider: str | None = None,
        paid_at: datetime | None = None,
    ) -> Payment | None:
        """Set payment status and optional paid timestamp."""
        payment = await self._get_payment(provider_payment_id, provider)
        if payment is None:
            return None
        payment.status = status
        if paid_at is not None:
            payment.paid_at = paid_at
        await self.session.flush()
        return payment

    async def get_provider_for_payment(self, provider_payment_id: str) -> str | None:
        """Return provider name for a provider payment id."""
        return await self.session.scalar(
            select(Payment.provider).where(
                Payment.provider_payment_id == provider_payment_id
            )
        )

    async def _get_payment(
        self, provider_payment_id: str, provider: str | None = None
    ) -> Payment | None:
        if provider is not None:
            return await self.get_by_provider_payment_id(provider, provider_payment_id)
        return await self.get_by_provider_payment_id_any_provider(provider_payment_id)
