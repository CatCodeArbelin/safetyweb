# safetyweb

TG bot

## Docker Compose

The compose stack contains only the application bot, PostgreSQL, and Redis. The
3x-ui panel is intentionally not included; configure the external panel URL via
`XUI_BASE_URL` in `.env`.

Required `.env` values for local compose runs:

```env
BOT_TOKEN=replace-me
POSTGRES_DB=safetyweb
POSTGRES_USER=postgres
POSTGRES_PASSWORD=replace-me
XUI_BASE_URL=https://xui.example.com
XUI_USERNAME=replace-me
XUI_PASSWORD=replace-me
XUI_INBOUND_ID=1
ADMIN_IDS=123456789,987654321
```

Start the stack:

```bash
docker compose up --build
```

The `bot` service waits for PostgreSQL and Redis health checks and runs
`alembic upgrade head` before starting the Telegram bot. To run migrations as a
separate one-off command instead, use:

```bash
docker compose run --rm bot alembic upgrade head
```
