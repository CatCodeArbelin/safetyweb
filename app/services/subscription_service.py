"""Subscription lifecycle service."""

from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import func, select

from app.db.models import CustomerBenefit, ReferralReward, Subscription, User
from app.db.repositories import SubscriptionRepository
from app.db.session import async_session_maker
from app.services.benefit_service import EARLY_BUYER_BENEFIT_TYPE


@dataclass(frozen=True)
class SubscriptionStatusDetails:
    """User-facing subscription state and related benefits."""

    subscription: Subscription | None
    early_buyer_discount_percent: int = 0
    pending_referral_bonus_days: int = 0


class SubscriptionService:
    """Coordinate subscription read operations for bot handlers."""

    async def get_active_subscription(self, telegram_id: int) -> Subscription | None:
        """Return the latest active subscription for a Telegram user."""
        async with async_session_maker() as session:
            return await SubscriptionRepository(session).get_active_by_telegram_id(telegram_id)

    async def get_status_details(self, telegram_id: int) -> SubscriptionStatusDetails:
        """Return active subscription and unapplied user benefits for status output."""
        async with async_session_maker() as session:
            subscription = await SubscriptionRepository(session).get_active_by_telegram_id(
                telegram_id
            )
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                return SubscriptionStatusDetails(subscription=subscription)

            discount_percent = await session.scalar(
                select(func.max(CustomerBenefit.discount_percent)).where(
                    CustomerBenefit.user_id == user.id,
                    CustomerBenefit.benefit_type == EARLY_BUYER_BENEFIT_TYPE,
                    CustomerBenefit.is_active.is_(True),
                )
            )
            pending_bonus_days = await session.scalar(
                select(func.coalesce(func.sum(ReferralReward.bonus_days), 0)).where(
                    ReferralReward.recipient_user_id == user.id,
                    ReferralReward.applied_at.is_(None),
                )
            )
            return SubscriptionStatusDetails(
                subscription=subscription,
                early_buyer_discount_percent=int(discount_percent or 0),
                pending_referral_bonus_days=int(pending_bonus_days or 0),
            )

    @staticmethod
    def format_status(
        subscription: Subscription | None,
        *,
        early_buyer_discount_percent: int = 0,
        pending_referral_bonus_days: int = 0,
    ) -> str:
        """Format subscription status for a user-facing Telegram message."""
        benefit_lines = []
        if early_buyer_discount_percent > 0:
            benefit_lines.append(f"Ваша постоянная скидка: {early_buyer_discount_percent}%")
        if pending_referral_bonus_days > 0:
            benefit_lines.append(
                f"Ожидают применения бонусные дни: {pending_referral_bonus_days}"
            )
        benefits_text = "\n\n" + "\n".join(benefit_lines) if benefit_lines else ""

        if subscription is None:
            return (
                "У вас пока нет активной подписки. Нажмите «🛒 Оформить доступ», чтобы создать заявку."
                f"{benefits_text}"
            )

        expires_at = subscription.expires_at.astimezone(UTC)
        link = (subscription.vpn_config or {}).get("connection_link")
        traffic_limit = subscription.traffic_limit_gb
        traffic_text = "безлимитный" if not traffic_limit else f"{traffic_limit} ГБ"
        text = (
            "Ваша подписка активна ✅\n\n"
            f"Действует до: <b>{expires_at:%d.%m.%Y %H:%M UTC}</b>\n"
            f"Объём трафика: <b>{traffic_text}</b>"
        )
        if benefits_text:
            text += benefits_text
        if isinstance(link, str) and link:
            text += f"\n\nСсылка для защищённого соединения:\n<code>{link}</code>"
        return text
