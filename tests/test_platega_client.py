import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.services.platega_client import PlategaClient


def _settings() -> Settings:
    return Settings(
        bot_token=SecretStr("bot-token"),
        postgres_password=SecretStr("postgres-password"),
        xui_username="xui-user",
        xui_password=SecretStr("xui-password"),
        xui_inbound_ids=[1],
        platega_base_url="https://platega.test",
        platega_merchant_id="merchant-id",
        platega_api_key=SecretStr("api-key"),
        platega_return_url="https://example.test/return",
        platega_failed_url="https://example.test/failed",
    )


def _create_transaction(**kwargs: Any) -> tuple[str, dict[str, Any]]:
    captured: dict[str, Any] = {}

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read().decode()
            return httpx.Response(
                200,
                json={"transactionId": "tx-1", "redirectUrl": "https://pay.test"},
            )

        client = PlategaClient(settings=_settings())
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="https://platega.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.create_transaction(
                amount=Decimal("10.50"),
                currency="RUB",
                description="Оплата подписки",
                payload={"internalPaymentId": 42},
                user_id=12345,
                **kwargs,
            )
        finally:
            await client.close()

    asyncio.run(run())
    return captured["path"], json.loads(captured["body"])


def test_create_transaction_with_payment_method_uses_v1_endpoint_and_adds_payment_method() -> None:
    path, body = _create_transaction(payment_method="CARD", user_name="Alice")

    assert path == "/transaction/process"
    assert body["paymentMethod"] == "CARD"
    assert body["metadata"] == {"userId": "12345", "userName": "Alice"}
    assert body["paymentDetails"] == {"amount": "10.50", "currency": "RUB"}
    assert body["description"] == "Оплата подписки"
    assert body["return"] == "https://example.test/return"
    assert body["failedUrl"] == "https://example.test/failed"
    assert body["payload"] == {"internalPaymentId": 42}


def test_create_transaction_without_payment_method_uses_v2_endpoint_and_user_id_as_name() -> None:
    path, body = _create_transaction()

    assert path == "/v2/transaction/process"
    assert "paymentMethod" not in body
    assert body["metadata"] == {"userId": "12345", "userName": "12345"}
    assert body["paymentDetails"] == {"amount": "10.50", "currency": "RUB"}
    assert body["description"] == "Оплата подписки"
    assert body["return"] == "https://example.test/return"
    assert body["failedUrl"] == "https://example.test/failed"
    assert body["payload"] == {"internalPaymentId": 42}
