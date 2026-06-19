import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic import SecretStr

from app.config import Settings
from app import main as main_module


class FakeMessage:
    def __init__(self, admin_id: int) -> None:
        self.from_user = SimpleNamespace(id=admin_id)
        self.bot = object()
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _settings(**kwargs) -> Settings:
    values = {
        "bot_token": SecretStr("bot-token"),
        "postgres_password": SecretStr("postgres-password"),
        "xui_username": "xui-user",
        "xui_password": SecretStr("xui-password"),
        "xui_inbound_ids": [1],
        "admin_ids": [123],
    }
    values.update(kwargs)
    return Settings(**values)


@pytest.mark.asyncio
async def test_check_payment_without_local_payment_skips_platega_when_credentials_missing(monkeypatch) -> None:
    class FakePaymentRepository:
        def __init__(self, session) -> None:
            pass

        async def get_by_provider_payment_id_any_provider(self, provider_payment_id: str):
            return None

    def fail_client(*args, **kwargs):
        raise AssertionError("PlategaClient should not be constructed without credentials")

    monkeypatch.setattr(main_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(main_module, "PaymentRepository", FakePaymentRepository)
    monkeypatch.setattr(main_module, "PlategaClient", fail_client)

    message = FakeMessage(admin_id=123)

    await main_module.check_payment_command(
        message,
        SimpleNamespace(args="tx-1"),
        _settings(),
    )

    assert message.answers == [
        "Локальный платеж не найден.\n"
        "Platega lookup не настроен: отсутствуют обязательные учетные данные."
    ]


@pytest.mark.asyncio
async def test_check_payment_without_local_payment_handles_platega_client_value_error(monkeypatch) -> None:
    class FakePaymentRepository:
        def __init__(self, session) -> None:
            pass

        async def get_by_provider_payment_id_any_provider(self, provider_payment_id: str):
            return None

    def fail_client(*args, **kwargs):
        raise ValueError("secret-value or raw setting should stay hidden")

    monkeypatch.setattr(main_module, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(main_module, "PaymentRepository", FakePaymentRepository)
    monkeypatch.setattr(main_module, "PlategaClient", fail_client)

    message = FakeMessage(admin_id=123)

    await main_module.check_payment_command(
        message,
        SimpleNamespace(args="tx-1"),
        _settings(
            platega_merchant_id="merchant-id",
            platega_api_key=SecretStr("secret-value"),
        ),
    )

    assert message.answers == [
        "Локальный платеж не найден.\n"
        "Platega lookup не настроен: отсутствуют обязательные учетные данные."
    ]
    assert "secret-value" not in message.answers[0]
