# zapretvless / Arbelin One

Windows-клиент для управления VLESS и DPI Bypass режимами.

## Status

PR-01 bootstrap. Реальная VPN/DPI логика ещё не реализована.

## One-command startup

На Windows с установленным .NET 8 SDK:

```powershell
.\up.ps1
```

Команда выполняет restore/build/test, создаёт локальные папки `%LOCALAPPDATA%\ArbelinOne`, проверяет наличие optional engine binaries с warning при отсутствии и запускает WPF UI. Она не запускает VLESS, DPI Bypass, Xray, Zapret или WinDivert и не меняет proxy/DNS/routes.

## Safe local check

```powershell
.\scripts\dev-check.ps1
```

Safe check выполняет restore/build/test без запуска UI, Windows Service, Xray, Zapret, WinDivert и без изменения сетевых настроек.

## Docker check

```powershell
docker compose up --build
```

Docker Compose используется только для безопасных проверок. Реальный Windows-клиент, WPF UI, Windows Service, WinDivert, system proxy и сетевые настройки не запускаются из Docker.

Полная сборка solution может быть недоступна в Linux-контейнере из-за WPF-проекта. Поэтому Compose проверяет container-friendly части: Shared и xUnit tests.

## Engine binaries

Optional binaries are expected at:

```text
engines/xray/xray.exe
engines/zapret/winws.exe
engines/zapret/winws2.exe
engines/zapret/WinDivert64.sys
```

Engine binaries are not committed to this repository in PR-01.

## PR plan

```text
PR-01: Bootstrap
PR-02: UI skeleton
PR-03: VLESS parser
PR-04: Xray config generator
PR-05: Process supervisor
PR-06: Xray engine MVP
PR-07: DPI strategy loader
PR-08: Zapret engine MVP
PR-09: Stop All + Repair Network
PR-10: Optional system proxy
PR-11: Hybrid experimental
```

## Legacy files

The old Python Telegram bot code in `app/`, `tests/`, `alembic/`, and related Python configuration remains in the repository but is not used by the PR-01 Arbelin One Windows-client bootstrap. The previous Python Docker files were preserved as `Dockerfile.legacy` and `docker-compose.legacy.yml`.
