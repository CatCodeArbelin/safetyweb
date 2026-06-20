import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.config import Settings
from app.db.models import SubscriptionStatus
from app.services import vpn_service as vpn_service_module
from app.services.vpn_service import VpnService


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.flush_count = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


class FakeSubscriptionRepository:
    def __init__(self, session) -> None:
        self.session = session

    async def get_active_by_telegram_id(self, telegram_id: int):
        active = [
            subscription
            for subscription in self.session.subscriptions
            if subscription.user.telegram_id == telegram_id
            and subscription.status == SubscriptionStatus.ACTIVE
        ]
        return active[-1] if active else None

    async def create_active(self, **kwargs):
        subscription = SimpleNamespace(
            id=len(self.session.subscriptions) + 1,
            user=kwargs["user"],
            xui_client_id=kwargs["xui_client_id"],
            xui_email=kwargs["xui_email"],
            inbound_id=kwargs["inbound_id"],
            status=SubscriptionStatus.ACTIVE,
            expires_at=kwargs["expires_at"],
            traffic_limit_gb=kwargs["traffic_limit_gb"],
            vpn_config=kwargs["vpn_config"],
            node_key=kwargs["node_key"],
            node_label=kwargs["node_label"],
        )
        self.session.subscriptions.append(subscription)
        self.session.add(subscription)
        await self.session.flush()
        return subscription


class FakeUserRepository:
    def __init__(self, session) -> None:
        self.session = session

    async def get_or_create(self, telegram_id: int):
        if self.session.user is None:
            self.session.user = SimpleNamespace(id=1, telegram_id=telegram_id)
        return self.session.user


class FakeXuiClient:
    def __init__(self) -> None:
        self.added = []
        self.deleted = []

    async def add_client(self, client_payload: dict, inbound_ids: list[int]):
        self.added.append((dict(client_payload), list(inbound_ids)))
        return {"success": True}

    async def delete_client(self, inbound_id: int, client_id: str) -> None:
        self.deleted.append((inbound_id, client_id))


class FakeNodeSelector:
    def __init__(self, node) -> None:
        self.node = node

    def get_node_for_subscription(self, subscription):
        return self.node

    async def select_node_for_new_subscription(self, exclude_payment_id=None):
        return self.node


@pytest.mark.anyio
async def test_paid_purchase_replaces_active_trial_with_new_paid_client(monkeypatch) -> None:
    telegram_id = 12345
    settings = Settings(
        bot_token="bot-token",
        postgres_password="postgres-password",
        xui_username="xui-user",
        xui_password="xui-password",
        xui_inbound_ids=[7],
        xui_sub_base_url="https://sub.example.test/sub",
    )
    session = FakeSession()
    user = SimpleNamespace(id=1, telegram_id=telegram_id)
    trial_subscription = SimpleNamespace(
        id=1,
        user=user,
        status=SubscriptionStatus.ACTIVE,
        inbound_id=7,
        xui_client_id="trial-client-id",
        xui_email="trial_tg_12345_abcd",
        node_key="default",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        vpn_config={"access_type": "trial", "client": {"id": "trial-client-id"}},
    )
    session.user = user
    session.subscriptions = [trial_subscription]
    xui_client = FakeXuiClient()
    service = VpnService(settings=settings, xui_client=xui_client, session=session)
    node = settings.xui_nodes[0]

    monkeypatch.setattr(vpn_service_module, "SubscriptionRepository", FakeSubscriptionRepository)
    monkeypatch.setattr(vpn_service_module, "UserRepository", FakeUserRepository)
    monkeypatch.setattr(
        vpn_service_module,
        "acquire_capacity_selection_lock",
        lambda session: _noop_async(),
    )
    monkeypatch.setattr(service, "_node_selector", lambda session: FakeNodeSelector(node))

    extend_calls = []

    async def fail_extend(*args, **kwargs):
        extend_calls.append((args, kwargs))
        raise AssertionError("trial subscriptions must not be extended as paid access")

    monkeypatch.setattr(service, "_extend_active_subscription", fail_extend)

    await service.provision_or_extend_client(telegram_id, months=1)

    active_subscriptions = [
        subscription
        for subscription in session.subscriptions
        if subscription.status == SubscriptionStatus.ACTIVE
    ]
    new_subscription = active_subscriptions[0]

    assert extend_calls == []
    assert xui_client.deleted == [(7, "trial-client-id")]
    assert trial_subscription.status == SubscriptionStatus.EXPIRED
    assert new_subscription is not trial_subscription
    assert new_subscription.xui_email.startswith("tg_")
    assert not new_subscription.xui_email.startswith("trial_tg_")
    assert new_subscription.vpn_config["access_type"] == "paid"


async def _noop_async() -> None:
    return None
