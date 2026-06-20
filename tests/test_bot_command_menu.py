import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_commands import (
    BTN_BUY_ACCESS,
    BTN_CUSTOM_SERVERS,
    BTN_DOCUMENTS,
    BTN_INSTRUCTION,
    BTN_INVITE_FRIEND,
    BTN_PROFILE,
    BTN_SUPPORT,
    admin_telegram_bot_commands,
    user_telegram_bot_commands,
)
from app.main import (
    custom_servers_keyboard,
    custom_servers_request_keyboard,
    main_menu_keyboard,
)


def test_user_command_menu_excludes_admin_commands() -> None:
    user_commands = {command.command for command in user_telegram_bot_commands()}
    admin_commands = {command.command for command in admin_telegram_bot_commands()}

    assert {"start", "help", "docs", "tariffs", "subscription", "invite", "renew", "link"} <= user_commands
    assert "admin" not in user_commands
    assert "ahelp" not in user_commands
    assert "admin" in admin_commands
    assert user_commands < admin_commands


def test_main_sets_command_menu_before_polling() -> None:
    main_source = Path("app/main.py").read_text()

    setup_index = main_source.index("await setup_bot_command_menu(bot, settings)")
    delete_webhook_index = main_source.index(
        "await bot.delete_webhook(drop_pending_updates=True)"
    )
    polling_index = main_source.index("dispatcher.start_polling(")

    assert setup_index < delete_webhook_index < polling_index
    assert (
        "drop_pending_updates=settings.telegram_drop_pending_updates_on_startup"
        not in main_source
    )
    assert "settings=settings" in main_source[polling_index:]
    assert "BotCommandScopeDefault" in main_source
    assert "BotCommandScopeChat(chat_id=admin_id)" in main_source


def test_main_menu_keyboard_layout() -> None:
    keyboard = main_menu_keyboard()

    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        [BTN_BUY_ACCESS, BTN_PROFILE],
        [BTN_INVITE_FRIEND, BTN_CUSTOM_SERVERS],
        [BTN_INSTRUCTION, BTN_SUPPORT],
        [BTN_DOCUMENTS],
    ]


def test_custom_servers_keyboard_has_no_back_button() -> None:
    keyboard = custom_servers_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert "⬅️ Назад" not in {button.text for button in buttons}


def test_custom_servers_request_keyboard_has_no_back_button() -> None:
    keyboard = custom_servers_request_keyboard(
        "custom_servers:family_request", "Оставить заявку"
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert "⬅️ Назад" not in {button.text for button in buttons}
