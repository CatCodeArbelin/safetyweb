"""Subscription repository helpers."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Subscription, SubscriptionStatus, User


class SubscriptionRepository:
    """Persist and query protected access subscriptions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_by_telegram_id(self, telegram_id: int) -> Subscription | None:
        """Return the latest active subscription for a Telegram user."""
        return await self.session.scalar(
            select(Subscription)
            .join(Subscription.user)
            .where(
                User.telegram_id == telegram_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(Subscription.expires_at.desc())
            .options(selectinload(Subscription.user))
            .limit(1)
        )

    async def get_by_last_payment_id(
        self, telegram_id: int, provider_payment_id: str
    ) -> Subscription | None:
        """Return an active subscription last changed by the given payment."""
        return await self.session.scalar(
            select(Subscription)
            .join(Subscription.user)
            .where(
                User.telegram_id == telegram_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.vpn_config["last_payment_id"].as_string()
                == provider_payment_id,
            )
            .order_by(Subscription.expires_at.desc())
            .options(selectinload(Subscription.user))
            .limit(1)
        )

    async def create_active(
        self,
        *,
        user: User,
        xui_client_id: str,
        xui_email: str,
        inbound_id: int,
        expires_at: datetime,
        traffic_limit_gb: int,
        vpn_config: dict,
    ) -> Subscription:
        """Create an active subscription record."""
        subscription = Subscription(
            user=user,
            xui_client_id=xui_client_id,
            xui_email=xui_email,
            inbound_id=inbound_id,
            status=SubscriptionStatus.ACTIVE,
            expires_at=expires_at,
            traffic_limit_gb=traffic_limit_gb,
            vpn_config=vpn_config,
        )
        self.session.add(subscription)
        await self.session.flush()
        return subscription
