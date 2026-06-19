import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.config import Settings
from app.db.models import PaymentStatus
from app.tasks import scheduler as scheduler_module
from app.tasks.scheduler import (
    PLATEGA_RECONCILE_JOB_ID,
    PLATEGA_WEBHOOK_RETRY_JOB_ID,
    create_scheduler,
    process_pending_payment_webhooks,
    reconcile_platega_payments,
)


def make_settings(**overrides: object) -> Settings:
    values = {
        "bot_token": "bot-token",
        "postgres_password": "postgres-password",
        "xui_username": "xui-user",
        "xui_password": "xui-password",
        "xui_inbound_ids": [1],
    }
    values.update(overrides)
    return Settings(**values)


def test_create_scheduler_skips_platega_jobs_for_manual_provider() -> None:
    scheduler = create_scheduler(
        bot=object(), settings=make_settings(payment_provider="manual")
    )

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is None


def test_create_scheduler_skips_platega_jobs_in_test_mode() -> None:
    scheduler = create_scheduler(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=True,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is None


def test_create_scheduler_adds_platega_jobs_for_live_platega_provider() -> None:
    scheduler = create_scheduler(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=False,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is not None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is not None


@pytest.mark.anyio
async def test_process_pending_payment_webhooks_skips_non_platega_provider(monkeypatch) -> None:
    def fail_session_maker():
        raise AssertionError("Webhook retry must not load events for non-Platega providers")

    monkeypatch.setattr(scheduler_module, "async_session_maker", fail_session_maker)

    await process_pending_payment_webhooks(
        bot=object(),
        settings=make_settings(payment_provider="manual"),
    )


@pytest.mark.anyio
async def test_process_pending_payment_webhooks_skips_test_mode(monkeypatch) -> None:
    def fail_session_maker():
        raise AssertionError("Webhook retry must not load events in test mode")

    monkeypatch.setattr(scheduler_module, "async_session_maker", fail_session_maker)

    await process_pending_payment_webhooks(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=True,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )


@pytest.mark.anyio
async def test_process_pending_payment_webhooks_loads_platega_events(monkeypatch) -> None:
    events = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    processed_event_ids = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeRepository:
        def __init__(self, session):
            self.session = session

        async def get_unprocessed_webhook_events(self, *, provider):
            assert provider == scheduler_module.PLATEGA_PROVIDER_NAME
            return events

    class FakeService:
        def __init__(self, settings, bot):
            self.settings = settings
            self.bot = bot

        async def process_event(self, event_id):
            processed_event_ids.append(event_id)

    monkeypatch.setattr(scheduler_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(scheduler_module, "PaymentRepository", FakeRepository)
    monkeypatch.setattr(scheduler_module, "PlategaWebhookService", FakeService)

    await process_pending_payment_webhooks(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=False,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert processed_event_ids == [1, 2]


@pytest.mark.anyio
async def test_reconcile_platega_payments_skips_pending_without_provider_id(monkeypatch) -> None:
    payments = [
        SimpleNamespace(
            provider_payment_id=None,
            status_reason="platega_create_failed",
        )
    ]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeRepository:
        def __init__(self, session):
            self.session = session

        async def get_pending_by_provider(self, provider):
            return payments

    def fail_client(*args, **kwargs):
        raise AssertionError("Platega client must not be created without provider ids")

    monkeypatch.setattr(scheduler_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(scheduler_module, "PaymentRepository", FakeRepository)
    monkeypatch.setattr(scheduler_module, "PlategaClient", fail_client)

    await reconcile_platega_payments(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=False,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert payments[0].status_reason == "platega_create_failed"

@pytest.mark.anyio
async def test_reconcile_platega_payments_expires_only_after_provider_pending(monkeypatch) -> None:
    now = datetime.now(tz=UTC)
    payment = SimpleNamespace(
        provider_payment_id="provider-payment-id",
        provider_expires_at=now - timedelta(minutes=1),
        tariff_months=1,
        status=PaymentStatus.PENDING,
        status_reason=None,
    )
    processed_statuses = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    class FakeRepository:
        def __init__(self, session):
            self.session = session

        async def get_pending_by_provider(self, provider):
            return [payment]

        async def get_by_provider_payment_id_for_update(
            self, provider, provider_payment_id
        ):
            assert provider_payment_id == payment.provider_payment_id
            return payment

    class FakeClient:
        def __init__(self, settings):
            self.settings = settings

        async def get_transaction(self, provider_payment_id):
            assert provider_payment_id == payment.provider_payment_id
            return {"status": "PENDING"}

        async def close(self):
            return None

    class FakeService:
        def __init__(self, settings, bot, client):
            self.settings = settings
            self.bot = bot
            self.client = client

        def _extract_transaction_status(self, transaction):
            return transaction["status"]

        async def process_transaction_status(
            self,
            provider_payment_id,
            status,
            *,
            months=None,
            transaction=None,
        ):
            processed_statuses.append(
                (provider_payment_id, status, months, transaction)
            )
            assert payment.status == PaymentStatus.PENDING
            return True

    monkeypatch.setattr(scheduler_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(scheduler_module, "PaymentRepository", FakeRepository)
    monkeypatch.setattr(scheduler_module, "PlategaClient", FakeClient)
    monkeypatch.setattr(scheduler_module, "PlategaWebhookService", FakeService)

    await reconcile_platega_payments(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=False,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert processed_statuses == [
        ("provider-payment-id", "PENDING", 1, {"status": "PENDING"})
    ]
    assert payment.status == PaymentStatus.EXPIRED
    assert payment.status_reason == "expired_locally"
