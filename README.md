# zapretvless / Arbelin One

Windows-клиент для управления VLESS и DPI Bypass режимами.

## Status

PR-01 bootstrap. Реальная VPN/DPI логика ещё не реализована.

## Быстрый запуск

На Windows с установленным .NET 8 SDK:

```powershell
.\up.ps1
```

`up.ps1` выполняет безопасный bootstrap-сценарий:

1. `dotnet restore .\zapretvless.sln`
2. `dotnet build .\zapretvless.sln`
3. `dotnet test .\zapretvless.sln`
4. создаёт локальные папки `%LOCALAPPDATA%\ArbelinOne`, `%LOCALAPPDATA%\ArbelinOne\logs` и `%LOCALAPPDATA%\ArbelinOne\configs`;
5. проверяет наличие optional engine binaries и выводит warning при их отсутствии;
6. запускает WPF UI проекта `src\Arbelin.One.Client`.

Скрипт не запускает Xray, Zapret или WinDivert и не меняет proxy, DNS или routes.

## Проверка

Без запуска UI можно выполнить safe check:

```powershell
.\scripts\dev-check.ps1
```

Safe check выполняет restore/build/test для solution и проверяет наличие bootstrap-файлов и папок. Он не запускает WPF UI, Windows Service, Xray, Zapret, WinDivert и не изменяет сетевые настройки.

## Docker check mode

Docker используется только для безопасной проверки container-friendly части bootstrap:

```powershell
docker compose config
docker compose up --build
```

Compose-контейнер копирует repository snapshot во временную папку и запускает restore/test для `src/Arbelin.One.Tests/Arbelin.One.Tests.csproj`.

## Ограничения Docker

Docker check mode не предназначен для запуска Windows UI. WPF-проект, Windows Service, WinDivert, system proxy и сетевые настройки не запускаются из Linux-контейнера.

Полная сборка `zapretvless.sln` может быть недоступна в Linux-контейнере из-за Windows-only WPF-проекта. Это ожидаемое ограничение PR-01; для полной проверки используйте Windows и .NET 8 SDK.

## Engine binaries

Optional binaries ожидаются по путям:

```text
engines/xray/xray.exe
engines/zapret/winws.exe
engines/zapret/winws2.exe
engines/zapret/WinDivert64.sys
```

Engine binaries не коммитятся в репозиторий в PR-01. Их отсутствие должно давать warning, но не должно приводить к crash bootstrap-скриптов.

## Что не реализовано в PR-01

В PR-01 намеренно не реализованы:

- VLESS parser;
- XrayEngine;
- ZapretEngine;
- генерация Xray config;
- запуск Xray/Zapret/WinDivert;
- изменение proxy/DNS/routes;
- поставка бинарников xray/zapret/WinDivert.

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
