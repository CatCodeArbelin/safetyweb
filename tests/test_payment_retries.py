from app.db.repositories.payments import webhook_retry_delay_seconds


def test_webhook_retry_delay_uses_exponential_backoff_with_cap() -> None:
    assert webhook_retry_delay_seconds(1, base_seconds=30, max_seconds=900) == 30
    assert webhook_retry_delay_seconds(2, base_seconds=30, max_seconds=900) == 60
    assert webhook_retry_delay_seconds(3, base_seconds=30, max_seconds=900) == 120
    assert webhook_retry_delay_seconds(6, base_seconds=30, max_seconds=900) == 900


def test_webhook_retry_delay_treats_non_positive_attempt_as_first_attempt() -> None:
    assert webhook_retry_delay_seconds(0, base_seconds=30, max_seconds=900) == 30
