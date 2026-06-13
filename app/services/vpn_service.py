"""VPN provisioning service."""

import json
from calendar import monthrange
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import SubscriptionRepository, UserRepository
from app.db.session import async_session_maker
from app.services.xui_client import XuiClient


class VpnService:
    """Coordinate VPN account provisioning and updates."""

    ALLOWED_TARIFF_MONTHS = {1, 3, 6}

    def __init__(
        self,
        settings: Settings | None = None,
        xui_client: XuiClient | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.xui_client = xui_client or XuiClient(settings=self.settings)
        self.session = session
        self._owns_xui_client = xui_client is None

    async def close(self) -> None:
        """Close resources owned by the service."""
        if self._owns_xui_client:
            await self.xui_client.close()

    async def create_client(self, telegram_id: int, months: int) -> str:
        """Create a paid subscription and return the user's connection link."""
        if self.session is not None:
            return await self._create_client(self.session, telegram_id, months)

        async with async_session_maker() as session:
            link = await self._create_client(session, telegram_id, months)
            await session.commit()
            return link

    async def _create_client(
        self,
        session: AsyncSession,
        telegram_id: int,
        months: int,
    ) -> str:
        """Provision a 3x-ui client and persist the matching subscription."""
        if months not in self.ALLOWED_TARIFF_MONTHS:
            msg = "Subscription tariff must be 1, 3, or 6 months"
            raise ValueError(msg)

        user = await UserRepository(session).get_or_create(telegram_id)
        client_id = str(uuid4())
        email = f"tg_{telegram_id}_{token_hex(4)}"
        expires_at = self._add_months(datetime.now(tz=UTC), months)
        total_bytes = self.settings.xui_default_traffic_gb * 1024 ** 3
        client_payload = {
            "id": client_id,
            "email": email,
            "tgId": telegram_id,
            "expiryTime": int(expires_at.timestamp() * 1000),
            "limitIp": self.settings.xui_default_limit_ip,
            "totalGB": total_bytes,
            "enable": True,
        }

        inbound_ids = self.settings.xui_inbound_ids
        if not inbound_ids:
            msg = "XUI_INBOUND_IDS must contain at least one inbound id"
            raise ValueError(msg)
        primary_inbound_id = inbound_ids[0]

        xui_response = await self.xui_client.add_client(
            client_payload,
            inbound_ids,
        )
        inbound_response = await self.xui_client.get_inbound(primary_inbound_id)
        provisioned_client = self._find_inbound_client(inbound_response, email)
        provisioned_client_id = self._extract_client_secret(provisioned_client, email)
        subscription_link = self._extract_subscription_link(xui_response)
        connection_link = subscription_link or self._build_vless_uri(
            provisioned_client_id,
            email,
        )

        await SubscriptionRepository(session).create_active(
            user=user,
            xui_client_id=provisioned_client_id,
            xui_email=email,
            inbound_id=primary_inbound_id,
            expires_at=expires_at,
            traffic_limit_gb=self.settings.xui_default_traffic_gb,
            vpn_config={
                "client": client_payload,
                "provisioned_client": provisioned_client,
                "xui_response": xui_response,
                "inbound_response": inbound_response,
                "subscription_link": subscription_link,
                "connection_link": connection_link,
            },
        )
        return connection_link

    @staticmethod
    def _add_months(value: datetime, months: int) -> datetime:
        """Add calendar months while preserving the day where possible."""
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    @staticmethod
    def _extract_subscription_link(response: dict[str, Any]) -> str | None:
        """Find a subscription link in a known 3x-ui response shape."""
        candidate_keys = {"subscriptionLink", "subscription_link", "subLink", "sub_link"}
        stack: list[Any] = [response]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in candidate_keys and isinstance(value, str) and value:
                        return value
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None

    @classmethod
    def _find_inbound_client(
        cls,
        inbound_response: dict[str, Any],
        generated_email: str,
    ) -> dict[str, Any]:
        """Return the newly provisioned client from an X-UI inbound response."""
        settings = cls._extract_inbound_settings(inbound_response)
        clients = settings.get("clients")
        if not isinstance(clients, list):
            msg = "X-UI provisioning failed: inbound settings do not contain clients list"
            raise RuntimeError(msg)

        for client in clients:
            if isinstance(client, dict) and client.get("email") == generated_email:
                return client

        msg = (
            "X-UI provisioning failed: created client was not found in primary "
            f"inbound by email {generated_email!r}"
        )
        raise RuntimeError(msg)

    @classmethod
    def _extract_inbound_settings(cls, inbound_response: dict[str, Any]) -> dict[str, Any]:
        """Extract and decode the inbound settings object from an X-UI response."""
        inbound = cls._extract_inbound_object(inbound_response)
        settings = inbound.get("settings")
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except json.JSONDecodeError as exc:
                msg = "X-UI provisioning failed: inbound settings is not valid JSON"
                raise RuntimeError(msg) from exc

        if isinstance(settings, dict):
            return settings

        msg = "X-UI provisioning failed: inbound response does not contain settings"
        raise RuntimeError(msg)

    @staticmethod
    def _extract_inbound_object(inbound_response: dict[str, Any]) -> dict[str, Any]:
        """Extract the inbound object from common X-UI response wrappers."""
        for key in ("obj", "data", "inbound"):
            value = inbound_response.get(key)
            if isinstance(value, dict):
                return value
        return inbound_response

    @staticmethod
    def _extract_client_secret(client: dict[str, Any], generated_email: str) -> str:
        """Extract the persisted X-UI client UUID/secret from a client object."""
        for key in ("id", "password"):
            value = client.get(key)
            if isinstance(value, str) and value:
                return value

        msg = (
            "X-UI provisioning failed: created client "
            f"{generated_email!r} has neither id nor password"
        )
        raise RuntimeError(msg)

    def _build_vless_uri(self, client_id: str, email: str) -> str:
        """Build a fallback VLESS URI when X-UI does not return a subscription link."""
        parsed_url = urlparse(self.settings.xui_base_url)
        host = parsed_url.hostname or (
            self.settings.xui_base_url.removeprefix("https://").removeprefix("http://")
        )
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        return f"vless://{client_id}@{host}:{port}?type=tcp&security=none#{email}"
