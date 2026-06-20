"""Protected access provisioning service."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, XuiNodeConfig
from app.db.repositories import (
    ActiveSubscriptionAlreadyExistsError,
    SubscriptionRepository,
    UserRepository,
)
from app.db.session import async_session_maker
from app.services.node_selector_service import (
    NodeSelectorService,
    acquire_capacity_selection_lock,
)
from app.services.xui_client import XuiClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProvisionResult:
    """Result of creating or extending a protected access subscription."""

    connection_link: str
    expires_at: datetime
    action: str
    subscription_id: int


class NoActiveSubscriptionError(ValueError):
    """Raised when a requested active subscription does not exist."""


class VpnService:
    """Coordinate protected access account provisioning and updates."""

    ALLOWED_TARIFF_MONTHS = {1, 3, 6, 12}

    def __init__(
        self,
        settings: Settings | None = None,
        xui_client: XuiClient | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.xui_client = xui_client
        self.session = session
        self._owns_xui_client = False

    async def close(self) -> None:
        """Close resources owned by the service."""
        if self._owns_xui_client and self.xui_client is not None:
            await self.xui_client.close()

    @asynccontextmanager
    async def _xui_client_for_node(
        self,
        node: XuiNodeConfig,
    ) -> AsyncIterator[XuiClient]:
        """Yield an X-UI client configured for the selected node."""
        if self.xui_client is not None:
            yield self.xui_client
            return

        client = XuiClient(settings=self.settings, node=node)
        try:
            yield client
        finally:
            await client.close()

    def _node_selector(self, session: AsyncSession) -> NodeSelectorService:
        """Return a node selector bound to the current service settings/session."""
        return NodeSelectorService(settings=self.settings, session=session)

    @staticmethod
    def _node_vpn_config(node: XuiNodeConfig) -> dict[str, Any]:
        """Return non-secret node metadata persisted with subscription diagnostics."""
        return {
            "node_key": node.key,
            "node_label": node.name or node.key,
            "node_public_host": node.xui_public_host,
        }

    def _node_default_traffic_gb(self, node: XuiNodeConfig) -> int:
        """Return per-node or global default traffic limit in GiB."""
        if node.default_traffic_gb is not None:
            return node.default_traffic_gb
        return self.settings.xui_default_traffic_gb

    def _node_default_limit_ip(self, node: XuiNodeConfig) -> int:
        """Return per-node or global default IP limit."""
        if node.default_limit_ip is not None:
            return node.default_limit_ip
        return self.settings.xui_default_limit_ip

    async def create_client(self, telegram_id: int, months: int) -> str:
        """Create or extend a paid subscription and return its connection link."""
        result = await self.provision_or_extend_client(telegram_id, months)
        return result.connection_link

    async def provision_or_extend_client(
        self,
        telegram_id: int,
        months: int,
        source_payment_id: str | None = None,
        preferred_node_key: str | None = None,
        exclude_payment_id: int | None = None,
    ) -> ProvisionResult:
        """Create a new subscription or extend the user's active one."""
        if self.session is not None:
            subscription = await SubscriptionRepository(
                self.session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is not None:
                return await self._extend_active_subscription(
                    self.session,
                    subscription,
                    months,
                    source_payment_id=source_payment_id,
                )
            return await self._create_client(
                self.session,
                telegram_id,
                months,
                source_payment_id=source_payment_id,
                preferred_node_key=preferred_node_key,
                exclude_payment_id=exclude_payment_id,
            )

        async with async_session_maker() as session:
            subscription = await SubscriptionRepository(
                session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is not None:
                result = await self._extend_active_subscription(
                    session,
                    subscription,
                    months,
                    source_payment_id=source_payment_id,
                )
            else:
                result = await self._create_client(
                    session,
                    telegram_id,
                    months,
                    source_payment_id=source_payment_id,
                    preferred_node_key=preferred_node_key,
                    exclude_payment_id=exclude_payment_id,
                )
            await session.commit()
            return result

    async def provision_trial_client(
        self,
        telegram_id: int,
        hours: int = 2,
    ) -> ProvisionResult:
        """Create a one-time trial subscription without payment/referral side effects."""
        if hours <= 0:
            msg = "Trial access hours must be positive"
            raise ValueError(msg)

        if self.session is not None:
            subscription = await SubscriptionRepository(
                self.session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is not None:
                msg = f"Telegram user {telegram_id} already has an active subscription"
                raise ValueError(msg)
            return await self._create_trial_client(self.session, telegram_id, hours)

        async with async_session_maker() as session:
            subscription = await SubscriptionRepository(
                session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is not None:
                msg = f"Telegram user {telegram_id} already has an active subscription"
                raise ValueError(msg)
            result = await self._create_trial_client(session, telegram_id, hours)
            await session.commit()
            return result

    async def extend_active_subscription_by_days(
        self,
        telegram_id: int,
        days: int,
        reason: str = "manual",
    ) -> ProvisionResult:
        """Extend a user's active subscription by a number of days."""
        if days <= 0:
            msg = "Extension days must be positive"
            raise ValueError(msg)

        if self.session is not None:
            subscription = await SubscriptionRepository(
                self.session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is None:
                msg = (
                    f"Active subscription for Telegram user {telegram_id} was not found"
                )
                raise NoActiveSubscriptionError(msg)
            return await self._extend_active_subscription_by_days(
                self.session, subscription, days, reason
            )

        async with async_session_maker() as session:
            subscription = await SubscriptionRepository(
                session
            ).get_active_by_telegram_id(telegram_id)
            if subscription is None:
                msg = (
                    f"Active subscription for Telegram user {telegram_id} was not found"
                )
                raise NoActiveSubscriptionError(msg)
            result = await self._extend_active_subscription_by_days(
                session, subscription, days, reason
            )
            await session.commit()
            return result

    async def _extend_active_subscription_by_days(
        self,
        session: AsyncSession,
        subscription: Any,
        days: int,
        reason: str,
    ) -> ProvisionResult:
        """Extend an active subscription by days both in X-UI and local storage."""
        current_expires_at = subscription.expires_at
        if current_expires_at.tzinfo is None:
            current_expires_at = current_expires_at.replace(tzinfo=UTC)
        base_time = max(current_expires_at, datetime.now(UTC))
        expires_at = base_time + timedelta(days=days)
        expiry_ms = int(expires_at.timestamp() * 1000)

        vpn_config = dict(subscription.vpn_config or {})
        client_payload = dict(
            vpn_config.get("client") or vpn_config.get("provisioned_client") or {}
        )
        client_payload["id"] = subscription.xui_client_id
        client_payload["email"] = subscription.xui_email
        client_payload["expiryTime"] = expiry_ms
        client_payload["enable"] = True

        node = self._node_selector(session).get_node_for_subscription(subscription)
        async with self._xui_client_for_node(node) as xui_client:
            await xui_client.update_client(
                subscription.inbound_id,
                subscription.xui_client_id,
                {"clients": [client_payload]},
                enable=True,
            )

        vpn_config["client"] = client_payload
        vpn_config["provisioned_client"] = client_payload
        vpn_config["expires_at"] = expires_at.isoformat()
        vpn_config.update(self._node_vpn_config(node))
        vpn_config.setdefault("extension_reasons", []).append(
            {
                "reason": reason,
                "days": days,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        connection_link = self._existing_connection_link(vpn_config)
        vpn_config["connection_link"] = connection_link
        vpn_config.setdefault("subscription_url", connection_link)

        subscription.expires_at = expires_at
        subscription.vpn_config = vpn_config
        session.add(subscription)
        await session.flush()

        return ProvisionResult(
            connection_link=connection_link,
            expires_at=expires_at,
            action="extended",
            subscription_id=subscription.id,
        )

    async def _create_trial_client(
        self,
        session: AsyncSession,
        telegram_id: int,
        hours: int,
    ) -> ProvisionResult:
        """Provision a trial 3x-ui client and persist an active subscription."""
        now = datetime.now(UTC)
        user = await UserRepository(session).get_or_create(telegram_id)
        existing = await SubscriptionRepository(session).get_active_by_telegram_id(
            telegram_id
        )
        if existing is not None:
            msg = f"Telegram user {telegram_id} already has an active subscription"
            raise ActiveSubscriptionAlreadyExistsError(msg)
        client_id = str(uuid4())
        sub_id = f"trial_tg_{telegram_id}_{token_hex(4)}"
        email = sub_id
        expires_at = now + timedelta(hours=hours)
        expiry_ms = int(expires_at.timestamp() * 1000)
        await acquire_capacity_selection_lock(session)
        node = await self._node_selector(session).select_node_for_new_subscription()
        traffic_limit_gb = self._node_default_traffic_gb(node)
        total_bytes = traffic_limit_gb * 1024**3
        client_payload = {
            "id": client_id,
            "email": email,
            "subId": sub_id,
            "tgId": telegram_id,
            "expiryTime": expiry_ms,
            "limitIp": self._node_default_limit_ip(node),
            "totalGB": total_bytes,
            "enable": True,
        }

        inbound_ids = node.xui_inbound_ids
        primary_inbound_id = inbound_ids[0]

        async with self._xui_client_for_node(node) as xui_client:
            xui_response = await xui_client.add_client(client_payload, inbound_ids)

        provisioned_client = client_payload
        provisioned_client_id = client_id
        inbound_response: dict[str, Any] | None = None
        inbound: dict[str, Any] = {}
        protocol: Any = None
        settings: dict[str, Any] = {}
        stream_settings: dict[str, Any] = {}
        diagnostic_subscription_link = self._extract_subscription_link(xui_response)

        if node.xui_sub_base_url:
            connection_link = self._build_subscription_url(
                node.xui_sub_base_url,
                sub_id,
            )
        else:
            async with self._xui_client_for_node(node) as xui_client:
                inbound_response = await xui_client.get_inbound(primary_inbound_id)
            provisioned_client = self._find_inbound_client(inbound_response, email)
            provisioned_client_id = self._extract_client_secret(
                provisioned_client, email
            )
            inbound = self._extract_inbound_object(inbound_response)
            protocol = inbound.get("protocol")
            settings = self._extract_inbound_settings(inbound_response)
            stream_settings = self._extract_inbound_stream_settings(inbound_response)
            if diagnostic_subscription_link is None:
                diagnostic_subscription_link = self._extract_subscription_link(
                    inbound_response
                )
            connection_link = self._build_vless_uri(
                client_secret=provisioned_client_id,
                email=email,
                inbound=inbound,
                protocol=protocol,
                public_host=node.xui_public_host,
                stream_settings=stream_settings,
            )

        subscription = await SubscriptionRepository(session).create_active(
            user=user,
            xui_client_id=provisioned_client_id,
            xui_email=email,
            inbound_id=primary_inbound_id,
            expires_at=expires_at,
            traffic_limit_gb=traffic_limit_gb,
            node_key=node.key,
            node_label=node.name or node.key,
            vpn_config={
                "email": email,
                **self._node_vpn_config(node),
                "access_type": "trial",
                "trial_hours": hours,
                "trial_created_at": now.isoformat(),
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
        return ProvisionResult(
            connection_link=connection_link,
            expires_at=expires_at,
            action="created",
            subscription_id=subscription.id,
        )

    async def _create_client(
        self,
        session: AsyncSession,
        telegram_id: int,
        months: int,
        source_payment_id: str | None = None,
        preferred_node_key: str | None = None,
        exclude_payment_id: int | None = None,
    ) -> ProvisionResult:
        """Provision a 3x-ui client and persist the matching subscription."""
        if months not in self.ALLOWED_TARIFF_MONTHS:
            msg = "Subscription tariff must be 1, 3, 6, or 12 months"
            raise ValueError(msg)

        user = await UserRepository(session).get_or_create(telegram_id)
        existing = await SubscriptionRepository(session).get_active_by_telegram_id(
            telegram_id
        )
        if existing is not None:
            return await self._extend_active_subscription(
                session, existing, months, source_payment_id=source_payment_id
            )
        client_id = str(uuid4())
        sub_id = f"tg_{telegram_id}_{token_hex(4)}"
        email = sub_id
        expires_at = datetime.now(UTC) + relativedelta(months=months)
        expiry_ms = int(expires_at.timestamp() * 1000)
        await acquire_capacity_selection_lock(session)
        node_selector = self._node_selector(session)
        node = None
        if preferred_node_key:
            node = await node_selector.select_preferred_node_for_new_subscription(
                preferred_node_key,
                exclude_payment_id=exclude_payment_id,
            )
        if node is None:
            node = await node_selector.select_node_for_new_subscription(
                exclude_payment_id=exclude_payment_id,
            )
        traffic_limit_gb = self._node_default_traffic_gb(node)
        total_bytes = traffic_limit_gb * 1024**3
        client_payload = {
            "id": client_id,
            "email": email,
            "subId": sub_id,
            "tgId": telegram_id,
            "expiryTime": expiry_ms,
            "limitIp": self._node_default_limit_ip(node),
            "totalGB": total_bytes,
            "enable": True,
        }

        inbound_ids = node.xui_inbound_ids
        primary_inbound_id = inbound_ids[0]

        async with self._xui_client_for_node(node) as xui_client:
            xui_response = await xui_client.add_client(
                client_payload,
                inbound_ids,
            )

        provisioned_client = client_payload
        provisioned_client_id = client_id
        inbound_response: dict[str, Any] | None = None
        inbound: dict[str, Any] = {}
        protocol: Any = None
        settings: dict[str, Any] = {}
        stream_settings: dict[str, Any] = {}
        diagnostic_subscription_link = self._extract_subscription_link(xui_response)

        if node.xui_sub_base_url:
            connection_link = self._build_subscription_url(
                node.xui_sub_base_url,
                sub_id,
            )
        else:
            async with self._xui_client_for_node(node) as xui_client:
                inbound_response = await xui_client.get_inbound(primary_inbound_id)
            provisioned_client = self._find_inbound_client(inbound_response, email)
            provisioned_client_id = self._extract_client_secret(
                provisioned_client, email
            )
            inbound = self._extract_inbound_object(inbound_response)
            protocol = inbound.get("protocol")
            settings = self._extract_inbound_settings(inbound_response)
            stream_settings = self._extract_inbound_stream_settings(inbound_response)
            if diagnostic_subscription_link is None:
                diagnostic_subscription_link = self._extract_subscription_link(
                    inbound_response
                )
            connection_link = self._build_vless_uri(
                client_secret=provisioned_client_id,
                email=email,
                inbound=inbound,
                protocol=protocol,
                public_host=node.xui_public_host,
                stream_settings=stream_settings,
            )

        logger.debug(
            "Prepared X-UI connection link",
            extra={
                "xui_sub_base_url": node.xui_sub_base_url,
                "sub_id": sub_id,
                "inbound_ids": inbound_ids,
                "connection_link": connection_link,
            },
        )

        subscription = await SubscriptionRepository(session).create_active(
            user=user,
            xui_client_id=provisioned_client_id,
            xui_email=email,
            inbound_id=primary_inbound_id,
            expires_at=expires_at,
            traffic_limit_gb=traffic_limit_gb,
            node_key=node.key,
            node_label=node.name or node.key,
            vpn_config={
                "email": email,
                **self._node_vpn_config(node),
                **self._payment_marker_config(
                    source_payment_id, months, "created", datetime.now(UTC)
                ),
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
        return ProvisionResult(
            connection_link=connection_link,
            expires_at=expires_at,
            action="created",
            subscription_id=subscription.id,
        )

    async def _extend_active_subscription(
        self,
        session: AsyncSession,
        subscription: Any,
        months: int,
        source_payment_id: str | None = None,
    ) -> ProvisionResult:
        """Extend an active subscription both in X-UI and in local storage."""
        if months not in self.ALLOWED_TARIFF_MONTHS:
            msg = "Subscription tariff must be 1, 3, 6, or 12 months"
            raise ValueError(msg)

        current_expires_at = subscription.expires_at
        if current_expires_at.tzinfo is None:
            current_expires_at = current_expires_at.replace(tzinfo=UTC)
        base_time = max(current_expires_at, datetime.now(UTC))
        expires_at = base_time + relativedelta(months=months)
        expiry_ms = int(expires_at.timestamp() * 1000)

        vpn_config = dict(subscription.vpn_config or {})
        client_payload = dict(
            vpn_config.get("client") or vpn_config.get("provisioned_client") or {}
        )
        client_payload["id"] = subscription.xui_client_id
        client_payload["email"] = subscription.xui_email
        client_payload["expiryTime"] = expiry_ms
        client_payload["enable"] = True

        node = self._node_selector(session).get_node_for_subscription(subscription)
        async with self._xui_client_for_node(node) as xui_client:
            await xui_client.update_client(
                subscription.inbound_id,
                subscription.xui_client_id,
                {"clients": [client_payload]},
                enable=True,
            )

        vpn_config["client"] = client_payload
        vpn_config["provisioned_client"] = client_payload
        vpn_config["expires_at"] = expires_at.isoformat()
        vpn_config.update(self._node_vpn_config(node))
        vpn_config.update(
            self._payment_marker_config(
                source_payment_id, months, "extended", datetime.now(UTC)
            )
        )
        connection_link = self._existing_connection_link(vpn_config)
        vpn_config["connection_link"] = connection_link
        vpn_config.setdefault("subscription_url", connection_link)

        subscription.expires_at = expires_at
        subscription.vpn_config = vpn_config
        session.add(subscription)
        await session.flush()

        return ProvisionResult(
            connection_link=connection_link,
            expires_at=expires_at,
            action="extended",
            subscription_id=subscription.id,
        )

    @staticmethod
    def provision_result_from_subscription(subscription: Any) -> ProvisionResult:
        """Build a provision result for an already provisioned subscription."""
        vpn_config = dict(subscription.vpn_config or {})
        action = vpn_config.get("last_payment_action")
        if action not in {"created", "extended"}:
            action = "extended"
        for key in ("connection_link", "subscription_url"):
            value = vpn_config.get(key)
            if isinstance(value, str) and value:
                return ProvisionResult(
                    connection_link=value,
                    expires_at=subscription.expires_at,
                    action=action,
                    subscription_id=subscription.id,
                )
        msg = "Active subscription does not contain a reusable connection link"
        raise RuntimeError(msg)

    @staticmethod
    def _payment_marker_config(
        source_payment_id: str | None,
        months: int,
        action: str,
        applied_at: datetime,
    ) -> dict[str, Any]:
        """Return idempotency metadata for a payment-applied subscription change."""
        if source_payment_id is None:
            return {}
        return {
            "last_payment_id": source_payment_id,
            "last_paid_months": months,
            "last_payment_applied_at": applied_at.isoformat(),
            "last_payment_action": action,
        }

    def _existing_connection_link(self, vpn_config: dict[str, Any]) -> str:
        """Return or rebuild the persisted connection link for a subscription."""
        for key in ("connection_link", "subscription_url"):
            value = vpn_config.get(key)
            if isinstance(value, str) and value:
                return value

        sub_id = vpn_config.get("subId")
        if isinstance(sub_id, str) and sub_id and self.settings.xui_sub_base_url:
            return self._build_subscription_url(self.settings.xui_sub_base_url, sub_id)

        msg = "Active subscription does not contain a reusable connection link"
        raise RuntimeError(msg)

    @staticmethod
    def _build_subscription_url(base_url: str, sub_id: str) -> str:
        """Build a 3x-ui subscription URL as subURI + URL-encoded subId."""
        return base_url.rstrip("/") + "/" + quote(sub_id, safe="")

    @staticmethod
    def _extract_subscription_link(response: dict[str, Any]) -> str | None:
        """Find a subscription link in a known 3x-ui response shape."""
        candidate_keys = {
            "subscriptionLink",
            "subscription_link",
            "subLink",
            "sub_link",
        }
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
            msg = (
                "X-UI provisioning failed: inbound settings do not contain clients list"
            )
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
    def _extract_inbound_settings(
        cls, inbound_response: dict[str, Any]
    ) -> dict[str, Any]:
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
                msg = (
                    "X-UI provisioning failed: inbound streamSettings is not valid JSON"
                )
                raise RuntimeError(msg) from exc

        if isinstance(stream_settings, dict):
            return stream_settings

        msg = (
            "X-UI provisioning failed: inbound response does not contain streamSettings"
        )
        raise RuntimeError(msg)

    @classmethod
    def _build_vless_uri(
        cls,
        *,
        client_secret: str,
        email: str,
        inbound: dict[str, Any],
        protocol: Any,
        public_host: str | None,
        stream_settings: dict[str, Any],
    ) -> str:
        """Build a VLESS URI from inbound data when X-UI has no ready link."""
        if protocol != "vless":
            msg = (
                "X-UI provisioning failed: ready subscription link is absent and "
                f"primary inbound protocol is {protocol!r}, not 'vless'"
            )
            raise RuntimeError(msg)

        address = public_host
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
            if (
                isinstance(short_ids, list)
                and short_ids
                and isinstance(short_ids[0], str)
            ):
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
