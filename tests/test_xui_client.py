from pydantic import SecretStr

from app.config import Settings, XuiNodeConfig
from app.services.xui_client import XuiClient


def _settings() -> Settings:
    return Settings(
        bot_token=SecretStr("bot-token"),
        postgres_password=SecretStr("postgres-password"),
        xui_base_url="https://legacy-panel.example.test/path/",
        xui_api_token=SecretStr("legacy-token"),
        xui_auth_mode="api_token",
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
        xui_auth_mode="api_token",
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
        "username": "node-user",
        "password": "***",
        "cookie": "***",
        "nested": {"token": "***", "message": "ok"},
    }

    text = XuiClient._redact_sensitive(
        'username=node-user password=node-password token=secret-token '
        'Authorization: Bearer secret-bearer cookie=session-secret'
    )

    assert "username=node-user" in text
    assert "node-password" not in text
    assert "secret-token" not in text
    assert "secret-bearer" not in text
    assert "session-secret" not in text


def test_xui_client_uses_session_cookie_by_default_even_with_token() -> None:
    settings = _settings()
    settings.xui_auth_mode = "session_cookie"

    client = XuiClient(settings)

    assert client._api_token == ""
    assert "Authorization" not in client._client.headers


def test_xui_client_rejects_api_token_mode_when_api_token_empty() -> None:
    settings = _settings()
    settings.xui_api_token = SecretStr("")
    settings.xui_auth_mode = "api_token"

    import pytest
    with pytest.raises(
        ValueError,
        match="XUI_AUTH_MODE=api_token requires XUI_API_TOKEN",
    ):
        XuiClient(settings)

import asyncio
import httpx
import pytest


def _install_mock_transport(client: XuiClient, handler):
    old_client = client._client
    client._client = httpx.AsyncClient(
        base_url=client._base_url,
        follow_redirects=True,
        headers=old_client.headers,
        transport=httpx.MockTransport(handler),
    )


def test_xui_client_api_token_mode_uses_bearer_and_skips_login() -> None:
    settings = _settings()
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        assert request.url.path != "/path/login"
        return httpx.Response(200, json={"success": True, "obj": []})

    client = XuiClient(settings)
    _install_mock_transport(client, handler)
    asyncio.run(client.list_inbounds())
    asyncio.run(client.close())

    assert seen == [("GET", "/path/panel/api/inbounds/list", "Bearer legacy-token")]


def test_xui_client_session_cookie_mode_logs_in_without_authorization() -> None:
    settings = _settings()
    settings.xui_auth_mode = "session_cookie"
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        return httpx.Response(200, json={"success": True, "obj": []})

    client = XuiClient(settings)
    _install_mock_transport(client, handler)
    asyncio.run(client.list_inbounds())
    asyncio.run(client.close())

    assert seen == [
        ("POST", "/path/login", None),
        ("GET", "/path/panel/api/inbounds/list", None),
    ]


def test_xui_client_auto_mode_falls_back_from_token_to_session_cookie() -> None:
    settings = _settings()
    settings.xui_auth_mode = "auto"
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        seen.append((request.method, request.url.path, auth))
        if request.url.path == "/path/panel/api/inbounds/list" and auth:
            return httpx.Response(403, json={"success": False})
        return httpx.Response(200, json={"success": True, "obj": []})

    client = XuiClient(settings)
    _install_mock_transport(client, handler)
    asyncio.run(client.list_inbounds())
    asyncio.run(client.close())

    assert seen == [
        ("GET", "/path/panel/api/inbounds/list", "Bearer legacy-token"),
        ("POST", "/path/login", None),
        ("GET", "/path/panel/api/inbounds/list", None),
    ]


def test_xui_client_rejects_panel_api_base_url() -> None:
    settings = _settings()
    settings.xui_base_url = "https://legacy-panel.example.test/path/panel/api"

    with pytest.raises(ValueError, match="XUI_BASE_URL must point to panel web root"):
        XuiClient(settings)
