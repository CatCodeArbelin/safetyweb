"""Application entry point."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from typing import Any, Awaitable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
import uvicorn
from sqlalchemy.exc import IntegrityError

from aiogram.types import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.bot_commands import (
    BTN_BUY_ACCESS,
    BTN_CUSTOM_SERVERS,
    BTN_DOCUMENTS,
    BTN_INSTRUCTION,
    BTN_INVITE_FRIEND,
    BTN_MY_LINK,
    BTN_MY_SUBSCRIPTION,
    BTN_PROFILE,
    BTN_SUPPORT,
    admin_telegram_bot_commands,
    render_admin_help_text,
    user_telegram_bot_commands,
)
from app.config import Settings, XuiNodeConfig
from app.db.models import PaymentStatus
from app.http_app import create_app
from app.db.repositories import (
    CustomerBenefitRepository,
    ReferralRewardRepository,
    SubscriptionRepository,
    UserRepository,
)
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.benefit_service import BenefitService
from app.services.payment_finalization_service import PaymentFinalizationService
from app.services.payment_service import (
    PLATEGA_PROVIDER_NAME,
    PaymentCreateResult,
    PaymentService,
)
from app.services.platega_client import PlategaClient
from app.services.node_selector_service import (
    NoAvailableNodeError,
    NodeCapacityInfo,
    NodeSelectorService,
)
from app.services.platega_webhook_service import PlategaWebhookService
from app.services.referral_service import ReferralService
from app.services.stats_service import AdminStats, StatsService
from app.services.subscription_service import SubscriptionService
from app.services.vpn_service import (
    NoActiveSubscriptionError,
    ProvisionResult,
    VpnService,
)
from app.services.xui_client import XuiClient, XuiError
from app.services.xui_health_service import XuiHealthResult, check_node_health
from app.tasks.scheduler import create_scheduler
from app.utils.sanitize import sanitize_exception, sanitize_string

TARIFFS = {
    1: "1 месяц",
    3: "3 месяца",
    6: "6 месяцев",
    12: "12 месяцев",
}

TARIFF_PRICES = {
    1: 249,
    3: 649,
    6: 1190,
    12: 2190,
}

PAYMENT_CURRENCY = "RUB"
PROVISIONING_USER_ERROR = (
    "Не удалось автоматически выдать или продлить доступ из-за технической ошибки..."
)

TARIFF_EMOJIS = {1: "🔹", 3: "🔷", 6: "💎", 12: "👑"}

logger = logging.getLogger(__name__)

router = Router(name="safetyweb")


class PurchaseState(StatesGroup):
    """FSM states for manual payment requests."""

    choosing_tariff = State()
    choosing_payment_method = State()
    waiting_payment = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent user main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY_ACCESS), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_INVITE_FRIEND), KeyboardButton(text=BTN_CUSTOM_SERVERS)],
            [KeyboardButton(text=BTN_INSTRUCTION), KeyboardButton(text=BTN_SUPPORT)],
            [KeyboardButton(text=BTN_DOCUMENTS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def apply_discount_to_price(base_price: int, discount_percent: int) -> int:
    """Apply a percentage discount to an integer ruble price."""
    price = Decimal(str(base_price))
    multiplier = Decimal(100 - discount_percent) / Decimal(100)
    return int((price * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_tariff_price(base_price: int, discount_percent: int) -> str:
    """Format tariff price with an optional discount comparison."""
    final_price = apply_discount_to_price(base_price, discount_percent)
    if discount_percent <= 0 or final_price >= base_price:
        return f"{base_price} ₽"
    return f"{final_price} ₽ вместо {base_price} ₽"


def tariff_keyboard(discount_percent: int = 0) -> InlineKeyboardMarkup:
    """Build inline keyboard with available protected access tariffs."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{TARIFF_EMOJIS.get(months, '🔹')} {label} — "
                        f"{format_tariff_price(TARIFF_PRICES[months], discount_percent)}"
                    ),
                    callback_data=f"buy:{months}",
                )
            ]
            for months, label in TARIFFS.items()
        ]
    )


def trial_access_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for one-time trial access."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧪 Получить тестовый доступ", callback_data="trial_access"
                )
            ]
        ]
    )


def profile_keyboard(
    has_active_subscription: bool, trial_available: bool
) -> InlineKeyboardMarkup:
    """Build inline keyboard for profile quick actions."""
    keyboard: list[list[InlineKeyboardButton]] = []
    if trial_available:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🧪 Получить тестовый доступ", callback_data="trial_access"
                )
            ]
        )
    if has_active_subscription:
        keyboard.append(
            [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="renew")]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=BTN_MY_SUBSCRIPTION, callback_data="profile:subscription"
                )
            ],
            [InlineKeyboardButton(text=BTN_MY_LINK, callback_data="profile:link")],
            [
                InlineKeyboardButton(
                    text=BTN_DOCUMENTS, callback_data="profile:documents"
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def renew_subscription_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for active subscription renewal."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="renew")]
        ]
    )


def docs_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Build inline keyboard with legal documents and support actions."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Политика конфиденциальности",
                    url=settings.privacy_policy_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 Пользовательское соглашение",
                    url=settings.terms_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Тарифы и условия оплаты",
                    url=settings.tariffs_url,
                )
            ],
            [InlineKeyboardButton(text="💬 Поддержка", callback_data="docs:support")],
        ]
    )


def custom_servers_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for custom server request section."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍👩‍👧 Семейный доступ",
                    callback_data="custom_servers:family",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏢 Корпоративным клиентам",
                    callback_data="custom_servers:business",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Написать в поддержку",
                    callback_data="custom_servers:support",
                )
            ],
        ]
    )


def custom_servers_request_keyboard(request_callback: str, request_text: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a specific custom server request type."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=request_text, callback_data=request_callback)],
            [
                InlineKeyboardButton(
                    text="💬 Написать в поддержку",
                    callback_data="custom_servers:support",
                )
            ],
        ]
    )


def custom_servers_intro_text() -> str:
    """Return the custom server section intro text."""
    return (
        "🖥 Индивидуальные серверы\n\n"
        "Этот раздел для клиентов, которым нужен отдельный серверный ресурс "
        "под семью, команду или организацию.\n\n"
        "Варианты:\n\n"
        "👨‍👩‍👧 Семейный доступ\n"
        "Для дома, семьи и нескольких личных устройств.\n\n"
        "🏢 Корпоративный доступ\n"
        "Для команды, офиса, сотрудников или нескольких рабочих мест.\n\n"
        "Выберите подходящий вариант ниже — бот отправит заявку поддержке."
    )


def custom_servers_disabled_text() -> str:
    """Return text for disabled custom server section."""
    return "Раздел временно недоступен. Пожалуйста, обратитесь в поддержку."


def format_capacity_snapshot(snapshot: list[NodeCapacityInfo]) -> str:
    """Format an X-UI node capacity snapshot for admin alerts."""
    if not snapshot:
        return "Ноды: <code>не настроены</code>"

    lines = ["Снимок capacity нод:"]
    for item in snapshot:
        limit = (
            "∞"
            if item.max_active_subscriptions is None
            else str(item.max_active_subscriptions)
        )
        status = "enabled" if item.enabled else "disabled"
        capacity = "available" if item.has_capacity else "full"
        title = item.name
        lines.append(
            f"- <code>{escape(item.key)}</code> ({escape(title)}): "
            f"{status}, {capacity}, active=<code>{item.active_count}</code>, "
            f"pending=<code>{item.pending_reservations}</code>, "
            f"occupied=<code>{item.occupied_count}</code>, "
            f"free=<code>{format_free_slots(item)}</code>, limit=<code>{limit}</code>"
        )
    return "\n".join(lines)


def format_provision_expires(result: ProvisionResult) -> str:
    """Format provision expiry timestamp for bot messages."""
    return escape(result.expires_at.strftime("%Y-%m-%d %H:%M UTC"))


async def notify_admins(bot: Bot, settings: Settings, text: str) -> None:
    """Send a notification message to every configured administrator."""
    for admin_id in settings.admin_ids:
        await bot.send_message(admin_id, text)


async def is_trial_available(telegram_id: int, settings: Settings) -> bool:
    """Return whether a user can receive one-time trial access."""
    if not settings.trial_access_enabled:
        return False

    async with async_session_maker() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None or user.trial_used_at is not None:
            return False

    active_subscription = await SubscriptionService().get_active_subscription(
        telegram_id
    )
    return active_subscription is None


def format_optional_datetime(value: datetime | None) -> str:
    """Format an optional datetime for admin diagnostics."""
    if value is None:
        return "—"
    return escape(value.strftime("%Y-%m-%d %H:%M:%S %Z"))


def format_yes_no(value: bool) -> str:
    """Format a boolean as a neutral yes/no value."""
    return "yes" if value else "no"


def format_access_error_alert(
    error: Exception,
    *,
    user_id: int | None = None,
    months: int | None = None,
    provider_payment_id: str | None = None,
) -> str:
    """Format an escaped access provisioning error alert for administrators."""
    lines = ["🚨 Ошибка выдачи доступа"]
    if user_id is not None:
        lines.append(f"Пользователь: <code>{user_id}</code>")
    if months is not None:
        tariff = TARIFFS.get(months, f"{months} мес.")
        lines.append(f"Тариф: <b>{escape(tariff)}</b>")
        lines.append(f"Месяцев: <code>{months}</code>")
    if provider_payment_id is not None:
        lines.append(f"Платёж провайдера: <code>{escape(provider_payment_id)}</code>")
    sanitized_error = sanitize_exception(error, limit=500)
    lines.append(f"Ошибка: <code>{escape(sanitized_error)}</code>")
    return "\n".join(lines)


