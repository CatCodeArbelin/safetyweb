from app.utils.sanitize import MASK, sanitize_dict, sanitize_mapping, sanitize_string


def test_sanitize_mapping_masks_explicit_sensitive_keys() -> None:
    data = {
        "connection_string": "postgres://user:pass@example/db",
        "postgres_password": "postgres-secret",
        "xui_password": "xui-secret",
        "platega_api_key": "platega-secret",
        "bot_token": "123456:abcdefghijklmnopqrstuvwxyz",
        "api_token": "api-secret",
        "safe": "visible",
    }

    sanitized = sanitize_mapping(data)

    assert sanitized == {
        "connection_string": MASK,
        "postgres_password": MASK,
        "xui_password": MASK,
        "platega_api_key": MASK,
        "bot_token": MASK,
        "api_token": MASK,
        "safe": "visible",
    }


def test_sanitize_mapping_preserves_non_secret_usernames() -> None:
    data = {
        "username": "alice",
        "xui_username": "panel-admin",
        "telegram_username": "public-user",
        "nested": {"owner_username": "owner"},
    }

    assert sanitize_mapping(data) == data


def test_sanitize_mapping_masks_passwords_tokens_and_cookies() -> None:
    sanitized = sanitize_mapping(
        {
            "password": "password-secret",
            "xui_password": "xui-secret",
            "token": "token-secret",
            "api_token": "api-secret",
            "authorization": "Bearer bearer-secret",
            "cookie": "session=cookie-secret",
            "set-cookie": "session=set-cookie-secret",
            "nested": {
                "access_token": "nested-token",
                "refresh_cookie": "nested-cookie",
            },
        }
    )

    assert sanitized == {
        "password": MASK,
        "xui_password": MASK,
        "token": MASK,
        "api_token": MASK,
        "authorization": MASK,
        "cookie": MASK,
        "set-cookie": MASK,
        "nested": {
            "access_token": MASK,
            "refresh_cookie": MASK,
        },
    }


def test_sanitize_string_masks_secret_assignments_but_preserves_username_assignments() -> None:
    text = sanitize_string(
        "username=alice XUI_USERNAME=panel-admin password=password-secret "
        "token=token-secret Authorization: Bearer bearer-secret "
        "cookie=session-secret set-cookie=set-cookie-secret"
    )

    assert "username=alice" in text
    assert "XUI_USERNAME=panel-admin" in text
    assert "password-secret" not in text
    assert "token-secret" not in text
    assert "bearer-secret" not in text
    assert "session-secret" not in text
    assert "set-cookie-secret" not in text


def test_sanitize_dict_remains_backward_compatible_alias() -> None:
    assert sanitize_dict({"api_token": "secret"}) == {"api_token": MASK}
