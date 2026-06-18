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

            await session.commit()
            client = self.client or PlategaClient(settings=self.settings)
            try:
                verified_transaction = await client.get_transaction(provider_payment_id)
            finally:
                if self.client is None:
                    await client.close()

            transaction_id = self._extract_transaction_id(verified_transaction)
            if transaction_id != provider_payment_id:
                msg = (
                    "Platega transaction id mismatch: "
                    f"webhook={provider_payment_id!r}, transaction={transaction_id!r}"
                )
                async with async_session_maker() as failed_session:
                    await PaymentRepository(failed_session).mark_webhook_failed(
                        webhook_event_id,
                        msg,
                    )
                    await failed_session.commit()
                await self._notify_admins(
                    "Platega webhook transaction id mismatch\n"
                    f"Webhook event ID: <code>{webhook_event_id}</code>\n"
                    f"Webhook payment ID: <code>{escape(provider_payment_id)}</code>\n"
                    f"Transaction ID: <code>{escape(str(transaction_id))}</code>",
                )
                return

            transaction_status = self._extract_transaction_status(verified_transaction)
            event_payment_id = event.payment_id

        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            payment = await repository.get_by_provider_payment_id_for_update(
                PLATEGA_PROVIDER_NAME,
                provider_payment_id,
            )

            if payment is None and event_payment_id is not None:
                payment = await repository.get_for_update(event_payment_id)
                if payment is None or payment.provider != PLATEGA_PROVIDER_NAME:
                    payment = None

            if payment is None:
                msg = f"Platega payment {provider_payment_id!r} was not found"
                await repository.mark_webhook_failed(webhook_event_id, msg)
                await session.commit()
                return

            db_event = await repository.get_webhook_event(webhook_event_id)
            if db_event is not None:
                if db_event.payment_id is None:
                    db_event.payment_id = payment.id
                if db_event.provider_payment_id is None:
                    db_event.provider_payment_id = provider_payment_id
            months = payment.tariff_months
            await session.commit()

        processed = await self.process_transaction_status(
            provider_payment_id,
            transaction_status,
            months=months,
            status_reason_prefix="Platega transaction status",
            transaction=verified_transaction,
        )
        if processed:
            async with async_session_maker() as processed_session:
                await PaymentRepository(processed_session).mark_webhook_processed(
                    webhook_event_id,
                    datetime.now(tz=UTC),
                )
                await processed_session.commit()
            return

        async with async_session_maker() as processed_session:
            await PaymentRepository(processed_session).mark_webhook_processed(
                webhook_event_id,
                datetime.now(tz=UTC),
            )
            await processed_session.commit()

    async def process_transaction_status(
        self,
        provider_payment_id: str,
        status: str | None,
        *,
        months: int | None = None,
        status_reason_prefix: str = "Platega transaction status",
        transaction: dict[str, Any] | None = None,
    ) -> bool:
        """Apply a Platega transaction status using the shared payment processor."""
        normalized_status = self._normalize_status(
            self._extract_transaction_status(transaction or {}) or status
        )
        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            payment = await repository.get_by_provider_payment_id_for_update(
                PLATEGA_PROVIDER_NAME,
                provider_payment_id,
            )
            if payment is None:
                return False

            if normalized_status in self.PAID_STATUSES:
                payment_months = months or payment.tariff_months
                if not payment_months:
                    msg = f"Cannot determine tariff months for Platega payment {payment.id}"
                    raise ValueError(msg)
                await session.commit()
                await PaymentFinalizationService(
                    settings=self.settings,
                    bot=self.bot,
                ).finalize_paid_payment(provider_payment_id, payment_months)
                return True

            if normalized_status == self.CHARGEBACK_STATUS:
                payment.status = PaymentStatus.REFUNDED
                payment.status_reason = "chargebacked"
                await session.commit()
                await self._notify_admins(
                    "Получен chargeback по Platega-платежу\n"
                    f"Payment ID: <code>{escape(provider_payment_id)}</code>\n"
                    "Подписка не отключалась автоматически.",
                )
                return True

            if normalized_status in self.FAILED_STATUSES:
                if payment.status != PaymentStatus.PAID:
                    payment.status = PaymentStatus.FAILED
                    payment.status_reason = f"{status_reason_prefix}: {status}"
                await session.commit()
                return True

            await session.commit()
            return True

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
    def _extract_transaction_id(cls, data: dict[str, Any]) -> str | None:
        for value in cls._candidate_values(
            data,
            "id",
            "transactionId",
            "transaction_id",
            "paymentId",
            "payment_id",
            "uuid",
        ):
            if value is None:
                continue
            transaction_id = str(value).strip()
            if transaction_id:
                return transaction_id
        return None

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
