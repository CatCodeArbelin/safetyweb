"""HTTP client for the X-UI panel."""

from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class XuiClient:
    """Asynchronous X-UI API client with cookie-based authentication."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.xui_base_url.rstrip("/"),
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def login(self) -> dict[str, Any]:
        """Authenticate in X-UI and store session cookies in the HTTP client."""
        response = await self._client.post(
            "/login",
            data={
                "username": self.settings.xui_username,
                "password": self.settings.xui_password.get_secret_value(),
            },
        )
        response.raise_for_status()
        return self._json(response)

    async def get_inbounds(self) -> dict[str, Any]:
        """Return all configured inbounds."""
        return await self._request("GET", "/panel/api/inbounds/list")

    async def get_inbound(self, inbound_id: int | None = None) -> dict[str, Any]:
        """Return a single inbound loaded from environment settings."""
        return await self._request(
            "GET",
            f"/panel/api/inbounds/get/{self._inbound_id(inbound_id)}",
        )

    async def add_client(
        self,
        inbound_id: int | None,
        client_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Add a client to the configured inbound."""
        payload = {"id": self._inbound_id(inbound_id), "settings": client_data}
        return await self._request("POST", "/panel/api/inbounds/addClient", json=payload)

    async def update_client(
        self,
        inbound_id: int | None,
        client_id: str,
        client_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a client in the configured inbound."""
        payload = {"id": self._inbound_id(inbound_id), "settings": client_data}
        return await self._request(
            "POST",
            f"/panel/api/inbounds/updateClient/{self._path_param(client_id)}",
            json=payload,
        )

    async def delete_client(self, inbound_id: int | None, client_id: str) -> dict[str, Any]:
        """Delete a client from the configured inbound."""
        return await self._request(
            "POST",
            f"/panel/api/inbounds/delClient/"
            f"{self._inbound_id(inbound_id)}/{self._path_param(client_id)}",
        )

    async def reset_client_traffic(
        self,
        inbound_id: int | None,
        client_email: str,
    ) -> dict[str, Any]:
        """Reset traffic statistics for a client in the configured inbound."""
        return await self._request(
            "POST",
            "/panel/api/inbounds/resetClientTraffic/"
            f"{self._inbound_id(inbound_id)}/{self._path_param(client_email)}",
        )

    async def get_client_traffic(self, client_email: str) -> dict[str, Any]:
        """Return traffic statistics for a client by email."""
        return await self._request(
            "GET",
            f"/panel/api/inbounds/getClientTraffics/{self._path_param(client_email)}",
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Perform an authenticated request, retrying once after auth failures."""
        response = await self._client.request(method, url, **kwargs)
        if response.status_code in {401, 403}:
            await self.login()
            response = await self._client.request(method, url, **kwargs)

        response.raise_for_status()
        return self._json(response)

    def _inbound_id(self, inbound_id: int | None = None) -> int:
        """Return the environment-configured inbound id, ignoring external values."""
        return self.settings.xui_inbound_id

    @staticmethod
    def _path_param(value: str) -> str:
        """Encode a value for safe use in an URL path segment."""
        return quote(value, safe="")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        """Decode an X-UI JSON response."""
        data = response.json()
        if isinstance(data, dict):
            return data

        msg = "X-UI response must be a JSON object"
        raise ValueError(msg)