def support_contact_text(settings: Settings) -> str:
    """Format support contact information for bot messages."""
    lines = ["Поддержка:", escape(settings.support_username)]
    if settings.support_second_username:
        lines.append(escape(settings.support_second_username))
    if settings.support_email:
        lines.extend(["", "Email:", escape(settings.support_email)])
    return "\n".join(lines)


def format_tariffs() -> str:
    """Format tariffs for document menu callbacks."""
    lines = ["Доступные тарифы:"]
    for months, label in TARIFFS.items():
        price = TARIFF_PRICES[months]
        emoji = TARIFF_EMOJIS.get(months, "🔹")
        price_text = f"{price} ₽" if price else "уточняйте у поддержки"
        lines.append(f"{emoji} {label} — {price_text}")
    return "\n".join(lines)


def format_price(value: Decimal | int | str) -> str:
    """Format ruble prices for Telegram messages."""
    price = Decimal(str(value))
    if price == price.to_integral_value():
        return f"{int(price)} ₽"
    return f"{price:.2f} ₽"


def format_discount_summary(
    base_price: Decimal | int | str,
    discount_percent: int,
    final_price: Decimal | int | str,
) -> str:
    """Format base price, discount, and final price for payment messages."""
    return (
        f"Базовая цена: <code>{format_price(base_price)}</code>\n"
        f"Скидка: <code>{discount_percent}%</code>\n"
        f"Итого к оплате: <code>{format_price(final_price)}</code>"
    )


def format_admin_stats(stats: AdminStats, early_buyer_limit: int) -> str:
    """Format aggregate admin statistics for Telegram."""
    return (
        "📊 Статистика ЛадНет\n\n"
        f"👥 Пользователей всего: <code>{stats.total_users_count}</code>\n"
        f"✅ Активных подписок: <code>{stats.active_subscriptions_count}</code>\n"
        f"💳 Оплаченных платежей за текущий месяц: "
        f"<code>{stats.paid_payments_count_current_month}</code>\n"
        f"💰 Сумма оплат за текущий месяц: "
        f"<code>{format_price(stats.paid_payments_sum_current_month)}</code>\n"
        f"🧾 Оплаченных платежей за всё время: "
        f"<code>{stats.paid_payments_count_all_time}</code>\n"
        f"🏦 Сумма оплат за всё время: "
        f"<code>{format_price(stats.paid_payments_sum_all_time)}</code>\n"
        f"🎁 Активных скидок раннего покупателя: "
        f"<code>{stats.active_early_buyer_benefits_count}/{early_buyer_limit}</code>\n"
        f"🔗 Рефералов всего: <code>{stats.referrals_count}</code>\n"
        f"🏆 Вознаграждённых рефералов: <code>{stats.rewarded_referrals_count}</code>"
    )


def format_node_limit(limit: int | None) -> str:
    """Format an optional node capacity limit for admin diagnostics."""
    return "—" if limit is None else escape(str(limit))


def format_capacity_limit(limit: int | None) -> str:
    """Format an effective node capacity limit for admin diagnostics."""
    return "∞" if limit is None else escape(str(limit))


def format_free_slots(capacity: NodeCapacityInfo) -> str:
    """Format remaining slots without recalculating capacity details."""
    if not capacity.enabled:
        return "—"
    if capacity.free_slots is None:
        return "∞"
    return escape(str(capacity.free_slots))


def format_node_status(enabled: bool) -> str:
    """Format node enabled status for admin diagnostics."""
    return "enabled" if enabled else "disabled"


def format_node_public_host(public_host: str | None) -> str:
    """Format an optional public node host for admin diagnostics."""
    return escape(public_host or "—")


def get_vpn_config_diagnostic_value(vpn_config: dict[str, Any], key: str) -> str:
    """Return a sanitized non-secret vpn_config diagnostic value."""
    value = vpn_config.get(key)
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return format_yes_no(value)
    return sanitize_string(str(value))[:500]


def get_subscription_node_detail(
    subscription: object | None,
    vpn_config_key: str,
    subscription_attr: str | None = None,
) -> str:
    """Return a non-secret subscription node detail for admin diagnostics."""
    if subscription is None:
        return "—"

    if subscription_attr is not None:
        value = getattr(subscription, subscription_attr, None)
        if value:
            return str(value)

    vpn_config = getattr(subscription, "vpn_config", None) or {}
    value = vpn_config.get(vpn_config_key)
    return str(value) if value else "—"


def format_node_label(node: XuiNodeConfig) -> str:
    """Format a node label without exposing secret node settings."""
    return escape(node.name or node.key)


def format_node_inbound_ids(inbound_ids: list[int]) -> str:
    """Format configured inbound IDs for admin diagnostics."""
    return ", ".join(escape(str(inbound_id)) for inbound_id in inbound_ids) or "—"


async def get_node_health_status(
    settings: Settings,
    node: XuiNodeConfig,
) -> XuiHealthResult | str:
    """Return a best-effort, non-secret health status for an X-UI node."""
    if not node.enabled:
        return "disabled"
    return await check_node_health(settings, node)


def format_node_health_lines(health: XuiHealthResult | str) -> list[str]:
    """Format safe X-UI health diagnostics for admin output."""
    if isinstance(health, str):
        return [f"Health: <code>{escape(health)}</code>"]
    lines = [
        f"Health: {'✅ healthy' if health.ok else '❌ unhealthy'}",
        f"Auth mode: <code>{escape(health.auth_mode)}</code>",
    ]
    if health.error_type:
        lines.append(f"Error: <code>{escape(health.error_type)}</code>")
    if health.hint:
        lines.append(f"Hint: {escape(health.hint)}")
    return lines


async def get_active_subscription_counts_by_node() -> dict[str, int]:
    """Return active subscription counts grouped by node key."""
    async with async_session_maker() as session:
        return await NodeSelectorService._get_active_subscription_counts(session)


def format_admin_payment_details(
    *,
    provider: str,
    provider_payment_id: str,
    local_status: str | None,
    amount: Decimal | int | str | None,
    currency: str | None,
    tariff_months: int | None,
    telegram_id: int | None,
    subscription_id: int | None,
    provider_expires_at: datetime | None,
    provider_status: str | None = None,
    header: str = "💳 Проверка платежа",
) -> str:
    """Format safe payment diagnostics for administrators."""
    amount_text = "—" if amount is None else str(amount)
    expires_text = (
        "—"
        if provider_expires_at is None
        else provider_expires_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    lines = [
        header,
        f"provider: <code>{escape(provider)}</code>",
        f"provider_payment_id: <code>{escape(provider_payment_id)}</code>",
        f"local status: <code>{escape(str(local_status or '—'))}</code>",
        f"amount/currency: <code>{escape(amount_text)} {escape(currency or '—')}</code>",
        f"tariff_months: <code>{escape(str(tariff_months or '—'))}</code>",
        f"user telegram id: <code>{escape(str(telegram_id or '—'))}</code>",
        f"subscription_id: <code>{escape(str(subscription_id or '—'))}</code>",
        f"provider_expires_at: <code>{escape(expires_text)}</code>",
    ]
    if provider_status is not None:
        lines.append(f"provider status: <code>{escape(provider_status)}</code>")
    return "\n".join(lines)


def platega_lookup_credentials_configured(settings: Settings) -> bool:
    """Return whether Platega lookup can be attempted with configured credentials."""
    return bool(settings.platega_merchant_id and settings.platega_api_key)


def platega_lookup_not_configured_message() -> str:
    """Return a safe admin message for unavailable Platega lookup."""
    return (
        "Локальный платеж не найден.\n"
        "Platega lookup не настроен: отсутствуют обязательные учетные данные."
    )


async def recover_orphan_platega_payment(
    provider_payment_id: str,
    transaction: dict,
    service: PlategaWebhookService,
) -> bool:
    """Try to create/attach a local Platega payment from provider metadata."""
    recovery_payload = service._extract_recovery_payload(transaction, None)
    if not recovery_payload:
        return False

    async with async_session_maker() as session:
        repository = PaymentRepository(session)
        payment = await service._recover_payment_by_internal_id(
            repository,
            provider_payment_id,
            recovery_payload,
            transaction,
        )
        if payment is None:
            payment = await service._create_recovery_payment(
                repository,
                provider_payment_id,
                recovery_payload,
                transaction,
            )
        await session.commit()
        return payment is not None


def platega_payment_methods_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Build inline keyboard for configured Platega payment methods."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=method.title,
                    callback_data=f"pay_method:{method_key}",
                )
            ]
            for method_key, method in settings.platega_payment_methods_json.items()
        ]
    )


def payment_request_keyboard(
    months: int, test_mode: bool = False
) -> InlineKeyboardMarkup:
    """Build inline keyboard for submitting a payment or test access request."""
    button_text = (
        "🧪 Получить тестовый доступ" if test_mode else "💳 Создать заявку на оплату"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"pay_request:{months}",
                )
            ]
        ]
    )


