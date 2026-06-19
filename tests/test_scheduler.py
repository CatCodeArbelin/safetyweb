import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.config import Settings
from app.tasks.scheduler import (
    PLATEGA_RECONCILE_JOB_ID,
    PLATEGA_WEBHOOK_RETRY_JOB_ID,
    create_scheduler,
)


def make_settings(**overrides: object) -> Settings:
    values = {
        "bot_token": "bot-token",
        "postgres_password": "postgres-password",
        "xui_username": "xui-user",
        "xui_password": "xui-password",
        "xui_inbound_ids": [1],
    }
    values.update(overrides)
    return Settings(**values)


def test_create_scheduler_skips_platega_jobs_for_manual_provider() -> None:
    scheduler = create_scheduler(bot=object(), settings=make_settings(payment_provider="manual"))

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is None


def test_create_scheduler_skips_platega_jobs_in_test_mode() -> None:
    scheduler = create_scheduler(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=True,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is None


def test_create_scheduler_adds_platega_jobs_for_live_platega_provider() -> None:
    scheduler = create_scheduler(
        bot=object(),
        settings=make_settings(
            payment_provider="platega",
            test_mode=False,
            platega_merchant_id="merchant",
            platega_api_key="api-key",
            platega_return_url="https://example.test/return",
            platega_failed_url="https://example.test/failed",
        ),
    )

    assert scheduler.get_job(PLATEGA_RECONCILE_JOB_ID) is not None
    assert scheduler.get_job(PLATEGA_WEBHOOK_RETRY_JOB_ID) is not None
