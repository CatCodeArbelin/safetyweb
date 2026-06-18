"""Platega webhook event processing."""

from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.db.models import PaymentStatus, PaymentWebhookEvent
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.payment_finalization_service import PaymentFinalizationService
from app.services.payment_service import PLATEGA_PROVIDER_NAME
from app.services.platega_client import PlategaClient

if TYPE_CHECKING:
    from aiogram import Bot


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
    FAILED_STATUSES = {
        "failed",
        "fail",
        "canceled",
        "cancelled",
        "declined",
        "error",
        "expired",
        "timeout",
        "timed_out",
    }
    CHARGEBACK_STATUS = "chargebacked"

    def __init__(
        self,
        settings: Settings | None = None,
        bot: "Bot | None" = None,
        client: PlategaClient | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.bot = bot
        self.client = client

    async def process_event(self, webhook_event_id: int) -> None:
        """Process a saved webhook event by idempotently updating payment state."""
        try:
            await self._process_event(webhook_event_id)
        except Exception as error:
            async with async_session_maker() as error_session:
                await PaymentRepository(error_session).mark_webhook_failed(
                    webhook_event_id,
                    self._sanitize_error(error),
                )
                await error_session.commit()
            raise

    async def _process_event(self, webhook_event_id: int) -> None:
        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            event = await repository.get_webhook_event(webhook_event_id)
            if event is None or event.provider != PLATEGA_PROVIDER_NAME:
                return
            if str(event.handling_state) == "processed":
                return

            provider_payment_id = event.provider_payment_id
            if not provider_payment_id:
                msg = "Platega webhook event does not contain provider payment id"
                await repository.mark_webhook_failed(webhook_event_id, msg)
                await session.commit()
                return

            payment = await repository.get_by_provider_payment_id_for_update(
                PLATEGA_PROVIDER_NAME,
                provider_payment_id,
            )
            if payment is None:
                msg = f"Platega payment {provider_payment_id!r} was not found"
                await repository.mark_webhook_failed(webhook_event_id, msg)
                await session.commit()
                return

            if event.payment_id is None:
                event.payment_id = payment.id

            status = self._normalize_status(event.event_status)
            if status in self.PAID_STATUSES:
                months = self._extract_months(event, payment.tariff_months)
                await session.commit()
                client = self.client or PlategaClient(settings=self.settings)
                try:
                    transaction = await client.get_transaction(provider_payment_id)
                finally:
                    if self.client is None:
                        await client.close()
                transaction_status = self._normalize_status(
                    self._extract_transaction_status(transaction) or event.event_status
                )
                if transaction_status not in self.PAID_STATUSES:
                    msg = (
                        "Platega transaction status is not successful: "
                        f"{transaction_status}"
                    )
                    async with async_session_maker() as failed_session:
                        await PaymentRepository(failed_session).mark_webhook_failed(
                            webhook_event_id,
                            msg,
                        )
                        await failed_session.commit()
                    return
                await PaymentFinalizationService(
                    settings=self.settings,
                    bot=self.bot,
                ).finalize_paid_payment(provider_payment_id, months)
                async with async_session_maker() as final_session:
                    await PaymentRepository(final_session).mark_webhook_processed(
                        webhook_event_id,
                        datetime.now(tz=UTC),
                    )
                    await final_session.commit()
                return

            if status == self.CHARGEBACK_STATUS:
                payment.status = PaymentStatus.REFUNDED
                payment.status_reason = "chargebacked"
                await repository.mark_webhook_processed(
                    webhook_event_id,
                    datetime.now(tz=UTC),
                )
                await session.commit()
                await self._notify_admins(
                    "Получен chargeback по Platega-платежу\n"
                    f"Payment ID: <code>{escape(provider_payment_id)}</code>\n"
                    "Подписка не отключалась автоматически.",
                )
                return

            if status in self.FAILED_STATUSES:
                if payment.status != PaymentStatus.PAID:
                    payment.status = PaymentStatus.FAILED
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
    def _extract_transaction_status(cls, data: dict[str, Any]) -> str | None:
        for value in cls._candidate_values(
            data,
            "status",
            "state",
            "transactionStatus",
            "transaction_status",
            "paymentStatus",
            "payment_status",
        ):
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _normalize_status(status: str | None) -> str:
        return (status or "").strip().lower()

    @staticmethod
    def _sanitize_error(error: Exception) -> str:
        return " ".join(str(error).split())[:2000]

    async def _notify_admins(self, text: str) -> None:
        if self.bot is None:
            return
        for admin_id in self.settings.admin_ids:
            await self.bot.send_message(admin_id, text)