def confirm_payment_keyboard(
    provider_payment_id: str, months: int
) -> InlineKeyboardMarkup:
    """Build admin confirmation keyboard for a manual payment request."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=f"confirm:{provider_payment_id}:{months}",
                )
            ]
        ]
    )


def payment_url_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with provider payment URL."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)]
        ]
    )


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Handle /start and show the main menu."""
    await state.clear()

    if command.args == "pay_return":
        await message.answer(
            "Спасибо! Если оплата прошла успешно, доступ будет выдан автоматически "
            "в течение нескольких минут.\n\n"
            "Статус подписки можно проверить в разделе «Мой профиль».",
            reply_markup=main_menu_keyboard(),
        )
        return

    if command.args == "pay_failed":
        await message.answer(
            "Оплата не была завершена.\n\n"
            "Вы можете вернуться к выбору тарифа и попробовать ещё раз.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if (
        message.from_user is not None
        and command.args
        and command.args.startswith("ref_")
    ):
        await ReferralService(settings=settings).register_referral(
            message.from_user.id, command.args.removeprefix("ref_")
        )

    telegram_id = message.from_user.id if message.from_user is not None else None
    benefit_granted = False
    trial_available = False
    if telegram_id is not None:
        try:
            async with async_session_maker() as session:
                try:
                    user, user_created = await UserRepository(
                        session
                    ).get_or_create_from_telegram(message.from_user)
                except IntegrityError:
                    await session.rollback()
                    user, user_created = await UserRepository(
                        session
                    ).get_or_create_from_telegram(message.from_user)
                await session.commit()
        except Exception:
            logger.exception("Failed to open main menu for Telegram user %s", telegram_id)
            await message.answer(
                "Не удалось открыть главное меню из-за временной технической ошибки.\n\n"
                "Пожалуйста, попробуйте ещё раз через несколько секунд."
            )
            return

        if user_created:
            benefit_granted = await BenefitService(
                settings=settings
            ).grant_early_buyer_discount_on_start_if_eligible(telegram_id)
        trial_available = await is_trial_available(telegram_id, settings)

    welcome_text = (
        "🌏 ЛадНет | Безопасный Интернет\n\n"
        "Цифровой сервис защищённого сетевого доступа.\n"
        "Нажмите «🛒 Оформить / продлить», проверьте подписку или обратитесь в поддержку."
    )
    if benefit_granted:
        welcome_text += (
            "\n\n🎁 Вам доступна постоянная скидка раннего пользователя.\n"
            "Она уже применена к тарифам."
        )

    if trial_available:
        await message.answer(
            welcome_text,
            reply_markup=trial_access_keyboard(),
        )
        await message.answer(
            "Выберите действие в меню.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.answer(
        welcome_text,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message, settings: Settings) -> None:
    """Show help and support contacts."""
    await message.answer(
        "Помощь по сервису ЛадНет:\n\n"
        "• 🛒 Оформить / продлить — выбрать тариф и создать заявку на оплату.\n"
        "• 👤 Мой профиль — подписка, ссылка, документы и продление.\n"
        "• 🎁 Пригласить друга — получить реферальную ссылку для приглашения.\n"
        "• 🖥 Индивидуальные серверы — заявки на семейный или корпоративный индивидуальный доступ.\n"
        "• 📲 Инструкция — открыть краткую инструкцию по настройке.\n"
        "• 💬 Поддержка — посмотреть контакты поддержки.\n\n"
        f"{support_contact_text(settings)}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("docs"))
async def docs_command(message: Message, settings: Settings) -> None:
    """Show service documents."""
    await message.answer(
        "Документы и полезная информация сервиса ЛадНет:",
        reply_markup=docs_keyboard(settings),
    )


@router.message(Command("tariffs"))
async def tariffs_command(message: Message, settings: Settings) -> None:
    """Show current tariffs and payment terms link."""
    await message.answer(
        "Актуальные тарифы ЛадНет:\n\n"
        f"{format_tariffs()}\n\n"
        "Полные условия оплаты доступны по ссылке ниже.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Тарифы и условия оплаты",
                        url=settings.tariffs_url,
                    )
                ]
            ]
        ),
    )


def admin_help_text() -> str:
    """Return the complete administrator help text."""
    return render_admin_help_text()


@router.message(Command("ahelp"))
async def admin_help_command(message: Message, settings: Settings) -> None:
    """Show the complete administrator command reference."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    await message.answer(admin_help_text())


@router.message(Command("stats"))
async def stats_command(message: Message, settings: Settings) -> None:
    """Show aggregate service statistics to administrators only."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    stats = await StatsService().get_admin_stats()
    await message.answer(format_admin_stats(stats, settings.early_buyer_limit))


