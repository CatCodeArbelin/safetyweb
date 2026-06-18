"""Payment provider abstractions and payment service helpers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Payment, PaymentStatus, User
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker


MANUAL_PROVIDER_NAME: Final = "manual"
PLATEGA_PROVIDER_NAME: Final = "platega"


@dataclass(frozen=True, slots=True)
class PaymentCreateResult:
    """Result returned after a provider payment is created."""

    payment: Payment
    provider_payment_id: str | None
    payment_url: str | None
    provider: str


class PaymentProvider(ABC):
    """Base interface for payment providers."""

    @abstractmethod
    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> PaymentCreateResult:
        """Create a provider payment and return creation metadata."""

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
    ) -> PaymentCreateResult:
        """Create a pending manual payment for a Telegram user."""
        if self.session is not None:
            payment = await self._create_payment(
                self.session,
                user_id=user_id,
                tariff_id=tariff_id,
                amount=amount,
                currency=currency,
            )
            return self._create_result(payment)

        async with async_session_maker() as session:
            payment = await self._create_payment(
                session,
                user_id=user_id,
                tariff_id=tariff_id,
                amount=amount,
                currency=currency,
            )
            await session.commit()
            return self._create_result(payment)

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

    @staticmethod
    def _create_result(payment: Payment) -> PaymentCreateResult:
        return PaymentCreateResult(
            payment=payment,
            provider_payment_id=payment.provider_payment_id,
            payment_url=None,
            provider=MANUAL_PROVIDER_NAME,
        )

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
        payment = await PaymentRepository(session).get_by_provider_payment_id(
            MANUAL_PROVIDER_NAME,
            provider_payment_id,
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


class PlategaPaymentProvider(PaymentProvider):
    """Platega payment provider placeholder selected by settings."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> PaymentCreateResult:
        """Create a Platega payment.

        The provider is selectable now; API-specific integration details are intentionally
        handled separately once Platega credentials and request schema are configured.
        """
        raise NotImplementedError("Platega payment creation is not configured yet")

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Return the locally saved Platega payment status."""
        async with async_session_maker() as session:
            payment = await PaymentRepository(session).get_by_provider_payment_id(
                PLATEGA_PROVIDER_NAME,
                provider_payment_id,
            )
            if payment is None:
                msg = f"Platega payment {provider_payment_id!r} was not found"
                raise ValueError(msg)
            return payment.status

    async def refund_payment(self, provider_payment_id: str) -> Payment:
        """Refund a Platega payment."""
        raise NotImplementedError("Platega payment refunds are not configured yet")


def create_payment_provider(settings: Settings) -> PaymentProvider:
    """Build the configured payment provider."""
    provider_name = settings.payment_provider.lower()
    if provider_name == MANUAL_PROVIDER_NAME:
        return ManualPaymentProvider()
    if provider_name == PLATEGA_PROVIDER_NAME:
        return PlategaPaymentProvider(settings=settings)
    msg = "PAYMENT_PROVIDER must be either 'manual' or 'platega'"
    raise ValueError(msg)


class PaymentService:
    """Coordinate payment creation, lookups, and status changes."""

    def __init__(
        self,
        provider: PaymentProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.provider = provider or (
            create_payment_provider(settings)
            if settings is not None
            else ManualPaymentProvider()
        )

    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> PaymentCreateResult:
        """Create a payment through the configured provider."""
        return await self.provider.create_payment(user_id, tariff_id, amount, currency)

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Get payment status from the configured provider."""
        return await self.provider.get_payment_status(provider_payment_id)

    async def refund_payment(self, provider_payment_id: str) -> Payment:
        """Refund a payment through the configured provider."""
        return await self.provider.refund_payment(provider_payment_id)

    async def get_payment_by_provider_payment_id(
        self, provider: str, provider_payment_id: str
    ) -> Payment:
        """Return a payment by provider and provider payment identifier."""
        async with async_session_maker() as session:
            payment = await PaymentRepository(session).get_by_provider_payment_id(
                provider,
                provider_payment_id,
            )
            if payment is None:
                msg = (
                    f"Payment {provider_payment_id!r} "
                    f"for provider {provider!r} was not found"
                )
                raise ValueError(msg)
            return payment

    async def set_status(
        self,
        provider_payment_id: str,
        status: PaymentStatus,
        provider: str | None = None,
        paid_at: datetime | None = None,
    ) -> Payment:
        """Set a saved payment status."""
        async with async_session_maker() as session:
            payment = await PaymentRepository(session).set_status(
                provider_payment_id=provider_payment_id,
                status=status,
                provider=provider,
                paid_at=paid_at,
            )
            if payment is None:
                msg = f"Payment {provider_payment_id!r} was not found"
                raise ValueError(msg)
            await session.commit()
            return payment

    async def attach_subscription(
        self,
        provider_payment_id: str,
        subscription_id: int,
        provider: str | None = None,
    ) -> Payment:
        """Attach a provisioned subscription to a payment."""
        async with async_session_maker() as session:
            payment = await PaymentRepository(session).attach_subscription(
                provider_payment_id=provider_payment_id,
                subscription_id=subscription_id,
                provider=provider,
            )
            if payment is None:
                msg = f"Payment {provider_payment_id!r} was not found"
                raise ValueError(msg)
            await session.commit()
            return payment

    async def get_provider_for_payment(self, provider_payment_id: str) -> str:
        """Return provider name for a provider payment identifier."""
        async with async_session_maker() as session:
            provider = await PaymentRepository(session).get_provider_for_payment(
                provider_payment_id
            )
            if provider is None:
                msg = f"Payment {provider_payment_id!r} was not found"
                raise ValueError(msg)
            return provider

    async def get_manual_payment(self, provider_payment_id: str) -> Payment:
        """Return a manual payment with its user and subscription id loaded."""
        return await self.get_payment_by_provider_payment_id(
            MANUAL_PROVIDER_NAME,
            provider_payment_id,
        )

    async def confirm_manual_payment(self, provider_payment_id: str) -> Payment:
        """Confirm a manual payment after admin verification."""
        payment = await self.get_manual_payment(provider_payment_id)
        if payment.status == PaymentStatus.PAID:
            return payment
        return await self.set_status(
            provider_payment_id,
            PaymentStatus.PAID,
            provider=MANUAL_PROVIDER_NAME,
            paid_at=datetime.now(tz=UTC),
        )
