import pytest
from pydantic import SecretStr, ValidationError

from app.config import PlategaPaymentMethodConfig, Settings, XuiNodeConfig


def _settings_kwargs() -> dict[str, object]:
    return {
        "bot_token": SecretStr("bot-token"),
        "postgres_password": SecretStr("postgres-password"),
    }


def test_xui_nodes_json_configures_nodes_and_hides_secrets() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_nodes_json="""
        [
            {
                "key": "eu-1",
                "xui_base_url": "https://panel.example.test",
                "xui_public_host": "public.example.test",
                "xui_username": "xui-user",
                "xui_password": "xui-password",
                "xui_inbound_ids": [1, 2]
            }
        ]
        """,
    )

    node = settings.get_xui_node("eu-1")

    assert settings.xui_nodes == [node]
    assert node.xui_base_url == "https://panel.example.test"
    assert node.xui_public_host == "public.example.test"
    assert node.xui_username == "xui-user"
    assert node.xui_password.get_secret_value() == "xui-password"
    assert node.xui_inbound_ids == [1, 2]
    assert "xui-password" not in repr(node)
    assert "xui-password" not in repr(settings)


def test_xui_nodes_json_accepts_canonical_node_keys() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_nodes_json="""
        [
            {
                "key": "eu-1",
                "name": "Europe 1",
                "enabled": true,
                "base_url": "https://panel.example.test",
                "public_host": "public.example.test",
                "sub_base_url": "https://sub.example.test",
                "api_token": "node-token",
                "auth_mode": "api_token",
                "username": "xui-user",
                "password": "xui-password",
                "inbound_ids": [1, 2],
                "default_traffic_gb": 128,
                "default_limit_ip": 3,
                "max_active_subscriptions": 50,
                "weight": 2
            }
        ]
        """,
    )

    node = settings.get_xui_node("eu-1")

    assert node.name == "Europe 1"
    assert node.enabled is True
    assert node.xui_base_url == "https://panel.example.test"
    assert node.xui_public_host == "public.example.test"
    assert node.xui_sub_base_url == "https://sub.example.test"
    assert node.xui_api_token is not None
    assert node.xui_api_token.get_secret_value() == "node-token"
    assert node.xui_auth_mode == "api_token"
    assert node.xui_username == "xui-user"
    assert node.xui_password.get_secret_value() == "xui-password"
    assert node.xui_inbound_ids == [1, 2]
    assert node.default_traffic_gb == 128
    assert node.default_limit_ip == 3
    assert node.max_active_subscriptions == 50
    assert node.weight == 2


def test_xui_nodes_falls_back_to_legacy_default_node() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_base_url="https://legacy-panel.example.test",
        xui_public_host="legacy-public.example.test",
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids="3,4",
        xui_default_max_active_subscriptions=75,
    )

    node = settings.get_xui_node("default")

    assert isinstance(node, XuiNodeConfig)
    assert node.key == "default"
    assert node.name == "Default"
    assert node.xui_base_url == "https://legacy-panel.example.test"
    assert node.xui_public_host == "legacy-public.example.test"
    assert node.xui_auth_mode == "session_cookie"
    assert node.xui_username == "legacy-user"
    assert node.xui_password.get_secret_value() == "legacy-password"
    assert node.xui_inbound_ids == [3, 4]
    assert node.max_active_subscriptions == 75


def test_legacy_default_node_capacity_can_be_unlimited() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids=[1],
        xui_default_max_active_subscriptions="null",
    )

    assert settings.get_xui_node("default").max_active_subscriptions is None


def test_legacy_default_node_capacity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_settings_kwargs(),
            xui_username="legacy-user",
            xui_password=SecretStr("legacy-password"),
            xui_inbound_ids=[1],
            xui_default_max_active_subscriptions=0,
        )


@pytest.mark.parametrize(
    "xui_nodes_json",
    [
        '[{"key":"","xui_username":"user","xui_password":"password","xui_inbound_ids":[1]}]',
        '[{"key":"eu","xui_username":"user","xui_password":"password","xui_inbound_ids":[]}]',
        '[{"key":"eu","xui_username":"user","xui_password":"password","xui_inbound_ids":[1]},'
        '{"key":"eu","xui_username":"user","xui_password":"password","xui_inbound_ids":[2]}]',
    ],
)
def test_xui_nodes_json_validation(xui_nodes_json: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(), xui_nodes_json=xui_nodes_json)


def test_legacy_xui_inbound_ids_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_settings_kwargs(),
            xui_username="legacy-user",
            xui_password=SecretStr("legacy-password"),
            xui_inbound_ids=[],
        )


