"""Idempotent paid payment finalization service."""

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING

from app.config import Settings
from app.db.models import Payment, PaymentStatus, ReferralReward
from app.db.repositories import SubscriptionRepository
from app.db.session import async_session_maker
from app.services.benefit_service import BenefitService
from app.services.payment_service import MANUAL_PROVIDER_NAME, PaymentService
from app.services.referral_service import ReferralService
from app.services.vpn_service import ProvisionResult, VpnService

if TYPE_CHECKING:
    from aiogram import Bot


@dataclass(frozen=True)
class PaymentFinalizationResult:
    """Result of an idempotent paid payment finalization."""

    payment: Payment
    provision_result: ProvisionResult | None
    benefit_granted: bool
    referral_rewards: list[ReferralReward]
    already_finalized: bool = False


class PaymentFinalizationService:
    """Finalize paid payments and run paid-access side effects once."""

    def __init__(self, settings: Settings, bot: "Bot | None" = None) -> None:
        self.settings = settings
        self.bot = bot
        self.payment_service = PaymentService(settings=settings)

    async def finalize_paid_payment(
        self,
        provider_payment_id: str,
        months: int,
    ) -> PaymentFinalizationResult:
        """Provision access and rewards for a paid payment idempotently."""
        provider = await self.payment_service.get_provider_for_payment(provider_payment_id)
        payment = await self.payment_service.get_payment_by_provider_payment_id(
            provider,
            provider_payment_id,
        )
        if payment.status == PaymentStatus.PAID and payment.subscription_id is not None:
            return PaymentFinalizationResult(
                payment=payment,
                provision_result=None,
                benefit_granted=False,
                referral_rewards=[],
                already_finalized=True,
            )

        if payment.status in {PaymentStatus.REFUNDED, PaymentStatus.FAILED}:
            msg = f"Payment {provider_payment_id!r} cannot be finalized from {payment.status}"
            raise ValueError(msg)

        if payment.status != PaymentStatus.PAID:
            if provider == MANUAL_PROVIDER_NAME:
                payment = await self.payment_service.confirm_manual_payment(provider_payment_id)
            else:
                payment = await self.payment_service.set_status(
                    provider_payment_id,
                    PaymentStatus.PAID,
                    provider=provider,
                    paid_at=datetime.now(tz=UTC),
                )

        user_id = payment.user.telegram_id
        provision_result = await self._provision_once(provider_payment_id, user_id, months)
        payment = await self.payment_service.attach_subscription(
            provider_payment_id,
            provision_result.subscription_id,
            provider=provider,
        )

        await self._apply_pending_rewards(user_id)
        benefit_granted = await self._grant_early_buyer_discount(user_id)
        referral_rewards = await self._apply_first_payment_rewards(user_id, months)

        return PaymentFinalizationResult(
            payment=payment,
            provision_result=provision_result,
            benefit_granted=benefit_granted,
            referral_rewards=referral_rewards,
        )

    async def _provision_once(
        self,
        provider_payment_id: str,
        user_id: int,
        months: int,
    ) -> ProvisionResult:
        async with async_session_maker() as session:
            subscription = await SubscriptionRepository(session).get_by_last_payment_id(
                user_id,
                provider_payment_id,
            )
            if subscription is not None:
                return VpnService.provision_result_from_subscription(subscription)

        vpn_service = VpnService(settings=self.settings)
        try:
            return await vpn_service.provision_or_extend_client(
                telegram_id=user_id,
                months=months,
                source_payment_id=provider_payment_id,
            )
        finally:
            await vpn_service.close()

    async def _apply_pending_rewards(self, user_id: int) -> None:
        try:
            await ReferralService(settings=self.settings).apply_pending_rewards(user_id)
        except Exception as error:
            await self._notify_admins(
                "Ошибка применения отложенных реферальных бонусов\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Ошибка: <code>{escape(str(error))}</code>",
            )

    async def _grant_early_buyer_discount(self, user_id: int) -> bool:
        try:
            return await BenefitService(
                settings=self.settings
            ).grant_early_buyer_discount_if_eligible(user_id)
        except Exception as error:
            await self._notify_admins(
                "Ошибка выдачи скидки раннего покупателя\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Ошибка: <code>{escape(str(error))}</code>",
            )
            return False

    async def _apply_first_payment_rewards(
        self,
        user_id: int,
        months: int,
    ) -> list[ReferralReward]:
        if self.settings.test_mode and not self.settings.test_mode_referral_rewards_enabled:
            return []
        try:
            return await ReferralService(settings=self.settings).apply_first_payment_rewards(
                user_id,
                months,
            )
        except Exception as error:
            await self._notify_admins(
                "Ошибка начисления реферального бонуса\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Месяцев: <code>{months}</code>\n"
                f"Ошибка: <code>{escape(str(error))}</code>",
            )
            return []

    async def _notify_admins(self, text: str) -> None:
        if self.bot is None:
            return
        for admin_id in self.settings.admin_ids:
            await self.bot.send_message(admin_id, text)
