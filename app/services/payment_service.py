"""Payment provider abstractions and payment service helpers."""

from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Payment, PaymentStatus, User
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.platega_client import PlategaClient, build_platega_payload


MANUAL_PROVIDER_NAME: Final = "manual"
PLATEGA_PROVIDER_NAME: Final = "platega"


def _extract_provider_expires_at(
    data: dict[str, Any], created_at: datetime | None = None
) -> datetime | None:
    """Extract Platega expiration time from ISO datetime or expiresIn values."""
    for key in ("expiresAt", "expires_at", "expiredAt", "expired_at"):
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    expires_in = data.get("expiresIn")
    if expires_in is None or expires_in == "":
        expires_in = data.get("expires_in")
    if expires_in is None or expires_in == "":
        return None

    try:
        numeric_expires_in = float(expires_in)
    except (TypeError, ValueError):
        return None

    if numeric_expires_in > 10_000_000_000:
        return datetime.fromtimestamp(numeric_expires_in / 1000, tz=UTC)
    if numeric_expires_in > 1_000_000_000:
        return datetime.fromtimestamp(numeric_expires_in, tz=UTC)

    base = created_at or datetime.now(tz=UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return base + timedelta(seconds=numeric_expires_in)


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
    """Platega payment provider selected by settings."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: PlategaClient | None = None,
        bot: Any | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.client = client
        self.bot = bot

    async def create_payment(
        self,
        user_id: int,
        tariff_id: int,
        amount: Decimal | int | str,
        currency: str,
    ) -> PaymentCreateResult:
        """Create a local pending payment and initialize a Platega transaction."""
        async with async_session_maker() as session:
            user = await ManualPaymentProvider._get_or_create_user(session, user_id)
            description = f"Payment for tariff {tariff_id}"
            repository = PaymentRepository(session)
            payment = await repository.create_payment(
                user_id=user.id,
                provider=PLATEGA_PROVIDER_NAME,
                status=PaymentStatus.PENDING,
                tariff_months=tariff_id,
                amount=amount,
                currency=currency,
                description=description,
            )
            await session.commit()

            payload = build_platega_payload(
                payment_id=payment.id,
                telegram_id=user_id,
                months=tariff_id,
            )
            create_request: dict[str, Any] = {
                "amount": payment.amount,
                "currency": payment.currency,
                "description": description,
                "payload": payload,
                "user_id": user_id,
                "user_name": str(user_id),
                "payment_method": self.settings.platega_payment_method,
            }

            client = self.client or PlategaClient(settings=self.settings)
            should_close_client = self.client is None
            sanitized_create_request = self._sanitize_provider_data(create_request)
            try:
                provider_response = await client.create_transaction(
                    amount=payment.amount,
                    currency=payment.currency,
                    description=description,
                    payload=payload,
                    user_id=user_id,
                    user_name=str(user_id),
                    payment_method=self.settings.platega_payment_method,
                )
            except Exception as error:
                sanitized_error = self._sanitize_error(error)
                provider_data = self._merge_provider_data(
                    payment.provider_data,
                    {
                        "create_request": sanitized_create_request,
                        "create_error": sanitized_error,
                    },
                )
                await self._fail_created_payment(
                    session,
                    payment,
                    provider_data,
                    "platega_create_failed",
                )
                await self._notify_admins(
                    "Ошибка создания платежа Platega\n"
                    f"Payment ID: <code>{payment.id}</code>\n"
                    f"Telegram ID: <code>{user_id}</code>\n"
                    f"Ошибка: <code>{escape(sanitized_error)}</code>"
                )
                msg = "Platega payment creation failed"
                raise RuntimeError(msg) from error
            finally:
                if should_close_client:
                    await client.close()

            sanitized_provider_response = self._sanitize_provider_data(
                provider_response
            )
            provider_data = {
                "create_response": sanitized_provider_response,
                "create_request": sanitized_create_request,
            }

            provider_payment_id = self._extract_first_str(
                provider_response,
                "transactionId",
                "transaction_id",
                "id",
                "paymentId",
                "payment_id",
                "uuid",
            )
            payment_url = self._extract_first_str(
                provider_response,
                "redirect",
                "redirectUrl",
                "redirect_url",
                "paymentUrl",
                "payment_url",
                "url",
                "link",
            )

            if provider_payment_id is None:
                await self._fail_created_payment(
                    session,
                    payment,
                    provider_data,
                    "platega_missing_transaction_id",
                )
                msg = "Platega response does not contain a transaction id"
                raise ValueError(msg)

            if payment_url is None:
                await self._fail_created_payment(
                    session,
                    payment,
                    provider_data,
                    "platega_missing_redirect_url",
                )
                msg = "Platega response does not contain a payment redirect URL"
                raise ValueError(msg)

            payment.provider_payment_id = provider_payment_id
            payment.provider_redirect_url = payment_url
            payment.provider_payment_method = self._extract_first_str(
                provider_response,
                "paymentMethod",
                "payment_method",
                "method",
            )
            payment.provider_expires_at = _extract_provider_expires_at(
                provider_response,
                payment.created_at,
            )
            payment.provider_data = provider_data
            await session.flush()
            await session.commit()

            return PaymentCreateResult(
                payment=payment,
                provider_payment_id=payment.provider_payment_id,
                payment_url=payment.provider_redirect_url,
                provider=PLATEGA_PROVIDER_NAME,
            )

    @staticmethod
    def _merge_provider_data(
        existing_provider_data: Any,
        new_provider_data: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(existing_provider_data, dict):
            return {**existing_provider_data, **new_provider_data}
        return new_provider_data

    def _sanitize_error(self, error: Exception) -> str:
        return str(
            self._sanitize_provider_data({"error": " ".join(str(error).split())})[
                "error"
            ]
        )[:2000]

    async def _notify_admins(self, text: str) -> None:
        if self.bot is None:
            return
        for admin_id in self.settings.admin_ids:
            with suppress(Exception):
                await self.bot.send_message(admin_id, text)

    @staticmethod
    async def _fail_created_payment(
        session: AsyncSession,
        payment: Payment,
        provider_data: dict[str, Any],
        status_reason: str,
    ) -> None:
        payment.status = PaymentStatus.FAILED
        payment.status_reason = status_reason
        payment.provider_data = provider_data
        await session.flush()
        await session.commit()

    @staticmethod
    def _extract_first_str(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value is not None and value != "":
                return str(value)
        return None

    @staticmethod
    def _extract_datetime(data: dict[str, Any], *keys: str) -> datetime | None:
        value = PlategaPaymentProvider._extract_first_str(data, *keys)
        if value is None:
            return None
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    def _sanitize_provider_data(self, data: dict[str, Any]) -> dict[str, Any]:
        secret_values = [
            secret.get_secret_value()
            for secret in (
                self.settings.platega_api_key,
                self.settings.platega_callback_secret,
            )
            if secret is not None
        ]
        return self._sanitize_value(data, secret_values)

    @classmethod
    def _sanitize_value(cls, value: Any, secret_values: list[str]) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for item_key, item_value in value.items():
                key = str(item_key)
                if cls._is_sensitive_key(key):
                    sanitized[key] = "***"
                else:
                    sanitized[key] = cls._sanitize_value(item_value, secret_values)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_value(item, secret_values) for item in value]
        if isinstance(value, str):
            sanitized_text = value
            for secret_value in secret_values:
                if secret_value:
                    sanitized_text = sanitized_text.replace(secret_value, "***")
            return sanitized_text
        if isinstance(value, Decimal):
            if value == value.to_integral_value():
                return int(value)
            return str(value)
        return value

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        normalized = key.lower()
        return any(
            marker in normalized
            for marker in (
                "secret",
                "token",
                "password",
                "authorization",
                "api_key",
                "apikey",
            )
        )

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
