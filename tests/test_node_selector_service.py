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
from app.services.node_selector_service import NodeSelectorService


def _settings_with_nodes() -> Settings:
    return Settings(
        bot_token=SecretStr("bot-token"),
        postgres_password=SecretStr("postgres-password"),
        xui_nodes_json="""
        [
            {
                "key": "disabled",
                "enabled": false,
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
    async def execute(self, _statement: object) -> FakeResult:
        return FakeResult([
            ("disabled", 0),
            ("full", 2),
            ("least-loaded", 1),
            ("busy", 5),
        ])


@dataclass
class FakeSubscription:
    node_key: str


def test_select_node_for_new_subscription_uses_enabled_least_loaded_node_with_capacity() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes(), session=FakeSession())

    node = asyncio.run(service.select_node_for_new_subscription())

    assert node.key == "least-loaded"


def test_get_node_for_subscription_uses_subscription_node_key() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes())

    node = service.get_node_for_subscription(FakeSubscription(node_key="busy"))

    assert node.key == "busy"


def test_get_node_for_subscription_does_not_fallback_for_missing_node() -> None:
    service = NodeSelectorService(settings=_settings_with_nodes())

    with pytest.raises(ValueError, match="XUI node 'missing' is not configured"):
        service.get_node_for_subscription(FakeSubscription(node_key="missing"))
