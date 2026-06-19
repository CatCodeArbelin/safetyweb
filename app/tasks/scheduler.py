"""Background task scheduler."""

from datetime import UTC, datetime, timedelta
from typing import Final

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import (
    PaymentStatus,
    Subscription,
    SubscriptionNotification,
    SubscriptionNotificationType,
    SubscriptionStatus,
)
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.payment_service import PLATEGA_PROVIDER_NAME
from app.services.platega_client import PlategaClient
from app.services.platega_webhook_service import PlategaWebhookService
from app.services.xui_client import XuiClient

EXPIRATION_JOB_ID: Final = "expire_subscriptions"
EXPIRATION_REMINDER_JOB_ID: Final = "subscription_expiration_reminders"
PLATEGA_RECONCILE_JOB_ID: Final = "platega_reconcile_payments"
PLATEGA_WEBHOOK_RETRY_JOB_ID: Final = "platega_retry_webhooks"
REMINDER_WINDOWS: Final[tuple[tuple[int, SubscriptionNotificationType], ...]] = (
    (3, SubscriptionNotificationType.EXPIRES_IN_3_DAYS),
    (1, SubscriptionNotificationType.EXPIRES_IN_1_DAY),
    (0, SubscriptionNotificationType.EXPIRES_TODAY),
)


