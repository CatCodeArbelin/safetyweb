"""Idempotent paid payment finalization service."""

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.db.models import Payment, PaymentStatus, ReferralReward
from app.db.repositories.payments import PaymentRepository
from app.db.repositories.subscriptions import SubscriptionRepository
from app.db.session import async_session_maker
from app.services.benefit_service import BenefitService
from app.services.payment_service import MANUAL_PROVIDER_NAME
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
    status: str
    already_finalized: bool = False


class PaymentFinalizationService:
    """Finalize paid payments and run paid-access side effects once."""

    def __init__(self, settings: Settings, bot: "Bot | None" = None) -> None:
        self.settings = settings
        self.bot = bot

    async def finalize_paid_payment(
        self,
        *,
        provider: str,
        provider_payment_id: str,
        source: str,
    ) -> PaymentFinalizationResult:
        """Provision access and rewards for a paid payment idempotently."""
        async with async_session_maker() as session:
            payment_repository = PaymentRepository(session)
            payment = await payment_repository.get_by_provider_payment_id_for_update(
                provider,
                provider_payment_id,
            )
            if payment is None:
                msg = (
                    f"Payment {provider_payment_id!r} "
                    f"for provider {provider!r} was not found"
                )
                raise ValueError(msg)

            if payment.subscription_id is not None:
                await session.commit()
                await self._notify_user_once(payment, None, "already_finalized")
                return PaymentFinalizationResult(
                    payment=payment,
                    provision_result=None,
                    benefit_granted=False,
                    referral_rewards=[],
                    status="already_finalized",
                    already_finalized=True,
                )

            if payment.status != PaymentStatus.PAID:
                if source != "manual_confirm" or provider != MANUAL_PROVIDER_NAME:
                    msg = (
                        f"Payment {provider_payment_id!r} cannot be finalized "
                        f"from {payment.status} by {source!r}"
                    )
                    raise ValueError(msg)
                payment.status = PaymentStatus.PAID
                payment.paid_at = payment.paid_at or datetime.now(tz=UTC)

            months = payment.tariff_months or self._extract_months_from_provider_data(
                payment.provider_data,
                payment.id,
                provider_payment_id,
            )
            if not months:
                msg = f"Cannot determine tariff months for payment {provider_payment_id!r}"
                raise ValueError(msg)

            user = payment.user
            subscription = await SubscriptionRepository(session).get_by_last_payment_id(
                user.telegram_id,
                provider_payment_id,
            )
            if subscription is not None:
                payment.subscription_id = subscription.id
                await session.flush()
                await session.commit()
                provision_result = VpnService.provision_result_from_subscription(subscription)
                await self._notify_user_once(payment, provision_result, "attached_existing")
                return PaymentFinalizationResult(
                    payment=payment,
                    provision_result=provision_result,
                    benefit_granted=False,
                    referral_rewards=[],
                    status="attached_existing",
                )

            await session.commit()

        user_id = payment.user.telegram_id
        provision_result = await self._provision(provider_payment_id, user_id, months)

        async with async_session_maker() as session:
            payment_repository = PaymentRepository(session)
            payment = await payment_repository.get_by_provider_payment_id_for_update(
                provider,
                provider_payment_id,
            )
            if payment is None:
                msg = (
                    f"Payment {provider_payment_id!r} "
                    f"for provider {provider!r} was not found"
                )
                raise ValueError(msg)
            if payment.subscription_id is None:
                payment.subscription_id = provision_result.subscription_id
                await session.flush()
            await session.commit()

        benefit_granted = False
        referral_rewards: list[ReferralReward] = []
        await self._apply_pending_rewards(user_id)
        benefit_granted = await self._grant_early_buyer_discount(user_id)
        referral_rewards = await self._apply_first_payment_rewards(user_id, months)

        await self._notify_user_once(payment, provision_result, provision_result.action)

        return PaymentFinalizationResult(
            payment=payment,
            provision_result=provision_result,
            benefit_granted=benefit_granted,
            referral_rewards=referral_rewards,
            status=provision_result.action,
        )

    async def _provision(
        self,
        provider_payment_id: str,
        user_id: int,
        months: int,
    ) -> ProvisionResult:
        vpn_service = VpnService(settings=self.settings)
        try:
            return await vpn_service.provision_or_extend_client(
                telegram_id=user_id,
                months=months,
                source_payment_id=provider_payment_id,
            )
        finally:
            await vpn_service.close()

    @classmethod
    def _extract_months_from_provider_data(
        cls,
        provider_data: dict[str, Any] | None,
        payment_id: int,
        provider_payment_id: str,
    ) -> int | None:
        if not provider_data:
            return None
        for payload in cls._iter_payload_candidates(provider_data):
            if not isinstance(payload, dict):
                continue
            internal_payment_id = payload.get("internalPaymentId")
            external_payment_id = payload.get("paymentId")
            if internal_payment_id is not None and str(internal_payment_id) != str(payment_id):
                continue
            if external_payment_id is not None and str(external_payment_id) != provider_payment_id:
                continue
            months = payload.get("months")
            if months in (None, ""):
                continue
            try:
                parsed_months = int(months)
            except (TypeError, ValueError):
                continue
            if parsed_months > 0:
                return parsed_months
        return None

    @classmethod
    def _iter_payload_candidates(cls, value: Any):
        if isinstance(value, dict):
            payload = value.get("payload")
            if isinstance(payload, dict):
                yield payload
            if "internalPaymentId" in value or "paymentId" in value or "months" in value:
                yield value
            for nested in value.values():
                yield from cls._iter_payload_candidates(nested)
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_payload_candidates(item)

    async def _notify_user_once(
        self,
        payment: Payment,
        provision_result: ProvisionResult | None,
        status: str,
    ) -> None:
        if self.bot is None or status == "already_finalized":
            return
        provider_data = dict(payment.provider_data or {})
        if provider_data.get("user_notified_at"):
            return
        if provision_result is None:
            return

        text = self._build_user_notification(provision_result, status)
        await self.bot.send_message(payment.user.telegram_id, text)

        async with async_session_maker() as session:
            stored_payment = await PaymentRepository(
                session
            ).get_by_provider_payment_id_for_update(
                payment.provider,
                payment.provider_payment_id or "",
            )
            if stored_payment is None:
                return
            stored_data = dict(stored_payment.provider_data or {})
            if stored_data.get("user_notified_at"):
                return
            stored_data["user_notified_at"] = datetime.now(tz=UTC).isoformat()
            stored_payment.provider_data = stored_data
            payment.provider_data = stored_data
            await session.commit()

    @staticmethod
    def _build_user_notification(
        provision_result: ProvisionResult,
        status: str,
    ) -> str:
        action_text = {
            "created": "Доступ создан",
            "extended": "Подписка продлена",
            "attached_existing": "Подписка уже была активирована",
        }.get(status, "Подписка активирована")
        expires_at = provision_result.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            "Оплата подтверждена ✅\n\n"
            f"{action_text}.\n"
            f"Действует до: <code>{escape(expires_at)}</code>\n\n"
            "Ваша ссылка для защищённого соединения:\n"
            f"<code>{escape(provision_result.connection_link)}</code>"
        )

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
