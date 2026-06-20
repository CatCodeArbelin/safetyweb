from pydantic import SecretStr

from app.config import XuiNodeConfig
from app.services.xui_health_service import safe_base_url, xui_auth_hint


def test_xui_auth_hint_for_token_configured_but_session_cookie_mode_is_safe() -> None:
    node = XuiNodeConfig(
        key="default",
        xui_base_url="https://example.test/private-path",
        xui_api_token=SecretStr("secret-token"),
        xui_auth_mode="session_cookie",
        xui_username="admin",
        xui_password=SecretStr("secret-password"),
        xui_inbound_ids=[10, 12, 5],
    )

    hint = xui_auth_hint(node)

    assert hint is not None
    assert "XUI_API_TOKEN" in hint
    assert "XUI_AUTH_MODE=api_token" in hint
    assert "secret-token" not in hint
    assert "secret-password" not in hint
    assert "admin" not in hint


def test_safe_base_url_hides_private_web_path() -> None:
    assert safe_base_url("https://example.test:31293/private-webpath") == "https://example.test:31293/…"
