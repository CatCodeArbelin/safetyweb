"""Subscription lifecycle service."""

from datetime import UTC

from app.db.models import Subscription
from app.db.repositories import SubscriptionRepository
from app.db.session import async_session_maker


class SubscriptionService:
    """Coordinate subscription read operations for bot handlers."""

    async def get_active_subscription(self, telegram_id: int) -> Subscription | None:
        """Return the latest active subscription for a Telegram user."""
        async with async_session_maker() as session:
            return await SubscriptionRepository(session).get_active_by_telegram_id(telegram_id)

    @staticmethod
    def format_status(subscription: Subscription | None) -> str:
        """Format subscription status for a user-facing Telegram message."""
        if subscription is None:
            return "У вас пока нет активной подписки. Нажмите «Оформить доступ», чтобы создать заявку."

        expires_at = subscription.expires_at.astimezone(UTC)
        link = (subscription.vpn_config or {}).get("connection_link")
        traffic_limit = subscription.traffic_limit_gb
        traffic_text = "безлимитный" if not traffic_limit else f"{traffic_limit} ГБ"
        text = (
            "Ваша подписка активна ✅\n\n"
            f"Inbound: <code>{subscription.inbound_id}</code>\n"
            f"Клиент: <code>{subscription.xui_email}</code>\n"
            f"Действует до: <b>{expires_at:%d.%m.%Y %H:%M UTC}</b>\n"
            f"Объём трафика: <b>{traffic_text}</b>"
        )
        if isinstance(link, str) and link:
            text += f"\n\nСсылка для защищённого соединения: <code>{link}</code>"
        return text
