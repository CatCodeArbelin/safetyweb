"""VPN provisioning service."""

import json
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from dateutil.relativedelta import relativedelta
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
        sub_id = f"tg_{telegram_id}_{token_hex(4)}"
        email = sub_id
        expires_at = datetime.now(UTC) + relativedelta(months=months)
        expiry_ms = int(expires_at.timestamp() * 1000)
        total_bytes = self.settings.xui_default_traffic_gb * 1024 ** 3
        client_payload = {
            "id": client_id,
            "email": email,
            "subId": sub_id,
            "tgId": telegram_id,
            "expiryTime": expiry_ms,
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
        inbound = self._extract_inbound_object(inbound_response)
        protocol = inbound.get("protocol")
        settings = self._extract_inbound_settings(inbound_response)
        stream_settings = self._extract_inbound_stream_settings(inbound_response)
        diagnostic_subscription_link = self._extract_subscription_link(xui_response)
        if diagnostic_subscription_link is None:
            diagnostic_subscription_link = self._extract_subscription_link(inbound_response)

        if self.settings.xui_sub_base_url:
            connection_link = f"{self.settings.xui_sub_base_url.rstrip('/')}/sub/{sub_id}"
        else:
            connection_link = self._build_vless_uri(
                client_secret=provisioned_client_id,
                email=email,
                inbound=inbound,
                protocol=protocol,
                app_settings=self.settings,
                stream_settings=stream_settings,
            )

        await SubscriptionRepository(session).create_active(
            user=user,
            xui_client_id=provisioned_client_id,
            xui_email=email,
            inbound_id=primary_inbound_id,
            expires_at=expires_at,
            traffic_limit_gb=self.settings.xui_default_traffic_gb,
            vpn_config={
                "email": email,
                "subId": sub_id,
                "subscription_url": connection_link,
                "inboundIds": inbound_ids,
                "xui_response": xui_response,
                "client": client_payload,
                "provisioned_client": provisioned_client,
                "inbound": {
                    "protocol": protocol,
                    "port": inbound.get("port"),
                    "settings": settings,
                    "streamSettings": stream_settings,
                },
                "inbound_response": inbound_response,
                "diagnostic_subscription_link": diagnostic_subscription_link,
                "connection_link": connection_link,
            },
        )
        return connection_link

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

    @classmethod
    def _extract_inbound_stream_settings(
        cls,
        inbound_response: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract and decode the inbound streamSettings object from X-UI."""
        inbound = cls._extract_inbound_object(inbound_response)
        stream_settings = inbound.get("streamSettings")
        if isinstance(stream_settings, str):
            try:
                stream_settings = json.loads(stream_settings)
            except json.JSONDecodeError as exc:
                msg = "X-UI provisioning failed: inbound streamSettings is not valid JSON"
                raise RuntimeError(msg) from exc

        if isinstance(stream_settings, dict):
            return stream_settings

        msg = "X-UI provisioning failed: inbound response does not contain streamSettings"
        raise RuntimeError(msg)

    @classmethod
    def _build_vless_uri(
        cls,
        *,
        client_secret: str,
        email: str,
        inbound: dict[str, Any],
        protocol: Any,
        app_settings: Settings,
        stream_settings: dict[str, Any],
    ) -> str:
        """Build a VLESS URI from inbound data when X-UI has no ready link."""
        if protocol != "vless":
            msg = (
                "X-UI provisioning failed: ready subscription link is absent and "
                f"primary inbound protocol is {protocol!r}, not 'vless'"
            )
            raise RuntimeError(msg)

        address = app_settings.xui_public_host
        if not address:
            msg = "XUI_PUBLIC_HOST is required for manual VLESS URI generation"
            raise RuntimeError(msg)

        port = inbound.get("port")
        if port in (None, ""):
            msg = (
                "X-UI provisioning failed: ready subscription link is absent and "
                "inbound response does not contain a VLESS port"
            )
            raise RuntimeError(msg)

        params = cls._vless_query_params(stream_settings)
        return (
            f"vless://{client_secret}@{address}:{port}?"
            f"{urlencode(params, doseq=True)}#{quote(email)}"
        )

    @staticmethod
    def _vless_query_params(stream_settings: dict[str, Any]) -> dict[str, str]:
        """Translate Xray stream settings into VLESS URI query parameters."""
        network = stream_settings.get("network")
        security = stream_settings.get("security")
        params = {
            "encryption": "none",
            "type": network if isinstance(network, str) and network else "tcp",
            "security": security if isinstance(security, str) and security else "none",
        }

        reality_settings = stream_settings.get("realitySettings")
        if isinstance(reality_settings, dict):
            public_key = reality_settings.get("publicKey")
            if isinstance(public_key, str) and public_key:
                params["pbk"] = public_key
            short_ids = reality_settings.get("shortIds")
            if isinstance(short_ids, list) and short_ids and isinstance(short_ids[0], str):
                params["sid"] = short_ids[0]
            server_names = reality_settings.get("serverNames")
            if (
                isinstance(server_names, list)
                and server_names
                and isinstance(server_names[0], str)
            ):
                params["sni"] = server_names[0]
            settings = reality_settings.get("settings")
            if isinstance(settings, dict):
                fingerprint = settings.get("fingerprint")
                if isinstance(fingerprint, str) and fingerprint:
                    params["fp"] = fingerprint
                spider_x = settings.get("spiderX")
                if isinstance(spider_x, str) and spider_x:
                    params["spx"] = spider_x

        ws_settings = stream_settings.get("wsSettings")
        if isinstance(ws_settings, dict):
            path = ws_settings.get("path")
            if isinstance(path, str) and path:
                params["path"] = path
            headers = ws_settings.get("headers")
            if isinstance(headers, dict):
                host = headers.get("Host")
                if isinstance(host, str) and host:
                    params["host"] = host

        return params
