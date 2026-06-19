import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot_commands import admin_telegram_bot_commands, user_telegram_bot_commands


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
    polling_index = main_source.index("dispatcher.start_polling(bot, settings=settings)")

    assert setup_index < polling_index
    assert "BotCommandScopeDefault" in main_source
    assert "BotCommandScopeChat(chat_id=admin_id)" in main_source
