import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.db.models import PaymentStatus
from app.services.platega_webhook_service import map_platega_status


def test_map_platega_status_handles_official_statuses() -> None:
    assert map_platega_status("PENDING") == PaymentStatus.PENDING
    assert map_platega_status("CONFIRMED") == PaymentStatus.PAID
    assert map_platega_status("CANCELED") == PaymentStatus.FAILED
    assert map_platega_status("CHARGEBACKED") == PaymentStatus.REFUNDED


def test_map_platega_status_does_not_treat_expired_as_provider_expired() -> None:
    assert map_platega_status("expired") == PaymentStatus.PENDING
