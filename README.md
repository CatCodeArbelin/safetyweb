# SafetyWeb

SafetyWeb — Telegram-бот для продажи и выдачи VPN-доступов через внешний 3x-ui. Бот показывает тарифы, создаёт MVP-заявку на оплату, уведомляет администраторов, после ручного подтверждения оплаты создаёт клиента в 3x-ui и отправляет пользователю VPN-ссылку.

## Стек

- Python 3.12
- aiogram и aiogram-dialog для Telegram-бота
- SQLAlchemy AsyncIO и asyncpg для работы с PostgreSQL
- Alembic для миграций базы данных
- Redis для FSM-хранилища aiogram
- APScheduler для фоновых задач и уведомлений
- httpx для интеграции с 3x-ui
- Pydantic Settings для конфигурации из окружения
- Docker Compose для локального запуска инфраструктуры

## Настройка `.env`

Скопируйте пример окружения и заполните значения:

```bash
cp .env.example .env
```

Минимальный набор переменных для локального запуска через Docker Compose:

```env
# Telegram bot token from BotFather.
BOT_TOKEN=replace-me

# PostgreSQL connection settings.
POSTGRES_DB=safetyweb
POSTGRES_USER=postgres
POSTGRES_PASSWORD=replace-me

# 3x-ui panel connection settings.
XUI_BASE_URL=https://xui.example.com
XUI_USERNAME=replace-me
XUI_PASSWORD=replace-me
XUI_INBOUND_ID=1

# Comma-separated Telegram user IDs with administrator permissions.
ADMIN_IDS=123456789,987654321
```

Дополнительно можно переопределить `POSTGRES_HOST`, `POSTGRES_PORT`, `REDIS_URL` и `XUI_EXPIRED_CLIENT_POLICY`. В Docker Compose значения `POSTGRES_HOST`, `POSTGRES_PORT` и `REDIS_URL` для контейнера бота задаются автоматически и указывают на сервисы `postgres` и `redis`.

### Обязательные настройки 3x-ui

3x-ui запускается и администрируется отдельно: compose-файл проекта поднимает только бота, PostgreSQL и Redis. Для интеграции с уже установленной панелью 3x-ui обязательно заполните:

- `XUI_BASE_URL` — базовый URL панели 3x-ui, например `https://xui.example.com`.
- `XUI_USERNAME` — имя пользователя администратора 3x-ui.
- `XUI_PASSWORD` — пароль администратора 3x-ui.
- `XUI_INBOUND_ID` — ID inbound, в который бот будет добавлять VPN-клиентов.

## Запуск через Docker Compose

1. Подготовьте `.env` по инструкции выше.
2. Запустите стек:

```bash
docker compose up --build
```

Compose поднимает сервисы:

- `bot` — приложение Telegram-бота;
- `postgres` — база данных PostgreSQL;
- `redis` — хранилище FSM-состояний.

Сервис `bot` ожидает успешные health checks PostgreSQL и Redis. При старте контейнера автоматически выполняется `alembic upgrade head`, затем запускается бот командой `python -m app.main`.

Для фонового запуска используйте:

```bash
docker compose up --build -d
```

Просмотр логов бота:

```bash
docker compose logs -f bot
```

Остановка стека:

```bash
docker compose down
```

## Alembic-миграции

Миграции находятся в каталоге `alembic/versions`. В штатном Docker Compose-сценарии они применяются автоматически при запуске контейнера `bot`.

Если нужно применить миграции вручную отдельной командой, выполните:

```bash
docker compose run --rm bot alembic upgrade head
```

Для локального запуска без Docker Compose убедитесь, что переменные подключения к PostgreSQL доступны в окружении или `.env`, затем выполните:

```bash
alembic upgrade head
```

## Ручной MVP-сценарий оплаты

Текущая MVP-реализация использует ручное подтверждение оплаты администратором:

1. Пользователь запускает бота командой `/start`.
2. Пользователь нажимает «Купить VPN» и выбирает тариф на 1, 3 или 6 месяцев.
3. Бот создаёт заявку на ручную оплату со статусом `pending` и провайдером `manual`.
4. Администраторы из `ADMIN_IDS` получают сообщение с данными пользователя, тарифом, суммой и ID платежа.
5. Администратор проверяет поступление оплаты вне бота и нажимает «Подтвердить оплату».
6. Бот переводит платёж в статус `paid`, создаёт клиента во внешнем 3x-ui inbound и отправляет пользователю VPN-ссылку.

Тарифные суммы в MVP сейчас заданы в коде как `0 RUB`; перед боевым использованием их нужно заменить на реальные значения и согласовать с будущим платёжным провайдером.

## Платёжные провайдеры

Интеграции YooKassa и Robokassa пока не подключены. Их подключение запланировано следующим этапом после ручного MVP-сценария оплаты.
