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
async def test_process_pending_payment_webhooks_skips_non_platega_provider(
    monkeypatch,
) -> None:
    def fail_session_maker():
        raise AssertionError(
            "Webhook retry must not load events for non-Platega providers"
        )

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
async def test_process_pending_payment_webhooks_loads_platega_events(
    monkeypatch,
) -> None:
    events = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    processed_event_ids = []

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

        async def get_retryable_webhook_events(self, *, provider, now, max_attempts):
            assert provider == scheduler_module.PLATEGA_PROVIDER_NAME
            assert max_attempts == 5
            return events

        async def mark_webhook_attempt(self, event_id, attempted_at):
            return SimpleNamespace(id=event_id, attempt_count=1)

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
async def test_reconcile_platega_payments_skips_pending_without_provider_id(
    monkeypatch,
) -> None:
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
async def test_reconcile_platega_payments_expires_only_after_provider_pending(
    monkeypatch,
) -> None:
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


class FakeExpiryXuiClient:
    created_nodes: list[str] = []
    closed_nodes: list[str] = []

    def __init__(self, settings: Settings, node: object | None = None) -> None:
        self.node = node
        FakeExpiryXuiClient.created_nodes.append(node.key)

    async def update_client(
        self,
        inbound_id: int,
        client_id: str,
        payload: dict[str, object],
        *,
        enable: bool,
    ) -> None:
        assert inbound_id == 2
        assert client_id == "client-id"
        assert payload["clients"][0]["enable"] is False
        assert enable is False

    async def close(self) -> None:
        FakeExpiryXuiClient.closed_nodes.append(self.node.key)


@pytest.mark.anyio
async def test_deprovision_subscription_client_uses_subscription_node_and_closes_client(
    monkeypatch,
) -> None:
    FakeExpiryXuiClient.created_nodes = []
    FakeExpiryXuiClient.closed_nodes = []
    monkeypatch.setattr(scheduler_module, "XuiClient", FakeExpiryXuiClient)
    settings = make_settings(
        xui_expired_client_policy="disable",
        xui_nodes_json="""
        [
            {
                "key": "node-a",
                "xui_base_url": "https://node-a.example.test/",
                "xui_username": "user-a",
                "xui_password": "password-a",
                "xui_inbound_ids": [1]
            },
            {
                "key": "node-b",
                "xui_base_url": "https://node-b.example.test/",
                "xui_username": "user-b",
                "xui_password": "password-b",
                "xui_inbound_ids": [2]
            }
        ]
        """,
    )
    node_selector = scheduler_module.NodeSelectorService(settings=settings)
    subscription = SimpleNamespace(
        node_key="node-b",
        inbound_id=2,
        xui_client_id="client-id",
        xui_email="client@example.test",
        vpn_config={"client": {"flow": "xtls-rprx-vision"}},
    )

    await scheduler_module._deprovision_subscription_client(
        subscription,
        node_selector,
        settings,
    )

    assert FakeExpiryXuiClient.created_nodes == ["node-b"]
    assert FakeExpiryXuiClient.closed_nodes == ["node-b"]


@pytest.mark.anyio
async def test_expire_subscriptions_records_deprovision_failure_and_continues(
    monkeypatch,
) -> None:
    commits = []
    messages = []
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    failed = SimpleNamespace(
        id=10,
        user=SimpleNamespace(telegram_id=1001),
        node_key="node-a",
        status=scheduler_module.SubscriptionStatus.ACTIVE,
        disabled_at=None,
        expires_at=now - timedelta(hours=1),
        vpn_config={"client": {"id": "failed-client"}},
    )
    succeeded = SimpleNamespace(
        id=11,
        user=SimpleNamespace(telegram_id=1002),
        node_key="node-b",
        status=scheduler_module.SubscriptionStatus.ACTIVE,
        disabled_at=None,
        expires_at=now - timedelta(hours=1),
        vpn_config={"client": {"id": "succeeded-client"}},
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            commits.append((failed.status, succeeded.status))

    class FakeBot:
        async def send_message(self, telegram_id, text):
            messages.append((telegram_id, text))

    async def fake_expired_active_subscriptions(session, current_time):
        assert current_time == now
        return [failed, succeeded]

    async def fake_deprovision(subscription, node_selector, settings):
        if subscription is failed:
            raise RuntimeError("secret=super-secret broken")

    async def fake_create_notification_event(
        session, *, subscription, notification_type
    ):
        assert subscription is succeeded
        assert (
            notification_type == scheduler_module.SubscriptionNotificationType.EXPIRED
        )
        return True

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(scheduler_module, "datetime", FixedDatetime)
    monkeypatch.setattr(scheduler_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(
        scheduler_module,
        "_expired_active_subscriptions",
        fake_expired_active_subscriptions,
    )
    monkeypatch.setattr(
        scheduler_module, "_deprovision_subscription_client", fake_deprovision
    )
    monkeypatch.setattr(
        scheduler_module, "_create_notification_event", fake_create_notification_event
    )

    await scheduler_module.expire_subscriptions(
        bot=FakeBot(),
        settings=make_settings(admin_ids=[9001], xui_expired_client_policy="delete"),
    )

    assert failed.status == scheduler_module.SubscriptionStatus.ACTIVE
    assert failed.disabled_at is None
    assert failed.node_key == "node-a"
    assert failed.vpn_config["deprovision_failed_at"] == now.isoformat()
    assert failed.vpn_config["deprovision_policy"] == "delete"
    assert failed.vpn_config["deprovision_error"] == "secret=*** broken"
    assert failed.vpn_config["node_slot_released"] is False
    assert succeeded.status == scheduler_module.SubscriptionStatus.EXPIRED
    assert succeeded.disabled_at == now
    assert succeeded.node_key == "node-b"
    assert succeeded.vpn_config["deprovisioned_at"] == now.isoformat()
    assert succeeded.vpn_config["deprovision_policy"] == "delete"
    assert succeeded.vpn_config["node_slot_released"] is True
    assert commits == [
        (
            scheduler_module.SubscriptionStatus.ACTIVE,
            scheduler_module.SubscriptionStatus.ACTIVE,
        ),
        (
            scheduler_module.SubscriptionStatus.ACTIVE,
            scheduler_module.SubscriptionStatus.EXPIRED,
        ),
    ]
    assert messages[0][0] == 9001
    assert "Subscription ID: <code>10</code>" in messages[0][1]
    assert "Telegram ID: <code>1001</code>" in messages[0][1]
    assert "Node key: <code>node-a</code>" in messages[0][1]
    assert "Policy: <code>delete</code>" in messages[0][1]
    assert "secret=*** broken" in messages[0][1]
    assert messages[1][0] == 1002
