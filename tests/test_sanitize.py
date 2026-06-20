from app.utils.sanitize import MASK, sanitize_dict, sanitize_mapping


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


def test_sanitize_dict_remains_backward_compatible_alias() -> None:
    assert sanitize_dict({"api_token": "secret"}) == {"api_token": MASK}
