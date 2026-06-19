import os
from datetime import UTC, datetime, timedelta, timezone

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")


def _extract_provider_expires_at(*args, **kwargs):
    from app.services.payment_service import _extract_provider_expires_at as extract

    return extract(*args, **kwargs)


def test_extract_provider_expires_at_parses_hh_mm_ss_duration() -> None:
    created_at = datetime(2026, 6, 19, 10, 30, tzinfo=UTC)

    result = _extract_provider_expires_at(
        {"expiresIn": "01:02:03"},
        created_at=created_at,
    )

    assert result == datetime(2026, 6, 19, 11, 32, 3, tzinfo=UTC)


def test_extract_provider_expires_at_normalizes_naive_created_at_for_duration() -> None:
    created_at = datetime(2026, 6, 19, 10, 30)

    result = _extract_provider_expires_at(
        {"expires_in": "00:30:00"},
        created_at=created_at,
    )

    assert result == datetime(2026, 6, 19, 11, 0, tzinfo=UTC)


def test_extract_provider_expires_at_returns_duration_as_utc_datetime() -> None:
    created_at = datetime(2026, 6, 19, 10, 30, tzinfo=timezone(timedelta(hours=3)))

    result = _extract_provider_expires_at(
        {"expiresIn": "00:30:00"},
        created_at=created_at,
    )

    assert result == datetime(2026, 6, 19, 8, 0, tzinfo=UTC)


def test_extract_provider_expires_at_keeps_numeric_seconds_behavior() -> None:
    created_at = datetime(2026, 6, 19, 10, 30, tzinfo=UTC)

    result = _extract_provider_expires_at(
        {"expiresIn": "90"},
        created_at=created_at,
    )

    assert result == created_at + timedelta(seconds=90)


def test_extract_provider_expires_at_keeps_iso_like_behavior() -> None:
    result = _extract_provider_expires_at({"expiresAt": "2026-06-19T10:30:00Z"})

    assert result == datetime(2026, 6, 19, 10, 30, tzinfo=UTC)