def create_scheduler(bot: Bot | None = None, settings: Settings | None = None) -> AsyncIOScheduler:
    """Create an application scheduler instance and register periodic jobs."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    if bot is None:
        return scheduler

    app_settings = settings or Settings()
    scheduler.add_job(
        expire_subscriptions,
        "interval",
        hours=1,
        id=EXPIRATION_JOB_ID,
        replace_existing=True,
        kwargs={"bot": bot, "settings": app_settings},
    )
    scheduler.add_job(
        send_expiration_reminders,
        "interval",
        hours=1,
        id=EXPIRATION_REMINDER_JOB_ID,
        replace_existing=True,
        kwargs={"bot": bot},
    )
    if app_settings.payment_provider == "platega" and not app_settings.test_mode:
        scheduler.add_job(
            reconcile_platega_payments,
            "interval",
            seconds=app_settings.platega_reconcile_interval_seconds,
            id=PLATEGA_RECONCILE_JOB_ID,
            replace_existing=True,
            kwargs={"bot": bot, "settings": app_settings},
        )
        scheduler.add_job(
            process_pending_payment_webhooks,
            "interval",
            seconds=app_settings.platega_webhook_retry_interval_seconds,
            id=PLATEGA_WEBHOOK_RETRY_JOB_ID,
            replace_existing=True,
            kwargs={"bot": bot, "settings": app_settings},
        )
    return scheduler


async def process_pending_payment_webhooks(
    bot: Bot, settings: Settings | None = None
) -> None:
    """Retry pending or failed Platega webhook events."""
    app_settings = settings or Settings()
    async with async_session_maker() as session:
        repository = PaymentRepository(session)
        events = await repository.get_unprocessed_webhook_events(provider=PLATEGA_PROVIDER_NAME)

    for event in events:
        await PlategaWebhookService(settings=app_settings, bot=bot).process_event(event.id)


async def reconcile_platega_payments(bot: Bot, settings: Settings | None = None) -> None:
    """Reconcile pending Platega payments against provider transaction status."""
    app_settings = settings or Settings()
    if app_settings.payment_provider != "platega" or app_settings.test_mode:
        return

    now = datetime.now(tz=UTC)
    async with async_session_maker() as session:
        repository = PaymentRepository(session)
        expired_payments = await repository.get_expired_pending_platega(now)
        for payment in expired_payments:
            payment.status = PaymentStatus.EXPIRED
            payment.status_reason = "expired_locally"
        await session.commit()

        pending_payments = await repository.get_pending_by_provider(PLATEGA_PROVIDER_NAME)

    if not pending_payments:
        return

    client = PlategaClient(settings=app_settings)
    service = PlategaWebhookService(settings=app_settings, bot=bot, client=client)
    try:
        for payment in pending_payments:
            if not payment.provider_payment_id:
                continue
            transaction = await client.get_transaction(payment.provider_payment_id)
            status = service._extract_transaction_status(transaction)
            await service.process_transaction_status(
                payment.provider_payment_id,
                status,
                months=payment.tariff_months,
                transaction=transaction,
            )
    finally:
        await client.close()


async def expire_subscriptions(bot: Bot, settings: Settings | None = None) -> None:
    """Disable or delete expired active protected access clients and notify users once."""
    app_settings = settings or Settings()
    now = datetime.now(tz=UTC)
    xui_client = XuiClient(settings=app_settings)
    try:
        async with async_session_maker() as session:
            subscriptions = await _expired_active_subscriptions(session, now)
            for subscription in subscriptions:
                await _deprovision_client(subscription, xui_client, app_settings)
                subscription.status = SubscriptionStatus.EXPIRED
                subscription.disabled_at = now
                await _create_notification_event(
                    session,
                    subscription=subscription,
                    notification_type=SubscriptionNotificationType.EXPIRED,
                )
                await session.commit()
                await _safe_send_message(
                    bot,
                    subscription.user.telegram_id,
                    _expiration_text(subscription.expires_at),
                )
    finally:
        await xui_client.close()


async def send_expiration_reminders(bot: Bot) -> None:
    """Send subscription expiration reminders for 3 days, 1 day, and expiration day."""
    now = datetime.now(tz=UTC)
    async with async_session_maker() as session:
        for days_before, notification_type in REMINDER_WINDOWS:
            window_start = now + timedelta(days=days_before)
            window_end = window_start + timedelta(hours=1)
            subscriptions = await _subscriptions_expiring_between(
                session,
                window_start=window_start,
                window_end=window_end,
            )
            for subscription in subscriptions:
                created = await _create_notification_event(
                    session,
                    subscription=subscription,
                    notification_type=notification_type,
                )
                if not created:
                    continue

                await session.commit()
                await _safe_send_message(
                    bot,
                    subscription.user.telegram_id,
                    _reminder_text(days_before, subscription.expires_at),
                )


async def _expired_active_subscriptions(
    session: AsyncSession,
    now: datetime,
) -> list[Subscription]:
    result = await session.scalars(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at <= now,
        )
        .order_by(Subscription.expires_at)
    )
    return list(result)


async def _subscriptions_expiring_between(
    session: AsyncSession,
    window_start: datetime,
    window_end: datetime,
) -> list[Subscription]:
    result = await session.scalars(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > window_start,
            Subscription.expires_at <= window_end,
        )
        .order_by(Subscription.expires_at)
    )
    return list(result)


async def _deprovision_client(
    subscription: Subscription,
    xui_client: XuiClient,
    settings: Settings,
) -> None:
    if settings.xui_expired_client_policy == "delete":
        await xui_client.delete_client(subscription.inbound_id, subscription.xui_client_id)
        return

    client_payload = dict((subscription.vpn_config or {}).get("client") or {})
    client_payload["id"] = subscription.xui_client_id
    client_payload["email"] = subscription.xui_email
    client_payload["enable"] = False
    await xui_client.update_client(
        subscription.inbound_id,
        subscription.xui_client_id,
        {"clients": [client_payload]},
        enable=False,
    )


async def _create_notification_event(
    session: AsyncSession,
    subscription: Subscription,
    notification_type: SubscriptionNotificationType,
) -> bool:
    statement = (
        insert(SubscriptionNotification)
        .values(
            subscription_id=subscription.id,
            notification_type=notification_type,
            period_expires_at=subscription.expires_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "subscription_id",
                "notification_type",
                "period_expires_at",
            ],
        )
    )
    result = await session.execute(statement)
    return bool(result.rowcount)


async def _safe_send_message(bot: Bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(telegram_id, text)
    except Exception:
        # Delivery failures must not prevent subscription state updates.
        return


def _reminder_text(days_before: int, expires_at: datetime) -> str:
    expires_text = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    if days_before == 0:
        return (
            "Срок действия вашей индивидуальной подписки ЛадНет истекает "
            f"сегодня ({expires_text})."
        )
    return (
        "Срок действия вашей индивидуальной подписки ЛадНет истекает "
        f"через {days_before} дн. ({expires_text})."
    )


def _expiration_text(expires_at: datetime) -> str:
    expires_text = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    return (
        "Срок действия вашей индивидуальной подписки ЛадНет "
        f"истёк ({expires_text})."
    )
