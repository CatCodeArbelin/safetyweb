"""HTTP client for the X-UI panel."""

import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import Settings, XuiNodeConfig


logger = logging.getLogger(__name__)


class XuiError(Exception):
    """Base exception for X-UI client errors."""


class XuiAuthError(XuiError):
    """Raised when X-UI authentication fails."""


class XuiRequestError(XuiError):
    """Raised when an X-UI HTTP request fails."""


class XuiApiError(XuiError):
    """Raised when X-UI returns an unsuccessful API response."""


class XuiClientCreate(BaseModel):
    """Client payload accepted by the X-UI clients API."""

    model_config = ConfigDict(extra="allow")

    tgId: int | str


class XuiAddClientRequest(BaseModel):
    """Request payload for adding a client to one or more inbounds."""

    client: XuiClientCreate
    inboundIds: list[int]


class XuiClient:
    """Asynchronous X-UI API client with cookie-based authentication."""

    def __init__(self, settings: Settings, node: XuiNodeConfig | None = None) -> None:
        self.settings = settings
        self._base_url = (
            node.xui_base_url if node is not None else settings.xui_base_url
        ).rstrip("/")
        self._api_token_value = self._secret_value(
            node.xui_api_token if node is not None else settings.xui_api_token
        )
        self._username = node.xui_username if node is not None else settings.xui_username
        self._password = node.xui_password if node is not None else settings.xui_password
        self._inbound_ids_value = (
            node.xui_inbound_ids if node is not None else settings.xui_inbound_ids
        )
        if self._username is None or self._password is None:
            msg = "X-UI credentials are not configured"
            raise ValueError(msg)

        api_token = self._api_token
        headers = (
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if api_token
            else None
        )
        logger.debug("X-UI client initialized")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            follow_redirects=True,
            headers=headers,
        )
        self._authenticated = False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def login(self) -> dict[str, Any]:
        """Authenticate in X-UI and store session cookies in the HTTP client."""
        response = await self._client.post(
            "login",
            data={
                "username": self._username,
                "password": self._password.get_secret_value(),
            },
        )
        self._raise_for_status(response)
        data = self._json(response)
        self._authenticated = True
        return data

    async def get_openapi(self) -> dict[str, Any]:
        """Return the panel OpenAPI schema for healthcheck/debug diagnostics."""
        return await self._request("GET", "/panel/api/openapi.json")

    async def list_inbounds(self) -> dict[str, Any]:
        """Return all configured inbounds."""
        return await self._request("GET", "/panel/api/inbounds/list")

    async def get_inbounds(self) -> dict[str, Any]:
        """Alias for list_inbounds kept for backward compatibility."""
        return await self.list_inbounds()

    async def get_inbound(self, inbound_id: int | None = None) -> dict[str, Any]:
        """Return a single inbound loaded from environment settings."""
        return await self._request(
            "GET",
            f"/panel/api/inbounds/get/{self._inbound_id(inbound_id)}",
        )

    async def add_client(
        self,
        client: XuiClientCreate | dict[str, Any],
        inbound_ids: list[int],
    ) -> dict[str, Any]:
        """Add a client to one or more inbounds using the clients API."""
        client_model = (
            client
            if isinstance(client, XuiClientCreate)
            else XuiClientCreate.model_validate(client)
        )
        payload = XuiAddClientRequest(
            client=client_model,
            inboundIds=inbound_ids,
        ).model_dump()
        return await self._request("POST", "/panel/api/clients/add", json=payload)

    async def add_client_legacy(
        self,
        inbound_id: int | list[int] | None,
        client_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Add a client using the legacy inbound addClient API."""
        payload = {
            "id": self._inbound_ids(inbound_id)[0],
            "settings": json.dumps({"clients": [self._single_client(client_data)]}),
        }
        return await self._request(
            "POST",
            "/panel/api/inbounds/addClient",
            json=payload,
        )

    async def update_client(
        self,
        inbound_id: int | None,
        client_id: str,
        client_data: dict[str, Any],
        *,
        enable: bool | None = None,
    ) -> dict[str, Any]:
        """Update a client in the configured inbound."""
        if enable is not None:
            client_data = self._with_client_enable(client_data, client_id, enable)

        try:
            return await self.update_client_via_clients_api(client_data)
        except XuiRequestError as error:
            if "status=404" not in str(error):
                raise
            return await self.update_client_legacy(inbound_id, client_id, client_data)

    async def update_client_via_clients_api(
        self,
        client_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a client by email using the current X-UI clients API."""
        client = self._extract_single_client(client_data)
        email = client.get("email")
        if not isinstance(email, str) or not email:
            msg = "X-UI client update requires client email"
            raise ValueError(msg)

        return await self._request(
            "POST",
            f"/panel/api/clients/update/{self._path_param(email)}",
            json=client,
        )

    async def update_client_legacy(
        self,
        inbound_id: int | None,
        client_id: str,
        client_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a client using the legacy inbound-specific updateClient API."""
        payload = {
            "id": self._inbound_id(inbound_id),
            "settings": json.dumps(client_data),
        }
        return await self._request(
            "POST",
            f"/panel/api/inbounds/updateClient/{self._path_param(client_id)}",
            json=payload,
        )

    @staticmethod
    def _with_client_enable(
        client_data: dict[str, Any],
        client_id: str,
        enable: bool,
    ) -> dict[str, Any]:
        """Return X-UI client settings with the requested enable flag applied."""
        data = dict(client_data)
        clients = data.get("clients")
        if isinstance(clients, list) and clients:
            updated_clients = []
            for client in clients:
                updated_client = dict(client) if isinstance(client, dict) else client
                if isinstance(updated_client, dict):
                    updated_client.setdefault("id", client_id)
                    updated_client["enable"] = enable
                updated_clients.append(updated_client)
            data["clients"] = updated_clients
        elif isinstance(data.get("email"), str):
            data.setdefault("id", client_id)
            data["enable"] = enable
        else:
            data["clients"] = [{"id": client_id, "enable": enable}]
        return data

    async def delete_client(self, inbound_id: int | None, client_id: str) -> dict[str, Any]:
        """Delete a client using the legacy inbound-specific delClient API."""
        return await self._request(
            "POST",
            f"/panel/api/inbounds/{self._inbound_id(inbound_id)}/delClient/"
            f"{self._path_param(client_id)}",
        )

    async def reset_client_traffic(
        self,
        inbound_id: int | None,
        client_email: str,
    ) -> dict[str, Any]:
        """Legacy helper to reset client traffic outside the main creation flow."""
        return await self._request(
            "POST",
            f"/panel/api/inbounds/{self._inbound_id(inbound_id)}/resetClientTraffic/"
            f"{self._path_param(client_email)}",
        )

    async def get_client_traffic(self, client_email: str) -> dict[str, Any]:
        """Return traffic statistics for a client by email."""
        return await self._request(
            "GET",
            f"/panel/api/inbounds/getClientTraffics/{self._path_param(client_email)}",
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Perform an authenticated request, retrying once after auth failures."""
        url = url.lstrip("/")

        if self._api_token:
            response = await self._client.request(method, url, **kwargs)
            self._raise_for_status(response)
            return self._json(response)

        if not self._authenticated:
            await self.login()

        response = await self._client.request(method, url, **kwargs)
        if response.status_code in {401, 403}:
            self._authenticated = False
            await self.login()
            response = await self._client.request(method, url, **kwargs)

        self._raise_for_status(response)
        return self._json(response)

    @property
    def _api_token(self) -> str:
        """Return the configured API token, if token-based auth is enabled."""
        return self._api_token_value

    @staticmethod
    def _secret_value(secret: Any) -> str:
        """Return the plain secret value when a secret is configured."""
        if secret is None:
            return ""
        if hasattr(secret, "get_secret_value"):
            return secret.get_secret_value()
        return str(secret)

    @staticmethod
    def _single_client(client_data: dict[str, Any]) -> dict[str, Any]:
        """Return the single client object required by the panel clients API."""
        clients = client_data.get("clients")
        if (
            isinstance(clients, list)
            and len(clients) == 1
            and isinstance(clients[0], dict)
        ):
            return clients[0]

        msg = "X-UI client payload must contain exactly one client"
        raise ValueError(msg)

    @staticmethod
    def _extract_single_client(client_data: dict[str, Any]) -> dict[str, Any]:
        """Return one client object from wrapper or direct client payloads."""
        clients = client_data.get("clients")
        if (
            isinstance(clients, list)
            and len(clients) == 1
            and isinstance(clients[0], dict)
        ):
            return dict(clients[0])
        if isinstance(client_data, dict) and isinstance(client_data.get("email"), str):
            return dict(client_data)
        msg = "X-UI client payload must contain one client with email"
        raise ValueError(msg)

    def _inbound_ids(self, inbound_id: int | list[int] | None = None) -> list[int]:
        """Return inbound ids from an explicit value or environment settings."""
        if inbound_id is None:
            inbound_ids = self._inbound_ids_value
        elif isinstance(inbound_id, list):
            inbound_ids = inbound_id
        else:
            inbound_ids = [inbound_id]

        if not inbound_ids:
            msg = "XUI_INBOUND_IDS must contain at least one inbound id"
            raise ValueError(msg)
        return inbound_ids

    def _inbound_id(self, inbound_id: int | None = None) -> int:
        """Return an explicit inbound id or the first configured inbound id."""
        if inbound_id is not None:
            return inbound_id

        inbound_ids = self._inbound_ids_value
        if not inbound_ids:
            msg = "XUI_INBOUND_IDS must contain at least one inbound id"
            raise ValueError(msg)
        return inbound_ids[0]

    @staticmethod
    def _path_param(value: str) -> str:
        """Encode a value for safe use in an URL path segment."""
        return quote(value, safe="")

    @classmethod
    def _raise_for_status(cls, response: httpx.Response) -> None:
        """Raise X-UI specific exceptions for failed HTTP responses."""
        status_code = response.status_code
        if status_code in {401, 403}:
            raise XuiAuthError(
                f"X-UI authentication failed (status={status_code}, "
                f"response={cls._response_text(response)})",
            )
        if status_code == 404:
            raise XuiRequestError(
                "Wrong endpoint or wrong XUI_BASE_URL web path "
                f"(status={status_code}, response={cls._response_text(response)})",
            )
        if 500 <= status_code <= 599:
            raise XuiRequestError(
                "3x-ui panel server error "
                f"(status={status_code}, response={cls._response_text(response)})",
            )
        if 400 <= status_code <= 599:
            raise XuiRequestError(
                f"X-UI request failed (status={status_code}, "
                f"response={cls._response_text(response)})",
            )

    @classmethod
    def _json(cls, response: httpx.Response) -> dict[str, Any]:
        """Decode an X-UI JSON response and validate the API success flag."""
        data = response.json()
        if not isinstance(data, dict):
            msg = (
                "X-UI response must be a JSON object "
                f"(status={response.status_code}, "
                f"response={cls._response_text(response)})"
            )
            raise XuiApiError(msg)

        if data.get("success") is False:
            api_message = cls._redact_sensitive(
                data.get("msg") or data.get("message") or data
            )
            raise XuiApiError(
                f"X-UI API error: {api_message} "
                f"(status={response.status_code}, "
                f"response={cls._response_text(response)})",
            )

        return data

    @classmethod
    def _response_text(cls, response: httpx.Response) -> str:
        """Return redacted response text for diagnostics."""
        try:
            text = response.text
        except UnicodeDecodeError:
            text = repr(response.content)
        return cls._redact_sensitive(text)

    @staticmethod
    def _redact_sensitive(value: Any) -> Any:
        """Redact credentials, cookies, and tokens from diagnostic values."""
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if any(
                    marker in key_text
                    for marker in (
                        "username",
                        "password",
                        "cookie",
                        "token",
                        "authorization",
                        "secret",
                        "api_key",
                    )
                ):
                    redacted[key] = "***"
                else:
                    redacted[key] = XuiClient._redact_sensitive(item)
            return redacted
        if isinstance(value, list):
            return [XuiClient._redact_sensitive(item) for item in value]
        if not isinstance(value, str):
            return value

        redacted_text = re.sub(
            r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
            r"\1***",
            value,
        )
        redacted_text = re.sub(
            r'(?i)("?(?:username|password|cookie|token|authorization|secret|api[_-]?key)"?\s*[:=]\s*)"?[^",&;\s}]+"?',
            r"\1***",
            redacted_text,
        )
        redacted_text = re.sub(
            r"(?i)(bearer\s+)[^\s,;]+",
            r"\1***",
            redacted_text,
        )
        return redacted_text
