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
from app.services.vpn_service import VpnService

TARIFFS = {
    1: "1 месяц",
    3: "3 месяца",
    6: "6 месяцев",
}

router = Router(name="safetyweb")


class PurchaseState(StatesGroup):
    """FSM states for manual payment requests."""

    choosing_tariff = State()
    waiting_payment = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent user main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Купить VPN"), KeyboardButton(text="Моя подписка")],
            [KeyboardButton(text="Инструкция"), KeyboardButton(text="Поддержка")],
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


def payment_request_keyboard(months: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for submitting a manual payment request."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать заявку на оплату",
                    callback_data=f"pay_request:{months}",
                )
            ]
        ]
    )


def confirm_payment_keyboard(user_id: int, months: int) -> InlineKeyboardMarkup:
    """Build admin confirmation keyboard for a manual payment request."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить оплату",
                    callback_data=f"confirm:{user_id}:{months}",
                )
            ]
        ]
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    """Handle /start and show the main menu."""
    await state.clear()
    await message.answer(
        "Добро пожаловать в SafetyWeb VPN! Выберите действие в меню ниже.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "Купить VPN")
async def show_tariffs(message: Message, state: FSMContext) -> None:
    """Show available tariffs."""
    await state.set_state(PurchaseState.choosing_tariff)
    await message.answer("Выберите срок подписки:", reply_markup=tariff_keyboard())


@router.callback_query(
    F.data.startswith("buy:"), StateFilter(PurchaseState.choosing_tariff)
)
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    """Persist chosen tariff and offer manual payment request creation."""
    months = int(callback.data.split(":", maxsplit=1)[1]) if callback.data else 0
    if months not in TARIFFS:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    await state.update_data(months=months)
    await state.set_state(PurchaseState.waiting_payment)
    await callback.message.answer(
        f"Вы выбрали тариф: <b>{TARIFFS[months]}</b>.\n"
        "Нажмите кнопку ниже, чтобы создать заявку на ручную оплату.",
        reply_markup=payment_request_keyboard(months),
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
    await state.update_data(months=months)
    admin_text = (
        "Новая заявка на ручную оплату\n\n"
        f"Пользователь: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Username: @{escape(user.username) if user.username else '—'}\n"
        f"Тариф: <b>{TARIFFS[months]}</b>"
    )

    for admin_id in settings.admin_ids:
        await bot.send_message(
            admin_id,
            admin_text,
            reply_markup=confirm_payment_keyboard(user.id, months),
        )

    await callback.message.answer(
        "Заявка создана. После проверки оплаты администратор подтвердит её, "
        "и бот отправит вам VPN-ссылку.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Заявка отправлена")


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment(callback: CallbackQuery, settings: Settings) -> None:
    """Let an admin confirm payment and provision VPN access for the user."""
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    _, user_id_raw, months_raw = (callback.data or "").split(":", maxsplit=2)
    user_id = int(user_id_raw)
    months = int(months_raw)
    vpn_service = VpnService(settings=settings)
    try:
        vpn_link = await vpn_service.create_client(telegram_id=user_id, months=months)
    finally:
        await vpn_service.close()

    await callback.bot.send_message(
        user_id,
        "Оплата подтверждена ✅\n\n"
        f"Ваша VPN-ссылка на тариф <b>{TARIFFS.get(months, f'{months} мес.')}</b>:\n"
        f"<code>{escape(vpn_link)}</code>",
    )
    await callback.message.edit_text(
        f"Оплата подтверждена. VPN-ссылка отправлена пользователю <code>{user_id}</code>."
    )
    await callback.answer("Оплата подтверждена")


@router.message(F.text == "Моя подписка")
async def my_subscription(message: Message) -> None:
    """Show subscription placeholder."""
    await message.answer("Данные о подписке появятся здесь после подтверждения оплаты.")


@router.message(F.text == "Инструкция")
async def instruction(message: Message) -> None:
    """Show VPN setup instructions."""
    await message.answer(
        "Инструкция:\n"
        "1. Оплатите выбранный тариф и дождитесь подтверждения.\n"
        "2. Скопируйте полученную VPN-ссылку.\n"
        "3. Импортируйте ссылку в совместимое VPN-приложение."
    )


@router.message(F.text == "Поддержка")
async def support(message: Message) -> None:
    """Show support information."""
    await message.answer("Напишите в поддержку: @support")


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

    await dispatcher.start_polling(bot, settings=settings)


if __name__ == "__main__":
    asyncio.run(main())
