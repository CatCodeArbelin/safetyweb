"""Central registry for bot commands and important user actions."""

from dataclasses import dataclass
from enum import StrEnum


class BotCommandCategory(StrEnum):
    """Command categories shown in administrator help."""

    USER_SLASH = "Пользовательские slash-команды"
    DEEP_LINK = "Deep-link сценарии /start"
    ADMIN_SLASH = "Админские slash-команды"
    ADMIN_TEXT = "Текстовые админ-команды"
    USER_BUTTON = "Основные пользовательские кнопки"


@dataclass(frozen=True)
class BotCommandSpec:
    """Registry entry for a bot command, deep link, or important user action."""

    category: BotCommandCategory
    command: str
    description: str


BTN_BUY_ACCESS = "🛒 Оформить / продлить"
BTN_PROFILE = "👤 Мой профиль"
BTN_MY_SUBSCRIPTION = "📅 Моя подписка"
BTN_MY_LINK = "🔗 Моя ссылка"
BTN_INSTRUCTION = "📲 Инструкция"
BTN_SUPPORT = "💬 Поддержка"
BTN_DOCUMENTS = "📄 Документы"
BTN_INVITE_FRIEND = "🎁 Пригласить друга"


# When adding any new slash-command, text admin command, or important user command, update this registry. /ahelp and Telegram command scopes are generated from it.
BOT_COMMAND_REGISTRY: tuple[BotCommandSpec, ...] = (
    BotCommandSpec(
        BotCommandCategory.USER_SLASH, "/start", "открыть главное меню."
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/help",
        "помощь по сервису и контакты поддержки.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/docs",
        "документы и полезная информация.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/tariffs",
        "актуальные тарифы и условия оплаты.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH, "/subscription", "статус подписки."
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/invite",
        "реферальная ссылка пользователя.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/renew",
        "оформление или продление цифрового доступа.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_SLASH,
        "/link",
        "ссылка для защищённого соединения при активной подписке.",
    ),
    BotCommandSpec(
        BotCommandCategory.DEEP_LINK,
        "/start pay_return",
        "возврат после успешного перехода из оплаты.",
    ),
    BotCommandSpec(
        BotCommandCategory.DEEP_LINK,
        "/start pay_failed",
        "возврат после неуспешной оплаты.",
    ),
    BotCommandSpec(
        BotCommandCategory.DEEP_LINK,
        "/start ref_&lt;code&gt;",
        "регистрация реферального приглашения.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH, "/admin", "открыть административное меню."
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/ahelp",
        "список всех команд администратора.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/stats",
        "общая статистика пользователей, подписок, оплат, скидок и рефералки.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/nodes",
        "безопасная сводка по настроенным нодам.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/node &lt;node_key&gt;",
        "безопасная диагностика одной ноды.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/check_payment &lt;provider_payment_id&gt;",
        "проверить и при необходимости финализировать платёж.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/payment &lt;provider_payment_id&gt;",
        "alias для /check_payment.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/user &lt;telegram_id&gt;",
        "карточка пользователя, подписка, trial, скидки, рефералка и платежи.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_SLASH,
        "/add_days &lt;telegram_id&gt; &lt;days&gt; [reason]",
        "вручную добавить дни к активной подписке.",
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_TEXT, "Админ", "открыть административное меню."
    ),
    BotCommandSpec(
        BotCommandCategory.ADMIN_TEXT,
        "XUI debug",
        "проверить доступность OpenAPI внешнего контура без создания пользователя.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_BUY_ACCESS,
        "выбрать тариф и создать заявку на оплату.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_PROFILE,
        "профиль, подписка, ссылка, документы и продление.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_INVITE_FRIEND,
        "получить реферальную ссылку.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_INSTRUCTION,
        "краткая инструкция по настройке.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON, BTN_SUPPORT, "контакты поддержки."
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_MY_SUBSCRIPTION,
        "статус подписки из профиля.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON,
        BTN_MY_LINK,
        "ссылка защищённого соединения из профиля.",
    ),
    BotCommandSpec(
        BotCommandCategory.USER_BUTTON, BTN_DOCUMENTS, "документы из профиля."
    ),
)


ADMIN_HELP_CATEGORY_ORDER: tuple[BotCommandCategory, ...] = (
    BotCommandCategory.USER_SLASH,
    BotCommandCategory.DEEP_LINK,
    BotCommandCategory.ADMIN_SLASH,
    BotCommandCategory.ADMIN_TEXT,
    BotCommandCategory.USER_BUTTON,
)


def render_admin_help_text() -> str:
    """Render complete administrator help text from the command registry."""
    sections = ["Справка администратора ЛадНет:"]
    for category in ADMIN_HELP_CATEGORY_ORDER:
        entries = [
            entry for entry in BOT_COMMAND_REGISTRY if entry.category == category
        ]
        if not entries:
            continue
        sections.append(
            f"{category.value}:\n"
            + "\n".join(
                f"• {entry.command} — {entry.description}" for entry in entries
            )
        )
    return "\n\n".join(sections)
