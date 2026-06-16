"""Application entry point."""

import asyncio
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
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
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService
from app.services.vpn_service import VpnService
from app.services.xui_client import XuiClient, XuiError
from app.tasks.scheduler import create_scheduler

TARIFFS = {
    1: "1 месяц",
    3: "3 месяца",
    6: "6 месяцев",
}

TARIFF_PRICES = {
    1: 299,
    3: 799,
    6: 1490,
}

PAYMENT_CURRENCY = "RUB"

router = Router(name="safetyweb")


class PurchaseState(StatesGroup):
    """FSM states for manual payment requests."""

    choosing_tariff = State()
    waiting_payment = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent user main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Оформить доступ"), KeyboardButton(text="Моя подписка")],
            [KeyboardButton(text="Инструкция"), KeyboardButton(text="Поддержка")],
            [KeyboardButton(text="Документы")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def tariff_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard with available VPN tariffs."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"buy:{months}")]
            for months, label in TARIFFS.items()
        ]
    )


def docs_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Build inline keyboard with legal documents and support actions."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Политика конфиденциальности",
                    url=settings.privacy_policy_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пользовательское соглашение",
                    url=settings.terms_url,
                )
            ],
            [InlineKeyboardButton(text="Тарифы", callback_data="docs:tariffs")],
            [InlineKeyboardButton(text="Поддержка", callback_data="docs:support")],
        ]
    )


def support_contact_text(settings: Settings) -> str:
    """Format support contact information for bot messages."""
    lines = [f"Поддержка: {escape(settings.support_username)}"]
    if settings.support_email:
        lines.append(f"Email: {escape(settings.support_email)}")
    return "\n".join(lines)


def format_tariffs() -> str:
    """Format tariffs for document menu callbacks."""
    lines = ["Доступные тарифы:"]
    for months, label in TARIFFS.items():
        price = TARIFF_PRICES[months]
        price_text = f"{price} {PAYMENT_CURRENCY}" if price else "уточняйте у поддержки"
        lines.append(f"• {label}: {price_text}")
    return "\n".join(lines)


def payment_request_keyboard(months: int, test_mode: bool = False) -> InlineKeyboardMarkup:
    """Build inline keyboard for submitting a payment or test access request."""
    button_text = "Получить тестовый ключ" if test_mode else "Создать заявку на оплату"
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
                    text="Подтвердить оплату",
                    callback_data=f"confirm:{provider_payment_id}:{months}",
                )
            ]
        ]
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    """Handle /start and show the main menu."""
    await state.clear()
    await message.answer(
        "Добро пожаловать в SafetyWeb! Выберите действие в меню ниже.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "Документы")
async def show_documents(message: Message, settings: Settings) -> None:
    """Show legal documents and related quick actions."""
    await message.answer("Документы и полезная информация:", reply_markup=docs_keyboard(settings))


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


@router.message(F.text == "Оформить доступ")
async def show_tariffs(message: Message, state: FSMContext) -> None:
    """Show available tariffs."""
    await state.set_state(PurchaseState.choosing_tariff)
    await message.answer("Выберите срок подписки:", reply_markup=tariff_keyboard())


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

    await state.update_data(months=months)
    await state.set_state(PurchaseState.waiting_payment)
    payment_hint = (
        "Тестовый режим включён: оплата не потребуется."
        if settings.test_mode
        else "Нажмите кнопку ниже, чтобы создать заявку на ручную оплату."
    )
    await callback.message.answer(
        f"Вы выбрали тариф: <b>{TARIFFS[months]}</b>.\n"
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
            vpn_link = await vpn_service.create_client(telegram_id=user.id, months=months)
        finally:
            await vpn_service.close()

        await state.clear()
        await callback.message.answer(
            "Тестовый режим включён ✅\n\n"
            f"Ваша ссылка для защищённого соединения на тариф <b>{TARIFFS[months]}</b>:\n"
            f"<code>{escape(vpn_link)}</code>",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer("Тестовый ключ выдан")
        return

    payment_service = PaymentService()
    payment = await payment_service.create_payment(
        user_id=user.id,
        tariff_id=months,
        amount=TARIFF_PRICES[months],
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
        f"Сумма: <code>{payment.amount} {payment.currency}</code>"
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
        "и бот отправит вам ссылку для защищённого соединения.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Заявка отправлена")


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment(callback: CallbackQuery, settings: Settings) -> None:
    """Let an admin confirm payment and provision VPN access for the user."""
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    _, provider_payment_id, months_raw = (callback.data or "").split(":", maxsplit=2)
    months = int(months_raw)
    payment_service = PaymentService()
    payment = await payment_service.confirm_manual_payment(provider_payment_id)
    user_id = payment.user.telegram_id
    vpn_service = VpnService(settings=settings)
    try:
        vpn_link = await vpn_service.create_client(telegram_id=user_id, months=months)
    finally:
        await vpn_service.close()

    await callback.bot.send_message(
        user_id,
        "Оплата подтверждена ✅\n\n"
        f"Ваша ссылка для защищённого соединения на тариф <b>{TARIFFS.get(months, f'{months} мес.')}</b>:\n"
        f"<code>{escape(vpn_link)}</code>",
    )
    await callback.message.edit_text(
        f"Оплата подтверждена. Ссылка для защищённого соединения отправлена пользователю <code>{user_id}</code>."
    )
    await callback.answer("Оплата подтверждена")


@router.message(F.text == "Моя подписка")
async def my_subscription(message: Message) -> None:
    """Show current subscription status."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    subscription = await SubscriptionService().get_active_subscription(message.from_user.id)
    await message.answer(SubscriptionService.format_status(subscription))


@router.message(F.text == "Админ")
async def admin_menu(message: Message, settings: Settings) -> None:
    """Show MVP admin menu entry point."""
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("Недостаточно прав.")
        return
    await message.answer(
        "Админ-меню MVP:\n"
        "• заявки приходят администраторам автоматически;\n"
        "• подтверждение оплаты — кнопкой «Подтвердить оплату» в заявке;\n"
        "• проверка 3x-ui без создания клиента — командой «XUI debug»."
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
        await message.answer(f"3x-ui debug: ошибка ❌\n<code>{escape(str(error))}</code>")
        return
    finally:
        await xui_client.close()

    title = schema.get("info", {}).get("title", "OpenAPI")
    version = schema.get("info", {}).get("version", "unknown")
    paths = schema.get("paths", {})
    paths_count = len(paths) if isinstance(paths, dict) else 0
    await message.answer(
        "3x-ui debug: OpenAPI доступен ✅\n"
        f"Схема: <b>{escape(str(title))}</b>\n"
        f"Версия: <code>{escape(str(version))}</code>\n"
        f"Paths: <code>{paths_count}</code>"
    )


@router.message(F.text == "Инструкция")
async def instruction(message: Message) -> None:
    """Show VPN setup instructions."""
    await message.answer(
        "Инструкция:\n"
        "1. Оплатите выбранный тариф и дождитесь подтверждения.\n"
        "2. Скопируйте полученную ссылку для защищённого соединения.\n"
        "3. Установите Happ на Android или iOS.\n"
        "4. Нажмите импорт из буфера обмена или вставьте ссылку вручную."
    )


@router.message(F.text == "Поддержка")
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
