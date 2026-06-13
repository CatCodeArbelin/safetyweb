"""VPN provisioning service."""

from calendar import monthrange
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Subscription, SubscriptionStatus, User
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

        user = await self._get_or_create_user(session, telegram_id)
        client_id = str(uuid4())
        email = f"tg_{telegram_id}_{token_hex(4)}"
        expires_at = self._add_months(datetime.now(tz=UTC), months)
        client_payload = {
            "id": client_id,
            "email": email,
            "expiryTime": int(expires_at.timestamp() * 1000),
            "limitIp": 0,
            "totalGB": 0,
            "enable": True,
        }

        xui_response = await self.xui_client.add_client(
            self.settings.xui_inbound_id,
            {"clients": [client_payload]},
        )
        subscription_link = self._extract_subscription_link(xui_response)
        connection_link = subscription_link or self._build_vless_uri(client_id, email)

        session.add(
            Subscription(
                user=user,
                status=SubscriptionStatus.ACTIVE,
                expires_at=expires_at,
                vpn_client_id=client_id,
                vpn_config={
                    "client": client_payload,
                    "xui_response": xui_response,
                    "subscription_link": subscription_link,
                    "connection_link": connection_link,
                },
            )
        )
        await session.flush()
        return connection_link

    @staticmethod
    async def _get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
        """Return an existing Telegram user or create a minimal user record."""
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is not None:
            return user

        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
        return user

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

    def _build_vless_uri(self, client_id: str, email: str) -> str:
        """Build a fallback VLESS URI when X-UI does not return a subscription link."""
        parsed_url = urlparse(self.settings.xui_base_url)
        host = parsed_url.hostname or (
            self.settings.xui_base_url.removeprefix("https://").removeprefix("http://")
        )
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        return f"vless://{client_id}@{host}:{port}?type=tcp&security=none#{email}"
