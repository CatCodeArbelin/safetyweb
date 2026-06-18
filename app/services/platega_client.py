"""HTTP client for the Platega payment API."""

from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings


class PlategaError(RuntimeError):
    """Raised when Platega returns an unsuccessful response."""


class PlategaClient:
    """Asynchronous Platega API client."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        if not self.settings.platega_merchant_id:
            msg = "PLATEGA_MERCHANT_ID is required to use PlategaClient"
            raise ValueError(msg)
        if self.settings.platega_api_key is None:
            msg = "PLATEGA_API_KEY is required to use PlategaClient"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self.settings.platega_base_url.rstrip("/"),
            headers={
                "X-MerchantId": self.settings.platega_merchant_id,
                "X-Secret": self.settings.platega_api_key.get_secret_value(),
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def create_transaction(
        self,
        *,
        amount: Decimal | int | float | str,
        currency: str,
        description: str,
        return_url: str | None = None,
        failed_url: str | None = None,
        payload: dict[str, Any] | None = None,
        user_id: int | str,
        user_name: str | None = None,
        payment_method: str | None = None,
    ) -> dict[str, Any]:
        """Create a Platega transaction and return the API response."""
        body: dict[str, Any] = {
            "paymentDetails": {
                "amount": self._jsonable_amount(amount),
                "currency": currency,
            },
            "description": description,
            "return": return_url or self.settings.platega_return_url,
            "failedUrl": failed_url or self.settings.platega_failed_url,
            "payload": payload or {},
            "metadata": {
                "userId": user_id,
                "userName": user_name or "",
            },
        }

        if payment_method:
            body["paymentMethod"] = payment_method
            endpoint = "/transaction/process"
        else:
            endpoint = "/v2/transaction/process"

        response = await self._client.post(endpoint, json=body)
        self._raise_for_status(response)
        return self._json(response)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        """Return a Platega transaction by identifier."""
        response = await self._client.get(f"/transaction/{transaction_id}")
        self._raise_for_status(response)
        return self._json(response)

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise PlategaError with safe diagnostics for failed responses."""
        if response.status_code < 400:
            return

        raise PlategaError(
            "Platega request failed "
            f"(status={response.status_code}, response={self._response_text(response)})",
        )

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        """Decode a Platega JSON object response."""
        try:
            data = response.json()
        except ValueError as error:
            msg = (
                "Platega response must be valid JSON "
                f"(status={response.status_code}, response={self._response_text(response)})"
            )
            raise PlategaError(msg) from error

        if not isinstance(data, dict):
            msg = (
                "Platega response must be a JSON object "
                f"(status={response.status_code}, response={self._response_text(response)})"
            )
            raise PlategaError(msg)

        return data

    @staticmethod
    def _jsonable_amount(amount: Decimal | int | float | str) -> int | float | str:
        """Return an amount value that httpx can JSON-encode."""
        if isinstance(amount, Decimal):
            if amount == amount.to_integral_value():
                return int(amount)
            return str(amount)
        return amount

    def _response_text(self, response: httpx.Response) -> str:
        """Return sanitized response text for diagnostics without secrets."""
        try:
            text = response.text
        except UnicodeDecodeError:
            text = repr(response.content)

        secret_values = []
        if self.settings.platega_api_key is not None:
            secret_values.append(self.settings.platega_api_key.get_secret_value())
        if self.settings.platega_callback_secret is not None:
            secret_values.append(
                self.settings.platega_callback_secret.get_secret_value(),
            )

        for secret_value in secret_values:
            if secret_value:
                text = text.replace(secret_value, "***")
        return text[:2000]


def build_platega_payload(
    payment_id: int | str,
    telegram_id: int | str,
    months: int,
) -> dict[str, int | str]:
    """Build an internal payload persisted in Platega transaction metadata."""
    return {
        "v": 1,
        "internalPaymentId": payment_id,
        "telegramId": telegram_id,
        "months": months,
    }