@router.message(Command("nodes"))
async def nodes_command(message: Message, settings: Settings) -> None:
    """Show safe configured node summary to administrators only."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    snapshot = await NodeSelectorService(settings=settings).get_capacity_snapshot()
    lines = ["🧩 Ноды цифрового доступа"]
    for capacity in snapshot:
        node = settings.get_xui_node(capacity.key)
        lines.extend(
            [
                "",
                f"key: <code>{escape(node.key)}</code>",
                f"label: <code>{format_node_label(node)}</code>",
                f"Статус: <code>{format_node_status(capacity.enabled)}</code>",
                f"Active: <code>{capacity.active_count}</code>",
                f"Pending reservations: <code>{capacity.pending_reservations}</code>",
                f"Occupied: <code>{capacity.occupied_count}</code>",
                f"Свободно: <code>{format_free_slots(capacity)}</code>",
                f"Public host: <code>{format_node_public_host(node.xui_public_host)}</code>",
            ]
        )

    await message.answer("\n".join(lines))


@router.message(Command("node"))
async def node_command(
    message: Message,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Show safe details for one configured node to administrators only."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    node_key = (command.args or "").strip()
    if not node_key:
        await message.answer("Использование: <code>/node &lt;node_key&gt;</code>")
        return

    try:
        node = settings.get_xui_node(node_key)
    except (KeyError, ValueError):
        await message.answer(
            f"Нода с ключом <code>{escape(node_key)}</code> не найдена."
        )
        return

    snapshot = await NodeSelectorService(settings=settings).get_capacity_snapshot()
    capacity_by_key = {capacity.key: capacity for capacity in snapshot}
    capacity = capacity_by_key[node.key]
    health_status = await get_node_health_status(settings, node)
    lines = [
        f"🧩 Нода {escape(node.key)}",
        f"key: <code>{escape(node.key)}</code>",
        f"label: <code>{format_node_label(node)}</code>",
        f"Статус: <code>{format_node_status(capacity.enabled)}</code>",
        f"Public host: <code>{format_node_public_host(node.xui_public_host)}</code>",
        f"inbound IDs: <code>{format_node_inbound_ids(node.xui_inbound_ids)}</code>",
        f"Active subscriptions: <code>{capacity.active_count}</code>",
        f"Pending reservations: <code>{capacity.pending_reservations}</code>",
        f"Occupied: <code>{capacity.occupied_count}</code>",
        f"Max active subscriptions: <code>{format_capacity_limit(capacity.max_active_subscriptions)}</code>",
        f"Free slots: <code>{format_free_slots(capacity)}</code>",
        *format_node_health_lines(health_status),
    ]
    await message.answer("\n".join(lines))


@router.message(Command("check_payment", "payment"))
async def check_payment_command(
    message: Message,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Check and reconcile a provider payment by id for administrators."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    provider_payment_id = (command.args or "").strip()
    if not provider_payment_id:
        await message.answer(
            "Использование: <code>/check_payment &lt;provider_payment_id&gt;</code>\n"
            "Alias: <code>/payment &lt;provider_payment_id&gt;</code>"
        )
        return

    async with async_session_maker() as session:
        payment = await PaymentRepository(
            session
        ).get_by_provider_payment_id_any_provider(provider_payment_id)

    service = PlategaWebhookService(settings=settings, bot=message.bot)
    if payment is not None:
        provider_status = None
        if payment.provider == PLATEGA_PROVIDER_NAME:
            client = None
            try:
                client = PlategaClient(settings=settings)
                transaction = await client.get_transaction(provider_payment_id)
            except ValueError:
                transaction = None
            finally:
                if client is not None:
                    await client.close()

            if transaction is None:
                provider_status = "Platega lookup не настроен"
            else:
                provider_status = (
                    service._extract_transaction_status(transaction) or "—"
                )
                processed = await service.process_transaction_status(
                    provider_payment_id,
                    provider_status,
                    months=payment.tariff_months,
                    status_reason_prefix="Admin payment check",
                    transaction=transaction,
                )
                if processed:
                    async with async_session_maker() as session:
                        payment = await PaymentRepository(
                            session
                        ).get_by_provider_payment_id_any_provider(provider_payment_id)

        if (
            payment.status == PaymentStatus.PAID
            and payment.subscription_id is None
            and (payment.provider_data or {}).get("provisioning_blocked_reason")
            == "no_available_nodes"
        ):
            await PaymentFinalizationService(
                settings=settings, bot=message.bot
            ).finalize_paid_payment(
                provider=payment.provider,
                provider_payment_id=provider_payment_id,
                source="admin_check_payment",
            )
            async with async_session_maker() as session:
                payment = await PaymentRepository(
                    session
                ).get_by_provider_payment_id_any_provider(provider_payment_id)

        await message.answer(
            format_admin_payment_details(
                provider=payment.provider,
                provider_payment_id=provider_payment_id,
                local_status=str(payment.status),
                amount=payment.amount,
                currency=payment.currency,
                tariff_months=payment.tariff_months,
                telegram_id=payment.user.telegram_id if payment.user else None,
                subscription_id=payment.subscription_id,
                provider_expires_at=payment.provider_expires_at,
                provider_status=provider_status,
            )
        )
        return

    if not platega_lookup_credentials_configured(settings):
        await message.answer(platega_lookup_not_configured_message())
        return

    client = None
    try:
        client = PlategaClient(settings=settings)
        transaction = await client.get_transaction(provider_payment_id)
    except ValueError:
        await message.answer(platega_lookup_not_configured_message())
        return
    except Exception:
        await message.answer(
            "Локальный платеж не найден.\n"
            "Platega transaction recovery невозможен: ошибка запроса к Platega."
        )
        return
    finally:
        if client is not None:
            await client.close()

    recovered = await recover_orphan_platega_payment(
        provider_payment_id,
        transaction,
        service,
    )
    if not recovered:
        provider_status = service._extract_transaction_status(transaction) or "—"
        await message.answer(
            "Локальный платеж не найден.\n"
            "Platega transaction получен, но orphan recovery невозможен.\n"
            f"provider status: <code>{escape(provider_status)}</code>"
        )
        return

    provider_status = service._extract_transaction_status(transaction) or "—"
    await service.process_transaction_status(
        provider_payment_id,
        provider_status,
        status_reason_prefix="Admin orphan payment recovery",
        transaction=transaction,
    )
    async with async_session_maker() as session:
        payment = await PaymentRepository(
            session
        ).get_by_provider_payment_id_any_provider(provider_payment_id)

    if payment is None:
        await message.answer(
            "Platega transaction recovery выполнен, но локальный платеж не найден "
            "после повторной загрузки."
        )
        return

    await message.answer(
        format_admin_payment_details(
            provider=payment.provider,
            provider_payment_id=provider_payment_id,
            local_status=str(payment.status),
            amount=payment.amount,
            currency=payment.currency,
            tariff_months=payment.tariff_months,
            telegram_id=payment.user.telegram_id if payment.user else None,
            subscription_id=payment.subscription_id,
            provider_expires_at=payment.provider_expires_at,
            provider_status=provider_status,
            header="💳 Platega orphan recovery",
        )
    )


@router.message(Command("user"))
async def user_command(
    message: Message,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Show safe user diagnostics for administrators."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    args = (command.args or "").split()
    if len(args) != 1:
        await message.answer("Использование: <code>/user &lt;telegram_id&gt;</code>")
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await message.answer("Использование: <code>/user &lt;telegram_id&gt;</code>")
        return

    async with async_session_maker() as session:
        user_repository = UserRepository(session)
        subscription_repository = SubscriptionRepository(session)
        benefit_repository = CustomerBenefitRepository(session)
        referral_reward_repository = ReferralRewardRepository(session)
        payment_repository = PaymentRepository(session)

        user = await user_repository.get_by_telegram_id(telegram_id)
        if user is None:
            await message.answer(
                f"Пользователь с Telegram ID <code>{escape(str(telegram_id))}</code> не найден."
            )
            return

        active_subscription = await subscription_repository.get_active_by_telegram_id(
            telegram_id
        )
        latest_subscription = active_subscription
        if latest_subscription is None:
            latest_subscription = (
                await subscription_repository.get_latest_by_telegram_id(telegram_id)
            )
        discount_percent = (
            await benefit_repository.get_active_discount_percent_by_telegram_id(
                telegram_id
            )
        )
        pending_bonus_days = (
            await referral_reward_repository.get_pending_bonus_days_by_telegram_id(
                telegram_id
            )
        )
        payments = await payment_repository.get_latest_by_user_id(user.id, limit=5)

    full_name = (
        " ".join(part for part in [user.first_name, user.last_name] if part) or "—"
    )
    username = f"@{user.username}" if user.username else "—"
    has_connection_link = bool(
        latest_subscription is not None
        and latest_subscription.vpn_config
        and latest_subscription.vpn_config.get("connection_link")
    )

    diagnostic_subscription = latest_subscription
    vpn_config = (
        (diagnostic_subscription.vpn_config or {}) if diagnostic_subscription else {}
    )
    latest_pending_payment = next(
        (payment for payment in payments if payment.status == PaymentStatus.PENDING),
        None,
    )

    lines = [
        "👤 Пользователь",
        f"Telegram ID: <code>{escape(str(user.telegram_id))}</code>",
        f"username: <code>{escape(username)}</code>",
        f"full name: <code>{escape(full_name)}</code>",
        f"trial used: <code>{format_yes_no(user.trial_used_at is not None)}</code>",
        f"trial used at: <code>{format_optional_datetime(user.trial_used_at)}</code>",
        f"trial subscription id: <code>{escape(str(user.trial_subscription_id or '—'))}</code>",
        f"active subscription: <code>{format_yes_no(active_subscription is not None)}</code>",
        f"subscription id: <code>{escape(str(diagnostic_subscription.id if diagnostic_subscription else '—'))}</code>",
        f"expires at: <code>{format_optional_datetime(diagnostic_subscription.expires_at if diagnostic_subscription else None)}</code>",
        f"xui_email: <code>{escape(diagnostic_subscription.xui_email if diagnostic_subscription else '—')}</code>",
        f"status: <code>{escape(str(diagnostic_subscription.status if diagnostic_subscription else '—'))}</code>",
        "node_key: "
        f"<code>{escape(get_subscription_node_detail(diagnostic_subscription, 'node_key', 'node_key'))}</code>",
        "node_label: "
        f"<code>{escape(get_subscription_node_detail(diagnostic_subscription, 'node_label', 'node_label'))}</code>",
        "node_public_host: "
        f"<code>{escape(get_subscription_node_detail(diagnostic_subscription, 'node_public_host'))}</code>",
        f"connection link exists: <code>{format_yes_no(has_connection_link)}</code>",
        "deprovisioned_at: "
        f"<code>{escape(get_vpn_config_diagnostic_value(vpn_config, 'deprovisioned_at'))}</code>",
        "deprovision_policy: "
        f"<code>{escape(get_vpn_config_diagnostic_value(vpn_config, 'deprovision_policy'))}</code>",
        "node_slot_released: "
        f"<code>{escape(get_vpn_config_diagnostic_value(vpn_config, 'node_slot_released'))}</code>",
        "deprovision_failed_at: "
        f"<code>{escape(get_vpn_config_diagnostic_value(vpn_config, 'deprovision_failed_at'))}</code>",
        "deprovision_error: "
        f"<code>{escape(get_vpn_config_diagnostic_value(vpn_config, 'deprovision_error'))}</code>",
        "latest pending reserved_node_key: "
        f"<code>{escape(latest_pending_payment.reserved_node_key if latest_pending_payment else '—')}</code>",
        "latest pending reserved_node_name: "
        f"<code>{escape(latest_pending_payment.reserved_node_name if latest_pending_payment else '—')}</code>",
        "latest pending node_reservation_expires_at: "
        f"<code>{format_optional_datetime(latest_pending_payment.node_reservation_expires_at if latest_pending_payment else None)}</code>",
        f"early buyer discount percent: <code>{discount_percent}</code>",
        f"pending referral bonus days: <code>{pending_bonus_days}</code>",
        "",
        "Последние 5 payments:",
    ]
    if not payments:
        lines.append("—")
    else:
        for payment in payments:
            lines.append(
                "• "
                f"provider=<code>{escape(payment.provider)}</code>; "
                f"provider_payment_id=<code>{escape(payment.provider_payment_id or '—')}</code>; "
                f"status=<code>{escape(str(payment.status))}</code>; "
                f"amount=<code>{escape(str(payment.amount))} {escape(payment.currency)}</code>; "
                f"tariff_months=<code>{escape(str(payment.tariff_months or '—'))}</code>; "
                f"created_at=<code>{format_optional_datetime(payment.created_at)}</code>"
            )

    await message.answer("\n".join(lines))


@router.message(Command("add_days"))
async def add_days_command(
    message: Message,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Manually extend an active subscription by days for administrators."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    args = (command.args or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/add_days &lt;telegram_id&gt; &lt;days&gt; [reason]</code>"
        )
        return

    telegram_id_raw, days_raw = args[0], args[1]
    reason = args[2] if len(args) > 2 else "manual"
    try:
        telegram_id = int(telegram_id_raw)
    except ValueError:
        await message.answer("Telegram ID должен быть целым числом.")
        return

    try:
        days = int(days_raw)
    except ValueError:
        await message.answer("Количество дней должно быть целым числом больше 0.")
        return

    if days <= 0:
        await message.answer("Количество дней должно быть больше 0.")
        return

    vpn_service = VpnService(settings=settings)
    try:
        provision_result = await vpn_service.extend_active_subscription_by_days(
            telegram_id=telegram_id,
            days=days,
            reason=reason,
        )
    except NoActiveSubscriptionError:
        await message.answer(
            f"Активная подписка для пользователя <code>{telegram_id}</code> не найдена."
        )
        return
    except XuiError as error:
        await notify_admins(
            message.bot,
            settings,
            format_access_error_alert(error, user_id=telegram_id),
        )
        await message.answer(PROVISIONING_USER_ERROR)
        return
    except RuntimeError as error:
        await notify_admins(
            message.bot,
            settings,
            format_access_error_alert(error, user_id=telegram_id),
        )
        await message.answer(PROVISIONING_USER_ERROR)
        return
    except Exception as error:
        await notify_admins(
            message.bot,
            settings,
            format_access_error_alert(error, user_id=telegram_id),
        )
        await message.answer(PROVISIONING_USER_ERROR)
        return
    finally:
        await vpn_service.close()

    await message.answer(
        "Срок цифрового доступа изменён.\n\n"
        f"Пользователь: <code>{telegram_id}</code>\n"
        f"Добавлено дней: <code>{days}</code>\n"
        f"Действует до: <code>{format_provision_expires(provision_result)}</code>"
    )
    await message.bot.send_message(
        telegram_id,
        "Срок вашего цифрового доступа изменён.\n\n"
        f"Добавлено дней: <code>{days}</code>\n"
        f"Действует до: <code>{format_provision_expires(provision_result)}</code>\n\n"
        "Ссылка для защищённого соединения остаётся прежней.",
    )


@router.message(Command("subscription"))
async def subscription_command(message: Message) -> None:
    """Show subscription status from a bot command."""
    await my_subscription(message)


@router.message(F.text == BTN_DOCUMENTS)
async def show_documents(message: Message, settings: Settings) -> None:
    """Show legal documents and related quick actions."""
    await message.answer(
        "Документы и полезная информация сервиса ЛадНет:",
        reply_markup=docs_keyboard(settings),
    )


@router.callback_query(F.data == "docs:tariffs")
async def docs_tariffs(callback: CallbackQuery) -> None:
    """Show tariff list from the documents menu."""
    await callback.message.answer(format_tariffs())
    await callback.answer()


@router.callback_query(F.data == "docs:support")
async def docs_support(callback: CallbackQuery, settings: Settings) -> None:
    """Show support contacts from the documents menu."""
    await callback.message.answer(support_contact_text(settings))
    await callback.answer()



@router.message(F.text == BTN_CUSTOM_SERVERS)
async def custom_servers_entry(message: Message, settings: Settings) -> None:
    """Show the custom server request section."""
    if not settings.custom_servers_enabled:
        await message.answer(custom_servers_disabled_text())
        return

    await message.answer(
        custom_servers_intro_text(),
        reply_markup=custom_servers_keyboard(),
    )


@router.callback_query(F.data == "custom_servers:family")
async def custom_servers_family(callback: CallbackQuery, settings: Settings) -> None:
    """Show family custom server request details."""
    if not settings.custom_servers_enabled:
        await callback.message.answer(custom_servers_disabled_text())
        await callback.answer()
        return

    await callback.message.answer(
        "👨‍👩‍👧 Семейный доступ\n\n"
        "Подходит для дома, семьи и нескольких личных устройств.\n\n"
        "Что входит:\n\n"
        "✅ отдельный серверный ресурс под вашу семью;\n"
        "✅ до 10 одновременных подключений/IP;\n"
        "✅ персональная ссылка для защищённого соединения;\n"
        "✅ помощь с первичной настройкой;\n"
        "✅ можно настроить подключение на роутере Keenetic или DD-WRT;\n"
        "✅ удобно для телефона, ПК, планшета, телевизора и других домашних устройств.\n\n"
        "Рекомендуемая цена:\n"
        f"от {settings.family_server_price_rub} ₽ / месяц\n\n"
        "Для первых клиентов возможны индивидуальные условия.\n\n"
        "Нажмите кнопку ниже, чтобы оставить заявку — поддержка свяжется с вами и уточнит детали.",
        reply_markup=custom_servers_request_keyboard(
            "custom_servers:request_family",
            "📨 Оставить заявку",
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "custom_servers:business")
async def custom_servers_business(callback: CallbackQuery, settings: Settings) -> None:
    """Show business custom server request details."""
    if not settings.custom_servers_enabled:
        await callback.message.answer(custom_servers_disabled_text())
        await callback.answer()
        return

    await callback.message.answer(
        "🏢 Корпоративным клиентам\n\n"
        "Решение для команд, небольших компаний, офисов и сотрудников на удалёнке.\n\n"
        "Варианты:\n\n"
        "🔹 До 20 подключений\n"
        "Рекомендуемая конфигурация серверной ноды:\n"
        "2 CPU / 2 GB RAM\n\n"
        "Цена:\n"
        f"от {settings.business_server_20_price_rub} ₽ / месяц\n\n"
        "🔷 До 40 подключений\n"
        "Рекомендуемая конфигурация серверной ноды:\n"
        "2 CPU / 4 GB RAM\n\n"
        "Цена:\n"
        f"от {settings.business_server_40_price_rub} ₽ / месяц\n\n"
        "Что входит:\n\n"
        "✅ отдельная серверная нода под вашу команду;\n"
        "✅ персональная настройка под нужное количество подключений;\n"
        "✅ первичная настройка через удалённый доступ в подарок;\n"
        "✅ помощь с подключением сотрудников;\n"
        "✅ настройка Keenetic или DD-WRT по договорённости;\n"
        "✅ при необходимости можно масштабировать количество подключений.\n\n"
        "Нажмите кнопку ниже, чтобы оставить заявку — поддержка свяжется с вами и уточнит детали.",
        reply_markup=custom_servers_request_keyboard(
            "custom_servers:request_business",
            "📨 Заявка для компании",
        ),
    )
    await callback.answer()


def custom_server_user_identity(callback: CallbackQuery) -> tuple[int, str, str]:
    """Return escaped user details for custom server admin alerts."""
    if callback.from_user is None:
        raise ValueError("Callback user is not available")

    telegram_id = callback.from_user.id
    full_name = escape(callback.from_user.full_name or "—")
    username = escape(callback.from_user.username or "—")
    return telegram_id, full_name, username


async def send_custom_server_request_to_admins(
    callback: CallbackQuery,
    settings: Settings,
    alert_text: str,
    user_text: str,
) -> None:
    """Notify administrators about a custom server request and confirm it to user."""
    if callback.from_user is None:
        await callback.message.answer("Не удалось определить пользователя.")
        await callback.answer()
        return

    await notify_admins(callback.bot, settings, alert_text)
    await callback.message.answer(user_text)
    await callback.answer("Заявка принята")


@router.callback_query(F.data == "custom_servers:request_family")
async def custom_servers_request_family(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    """Accept a family custom server request and notify administrators."""
    if not settings.custom_servers_enabled:
        await callback.message.answer(custom_servers_disabled_text())
        await callback.answer()
        return

    telegram_id, full_name, username = custom_server_user_identity(callback)
    alert_text = (
        "📨 Новая заявка: семейный индивидуальный доступ\n\n"
        f"Пользователь: <a href=\"tg://user?id={telegram_id}\">{full_name}</a>\n"
        f"Telegram ID: <code>{telegram_id}</code>\n"
        f"Username: @{username}\n\n"
        "Интерес: семейный доступ\n"
        f"Рекомендуемая цена: от {settings.family_server_price_rub} ₽/мес\n"
        "Лимит: до 10 одновременных подключений/IP\n\n"
        "Что уточнить:\n"
        "• сколько устройств планируется;\n"
        "• нужен ли роутер;\n"
        "• модель роутера: Keenetic / DD-WRT / другое;\n"
        "• нужен ли удалённый доступ для настройки;\n"
        "• удобный способ связи."
    )
    user_text = (
        "Заявка принята ✅\n\n"
        "Вы выбрали: семейный индивидуальный доступ.\n\n"
        "Поддержка свяжется с вами, уточнит количество устройств, удобный способ настройки "
        "и подготовит персональные условия."
    )
    await send_custom_server_request_to_admins(callback, settings, alert_text, user_text)


@router.callback_query(F.data == "custom_servers:request_business")
async def custom_servers_request_business(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    """Accept a business custom server request and notify administrators."""
    if not settings.custom_servers_enabled:
        await callback.message.answer(custom_servers_disabled_text())
        await callback.answer()
        return

    telegram_id, full_name, username = custom_server_user_identity(callback)
    alert_text = (
        "📨 Новая заявка: корпоративный доступ\n\n"
        f"Пользователь: <a href=\"tg://user?id={telegram_id}\">{full_name}</a>\n"
        f"Telegram ID: <code>{telegram_id}</code>\n"
        f"Username: @{username}\n\n"
        "Интерес: корпоративный доступ\n\n"
        "Варианты:\n"
        f"• до 20 подключений — от {settings.business_server_20_price_rub} ₽/мес, "
        "рекомендуемая нода 2 CPU / 2 GB RAM;\n"
        f"• до 40 подключений — от {settings.business_server_40_price_rub} ₽/мес, "
        "рекомендуемая нода 2 CPU / 4 GB RAM.\n\n"
        "Что уточнить:\n"
        "• сколько сотрудников/устройств;\n"
        "• нужна ли настройка роутера;\n"
        "• модель роутера: Keenetic / DD-WRT / другое;\n"
        "• нужна ли удалённая настройка через AnyDesk;\n"
        "• есть ли доступ к веб-интерфейсу роутера;\n"
        "• удобный способ связи."
    )
    user_text = (
        "Заявка принята ✅\n\n"
        "Вы выбрали: корпоративный доступ.\n\n"
        "Поддержка свяжется с вами, уточнит количество подключений, сценарий использования "
        "и подготовит персональное предложение."
    )
    await send_custom_server_request_to_admins(callback, settings, alert_text, user_text)


@router.callback_query(F.data == "custom_servers:support")
async def custom_servers_support(callback: CallbackQuery, settings: Settings) -> None:
    """Show support and remote setup requirements for custom servers."""
    await callback.message.answer(
        "💬 Поддержка и настройка\n\n"
        "Для первичной настройки через удалённый доступ обычно понадобится:\n\n"
        "🖥 AnyDesk или другой согласованный способ удалённого подключения;\n"
        "🌐 доступ к веб-интерфейсу роутера в браузере, например 192.168.1.1;\n"
        "🔐 логин и пароль администратора роутера;\n"
        "📦 для Keenetic может понадобиться флешка, если используется установка через XKeen;\n"
        "📌 модель роутера и версия прошивки.\n\n"
        "Пароль не нужно отправлять в бот. Доступ вводится клиентом во время удалённой настройки.\n\n"
        "Поддерживаемые варианты:\n"
        "• Keenetic;\n"
        "• DD-WRT;\n"
        "• другие модели — по согласованию.\n\n"
        "Напишите в поддержку, чтобы уточнить возможность настройки именно вашего оборудования.\n\n"
        f"{support_contact_text(settings)}",
        reply_markup=custom_servers_keyboard(),
    )
    await callback.answer()


@router.message(Command("invite"))
@router.message(F.text == BTN_INVITE_FRIEND)
async def invite_friend(message: Message, settings: Settings) -> None:
    """Return the user's referral invite link."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return
    if not settings.referral_enabled:
        await message.answer("Реферальная программа сейчас недоступна.")
        return

    code = await ReferralService(settings=settings).get_or_create_code(
        message.from_user.id
    )
    invite_url = f"{settings.bot_public_url}?start=ref_{code}"
    await message.answer(
        "🎁 Пригласить друга\n\n"
        f"Ваша ссылка:\n<code>{escape(invite_url)}</code>\n\n"
        "Бонусы за приглашение:\n"
        "• 1 месяц — +3 дня\n"
        "• 3 месяца — +7 дней\n"
        "• 6 месяцев — +14 дней\n"
        "• 12 месяцев — +21 день\n\n"
        "Новый пользователь получает +3 дня к первой оплаченной подписке.\n\n"
        "Учитываются только пользователи, оплатившие минимум 1 месяц. "
        "Тестовый доступ не учитывается."
    )


async def send_tariffs_screen(
    message: Message,
    state: FSMContext,
    settings: Settings,
    telegram_id: int,
) -> None:
    """Show available tariffs with renewal-aware copy."""
    subscription = await SubscriptionService().get_active_subscription(telegram_id)
    text = (
        "Ваша подписка активна ✅\n\n"
        "Выберите, на сколько продлить текущий цифровой доступ.\n"
        "Новая ссылка создаваться не будет.\n"
        "Оставшийся срок сохранится."
        if subscription is not None
        else "Выберите срок цифрового доступа:"
    )

    if settings.test_mode:
        text = (
            f"{text}\n\n"
            "Тестовый режим включён: оплата не потребуется.\n"
            "Скидки и реальные платежи в тестовом режиме не применяются."
        )
        discount_percent = 0
    else:
        discount_percent = await BenefitService(
            settings=settings
        ).get_active_discount_percent(telegram_id)

    await state.set_state(PurchaseState.choosing_tariff)
    keyboard = tariff_keyboard(discount_percent)
    trial_available = await is_trial_available(telegram_id, settings)
    if trial_available:
        keyboard.inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"🧪 Тестовый доступ на {settings.trial_access_hours} ч",
                    callback_data="trial_access",
                )
            ]
        )
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("renew"))
async def renew_command(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Show renewal-aware tariffs from the renew bot command."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    await send_tariffs_screen(message, state, settings, message.from_user.id)


@router.message(F.text == BTN_BUY_ACCESS)
async def show_tariffs(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Show available tariffs."""
    await renew_command(message, state, settings)


@router.callback_query(F.data == "renew")
async def renew_subscription(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Show available tariffs from renewal callbacks."""
    if not isinstance(callback.message, Message) or callback.from_user is None:
        await callback.answer("Не удалось открыть тарифы", show_alert=True)
        return

    await send_tariffs_screen(
        callback.message,
        state,
        settings,
        callback.from_user.id,
    )
    await callback.answer()


@router.callback_query(F.data == "trial_access")
async def trial_access(callback: CallbackQuery, settings: Settings) -> None:
    """Provision one-time user trial access independent from TEST_MODE."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer("Не удалось выдать тестовый доступ", show_alert=True)
        return
    if not settings.trial_access_enabled:
        await callback.answer("Тестовый доступ сейчас недоступен", show_alert=True)
        return

    telegram_user = callback.from_user
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user, _ = await user_repo.get_or_create_from_telegram(telegram_user)
        if user.trial_used_at is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await session.commit()
            await callback.answer("Тестовый доступ уже использован", show_alert=True)
            return

        active_subscription = await SubscriptionRepository(
            session
        ).get_active_by_telegram_id(telegram_user.id)
        if active_subscription is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await session.commit()
            await callback.answer("У вас уже есть активный доступ", show_alert=True)
            return

        vpn_service = VpnService(settings=settings, session=session)
        try:
            provision_result = await vpn_service.provision_trial_client(
                telegram_id=telegram_user.id,
                hours=settings.trial_access_hours,
            )
        except XuiError as error:
            await session.rollback()
            await notify_admins(
                callback.bot,
                settings,
                format_access_error_alert(error, user_id=telegram_user.id),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        except (RuntimeError, ValueError) as error:
            await session.rollback()
            await notify_admins(
                callback.bot,
                settings,
                format_access_error_alert(error, user_id=telegram_user.id),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        except Exception as error:
            await session.rollback()
            await notify_admins(
                callback.bot,
                settings,
                format_access_error_alert(error, user_id=telegram_user.id),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        finally:
            await vpn_service.close()

        user.trial_used_at = datetime.now(UTC)
        user.trial_subscription_id = provision_result.subscription_id
        session.add(user)
        await session.commit()

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Тестовый доступ выдан ✅\n\n"
        f"Действует до: <code>{format_provision_expires(provision_result)}</code>\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        f"<code>{escape(provision_result.connection_link)}</code>",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Тестовый доступ выдан")


@router.callback_query(
    F.data.startswith("buy:"), StateFilter(PurchaseState.choosing_tariff)
)
async def choose_tariff(
    callback: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    """Persist chosen tariff and offer manual payment request creation."""
    months = int(callback.data.split(":", maxsplit=1)[1]) if callback.data else 0
    if months not in TARIFFS:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    base_price = TARIFF_PRICES[months]
    if settings.test_mode:
        selected_tariff = f"{TARIFFS[months]} — {format_tariff_price(base_price, 0)}"
    else:
        discount_percent = await BenefitService(
            settings=settings
        ).get_active_discount_percent(callback.from_user.id)
        if discount_percent > 0:
            final_price = apply_discount_to_price(base_price, discount_percent)
            selected_tariff = (
                f"{TARIFFS[months]}\n\n"
                f"Базовая цена: {format_price(base_price)}\n"
                f"Скидка: {discount_percent}%\n"
                f"Итого к оплате: {format_price(final_price)}"
            )
        else:
            selected_tariff = f"{TARIFFS[months]} — {format_price(base_price)}"
    active_subscription = await SubscriptionService().get_active_subscription(
        callback.from_user.id
    )
    await state.update_data(months=months, payment_method_key=None)
    payment_hint = (
        "Тестовый режим включён: оплата не потребуется. "
        "Нажмите кнопку ниже, чтобы получить тестовый доступ."
        if settings.test_mode
        else "Нажмите кнопку ниже, чтобы создать заявку на оплату."
    )
    if active_subscription is not None:
        selection_text = (
            f"Вы выбрали продление: {selected_tariff}\n\n"
            "Новая ссылка создаваться не будет.\n"
            "Оставшийся срок сохранится.\n\n"
            f"{payment_hint}"
        )
    else:
        selection_text = f"Вы выбрали тариф: {selected_tariff}\n\n{payment_hint}"

    if (
        settings.payment_provider == PLATEGA_PROVIDER_NAME
        and settings.platega_payment_methods_json
        and not settings.test_mode
    ):
        await state.set_state(PurchaseState.choosing_payment_method)
        await callback.message.answer(
            f"{selection_text}\n\nВыберите способ оплаты:",
            reply_markup=platega_payment_methods_keyboard(settings),
        )
    else:
        await state.set_state(PurchaseState.waiting_payment)
        await callback.message.answer(
            selection_text,
            reply_markup=payment_request_keyboard(months, test_mode=settings.test_mode),
        )
    await callback.answer()


@router.callback_query(
    F.data.startswith("pay_method:"), StateFilter(PurchaseState.choosing_payment_method)
)
async def choose_payment_method(
    callback: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings
) -> None:
    """Persist the selected Platega payment method and create a payment."""
    method_key = (callback.data or "").split(":", maxsplit=1)[1].strip()
    data = await state.get_data()
    months = int(data.get("months") or 0)
    if not method_key or method_key not in settings.platega_payment_methods_json:
        await notify_admins(
            bot,
            settings,
            "Ошибка конфигурации способов оплаты Platega\n"
            f"Method code: <code>{escape(method_key or '—')}</code>\n"
            "Причина: <code>код способа оплаты отсутствует в конфигурации</code>",
        )
        await callback.answer("Способ оплаты недоступен", show_alert=True)
        return
    await state.update_data(payment_method_key=method_key)
    await state.set_state(PurchaseState.waiting_payment)
    await create_payment_request(
        callback, state, bot, settings, months=months, payment_method_key=method_key
    )


@router.callback_query(
    F.data.startswith("pay_request:"), StateFilter(PurchaseState.waiting_payment)
)
async def create_payment_request(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    months: int | None = None,
    payment_method_key: str | None = None,
) -> None:
    """Create a payment request and notify admins when needed."""
    if months is None:
        months = int(callback.data.split(":", maxsplit=1)[1]) if callback.data else 0
    if months not in TARIFFS or callback.from_user is None:
        await callback.answer("Не удалось создать заявку", show_alert=True)
        return

    user = callback.from_user
    if settings.test_mode:
        vpn_service = VpnService(settings=settings)
        try:
            provision_result = await vpn_service.provision_or_extend_client(
                telegram_id=user.id,
                months=months,
            )
        except XuiError as error:
            await notify_admins(
                bot,
                settings,
                format_access_error_alert(error, user_id=user.id, months=months),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        except RuntimeError as error:
            await notify_admins(
                bot,
                settings,
                format_access_error_alert(error, user_id=user.id, months=months),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        except Exception as error:
            await notify_admins(
                bot,
                settings,
                format_access_error_alert(error, user_id=user.id, months=months),
            )
            await callback.message.answer(
                PROVISIONING_USER_ERROR,
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer("Техническая ошибка", show_alert=True)
            return
        finally:
            await vpn_service.close()

        if settings.test_mode_referral_rewards_enabled:
            try:
                await ReferralService(settings=settings).apply_first_payment_rewards(
                    user.id, months
                )
            except Exception as error:
                await notify_admins(
                    bot,
                    settings,
                    "Ошибка начисления реферального бонуса в TEST_MODE\n"
                    f"Пользователь: <code>{user.id}</code>\n"
                    f"Месяцев: <code>{months}</code>\n"
                    f"Ошибка: <code>{escape(str(error))}</code>",
                )

        await state.clear()
        await callback.message.answer(
            "Тестовый режим включён ✅\n\n"
            f"{'Доступ создан' if provision_result.action == 'created' else 'Подписка продлена'} "
            f"на тариф <b>{TARIFFS[months]}</b>.\n"
            f"Действует до: <code>{format_provision_expires(provision_result)}</code>\n\n"
            f"Ваша ссылка для защищённого соединения:\n"
            f"<code>{escape(provision_result.connection_link)}</code>",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer("Тестовый доступ выдан")
        return

    active_subscription = await SubscriptionService().get_active_subscription(user.id)

    benefit_service = BenefitService(settings=settings)
    base_price = TARIFF_PRICES[months]
    discount_percent = await benefit_service.get_active_discount_percent(user.id)
    final_price = apply_discount_to_price(base_price, discount_percent)
    discount_summary = format_discount_summary(
        base_price, discount_percent, final_price
    )

    if payment_method_key is None:
        data = await state.get_data()
        stored_method_key = data.get("payment_method_key")
        payment_method_key = (
            str(stored_method_key) if stored_method_key is not None else None
        )

    try:
        async with async_session_maker() as session:
            async with session.begin():
                node_selector = NodeSelectorService(settings=settings, session=session)
                await node_selector.select_node_for_new_subscription()
    except NoAvailableNodeError:
        await state.clear()
        snapshot = await NodeSelectorService(settings=settings).get_capacity_snapshot()
        await notify_admins(
            bot,
            settings,
            "⚠️ Нет свободной capacity для новой оплаты\n"
            f"Telegram ID: <code>{user.id}</code>\n"
            f"Тариф: <code>{months}</code>\n"
            f"{format_capacity_snapshot(snapshot)}",
        )
        await callback.message.answer(
            "Сейчас все серверы временно заполнены. "
            "Пожалуйста, напишите в поддержку или попробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer("Серверы временно заполнены", show_alert=True)
        return

    payment_service = PaymentService(settings=settings)
    try:
        result: PaymentCreateResult = await payment_service.create_payment(
            user_id=user.id,
            tariff_id=months,
            amount=final_price,
            currency=PAYMENT_CURRENCY,
            payment_method_key=payment_method_key,
        )
    except NoAvailableNodeError:
        await state.clear()
        snapshot = await NodeSelectorService(settings=settings).get_capacity_snapshot()
        await notify_admins(
            bot,
            settings,
            "⚠️ Capacity закончилась при создании payment\n"
            f"Telegram ID: <code>{user.id}</code>\n"
            f"Тариф: <code>{months}</code>\n"
            f"{format_capacity_snapshot(snapshot)}",
        )
        await callback.message.answer(
            "Сейчас все серверы временно заполнены. "
            "Пожалуйста, напишите в поддержку или попробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer("Серверы временно заполнены", show_alert=True)
        return
    except KeyError:
        await notify_admins(
            bot,
            settings,
            "Ошибка конфигурации способов оплаты Platega\n"
            f"Method code: <code>{escape(payment_method_key or '—')}</code>\n"
            "Причина: <code>код способа оплаты отсутствует в конфигурации</code>",
        )
        await callback.answer("Способ оплаты недоступен", show_alert=True)
        return
    payment = result.payment
    await state.update_data(months=months, payment_id=result.provider_payment_id)

    if result.provider == "manual":
        admin_title = (
            "Новая заявка на продление подписки"
            if active_subscription is not None
            else "Новая заявка на оформление доступа"
        )
        admin_text = (
            f"{admin_title}\n\n"
            f'Пользователь: <a href="tg://user?id={user.id}">{escape(user.full_name)}</a>\n'
            f"Telegram ID: <code>{user.id}</code>\n"
            f"Username: @{escape(user.username) if user.username else '—'}\n"
            f"Тариф: <b>{TARIFFS[months]}</b>\n"
            f"Платёж: <code>{payment.provider_payment_id}</code>\n"
            f"{discount_summary}\n"
            f"Сумма платежа: <code>{payment.amount} {payment.currency}</code>"
        )

        for admin_id in settings.admin_ids:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=confirm_payment_keyboard(
                    payment.provider_payment_id or "", months
                ),
            )

        await state.clear()
        if active_subscription is not None:
            user_request_text = (
                "Заявка на продление создана.\n"
                "После подтверждения оплаты текущий доступ будет продлён.\n"
                "Ссылка останется прежней.\n\n"
                f"{discount_summary}"
            )
        else:
            user_request_text = (
                "Заявка создана. После проверки оплаты администратор подтвердит её, "
                "и бот отправит вам ссылку для защищённого соединения.\n\n"
                f"{discount_summary}"
            )

        await callback.message.answer(
            user_request_text,
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer("Заявка отправлена")
        return

    if result.provider == "platega" and result.payment_url is not None:
        await state.clear()
        payment_text = (
            "Заявка на оплату создана ✅\n\n"
            f"Тариф: <b>{TARIFFS[months]}</b>\n"
            f"К оплате: <code>{format_price(final_price)}</code>\n\n"
            "Нажмите кнопку ниже, чтобы перейти к оплате.\n"
            "После успешной оплаты доступ будет выдан автоматически."
        )
        if active_subscription is not None:
            payment_text += (
                "\n\n"
                "После успешной оплаты текущая подписка будет продлена.\n"
                "Ссылка для защищённого соединения останется прежней."
            )
        await callback.message.answer(
            payment_text,
            reply_markup=payment_url_keyboard(result.payment_url),
        )
        await callback.answer("Заявка на оплату создана")
        return

    await callback.answer("Не удалось подготовить подписку", show_alert=True)


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment(callback: CallbackQuery, settings: Settings) -> None:
    """Let an admin confirm a manual payment through the finalization service."""
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    try:
        _, provider_payment_id, _ = (callback.data or "").split(":", maxsplit=2)
    except ValueError:
        await callback.answer("Некорректные данные платежа", show_alert=True)
        return

    try:
        finalization_result = await PaymentFinalizationService(
            settings=settings,
            bot=callback.bot,
        ).finalize_paid_payment(
            provider="manual",
            provider_payment_id=provider_payment_id,
            source="manual_confirm",
        )
    except ValueError:
        await callback.answer(
            "Платёж нельзя подтвердить в этом статусе", show_alert=True
        )
        return
    except XuiError as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                provider_payment_id=provider_payment_id,
            ),
        )
        await callback.answer("Техническая ошибка", show_alert=True)
        return
    except RuntimeError as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                provider_payment_id=provider_payment_id,
            ),
        )
        await callback.answer("Техническая ошибка", show_alert=True)
        return
    except Exception as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                provider_payment_id=provider_payment_id,
            ),
        )
        await callback.answer("Техническая ошибка", show_alert=True)
        return

    if finalization_result.already_finalized:
        await callback.answer("Платёж уже подтверждён ранее", show_alert=True)
        await callback.message.edit_text(
            f"Платёж <code>{escape(provider_payment_id)}</code> уже подтверждён ранее. "
            "Повторная выдача доступа не выполнена."
        )
        return

    if finalization_result.status == "attached_existing":
        await callback.message.edit_text(
            f"Платёж <code>{escape(provider_payment_id)}</code> уже был связан "
            "с активированной подпиской. Пользовательское сообщение отправляет finalizer."
        )
        await callback.answer("Платёж уже был активирован")
        return

    user_id = finalization_result.payment.user.telegram_id
    await callback.message.edit_text(
        f"Оплата подтверждена. Пользовательское сообщение отправляет finalizer. "
        f"Пользователь: <code>{user_id}</code>."
    )
    await callback.answer("Оплата подтверждена")


@router.message(F.text == BTN_PROFILE)
async def my_profile(message: Message, settings: Settings) -> None:
    """Show profile actions and user benefit summary."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    details = await SubscriptionService().get_status_details(message.from_user.id)
    has_active_subscription = details.subscription is not None
    async with async_session_maker() as session:
        await UserRepository(session).get_or_create_from_telegram(message.from_user)
        await session.commit()
    trial_available = await is_trial_available(message.from_user.id, settings)

    lines = [
        "👤 Мой профиль",
        "",
        (
            "Здесь можно проверить подписку, открыть ссылку подключения, "
            "посмотреть документы или продлить цифровой доступ."
        ),
    ]
    if details.early_buyer_discount_percent > 0:
        lines.extend(
            ["", f"Ваша постоянная скидка: {details.early_buyer_discount_percent}%"]
        )
    if details.pending_referral_bonus_days > 0:
        lines.extend(
            [
                "",
                f"Ожидают применения бонусные дни: {details.pending_referral_bonus_days}",
            ]
        )

    await message.answer(
        "\n".join(lines),
        reply_markup=profile_keyboard(has_active_subscription, trial_available),
    )


async def send_subscription_status(message: Message, telegram_id: int) -> None:
    """Send current subscription status for a Telegram user."""
    details = await SubscriptionService().get_status_details(telegram_id)
    status_text = SubscriptionService.format_status(
        details.subscription,
        early_buyer_discount_percent=details.early_buyer_discount_percent,
        pending_referral_bonus_days=details.pending_referral_bonus_days,
    )
    if details.subscription is not None:
        await message.answer(status_text, reply_markup=renew_subscription_keyboard())
        return

    await message.answer(status_text)


@router.message(F.text == BTN_MY_SUBSCRIPTION)
async def my_subscription(message: Message) -> None:
    """Show current subscription status."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    await send_subscription_status(message, message.from_user.id)


async def send_protected_link(message: Message, telegram_id: int) -> None:
    """Send the protected connection link for a Telegram user."""
    subscription = await SubscriptionService().get_active_subscription(telegram_id)
    if subscription is None:
        await message.answer(SubscriptionService.format_status(subscription))
        return

    vpn_config = subscription.vpn_config or {}
    link = vpn_config.get("connection_link") or vpn_config.get("subscription_url")
    if not isinstance(link, str) or not link:
        await message.answer(SubscriptionService.format_status(None))
        return

    await message.answer(
        f"🔗 Ваша ссылка для защищённого соединения:\n<code>{escape(link)}</code>"
    )


@router.message(Command("link"))
@router.message(F.text == BTN_MY_LINK)
async def my_link(message: Message) -> None:
    """Show the protected connection link for the active subscription."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    await send_protected_link(message, message.from_user.id)


@router.callback_query(F.data == "profile:subscription")
async def profile_subscription(callback: CallbackQuery) -> None:
    """Show subscription status from the profile menu."""
    if not isinstance(callback.message, Message):
        await callback.answer("Не удалось проверить подписку", show_alert=True)
        return

    await send_subscription_status(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "profile:link")
async def profile_link(callback: CallbackQuery) -> None:
    """Show protected connection link from the profile menu."""
    if not isinstance(callback.message, Message):
        await callback.answer("Не удалось открыть ссылку", show_alert=True)
        return

    await send_protected_link(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "profile:documents")
async def profile_documents(callback: CallbackQuery, settings: Settings) -> None:
    """Show service documents from the profile menu."""
    if not isinstance(callback.message, Message):
        await callback.answer("Не удалось открыть документы", show_alert=True)
        return

    await callback.message.answer(
        "Документы и полезная информация сервиса ЛадНет:",
        reply_markup=docs_keyboard(settings),
    )
    await callback.answer()


@router.message(Command("admin"))
@router.message(F.text == "Админ")
async def admin_menu(message: Message, settings: Settings) -> None:
    """Show MVP admin menu entry point."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return
    await message.answer(
        "Админ-меню MVP:\n"
        "• заявки приходят администраторам автоматически;\n"
        "• подтверждение оплаты — кнопкой «✅ Подтвердить оплату» в заявке;\n"
        "• диагностика внешнего контура без создания пользователя — командой /xui_debug или «XUI debug»;\n"
        "• /ahelp — список всех команд администратора."
    )


@router.message(Command("xui_debug"))
@router.message(F.text.casefold() == "xui debug")
async def xui_debug(message: Message, settings: Settings) -> None:
    """Check X-UI OpenAPI availability without changing provisioning state."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    lines = ["🧪 X-UI diagnostics"]
    for node in settings.xui_nodes:
        result = await check_node_health(settings, node)
        token_configured = bool(
            node.xui_api_token and node.xui_api_token.get_secret_value().strip()
        )
        username_configured = bool((node.xui_username or "").strip())
        lines.extend(
            [
                "",
                f"Node: <code>{escape(node.key)}</code>",
                f"Status: {'✅ healthy' if result.ok else '❌ unhealthy'}",
                f"Auth mode: <code>{escape(result.auth_mode)}</code>",
                f"Token configured: <code>{'yes' if token_configured else 'no'}</code>",
                f"Username configured: <code>{'yes' if username_configured else 'no'}</code>",
                f"Base URL: <code>{escape(result.base_url_safe)}</code>",
                f"Public host: <code>{format_node_public_host(node.xui_public_host)}</code>",
                f"Inbound IDs: <code>{format_node_inbound_ids(node.xui_inbound_ids)}</code>",
            ]
        )
        if result.error_type:
            lines.append(f"Error: <code>{escape(result.error_type)}</code>")
        if result.hint:
            lines.append(f"Hint: {escape(result.hint)}")
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_INSTRUCTION)
async def instruction(message: Message) -> None:
    """Show protected access setup instructions."""
    await message.answer(
        "Инструкция:\n\n"
        "1. Нажмите «🛒 Оформить / продлить» и дождитесь выдачи ссылки для защищённого соединения.\n"
        "2. Скопируйте полученную ссылку.\n"
        "3. Установите приложение Happ на Android или iOS.\n"
        "4. Нажмите импорт из буфера обмена или вставьте ссылку вручную.\n"
        "5. Если Happ недоступен, можно использовать совместимые приложения: "
        "Hiddify, Shadowrocket, v2ray и другие.\n"
        "6. Если возникли сложности, обратитесь в поддержку."
    )


@router.message(F.text == BTN_SUPPORT)
async def support(message: Message, settings: Settings) -> None:
    """Show support information."""
    await message.answer(support_contact_text(settings))


async def setup_bot_command_menu(bot: Bot, settings: Settings) -> None:
    """Configure Telegram command menus for users and administrators."""
    await bot.set_my_commands(
        user_telegram_bot_commands(),
        scope=BotCommandScopeDefault(),
    )
    admin_commands = admin_telegram_bot_commands()
    for admin_id in settings.admin_ids:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


async def run_http_server(settings: Settings, bot: Bot) -> None:
    """Run the Platega webhook HTTP server until cancelled."""
    if settings.payment_provider != "platega" or settings.test_mode:
        return

    config = uvicorn.Config(
        create_app(settings=settings, bot=bot),
        host=settings.app_http_host,
        port=settings.app_http_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        await asyncio.shield(serve_task)
    finally:
        server.should_exit = True
        if not serve_task.done():
            try:
                await asyncio.wait_for(serve_task, timeout=10)
            except TimeoutError:
                serve_task.cancel()
                with suppress(asyncio.CancelledError):
                    await serve_task


async def run_scheduler(settings: Settings, bot: Bot) -> None:
    """Run background scheduler jobs until cancelled."""
    scheduler = create_scheduler(bot=bot, settings=settings)
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


async def _run_until_first_service_stops(*coroutines: Awaitable[Any]) -> None:
    """Run supervised services and cancel siblings when any service stops."""
    tasks = {asyncio.create_task(coroutine) for coroutine in coroutines}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


async def main() -> None:
    """Start the Telegram bot with Redis-backed FSM storage."""
    settings = Settings()
    storage = RedisStorage.from_url(settings.redis_url)
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    await setup_bot_command_menu(bot, settings)
    if settings.telegram_drop_pending_updates_on_startup:
        await bot.delete_webhook(drop_pending_updates=True)

    services: list[Awaitable[Any]] = [
        dispatcher.start_polling(
            bot,
            settings=settings,
        ),
        run_scheduler(settings, bot),
    ]
    if settings.payment_provider == "platega" and not settings.test_mode:
        services.append(run_http_server(settings, bot))

    try:
        await _run_until_first_service_stops(*services)
    finally:
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
