"""Payment provider abstractions and MVP manual payment implementation."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Payment, PaymentStatus, User
from app.db.session import async_session_maker


MANUAL_PROVIDER_NAME: Final = "manual"


class PaymentProvider(ABC):
    """Base interface for payment providers."""

    @abstractmethod
    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> Payment:
        """Create a provider payment and return the persisted payment record."""

    @abstractmethod
    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Return current provider payment status."""

    @abstractmethod
    async def refund_payment(self, provider_payment_id: str) -> Payment:
        """Refund a provider payment and return the updated payment record."""


class ManualPaymentProvider(PaymentProvider):
    """Manual payment provider for MVP admin-confirmed payments."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> Payment:
        """Create a pending manual payment for a Telegram user."""
        if self.session is not None:
            return await self._create_payment(
                self.session,
                user_id=user_id,
                tariff_id=tariff_id,
                amount=amount,
                currency=currency,
            )

        async with async_session_maker() as session:
            payment = await self._create_payment(
                session,
                user_id=user_id,
                tariff_id=tariff_id,
                amount=amount,
                currency=currency,
            )
            await session.commit()
            return payment

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Return the status saved for a manual payment."""
        if self.session is not None:
            payment = await self._get_payment(self.session, provider_payment_id)
            return payment.status

        async with async_session_maker() as session:
            payment = await self._get_payment(session, provider_payment_id)
            return payment.status

    async def refund_payment(self, provider_payment_id: str) -> Payment:
        """Mark a manual payment as refunded."""
        if self.session is not None:
            return await self._set_status(
                self.session,
                provider_payment_id,
                PaymentStatus.REFUNDED,
            )

        async with async_session_maker() as session:
            payment = await self._set_status(
                session,
                provider_payment_id,
                PaymentStatus.REFUNDED,
            )
            await session.commit()
            return payment

    async def confirm_payment(self, provider_payment_id: str) -> Payment:
        """Confirm a manual payment after an admin verifies it."""
        paid_at = datetime.now(tz=UTC)
        if self.session is not None:
            payment = await self._get_payment(self.session, provider_payment_id)
            if payment.status == PaymentStatus.PAID:
                return payment
            return await self._set_status(
                self.session,
                provider_payment_id,
                PaymentStatus.PAID,
                paid_at=paid_at,
            )

        async with async_session_maker() as session:
            payment = await self._get_payment(session, provider_payment_id)
            if payment.status == PaymentStatus.PAID:
                return payment
            payment = await self._set_status(
                session,
                provider_payment_id,
                PaymentStatus.PAID,
                paid_at=paid_at,
            )
            await session.commit()
            return payment

    @staticmethod
    async def _create_payment(
        session: AsyncSession,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> Payment:
        user = await ManualPaymentProvider._get_or_create_user(session, user_id)
        payment = Payment(
            user=user,
            provider=MANUAL_PROVIDER_NAME,
            status=PaymentStatus.PENDING,
            amount=Decimal(str(amount)),
            currency=currency.upper(),
            description=f"Manual payment for tariff {tariff_id}",
        )
        session.add(payment)
        await session.flush()
        payment.provider_payment_id = f"{MANUAL_PROVIDER_NAME}-{payment.id}"
        await session.flush()
        return payment

    @staticmethod
    async def _get_payment(session: AsyncSession, provider_payment_id: str) -> Payment:
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(
                Payment.provider == MANUAL_PROVIDER_NAME,
                Payment.provider_payment_id == provider_payment_id,
            )
        )
        if payment is None:
            msg = f"Manual payment {provider_payment_id!r} was not found"
            raise ValueError(msg)
        return payment

    @staticmethod
    async def _set_status(
        session: AsyncSession,
        provider_payment_id: str,
        status: PaymentStatus,
        paid_at: datetime | None = None,
    ) -> Payment:
        payment = await ManualPaymentProvider._get_payment(session, provider_payment_id)
        payment.status = status
        if paid_at is not None:
            payment.paid_at = paid_at
        await session.flush()
        return payment

    @staticmethod
    async def _get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is not None:
            return user

        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
        return user


class PaymentService:
    """Coordinate payment creation and status checks."""

    def __init__(self, provider: PaymentProvider | None = None) -> None:
        self.provider = provider or ManualPaymentProvider()

    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> Payment:
        """Create a payment through the configured provider."""
        return await self.provider.create_payment(user_id, tariff_id, amount, currency)

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Get payment status from the configured provider."""
        return await self.provider.get_payment_status(provider_payment_id)

    async def refund_payment(self, provider_payment_id: str) -> Payment:
        """Refund a payment through the configured provider."""
        return await self.provider.refund_payment(provider_payment_id)

    async def confirm_manual_payment(self, provider_payment_id: str) -> Payment:
        """Confirm a manual payment after admin verification."""
        if not isinstance(self.provider, ManualPaymentProvider):
            msg = "Manual confirmation is only available for the manual provider"
            raise TypeError(msg)
        return await self.provider.confirm_payment(provider_payment_id)
