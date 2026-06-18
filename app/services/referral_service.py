"""Referral program orchestration."""

from datetime import UTC, datetime
from secrets import token_urlsafe

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Referral, ReferralCode, ReferralReward, User
from app.db.repositories import UserRepository
from app.db.session import async_session_maker
from app.services.vpn_service import VpnService


class ReferralService:
    """Create referral links and grant first-payment rewards."""

    def __init__(
        self,
        settings: Settings | None = None,
        session: AsyncSession | None = None,
        vpn_service: VpnService | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.session = session
        self.vpn_service = vpn_service

    async def get_or_create_code(self, telegram_id: int) -> str:
        """Return a stable referral code for a Telegram user."""
        if self.session is not None:
            return await self._get_or_create_code(self.session, telegram_id)

        async with async_session_maker() as session:
            code = await self._get_or_create_code(session, telegram_id)
            await session.commit()
            return code

    async def register_referral(self, referred_telegram_id: int, code: str) -> bool:
        """Record that a user started the bot through a referral code."""
        if not self.settings.referral_enabled:
            return False
        if self.session is not None:
            return await self._register_referral(self.session, referred_telegram_id, code)

        async with async_session_maker() as session:
            registered = await self._register_referral(session, referred_telegram_id, code)
            await session.commit()
            return registered

    async def apply_first_payment_rewards(
        self, referred_telegram_id: int, paid_months: int
    ) -> list[ReferralReward]:
        """Grant referral rewards after the referred user's first real payment."""
        if not self.settings.referral_enabled:
            return []
        if self.session is not None:
            return await self._apply_first_payment_rewards(
                self.session, referred_telegram_id, paid_months
            )

        async with async_session_maker() as session:
            rewards = await self._apply_first_payment_rewards(
                session, referred_telegram_id, paid_months
            )
            await session.commit()
            return rewards

    async def _get_or_create_code(self, session: AsyncSession, telegram_id: int) -> str:
        user = await UserRepository(session).get_or_create(telegram_id)
        existing = await session.scalar(
            select(ReferralCode).where(ReferralCode.user_id == user.id)
        )
        if existing is not None:
            return existing.code

        while True:
            code = token_urlsafe(8).replace("-", "_")
            duplicate = await session.scalar(
                select(ReferralCode.id).where(ReferralCode.code == code)
            )
            if duplicate is None:
                break
        referral_code = ReferralCode(user=user, code=code)
        session.add(referral_code)
        await session.flush()
        return code

    async def _register_referral(
        self, session: AsyncSession, referred_telegram_id: int, code: str
    ) -> bool:
        referral_code = await session.scalar(
            select(ReferralCode).where(ReferralCode.code == code)
        )
        if referral_code is None:
            return False

        referred = await UserRepository(session).get_or_create(referred_telegram_id)
        if referred.id == referral_code.user_id:
            return False

        existing = await session.scalar(
            select(Referral).where(Referral.referred_user_id == referred.id)
        )
        if existing is not None:
            return False

        referral = Referral(
            referrer_user_id=referral_code.user_id,
            referred_user_id=referred.id,
            referral_code_id=referral_code.id,
        )
        session.add(referral)
        await session.flush()
        return True

    async def _apply_first_payment_rewards(
        self, session: AsyncSession, referred_telegram_id: int, paid_months: int
    ) -> list[ReferralReward]:
        referral = await session.scalar(
            select(Referral)
            .join(Referral.referred)
            .where(User.telegram_id == referred_telegram_id)
        )
        if referral is None or referral.first_paid_at is not None:
            return []

        referral.first_paid_months = paid_months
        referral.first_paid_at = datetime.now(UTC)
        rewards: list[ReferralReward] = []

        referred_days = self.settings.referral_new_user_bonus_days
        if referred_days > 0:
            reward = await self._grant_reward(
                session,
                referral,
                referral.referred_user_id,
                "new_user",
                referred_days,
            )
            if reward is not None:
                rewards.append(reward)

        referrer_days = self._referrer_bonus_days(paid_months)
        referrer_days = await self._apply_monthly_cap(
            session, referral.referrer_user_id, referrer_days
        )
        if referrer_days > 0:
            reward = await self._grant_reward(
                session,
                referral,
                referral.referrer_user_id,
                f"month_{paid_months}",
                referrer_days,
            )
            if reward is not None:
                rewards.append(reward)

        return rewards

    def _referrer_bonus_days(self, paid_months: int) -> int:
        return {
            1: self.settings.referral_month_1_bonus_days,
            3: self.settings.referral_month_3_bonus_days,
            6: self.settings.referral_month_6_bonus_days,
            12: self.settings.referral_month_12_bonus_days,
        }.get(paid_months, 0)

    async def _apply_monthly_cap(
        self, session: AsyncSession, recipient_user_id: int, bonus_days: int
    ) -> int:
        max_days = self.settings.referral_max_bonus_days_per_month
        if max_days <= 0 or bonus_days <= 0:
            return 0
        now = datetime.now(UTC)
        used_days = await session.scalar(
            select(func.coalesce(func.sum(ReferralReward.bonus_days), 0)).where(
                ReferralReward.recipient_user_id == recipient_user_id,
                ReferralReward.reward_type != "new_user",
                extract("year", ReferralReward.created_at) == now.year,
                extract("month", ReferralReward.created_at) == now.month,
            )
        )
        return max(0, min(bonus_days, max_days - int(used_days or 0)))

    async def _grant_reward(
        self,
        session: AsyncSession,
        referral: Referral,
        recipient_user_id: int,
        reward_type: str,
        bonus_days: int,
    ) -> ReferralReward | None:
        existing = await session.scalar(
            select(ReferralReward).where(
                ReferralReward.referral_id == referral.id,
                ReferralReward.reward_type == reward_type,
            )
        )
        if existing is not None:
            return None

        recipient = await session.get(User, recipient_user_id)
        if recipient is None:
            return None

        owns_vpn_service = self.vpn_service is None
        vpn_service = self.vpn_service or VpnService(settings=self.settings, session=session)
        try:
            await vpn_service.extend_active_subscription_by_days(
                recipient.telegram_id,
                bonus_days,
                reason=f"referral:{reward_type}",
            )
        except ValueError:
            return None
        finally:
            if owns_vpn_service:
                await vpn_service.close()
        reward = ReferralReward(
            referral_id=referral.id,
            recipient_user_id=recipient_user_id,
            reward_type=reward_type,
            bonus_days=bonus_days,
            reason=f"referral:{reward_type}",
        )
        session.add(reward)
        await session.flush()
        return reward
