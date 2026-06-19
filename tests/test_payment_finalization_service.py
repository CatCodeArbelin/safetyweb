import os
from datetime import UTC, datetime

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.services.payment_finalization_service import PaymentFinalizationService
from app.services.vpn_service import ProvisionResult


def _provision_result() -> ProvisionResult:
    return ProvisionResult(
        connection_link="vless://protected-link",
        expires_at=datetime(2026, 7, 19, 12, 30, tzinfo=UTC),
        action="created",
        subscription_id=42,
    )


def test_build_user_notification_for_created_includes_tariff_expiry_and_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "created",
        3,
    )

    assert text == (
        "Оплата подтверждена ✅\n\n"
        "Доступ создан на тариф <b>3 месяца</b>.\n"
        "Действует до: <code>2026-07-19 12:30 UTC</code>\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )


def test_build_user_notification_for_extended_mentions_existing_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "extended",
        12,
    )

    assert text == (
        "Оплата подтверждена ✅\n\n"
        "Подписка продлена на тариф <b>12 месяцев</b>.\n"
        "Ссылка для защищённого соединения остаётся прежней.\n"
        "Действует до: <code>2026-07-19 12:30 UTC</code>\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )


def test_build_user_notification_for_attached_existing_only_mentions_processed_payment_and_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "attached_existing",
        1,
    )

    assert text == (
        "Оплата уже была обработана ✅\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )
