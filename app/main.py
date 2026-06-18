"""Application entry point."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.config import Settings
from app.db.models import PaymentStatus
from app.db.repositories import SubscriptionRepository, UserRepository
from app.db.session import async_session_maker
from app.services.benefit_service import BenefitService
from app.services.payment_service import PaymentService
from app.services.referral_service import ReferralService
from app.services.stats_service import AdminStats, StatsService
from app.services.subscription_service import SubscriptionService
from app.services.vpn_service import (
    NoActiveSubscriptionError,
    ProvisionResult,
    VpnService,
)
from app.services.xui_client import XuiClient, XuiError
from app.tasks.scheduler import create_scheduler

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

BTN_BUY_ACCESS = "🛒 Оформить / продлить"
BTN_PROFILE = "👤 Мой профиль"
BTN_MY_SUBSCRIPTION = "📅 Моя подписка"
BTN_MY_LINK = "🔗 Моя ссылка"
BTN_INSTRUCTION = "📲 Инструкция"
BTN_SUPPORT = "💬 Поддержка"
BTN_DOCUMENTS = "📄 Документы"
BTN_INVITE_FRIEND = "🎁 Пригласить друга"
TARIFF_EMOJIS = {1: "🔹", 3: "🔷", 6: "💎", 12: "👑"}

router = Router(name="safetyweb")


class PurchaseState(StatesGroup):
    """FSM states for manual payment requests."""

    choosing_tariff = State()
    waiting_payment = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent user main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY_ACCESS), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_INVITE_FRIEND)],
            [KeyboardButton(text=BTN_INSTRUCTION), KeyboardButton(text=BTN_SUPPORT)],
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
            [InlineKeyboardButton(text="🧪 Получить тестовый доступ", callback_data="trial_access")]
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
            [InlineKeyboardButton(text=BTN_MY_SUBSCRIPTION, callback_data="profile:subscription")],
            [InlineKeyboardButton(text=BTN_MY_LINK, callback_data="profile:link")],
            [InlineKeyboardButton(text=BTN_DOCUMENTS, callback_data="profile:documents")],
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

    active_subscription = await SubscriptionService().get_active_subscription(telegram_id)
    return active_subscription is None


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
    lines.append(f"Ошибка: <code>{escape(str(error))}</code>")
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


def payment_request_keyboard(months: int, test_mode: bool = False) -> InlineKeyboardMarkup:
    """Build inline keyboard for submitting a payment or test access request."""
    button_text = "🧪 Получить тестовый доступ" if test_mode else "💳 Создать заявку на оплату"
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


def confirm_payment_keyboard(provider_payment_id: str, months: int) -> InlineKeyboardMarkup:
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


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    command: CommandObject,
    settings: Settings,
) -> None:
    """Handle /start and show the main menu."""
    await state.clear()
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
        async with async_session_maker() as session:
            await UserRepository(session).get_or_create_from_telegram(message.from_user)
            await session.commit()

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
            "Главное меню доступно ниже.",
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
        "• 📅 Моя подписка — проверить активный доступ.\n"
        "• 🔗 Моя ссылка — получить ссылку для защищённого соединения (/link).\n"
        "• 🎁 Пригласить друга — получить реферальную ссылку для приглашения.\n"
        "• 📲 Инструкция — открыть краткую инструкцию по настройке.\n"
        "• 💬 Поддержка — посмотреть контакты поддержки.\n"
        "• 📄 Документы — политика, соглашение, тарифы и условия оплаты.\n\n"
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


@router.message(Command("stats"))
async def stats_command(message: Message, settings: Settings) -> None:
    """Show aggregate service statistics to administrators only."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    stats = await StatsService().get_admin_stats()
    await message.answer(format_admin_stats(stats, settings.early_buyer_limit))


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

    code = await ReferralService(settings=settings).get_or_create_code(message.from_user.id)
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
        "Ваша подписка уже активна ✅\n\n"
        "Выберите, на сколько продлить цифровой доступ.\n"
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
    if subscription is None and settings.trial_access_enabled:
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
        user = await user_repo.get_or_create_from_telegram(telegram_user)
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
    await state.update_data(months=months)
    await state.set_state(PurchaseState.waiting_payment)
    payment_hint = (
        "Тестовый режим включён: оплата не потребуется. "
        "Нажмите кнопку ниже, чтобы получить тестовый доступ."
        if settings.test_mode
        else "Нажмите кнопку ниже, чтобы создать заявку на оплату."
    )
    await callback.message.answer(
        f"Вы выбрали тариф: {selected_tariff}\n\n"
        f"{payment_hint}",
        reply_markup=payment_request_keyboard(months, test_mode=settings.test_mode),
    )
    await callback.answer()


@router.callback_query(
    F.data.startswith("pay_request:"), StateFilter(PurchaseState.waiting_payment)
)
async def create_payment_request(
    callback: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings
) -> None:
    """Create a manual payment request and notify admins."""
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

    benefit_service = BenefitService(settings=settings)
    base_price = TARIFF_PRICES[months]
    discount_percent = await benefit_service.get_active_discount_percent(user.id)
    final_price = apply_discount_to_price(base_price, discount_percent)
    discount_summary = format_discount_summary(base_price, discount_percent, final_price)

    payment_service = PaymentService()
    payment = await payment_service.create_payment(
        user_id=user.id,
        tariff_id=months,
        amount=final_price,
        currency=PAYMENT_CURRENCY,
    )
    await state.update_data(months=months, payment_id=payment.provider_payment_id)
    admin_text = (
        "Новая заявка на ручную оплату\n\n"
        f"Пользователь: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
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
            reply_markup=confirm_payment_keyboard(payment.provider_payment_id or "", months),
        )

    await state.clear()
    await callback.message.answer(
        "Заявка создана. После проверки оплаты администратор подтвердит её, "
        "и бот отправит вам ссылку для защищённого соединения.\n\n"
        f"{discount_summary}",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Заявка отправлена")


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment(callback: CallbackQuery, settings: Settings) -> None:
    """Let an admin confirm payment and provision protected access for the user."""
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    _, provider_payment_id, months_raw = (callback.data or "").split(":", maxsplit=2)
    months = int(months_raw)
    payment_service = PaymentService()
    status = await payment_service.get_payment_status(provider_payment_id)
    payment = None
    if status == PaymentStatus.PAID:
        payment = await payment_service.get_manual_payment(provider_payment_id)
        if payment.subscription_id is not None:
            await callback.answer("Платёж уже подтверждён ранее", show_alert=True)
            await callback.message.answer(
                f"Платёж <code>{escape(provider_payment_id)}</code> уже подтверждён ранее. "
                "Повторная выдача доступа не выполнена."
            )
            return
    if status in {PaymentStatus.REFUNDED, PaymentStatus.FAILED}:
        await callback.answer("Платёж нельзя подтвердить в этом статусе", show_alert=True)
        return

    user_id = None
    vpn_service = None
    try:
        if payment is None:
            payment = await payment_service.confirm_manual_payment(provider_payment_id)
        user_id = payment.user.telegram_id
        provision_result = None
        if status == PaymentStatus.PAID and payment.subscription_id is None:
            async with async_session_maker() as session:
                subscription = await SubscriptionRepository(
                    session
                ).get_by_last_payment_id(user_id, provider_payment_id)
                if subscription is not None:
                    provision_result = VpnService.provision_result_from_subscription(
                        subscription
                    )

        if provision_result is None:
            vpn_service = VpnService(settings=settings)
            provision_result = await vpn_service.provision_or_extend_client(
                telegram_id=user_id,
                months=months,
                source_payment_id=provider_payment_id,
            )
        await payment_service.attach_subscription(
            provider_payment_id, provision_result.subscription_id
        )
    except XuiError as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                user_id=user_id,
                months=months,
                provider_payment_id=provider_payment_id,
            ),
        )
        if user_id is not None:
            await callback.bot.send_message(user_id, PROVISIONING_USER_ERROR)
        await callback.answer("Техническая ошибка", show_alert=True)
        return
    except RuntimeError as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                user_id=user_id,
                months=months,
                provider_payment_id=provider_payment_id,
            ),
        )
        if user_id is not None:
            await callback.bot.send_message(user_id, PROVISIONING_USER_ERROR)
        await callback.answer("Техническая ошибка", show_alert=True)
        return
    except Exception as error:
        await notify_admins(
            callback.bot,
            settings,
            format_access_error_alert(
                error,
                user_id=user_id,
                months=months,
                provider_payment_id=provider_payment_id,
            ),
        )
        if user_id is not None:
            await callback.bot.send_message(user_id, PROVISIONING_USER_ERROR)
        await callback.answer("Техническая ошибка", show_alert=True)
        return
    finally:
        if vpn_service is not None:
            await vpn_service.close()

    try:
        await ReferralService(settings=settings).apply_pending_rewards(user_id)
    except Exception as error:
        await notify_admins(
            callback.bot,
            settings,
            "Ошибка применения отложенных реферальных бонусов\n"
            f"Пользователь: <code>{user_id}</code>\n"
            f"Ошибка: <code>{escape(str(error))}</code>",
        )

    benefit_granted = False
    try:
        benefit_granted = await BenefitService(
            settings=settings
        ).grant_early_buyer_discount_if_eligible(user_id)
    except Exception as error:
        await notify_admins(
            callback.bot,
            settings,
            "Ошибка выдачи скидки раннего покупателя\n"
            f"Пользователь: <code>{user_id}</code>\n"
            f"Ошибка: <code>{escape(str(error))}</code>",
        )
        benefit_granted = False

    referral_rewards = []
    if not settings.test_mode or settings.test_mode_referral_rewards_enabled:
        try:
            referral_rewards = await ReferralService(
                settings=settings
            ).apply_first_payment_rewards(user_id, months)
        except Exception as error:
            await notify_admins(
                callback.bot,
                settings,
                "Ошибка начисления реферального бонуса\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Месяцев: <code>{months}</code>\n"
                f"Ошибка: <code>{escape(str(error))}</code>",
            )
            referral_rewards = []

    paid_base_price = Decimal(str(TARIFF_PRICES.get(months, payment.amount)))
    paid_amount = Decimal(str(payment.amount))
    paid_discount = paid_base_price - paid_amount
    paid_discount_percent = (
        int((paid_discount * Decimal(100) / paid_base_price).quantize(Decimal("1")))
        if paid_base_price
        else 0
    )
    paid_discount_summary = format_discount_summary(
        paid_base_price,
        paid_discount_percent,
        paid_amount,
    )
    benefit_granted_text = (
        "Вам доступна постоянная скидка раннего пользователя 🎁\n\n"
        if benefit_granted
        else ""
    )
    referral_rewards_text = (
        "Начислены реферальные бонусные дни 🎁\n\n"
        if referral_rewards
        else ""
    )

    await callback.bot.send_message(
        user_id,
        "Оплата подтверждена ✅\n\n"
        f"{'Доступ создан' if provision_result.action == 'created' else 'Подписка продлена'} "
        f"на тариф <b>{TARIFFS.get(months, f'{months} мес.')}</b>.\n"
        f"{paid_discount_summary}\n"
        f"Действует до: <code>{format_provision_expires(provision_result)}</code>\n\n"
        f"{benefit_granted_text}"
        f"{referral_rewards_text}"
        f"Ваша ссылка для защищённого соединения:\n"
        f"<code>{escape(provision_result.connection_link)}</code>",
    )
    admin_action_text = (
        "создана новая подписка"
        if provision_result.action == "created"
        else "продлена активная подписка"
    )
    await callback.message.edit_text(
        f"Оплата подтверждена: {admin_action_text}. "
        f"Ссылка для защищённого соединения отправлена пользователю <code>{user_id}</code>.\n"
        f"{paid_discount_summary}\n"
        f"Скидка раннего покупателя выдана: <code>{'да' if benefit_granted else 'нет'}</code>\n"
        f"Реферальных наград начислено: <code>{len(referral_rewards)}</code>"
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
        "🔗 Ваша ссылка для защищённого соединения:\n"
        f"<code>{escape(link)}</code>"
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
        "• диагностика внешнего контура без создания пользователя — командой «XUI debug»."
    )


@router.message(F.text.casefold() == "xui debug")
async def xui_debug(message: Message, settings: Settings) -> None:
    """Check X-UI OpenAPI availability without changing provisioning state."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return

    xui_client = XuiClient(settings=settings)
    try:
        schema = await xui_client.get_openapi()
    except XuiError as error:
        await message.answer(
            f"Диагностика внешнего контура: ошибка ❌\n<code>{escape(str(error))}</code>"
        )
        return
    finally:
        await xui_client.close()

    title = schema.get("info", {}).get("title", "OpenAPI")
    version = schema.get("info", {}).get("version", "unknown")
    paths = schema.get("paths", {})
    paths_count = len(paths) if isinstance(paths, dict) else 0
    await message.answer(
        "Диагностика внешнего контура: OpenAPI доступен ✅\n"
        f"Схема: <b>{escape(str(title))}</b>\n"
        f"Версия: <code>{escape(str(version))}</code>\n"
        f"Paths: <code>{paths_count}</code>"
    )


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
    scheduler = create_scheduler(bot=bot, settings=settings)
    scheduler.start()

    try:
        await dispatcher.start_polling(bot, settings=settings)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
