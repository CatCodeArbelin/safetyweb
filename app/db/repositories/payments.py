"""Payment repository helpers."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    PaymentWebhookHandlingState,
)


WEBHOOK_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 10800)


def webhook_retry_delay_seconds(retry_count: int) -> int:
    """Return retry backoff delay for a webhook attempt count."""
    if retry_count <= 0:
        return WEBHOOK_RETRY_DELAYS_SECONDS[0]
    index = min(retry_count - 1, len(WEBHOOK_RETRY_DELAYS_SECONDS) - 1)
    return WEBHOOK_RETRY_DELAYS_SECONDS[index]


class PaymentRepository:
    """Persist and query payments."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_payment(
        self,
        *,
        user_id: int,
        provider: str,
        amount: Decimal | int | str,
        currency: str = "RUB",
        provider_payment_id: str | None = None,
        subscription_id: int | None = None,
        status: PaymentStatus = PaymentStatus.PENDING,
        tariff_months: int | None = None,
        description: str | None = None,
        provider_redirect_url: str | None = None,
        provider_expires_at: datetime | None = None,
        provider_payment_method: str | None = None,
        provider_data: dict[str, Any] | None = None,
        status_reason: str | None = None,
        paid_at: datetime | None = None,
    ) -> Payment:
        """Create and flush a payment."""
        payment = Payment(
            user_id=user_id,
            subscription_id=subscription_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            status=status,
            amount=Decimal(str(amount)),
            currency=currency.upper(),
            tariff_months=tariff_months,
            description=description,
            provider_redirect_url=provider_redirect_url,
            provider_expires_at=provider_expires_at,
            provider_payment_method=provider_payment_method,
            provider_data=provider_data,
            status_reason=status_reason,
            paid_at=paid_at,
        )
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def get_by_id(self, payment_id: int) -> Payment | None:
        """Load a payment by primary key with its user."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.id == payment_id)
        )

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

    async def get_for_update(self, payment_id: int) -> Payment | None:
        """Load a payment by primary key with a row lock."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.id == payment_id)
            .with_for_update()
        )

    async def get_by_provider_payment_id_for_update(
        self, provider: str, provider_payment_id: str
    ) -> Payment | None:
        """Load a payment for a provider with a row lock."""
        return await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(
                Payment.provider == provider,
                Payment.provider_payment_id == provider_payment_id,
            )
            .with_for_update()
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
        status_reason: str | None = None,
        provider_data: dict[str, Any] | None = None,
        provider_data_patch: dict[str, Any] | None = None,
    ) -> Payment | None:
        """Set payment status and optionally update provider metadata."""
        payment = await self._get_payment(provider_payment_id, provider)
        if payment is None:
            return None
        payment.status = status
        if paid_at is not None:
            payment.paid_at = paid_at
        if status_reason is not None:
            payment.status_reason = status_reason
        if provider_data is not None:
            payment.provider_data = provider_data
        if provider_data_patch is not None:
            payment.provider_data = {
                **(payment.provider_data or {}),
                **provider_data_patch,
            }
        await self.session.flush()
        return payment

    async def get_pending_by_provider(self, provider: str) -> list[Payment]:
        """Load pending payments for a provider."""
        return list(
            await self.session.scalars(
                select(Payment)
                .options(selectinload(Payment.user))
                .where(
                    Payment.provider == provider,
                    Payment.status == PaymentStatus.PENDING,
                )
                .order_by(Payment.created_at)
            )
        )

    async def get_expired_pending_platega(self, now: datetime) -> list[Payment]:
        """Load pending Platega payments whose provider expiry time has passed."""
        return list(
            await self.session.scalars(
                select(Payment)
                .options(selectinload(Payment.user))
                .where(
                    Payment.provider == "platega",
                    Payment.status == PaymentStatus.PENDING,
                    Payment.provider_expires_at.is_not(None),
                    Payment.provider_expires_at <= now,
                )
                .order_by(Payment.provider_expires_at)
            )
        )

    async def create_webhook_event(
        self,
        *,
        provider: str,
        payload_hash: str,
        headers: dict[str, Any],
        raw_body: bytes,
        provider_payment_id: str | None = None,
        payment_id: int | None = None,
        event_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> PaymentWebhookEvent:
        """Create and flush a payment webhook event."""
        event = PaymentWebhookEvent(
            provider=provider,
            provider_payment_id=provider_payment_id,
            payment_id=payment_id,
            event_status=event_status,
            payload_hash=payload_hash,
            headers=headers,
            raw_body=raw_body,
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_retryable_webhook_events(
        self, provider: str, now: datetime, limit: int = 100
    ) -> list[PaymentWebhookEvent]:
        """Load webhook events due for initial processing or retry."""
        statement = (
            select(PaymentWebhookEvent)
            .where(
                PaymentWebhookEvent.provider == provider,
                PaymentWebhookEvent.handling_state.in_(
                    [
                        PaymentWebhookHandlingState.PENDING,
                        PaymentWebhookHandlingState.FAILED,
                    ]
                ),
                or_(
                    PaymentWebhookEvent.next_retry_at.is_(None),
                    PaymentWebhookEvent.next_retry_at <= now,
                ),
            )
            .order_by(
                PaymentWebhookEvent.next_retry_at.asc().nullsfirst(),
                PaymentWebhookEvent.created_at,
            )
            .limit(limit)
        )
        return list(await self.session.scalars(statement))

    async def get_unprocessed_webhook_events(
        self, provider: str | None = None, limit: int = 100
    ) -> list[PaymentWebhookEvent]:
        """Load pending or failed webhook events ordered by creation time."""
        statement = (
            select(PaymentWebhookEvent)
            .where(
                PaymentWebhookEvent.handling_state.in_(
                    [
                        PaymentWebhookHandlingState.PENDING,
                        PaymentWebhookHandlingState.FAILED,
                    ]
                )
            )
            .order_by(PaymentWebhookEvent.created_at)
            .limit(limit)
        )
        if provider is not None:
            statement = statement.where(PaymentWebhookEvent.provider == provider)
        return list(await self.session.scalars(statement))

    async def get_webhook_event(
        self, webhook_event_id: int
    ) -> PaymentWebhookEvent | None:
        """Load a webhook event by primary key."""
        return await self.session.get(PaymentWebhookEvent, webhook_event_id)

    async def get_webhook_event_by_payload_hash(
        self, provider: str, payload_hash: str
    ) -> PaymentWebhookEvent | None:
        """Load a webhook event by provider and payload hash."""
        return await self.session.scalar(
            select(PaymentWebhookEvent).where(
                PaymentWebhookEvent.provider == provider,
                PaymentWebhookEvent.payload_hash == payload_hash,
            )
        )

    async def mark_webhook_processed(
        self, webhook_event_id: int, processed_at: datetime
    ) -> PaymentWebhookEvent | None:
        """Mark a webhook event as processed."""
        event = await self.session.get(PaymentWebhookEvent, webhook_event_id)
        if event is None:
            return None
        event.handling_state = PaymentWebhookHandlingState.PROCESSED
        event.processing_error = None
        event.next_retry_at = None
        event.processed_at = processed_at
        await self.session.flush()
        return event

    async def mark_webhook_attempt(
        self, event_id: int, now: datetime
    ) -> PaymentWebhookEvent | None:
        """Record that webhook processing was attempted."""
        event = await self.session.get(PaymentWebhookEvent, event_id)
        if event is None:
            return None
        event.retry_count += 1
        event.last_attempt_at = now
        event.next_retry_at = None
        await self.session.flush()
        return event

    async def mark_webhook_failed(
        self, webhook_event_id: int, processing_error: str
    ) -> PaymentWebhookEvent | None:
        """Mark a webhook event as failed and schedule the next retry."""
        event = await self.session.get(PaymentWebhookEvent, webhook_event_id)
        if event is None:
            return None
        event.handling_state = PaymentWebhookHandlingState.FAILED
        event.processing_error = processing_error
        if event.last_attempt_at is not None:
            event.next_retry_at = event.last_attempt_at + timedelta(
                seconds=webhook_retry_delay_seconds(event.retry_count)
            )
        await self.session.flush()
        return event

    async def mark_webhook_dead(
        self, event_id: int, error: str
    ) -> PaymentWebhookEvent | None:
        """Stop retrying a webhook event after it exceeds retry attempts."""
        event = await self.session.get(PaymentWebhookEvent, event_id)
        if event is None:
            return None
        event.handling_state = PaymentWebhookHandlingState.DEAD
        event.processing_error = error
        event.next_retry_at = None
        await self.session.flush()
        return event

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
