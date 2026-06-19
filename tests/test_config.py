import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings, XuiNodeConfig


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


def test_xui_nodes_falls_back_to_legacy_default_node() -> None:
    settings = Settings(
        **_settings_kwargs(),
        xui_base_url="https://legacy-panel.example.test",
        xui_public_host="legacy-public.example.test",
        xui_username="legacy-user",
        xui_password=SecretStr("legacy-password"),
        xui_inbound_ids="3,4",
    )

    node = settings.get_xui_node("default")

    assert isinstance(node, XuiNodeConfig)
    assert node.key == "default"
    assert node.xui_base_url == "https://legacy-panel.example.test"
    assert node.xui_public_host == "legacy-public.example.test"
    assert node.xui_username == "legacy-user"
    assert node.xui_password.get_secret_value() == "legacy-password"
    assert node.xui_inbound_ids == [3, 4]


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