def test_get_xui_node_validates_key_and_missing_nodes() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids=[1],
    )

    with pytest.raises(ValueError):
        settings.get_xui_node(" ")
    with pytest.raises(KeyError):
        settings.get_xui_node("missing")


def test_platega_webhook_retry_settings_and_legacy_alias() -> None:
    settings = Settings(
        **_settings_kwargs(),
        platega_webhook_max_retries=7,
        platega_webhook_retry_base_seconds=45,
        platega_webhook_retry_max_seconds=600,
    )

    assert settings.platega_webhook_max_attempts == 7
    assert settings.platega_webhook_retry_base_seconds == 45
    assert settings.platega_webhook_retry_max_seconds == 600


def test_platega_payment_methods_json_parses_configured_methods() -> None:
    settings = Settings(
        **_settings_kwargs(),
        platega_payment_methods_json="""
        {
            "sbp": {
                "title": "СБП",
                "payment_method": "SBP_RUB"
            },
            "crypto": {
                "title": "Crypto",
                "payment_method": "USDT_TRC20"
            }
        }
        """,
    )

    sbp = settings.get_platega_payment_method("sbp")
    crypto = settings.get_platega_payment_method("crypto")

    assert isinstance(sbp, PlategaPaymentMethodConfig)
    assert sbp.title == "СБП"
    assert sbp.payment_method == "SBP_RUB"
    assert crypto.title == "Crypto"
    assert crypto.payment_method == "USDT_TRC20"


def test_platega_payment_methods_json_empty_preserves_single_method_flow() -> None:
    settings = Settings(
        **_settings_kwargs(),
        platega_payment_method="CARD_RUB",
        platega_payment_methods_json="",
    )

    assert settings.platega_payment_methods_json == {}
    assert settings.platega_payment_method == "CARD_RUB"


def test_empty_platega_payment_method_is_omitted_in_single_method_flow() -> None:
    settings = Settings(
        **_settings_kwargs(),
        platega_payment_method=" ",
        platega_payment_methods_json="",
    )

    assert settings.platega_payment_methods_json == {}
    assert settings.platega_payment_method is None


@pytest.mark.parametrize(
    "platega_payment_methods_json",
    [
        "[]",
        '{"": {"title": "СБП", "payment_method": "SBP_RUB"}}',
        '{"sbp": "SBP_RUB"}',
        '{"sbp": {"title": "СБП"}}',
        '{"sbp": {"title": "СБП", "payment_method": ""}}',
    ],
)
def test_platega_payment_methods_json_validation(
    platega_payment_methods_json: str,
) -> None:
    with pytest.raises((TypeError, ValidationError)):
        Settings(
            **_settings_kwargs(),
            platega_payment_methods_json=platega_payment_methods_json,
        )


def test_get_platega_payment_method_rejects_missing_method_code() -> None:
    settings = Settings(
        **_settings_kwargs(),
        platega_payment_methods_json="""
        {
            "sbp": {
                "title": "СБП",
                "payment_method": "SBP_RUB"
            }
        }
        """,
    )

    with pytest.raises(ValueError):
        settings.get_platega_payment_method(" ")
    with pytest.raises(KeyError) as error:
        settings.get_platega_payment_method("crypto")

    assert "crypto" in str(error.value)


def test_xui_auth_mode_api_token_requires_token() -> None:
    with pytest.raises(
        ValidationError,
        match="XUI_AUTH_MODE=api_token requires XUI_API_TOKEN",
    ):
        Settings(
            **_settings_kwargs(),
            xui_auth_mode="api_token",
            xui_api_token=SecretStr(""),
            xui_username="legacy-user",
            xui_password=SecretStr("legacy-password"),
            xui_inbound_ids=[1],
        )


def test_xui_auth_mode_session_cookie_with_token_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    Settings(
        **_settings_kwargs(),
        xui_auth_mode="session_cookie",
        xui_api_token=SecretStr("secret-token"),
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids=[1],
    )

    assert "XUI_API_TOKEN is configured but XUI_AUTH_MODE=session_cookie" in caplog.text
    assert "secret-token" not in caplog.text


def test_xui_nodes_inherit_global_auth_mode_when_node_omits_auth_mode() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_auth_mode="api_token",
        xui_api_token=SecretStr("global-token"),
        xui_nodes_json="""
        [
            {
                "key": "eu-1",
                "base_url": "https://panel.example.test",
                "api_token": "node-token",
                "username": "xui-user",
                "password": "xui-password",
                "inbound_ids": [1]
            }
        ]
        """,
    )

    assert settings.get_xui_node("eu-1").xui_auth_mode == "api_token"
