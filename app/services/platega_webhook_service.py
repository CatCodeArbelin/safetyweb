"""Platega webhook event processing."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.db.models import PaymentStatus, PaymentWebhookEvent
from app.db.repositories.payments import PaymentRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_maker
from app.services.payment_finalization_service import PaymentFinalizationService
from app.services.payment_service import PLATEGA_PROVIDER_NAME
from app.services.platega_client import PlategaClient

if TYPE_CHECKING:
    from aiogram import Bot


def map_platega_status(status: str | None) -> PaymentStatus:
    """Map official Platega transaction statuses to local payment statuses."""
    normalized = (status or "").strip().upper()
    status_map = {
        "PENDING": PaymentStatus.PENDING,
        "CONFIRMED": PaymentStatus.PAID,
        "CANCELED": PaymentStatus.FAILED,
        "CHARGEBACKED": PaymentStatus.REFUNDED,
    }
    return status_map.get(normalized, PaymentStatus.PENDING)


class PlategaWebhookService:
    """Process persisted Platega webhook events outside the HTTP handler."""

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
            recovery_payload = self._extract_recovery_payload(
                verified_transaction,
                event.payload,
            )

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
                payment = await self._recover_payment_by_internal_id(
                    repository,
                    provider_payment_id,
                    recovery_payload,
                    verified_transaction,
                )

            if payment is None:
                payment = await self._create_recovery_payment(
                    repository,
                    provider_payment_id,
                    recovery_payload,
                    verified_transaction,
                )

            if payment is None:
                msg = (
                    f"Platega payment {provider_payment_id!r} was not found and "
                    "could not be recovered from transaction payload"
                )
                await repository.mark_webhook_failed(webhook_event_id, msg)
                await session.commit()
                await self._notify_admins(
                    "Не удалось восстановить Platega-платеж из webhook\n"
                    f"Webhook event ID: <code>{webhook_event_id}</code>\n"
                    f"Payment ID: <code>{escape(provider_payment_id)}</code>\n"
                    "Доступ не выдавался.",
                )
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
        transaction_status = (
            self._extract_transaction_status(transaction or {}) or status
        )
        mapped_status = map_platega_status(transaction_status)
        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            payment = await repository.get_by_provider_payment_id_for_update(
                PLATEGA_PROVIDER_NAME,
                provider_payment_id,
            )
            if payment is None:
                return False

            sanitized_transaction = self._sanitize_transaction(transaction or {})
            payment.provider_data = {
                **(payment.provider_data or {}),
                "last_status_response": sanitized_transaction,
            }
            provider_payment_method = self._extract_provider_payment_method(
                transaction or {}
            )
            if provider_payment_method is not None:
                payment.provider_payment_method = provider_payment_method

            if mapped_status == PaymentStatus.PAID:
                payment_months = months or payment.tariff_months
                if not payment_months:
                    msg = f"Cannot determine tariff months for Platega payment {payment.id}"
                    raise ValueError(msg)
                payment.status = PaymentStatus.PAID
                payment.paid_at = payment.paid_at or datetime.now(tz=UTC)
                payment.status_reason = f"{status_reason_prefix}: {transaction_status}"
                await session.commit()
                await PaymentFinalizationService(
                    settings=self.settings,
                    bot=self.bot,
                ).finalize_paid_payment(
                    provider="platega",
                    provider_payment_id=provider_payment_id,
                    source="platega_webhook",
                )
                return True

            if mapped_status == PaymentStatus.REFUNDED:
                payment.status = PaymentStatus.REFUNDED
                payment.status_reason = f"{status_reason_prefix}: {transaction_status}"
                await session.commit()
                await self._notify_admins(
                    "Получен возврат/chargeback по Platega-платежу\n"
                    f"Payment ID: <code>{escape(provider_payment_id)}</code>\n"
                    "Подписка не отключалась автоматически.",
                )
                return True

            if mapped_status == PaymentStatus.FAILED:
                if payment.status == PaymentStatus.PAID:
                    await session.commit()
                    await self._notify_admins(
                        "Platega прислала failed для уже оплаченного платежа\n"
                        f"Payment ID: <code>{escape(provider_payment_id)}</code>\n"
                        f"Статус Platega: <code>{escape(str(transaction_status))}</code>\n"
                        "Локальный статус paid не откатывался.",
                    )
                    return True

                payment.status = PaymentStatus.FAILED
                payment.status_reason = f"{status_reason_prefix}: {transaction_status}"
                await session.commit()
                return True

            payment.status = PaymentStatus.PENDING
            payment.status_reason = f"{status_reason_prefix}: {transaction_status}"
            await session.commit()
            return True

    async def _recover_payment_by_internal_id(
        self,
        repository: PaymentRepository,
        provider_payment_id: str,
        payload: dict[str, Any],
        transaction: dict[str, Any],
    ) -> Any | None:
        """Attach the verified Platega id to an existing payment from payload ids."""
        internal_payment_id = self._extract_first_payload_value(
            payload,
            "internalPaymentId",
            "paymentId",
        )
        if internal_payment_id is None:
            return None
        try:
            payment_id = int(internal_payment_id)
        except (TypeError, ValueError):
            return None

        payment = await repository.get_for_update(payment_id)
        if payment is None:
            return None

        payment.provider = PLATEGA_PROVIDER_NAME
        payment.provider_payment_id = provider_payment_id
        payment.provider_data = {
            **(payment.provider_data or {}),
            "last_status_response": self._sanitize_transaction(transaction),
        }
        if payment.tariff_months is None:
            payment.tariff_months = self._extract_positive_int(payload, "months")
        await repository.session.flush()
        return payment

    async def _create_recovery_payment(
        self,
        repository: PaymentRepository,
        provider_payment_id: str,
        payload: dict[str, Any],
        transaction: dict[str, Any],
    ) -> Any | None:
        """Create a pending local payment when Platega returned enough metadata."""
        telegram_id = self._extract_positive_int(payload, "telegramId")
        months = self._extract_positive_int(payload, "months")
        if telegram_id is None or months is None:
            return None

        user = await UserRepository(repository.session).get_or_create(telegram_id)
        return await repository.create_payment(
            user_id=user.id,
            provider=PLATEGA_PROVIDER_NAME,
            provider_payment_id=provider_payment_id,
            status=PaymentStatus.PENDING,
            tariff_months=months,
            amount=self._extract_decimal(transaction, "amount") or Decimal("0"),
            currency=(
                str(self._extract_first_payload_value(transaction, "currency") or "RUB")
                .strip()
                .upper()
            ),
            provider_data={
                "recovered_from_webhook": True,
                "last_status_response": self._sanitize_transaction(transaction),
            },
        )

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
    def _extract_recovery_payload(
        cls,
        transaction: dict[str, Any],
        event_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Extract recovery metadata from payload and data.payload containers."""
        for source in (transaction, event_payload or {}):
            for payload in cls._payload_candidates(source):
                if (
                    cls._extract_first_payload_value(
                        payload,
                        "internalPaymentId",
                        "paymentId",
                        "telegramId",
                        "months",
                    )
                    is not None
                ):
                    return payload
        return {}

    @classmethod
    def _payload_candidates(cls, data: Any) -> list[dict[str, Any]]:
        parsed = cls._parse_payload_value(data)
        if not isinstance(parsed, dict):
            return []

        candidates = []
        for key in ("payload",):
            nested = cls._parse_payload_value(parsed.get(key))
            if isinstance(nested, dict):
                candidates.append(nested)

        data_value = cls._parse_payload_value(parsed.get("data"))
        if isinstance(data_value, dict):
            nested = cls._parse_payload_value(data_value.get("payload"))
            if isinstance(nested, dict):
                candidates.append(nested)

        candidates.append(parsed)
        return candidates

    @classmethod
    def _parse_payload_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @classmethod
    def _extract_first_payload_value(cls, data: dict[str, Any], *keys: str) -> Any:
        for value in cls._candidate_values(data, *keys):
            if value is not None and value != "":
                return value
        return None

    @classmethod
    def _extract_positive_int(cls, data: dict[str, Any], *keys: str) -> int | None:
        value = cls._extract_first_payload_value(data, *keys)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    @classmethod
    def _extract_decimal(cls, data: dict[str, Any], *keys: str) -> Decimal | None:
        value = cls._extract_first_payload_value(data, *keys)
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @classmethod
    def _sanitize_transaction(cls, data: dict[str, Any]) -> dict[str, Any]:
        sanitized = cls._sanitize_value(data)
        return sanitized if isinstance(sanitized, dict) else {}

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "***"
                    if cls._is_sensitive_key(str(key))
                    else cls._sanitize_value(item_value)
                )
                for key, item_value in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
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
            for marker in ("token", "secret", "password", "authorization", "api_key")
        )

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
    def _extract_provider_payment_method(cls, data: dict[str, Any]) -> str | None:
        for value in cls._candidate_values(
            data,
            "paymentMethod",
            "payment_method",
            "method",
            "paymentMethodType",
            "payment_method_type",
        ):
            if value is None:
                continue
            method = str(value).strip()
            if method:
                return method
        return None

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
    def _sanitize_error(error: Exception) -> str:
        return " ".join(str(error).split())[:2000]

    async def _notify_admins(self, text: str) -> None:
        if self.bot is None:
            return
        for admin_id in self.settings.admin_ids:
            await self.bot.send_message(admin_id, text)
