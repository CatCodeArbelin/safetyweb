import asyncio
import os
from dataclasses import dataclass

import pytest
from pydantic import SecretStr

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.config import Settings
from app.services.node_selector_service import (
    NodeSelectorService,
    acquire_capacity_selection_lock,
)


def _settings_with_nodes() -> Settings:
    return Settings(
        bot_token=SecretStr("bot-token"),
        postgres_password=SecretStr("postgres-password"),
        xui_default_max_active_subscriptions=None,
        xui_nodes_json="""
        [
            {
                "key": "disabled",
                "enabled": false,
                "max_active_subscriptions": 10,
                "xui_username": "user",
                "xui_password": "password",
                "xui_inbound_ids": [1]
            },
            {
                "key": "full",
                "max_active_subscriptions": 2,
                "xui_username": "user",
                "xui_password": "password",
                "xui_inbound_ids": [2]
            },
            {
                "key": "least-loaded",
                "max_active_subscriptions": 10,
                "xui_username": "user",
                "xui_password": "password",
                "xui_inbound_ids": [3]
            },
            {
                "key": "busy",
                "max_active_subscriptions": 10,
                "xui_username": "user",
                "xui_password": "password",
                "xui_inbound_ids": [4]
            },
            {
                "key": "unlimited",
                "max_active_subscriptions": null,
                "xui_username": "user",
                "xui_password": "password",
                "xui_inbound_ids": [5]
            }
        ]
        """,
    )


class FakeResult:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int]]:
        return self._rows


class FakeSession:
    async def execute(
        self, statement: object, parameters: dict[str, int] | None = None
    ) -> FakeResult:
        statement_text = str(statement)
        if "pg_advisory_xact_lock" in statement_text:
            return FakeResult([])
        if "subscriptions" in statement_text:
            return FakeResult([
                ("disabled", 0),
                ("full", 2),
                ("least-loaded", 1),
                ("busy", 5),
                ("unlimited", 7),
            ])
        if "payments" in statement_text:
            return FakeResult([
                ("disabled", 1),
                ("full", 1),
                ("least-loaded", 1),
                ("busy", 0),
                ("unlimited", 3),
            ])
        raise AssertionError(f"Unexpected statement: {statement_text}")


class CapturingSession:
    def __init__(self) -> None:
        self.statement: object | None = None
        self.parameters: dict[str, int] | None = None

    async def execute(
        self, statement: object, parameters: dict[str, int] | None = None
    ) -> FakeResult:
        self.statement = statement
        self.parameters = parameters
        return FakeResult([])


def test_acquire_capacity_selection_lock_uses_advisory_xact_lock_keys() -> None:
    session = CapturingSession()

    asyncio.run(acquire_capacity_selection_lock(session))

    assert str(session.statement) == "SELECT pg_advisory_xact_lock(:k1, :k2)"
    assert session.parameters == {"k1": 947201, "k2": 1}


@dataclass
class FakeSubscription:
    node_key: str


def test_select_node_for_new_subscription_uses_enabled_least_loaded_node_with_capacity() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    node = asyncio.run(service.select_node_for_new_subscription())

    assert node.key == "least-loaded"


def test_capacity_snapshot_reports_active_pending_occupied_and_free_slots() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    snapshot = asyncio.run(service.get_capacity_snapshot())
    capacity_by_key = {capacity.key: capacity for capacity in snapshot}

    least_loaded = capacity_by_key["least-loaded"]
    assert least_loaded.active_count == 1
    assert least_loaded.pending_reservations == 1
    assert least_loaded.occupied_count == 2
    assert least_loaded.max_active_subscriptions == 10
    assert least_loaded.free_slots == 8
    assert least_loaded.has_capacity is True


def test_capacity_snapshot_marks_disabled_node_without_capacity() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    snapshot = asyncio.run(service.get_capacity_snapshot())
    disabled = {capacity.key: capacity for capacity in snapshot}["disabled"]

    assert disabled.enabled is False
    assert disabled.active_count == 0
    assert disabled.pending_reservations == 1
    assert disabled.occupied_count == 1
    assert disabled.free_slots == 9
    assert disabled.has_capacity is False


def test_capacity_snapshot_clamps_over_capacity_free_slots_to_zero() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    snapshot = asyncio.run(service.get_capacity_snapshot())
    full = {capacity.key: capacity for capacity in snapshot}["full"]

    assert full.active_count == 2
    assert full.pending_reservations == 1
    assert full.occupied_count == 3
    assert full.max_active_subscriptions == 2
    assert full.free_slots == 0
    assert full.has_capacity is False


def test_capacity_snapshot_reports_unlimited_free_slots_as_none() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    snapshot = asyncio.run(service.get_capacity_snapshot())
    unlimited = {capacity.key: capacity for capacity in snapshot}["unlimited"]

    assert unlimited.active_count == 7
    assert unlimited.pending_reservations == 3
    assert unlimited.occupied_count == 10
    assert unlimited.max_active_subscriptions is None
    assert unlimited.free_slots is None
    assert unlimited.has_capacity is True


def test_get_node_for_subscription_uses_subscription_node_key() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes())

    node = service.get_node_for_subscription(FakeSubscription(node_key="busy"))

    assert node.key == "busy"


def test_get_node_for_subscription_does_not_fallback_for_missing_node() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes())

    with pytest.raises(ValueError, match="XUI node 'missing' is not configured"):
        service.get_node_for_subscription(FakeSubscription(node_key="missing"))
