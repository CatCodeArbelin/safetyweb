import asyncio
import os
import sys
import types
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from pydantic import SecretStr
from starlette.requests import Request

from app.config import Settings
from app.http_app import (
    CallbackHeaderVerificationError,
    INVALID_HEADER_ALERT_THROTTLE_SECONDS,
    _INVALID_HEADER_ALERTS,
    _notify_rejected_callback_headers,
    _verify_callback_headers,
)


def _settings() -> Settings:
    return Settings(
        platega_merchant_id="merchant-123",
        platega_api_key=SecretStr("api-key-secret"),
        platega_callback_secret=SecretStr("callback-secret"),
        platega_callback_path="/payments/platega/callback",
        admin_ids=[1001],
    )


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("example.test", 443),
            "path": "/payments/platega/callback",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.8", 43124),
        }
    )


def test_verify_callback_headers_distinguishes_invalid_merchant_id() -> None:
    request = _request(
        [
            (b"x-merchantid", b"wrong-merchant"),
            (b"x-secret", b"callback-secret"),
        ]
    )

    assert _verify_callback_headers(request, _settings()) == (
        CallbackHeaderVerificationError.MERCHANT_ID
    )


def test_verify_callback_headers_distinguishes_invalid_secret() -> None:
    request = _request(
        [
            (b"x-merchantid", b"merchant-123"),
            (b"x-secret", b"wrong-secret"),
        ]
    )

    assert _verify_callback_headers(request, _settings()) == (
        CallbackHeaderVerificationError.SECRET
    )


def test_notify_rejected_callback_headers_omits_secrets(monkeypatch) -> None:
    _INVALID_HEADER_ALERTS.clear()
    notify_admins = AsyncMock()
    fake_main = types.SimpleNamespace(notify_admins=notify_admins)
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    request = _request(
        [
            (b"x-merchantid", b"merchant-123"),
            (b"x-secret", b"callback-secret"),
            (b"user-agent", b"PlategaBot/1.0"),
            (b"x-forwarded-for", b"198.51.100.10"),
        ]
    )

    settings = _settings()
    asyncio.run(
        _notify_rejected_callback_headers(
            object(),
            settings,
            request,
            CallbackHeaderVerificationError.SECRET,
        )
    )

    notify_admins.assert_awaited_once()
    _, _, text = notify_admins.await_args.args
    assert "/payments/platega/callback" in text
    assert "203.0.113.8" in text
    assert "PlategaBot/1.0" in text
    assert "198.51.100.10" in text
    assert "merchant-123" in text
    assert "callback-secret" not in text
    assert "api-key-secret" not in text
    assert "PLATEGA_API_KEY" not in text
    assert "PLATEGA_CALLBACK_SECRET" not in text
    _INVALID_HEADER_ALERTS.clear()


def test_notify_rejected_callback_headers_throttles_by_reason_and_client(
    monkeypatch,
) -> None:
    _INVALID_HEADER_ALERTS.clear()
    notify_admins = AsyncMock()
    fake_main = types.SimpleNamespace(notify_admins=notify_admins)
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    request = _request(
        [
            (b"x-merchantid", b"merchant-123"),
            (b"x-secret", b"callback-secret"),
        ]
    )
    settings = _settings()

    asyncio.run(
        _notify_rejected_callback_headers(
            object(),
            settings,
            request,
            CallbackHeaderVerificationError.SECRET,
        )
    )
    asyncio.run(
        _notify_rejected_callback_headers(
            object(),
            settings,
            request,
            CallbackHeaderVerificationError.SECRET,
        )
    )

    notify_admins.assert_awaited_once()
    assert _INVALID_HEADER_ALERTS.keys() == {"secret:203.0.113.8"}
    _INVALID_HEADER_ALERTS.clear()


def test_notify_rejected_callback_headers_allows_after_throttle_window(
    monkeypatch,
) -> None:
    _INVALID_HEADER_ALERTS.clear()
    notify_admins = AsyncMock()
    fake_main = types.SimpleNamespace(notify_admins=notify_admins)
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    request = _request(
        [
            (b"x-merchantid", b"merchant-123"),
            (b"x-secret", b"callback-secret"),
        ]
    )
    settings = _settings()

    asyncio.run(
        _notify_rejected_callback_headers(
            object(),
            settings,
            request,
            CallbackHeaderVerificationError.SECRET,
        )
    )
    _INVALID_HEADER_ALERTS["secret:203.0.113.8"] -= timedelta(
        seconds=INVALID_HEADER_ALERT_THROTTLE_SECONDS + 1,
    )
    asyncio.run(
        _notify_rejected_callback_headers(
            object(),
            settings,
            request,
            CallbackHeaderVerificationError.SECRET,
        )
    )

    assert notify_admins.await_count == 2
    _INVALID_HEADER_ALERTS.clear()
