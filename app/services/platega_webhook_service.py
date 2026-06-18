"""Platega webhook event processing."""

from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.db.models import PaymentStatus, PaymentWebhookEvent
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.payment_finalization_service import PaymentFinalizationService
from app.services.payment_service import PLATEGA_PROVIDER_NAME


class PlategaWebhookService:
    """Process persisted Platega webhook events outside the HTTP handler."""

    PAID_STATUSES = {
        "paid",
        "success",
        "succeeded",
        "completed",
        "complete",
        "confirmed",
    }
    FAILED_STATUSES = {"failed", "fail", "canceled", "cancelled", "declined", "error"}
    EXPIRED_STATUSES = {"expired", "timeout", "timed_out"}
    REFUNDED_STATUSES = {"refunded", "refund"}

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    async def process_event(self, webhook_event_id: int) -> None:
        """Process a saved webhook event by idempotently updating payment state."""
        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            event = await repository.get_webhook_event(webhook_event_id)
            if event is None or event.provider != PLATEGA_PROVIDER_NAME:
                return
            if str(event.handling_state) == "processed":
                return

            provider_payment_id = event.provider_payment_id
            status = (event.event_status or "").lower()
            payment = None
            if provider_payment_id:
                payment = await repository.get_by_provider_payment_id(
                    PLATEGA_PROVIDER_NAME,
                    provider_payment_id,
                )
                if payment is not None and event.payment_id is None:
                    event.payment_id = payment.id

            try:
                if payment is None:
                    msg = f"Platega payment {provider_payment_id!r} was not found"
                    await repository.mark_webhook_failed(webhook_event_id, msg)
                    await session.commit()
                    return

                if status in self.PAID_STATUSES:
                    months = self._extract_months(event, payment.tariff_months)
                    await session.commit()
                    await PaymentFinalizationService(
                        settings=self.settings,
                    ).finalize_paid_payment(provider_payment_id, months)
                    async with async_session_maker() as final_session:
                        await PaymentRepository(final_session).mark_webhook_processed(
                            webhook_event_id,
                            datetime.now(tz=UTC),
                        )
                        await final_session.commit()
                    return

                terminal_statuses = (
                    self.FAILED_STATUSES
                    | self.EXPIRED_STATUSES
                    | self.REFUNDED_STATUSES
                )
                if status in terminal_statuses:
                    if payment.status != PaymentStatus.PAID:
                        payment.status = self._payment_status_for_event(status)
                        payment.status_reason = (
                            f"Platega webhook status: {event.event_status}"
                        )
                    await repository.mark_webhook_processed(
                        webhook_event_id,
                        datetime.now(tz=UTC),
                    )
                    await session.commit()
                    return

                await repository.mark_webhook_processed(
                    webhook_event_id,
                    datetime.now(tz=UTC),
                )
                await session.commit()
            except Exception as error:
                await session.rollback()
                async with async_session_maker() as error_session:
                    await PaymentRepository(error_session).mark_webhook_failed(
                        webhook_event_id,
                        str(error)[:2000],
                    )
                    await error_session.commit()
                raise

    def _extract_months(
        self, event: PaymentWebhookEvent, payment_tariff_months: int | None
    ) -> int:
        """Extract tariff duration from event payload or linked payment."""
        payload = event.payload or {}
        for value in self._candidate_values(
            payload,
            "months",
            "tariffMonths",
            "tariff_months",
        ):
            try:
                months = int(value)
            except (TypeError, ValueError):
                continue
            if months > 0:
                return months
        if payment_tariff_months:
            return payment_tariff_months
        msg = f"Cannot determine tariff months for webhook event {event.id}"
        raise ValueError(msg)

    @classmethod
    def _candidate_values(cls, data: Any, *keys: str) -> list[Any]:
        values: list[Any] = []
        if not isinstance(data, dict):
            return values
        for key in keys:
            if key in data:
                values.append(data[key])
        for nested_key in ("payload", "metadata", "data", "transaction"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                values.extend(cls._candidate_values(nested, *keys))
        return values

    @classmethod
    def _payment_status_for_event(cls, event_status: str) -> PaymentStatus:
        if event_status in cls.REFUNDED_STATUSES:
            return PaymentStatus.REFUNDED
        if event_status in cls.EXPIRED_STATUSES:
            return PaymentStatus.EXPIRED
        return PaymentStatus.FAILED
