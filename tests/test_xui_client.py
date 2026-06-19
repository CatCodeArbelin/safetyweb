from pydantic import SecretStr

from app.config import Settings, XuiNodeConfig
from app.services.xui_client import XuiClient


def _settings() -> Settings:
    return Settings(
        bot_token=SecretStr("bot-token"),
        postgres_password=SecretStr("postgres-password"),
        xui_base_url="https://legacy-panel.example.test/path/",
        xui_api_token=SecretStr("legacy-token"),
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids=[1],
    )


def test_xui_client_uses_node_connection_settings_when_node_is_provided() -> None:
    settings = _settings()
    node = XuiNodeConfig(
        key="node-a",
        xui_base_url="https://node-panel.example.test/base/",
        xui_api_token=SecretStr("node-token"),
        xui_username="node-user",
        xui_password=SecretStr("node-password"),
        xui_inbound_ids=[2, 3],
    )

    client = XuiClient(settings, node=node)

    assert str(client._client.base_url) == "https://node-panel.example.test/base/"
    assert client._username == "node-user"
    assert client._password.get_secret_value() == "node-password"
    assert client._api_token == "node-token"
    assert client._inbound_ids() == [2, 3]
    assert client._client.headers["Authorization"] == "Bearer node-token"


def test_xui_client_keeps_legacy_settings_without_node() -> None:
    client = XuiClient(_settings())

    assert str(client._client.base_url) == "https://legacy-panel.example.test/path/"
    assert client._username == "legacy-user"
    assert client._password.get_secret_value() == "legacy-password"
    assert client._api_token == "legacy-token"
    assert client._inbound_ids() == [1]
    assert client._client.headers["Authorization"] == "Bearer legacy-token"


def test_xui_client_redacts_sensitive_diagnostics() -> None:
    redacted = XuiClient._redact_sensitive(
        {
            "username": "node-user",
            "password": "node-password",
            "cookie": "session=secret-cookie",
            "nested": {"token": "secret-token", "message": "ok"},
        }
    )

    assert redacted == {
        "username": "***",
        "password": "***",
        "cookie": "***",
        "nested": {"token": "***", "message": "ok"},
    }

    text = XuiClient._redact_sensitive(
        'username=node-user password=node-password token=secret-token '
        'Authorization: Bearer secret-bearer cookie=session-secret'
    )

    assert "node-user" not in text
    assert "node-password" not in text
    assert "secret-token" not in text
    assert "secret-bearer" not in text
    assert "session-secret" not in text
