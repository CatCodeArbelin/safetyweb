import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.db.models import PaymentStatus
from app.services.platega_webhook_service import PlategaWebhookService, map_platega_status


def test_map_platega_status_handles_official_statuses() -> None:
    assert map_platega_status("PENDING") == PaymentStatus.PENDING
    assert map_platega_status("CONFIRMED") == PaymentStatus.PAID
    assert map_platega_status("CANCELED") == PaymentStatus.FAILED
    assert map_platega_status("CHARGEBACKED") == PaymentStatus.REFUNDED


def test_map_platega_status_does_not_treat_expired_as_provider_expired() -> None:
    assert map_platega_status("expired") == PaymentStatus.PENDING


def test_map_platega_status_treats_broad_aliases_as_pending() -> None:
    for status in (
        "PAID",
        "SUCCESS",
        "REFUNDED",
        "TIMEOUT",
        "DECLINED",
        "FAILED",
        "CHARGEBACK",
    ):
        assert map_platega_status(status) == PaymentStatus.PENDING


def test_extract_recovery_payload_handles_json_payload() -> None:
    payload = PlategaWebhookService._extract_recovery_payload(
        {
            "id": "provider-id",
            "payload": (
                '{"internalPaymentId": "42", "telegramId": "123", "months": "3"}'
            ),
        },
        None,
    )

    assert payload["internalPaymentId"] == "42"
    assert payload["telegramId"] == "123"
    assert payload["months"] == "3"


def test_extract_recovery_payload_handles_data_payload() -> None:
    payload = PlategaWebhookService._extract_recovery_payload(
        {"id": "provider-id", "data": {"payload": {"paymentId": 42, "months": 6}}},
        None,
    )

    assert payload["paymentId"] == 42
    assert payload["months"] == 6


def test_candidate_values_traverses_payment_details() -> None:
    values = PlategaWebhookService._candidate_values(
        {"paymentDetails": {"amount": "10.50", "currency": "RUB"}},
        "amount",
        "currency",
    )

    assert values == ["10.50", "RUB"]


def test_transaction_payment_details_take_precedence_over_fallbacks() -> None:
    transaction = {
        "amount": "99.99",
        "currency": "USD",
        "paymentDetails": {"amount": "10.50", "currency": "RUB"},
    }

    assert PlategaWebhookService._extract_transaction_amount(transaction) == Decimal(
        "10.50"
    )
    assert PlategaWebhookService._extract_transaction_currency(transaction) == "RUB"


def test_create_recovery_payment_returns_none_without_amount_or_currency() -> None:
    async def run() -> None:
        service = PlategaWebhookService()
        repository = type("Repository", (), {"session": object()})()

        missing_amount = await service._create_recovery_payment(
            repository,
            "provider-id",
            {"telegramId": "123", "months": "1"},
            {"paymentDetails": {"currency": "RUB"}},
        )
        missing_currency = await service._create_recovery_payment(
            repository,
            "provider-id",
            {"telegramId": "123", "months": "1"},
            {"paymentDetails": {"amount": "10.50"}},
        )

        assert missing_amount is None
        assert missing_currency is None

    asyncio.run(run())
