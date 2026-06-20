import asyncio
import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")
os.environ.setdefault("XUI_USERNAME", "xui-user")
os.environ.setdefault("XUI_PASSWORD", "xui-password")
os.environ.setdefault("XUI_INBOUND_IDS", "1")

from app.config import Settings
from app.db.models import Payment, PaymentStatus, User
from app.services.node_selector_service import NoAvailableNodeError
from app.services.payment_finalization_service import PaymentFinalizationService
import app.services.payment_finalization_service as finalization_module
from app.services.vpn_service import ProvisionResult


def _provision_result() -> ProvisionResult:
    return ProvisionResult(
        connection_link="vless://protected-link",
        expires_at=datetime(2026, 7, 19, 12, 30, tzinfo=UTC),
        action="created",
        subscription_id=42,
    )


def test_build_user_notification_for_created_includes_tariff_expiry_and_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "created",
        3,
    )

    assert text == (
        "Оплата подтверждена ✅\n\n"
        "Доступ создан на тариф <b>3 месяца</b>.\n"
        "Действует до: <code>2026-07-19 12:30 UTC</code>\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )


def test_build_user_notification_for_extended_mentions_existing_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "extended",
        12,
    )

    assert text == (
        "Оплата подтверждена ✅\n\n"
        "Подписка продлена на тариф <b>12 месяцев</b>.\n"
        "Ссылка для защищённого соединения остаётся прежней.\n"
        "Действует до: <code>2026-07-19 12:30 UTC</code>\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )


def test_build_user_notification_for_attached_existing_only_mentions_processed_payment_and_link() -> None:
    text = PaymentFinalizationService._build_user_notification(
        _provision_result(),
        "attached_existing",
        1,
    )

    assert text == (
        "Оплата уже была обработана ✅\n\n"
        "Ваша ссылка для защищённого соединения:\n"
        "<code>vless://protected-link</code>"
    )



class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def get(self, *args, **kwargs):
        return None


class _FakePaymentRepository:
    payment: Payment | None = None

    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def get_by_provider_payment_id_for_update(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> Payment | None:
        return self.payment


class _FakeSubscriptionRepository:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def get_by_last_payment_id_for_update(self, *args, **kwargs):
        return None


def _paid_payment(provider_payment_id: str = "payment-1") -> Payment:
    user = User(id=1, telegram_id=123456, is_active=True)
    payment = Payment(
        id=10,
        user_id=user.id,
        user=user,
        provider="test-provider",
        provider_payment_id=provider_payment_id,
        status=PaymentStatus.PAID,
        amount=100,
        currency="RUB",
        tariff_months=1,
        provider_data={"diagnostic": "kept"},
    )
    return payment


def _settings() -> Settings:
    return Settings(
        bot_token="bot-token",
        postgres_password="postgres-password",
        xui_username="xui-user",
        xui_password="xui-password",
        xui_inbound_ids=[1],
        test_mode=True,
    )


@pytest.fixture
def fake_finalization_dependencies(monkeypatch):
    _FakePaymentRepository.payment = None
    monkeypatch.setattr(finalization_module, "async_session_maker", lambda: _FakeSession())
    monkeypatch.setattr(finalization_module, "PaymentRepository", _FakePaymentRepository)
    monkeypatch.setattr(finalization_module, "SubscriptionRepository", _FakeSubscriptionRepository)

    async def noop_apply_pending_rewards(self, user_id: int) -> None:
        return None

    async def noop_grant_discount(self, user_id: int) -> bool:
        return False

    async def noop_apply_first_payment_rewards(self, user_id: int, months: int):
        return []

    monkeypatch.setattr(
        PaymentFinalizationService,
        "_apply_pending_rewards",
        noop_apply_pending_rewards,
    )
    monkeypatch.setattr(
        PaymentFinalizationService,
        "_grant_early_buyer_discount",
        noop_grant_discount,
    )
    monkeypatch.setattr(
        PaymentFinalizationService,
        "_apply_first_payment_rewards",
        noop_apply_first_payment_rewards,
    )
    return _FakePaymentRepository


def test_finalize_paid_payment_writes_started_and_finished_columns(
    fake_finalization_dependencies,
    monkeypatch,
) -> None:
    async def run() -> None:
        payment = _paid_payment()
        fake_finalization_dependencies.payment = payment
        provision_result = _provision_result()

        async def fake_provision(self, *args, **kwargs):
            return provision_result

        monkeypatch.setattr(PaymentFinalizationService, "_provision", fake_provision)

        result = await PaymentFinalizationService(_settings()).finalize_paid_payment(
            provider=payment.provider,
            provider_payment_id=payment.provider_payment_id,
            source="webhook",
        )

        assert result.status == "created"
        assert payment.finalization_started_at is not None
        assert payment.finalization_finished_at is not None
        assert payment.finalization_attempt_key
        assert payment.provider_data["diagnostic"] == "kept"
        assert "finalization_started_at" not in payment.provider_data
        assert "finalization_finished_at" not in payment.provider_data
        assert payment.provider_data["finalization_result"] == "created"

    asyncio.run(run())


def test_finalize_paid_payment_sets_no_available_nodes_block_columns(
    fake_finalization_dependencies,
    monkeypatch,
) -> None:
    async def run() -> None:
        payment = _paid_payment()
        fake_finalization_dependencies.payment = payment

        async def fake_provision(self, *args, **kwargs):
            raise NoAvailableNodeError("full")

        monkeypatch.setattr(PaymentFinalizationService, "_provision", fake_provision)

        result = await PaymentFinalizationService(_settings()).finalize_paid_payment(
            provider=payment.provider,
            provider_payment_id=payment.provider_payment_id,
            source="webhook",
        )

        assert result.status == "provisioning_blocked"
        assert payment.provisioning_blocked_reason == "no_available_nodes"
        assert payment.provisioning_blocked_at is not None
        assert payment.status == PaymentStatus.PAID
        assert "provisioning_blocked_reason" not in payment.provider_data
        assert "provisioning_blocked_at" not in payment.provider_data

    asyncio.run(run())


def test_finalize_paid_payment_retries_already_blocked_paid_payment(
    fake_finalization_dependencies,
    monkeypatch,
) -> None:
    async def run() -> None:
        payment = _paid_payment()
        payment.provisioning_blocked_reason = "no_available_nodes"
        payment.provisioning_blocked_at = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        fake_finalization_dependencies.payment = payment
        provision_result = _provision_result()

        async def fake_provision(self, *args, **kwargs):
            return provision_result

        monkeypatch.setattr(PaymentFinalizationService, "_provision", fake_provision)

        result = await PaymentFinalizationService(_settings()).finalize_paid_payment(
            provider=payment.provider,
            provider_payment_id=payment.provider_payment_id,
            source="retry",
        )

        assert result.status == "created"
        assert payment.subscription_id == provision_result.subscription_id
        assert payment.provisioning_blocked_reason is None
        assert payment.provisioning_blocked_at is None
        assert payment.finalization_finished_at is not None

    asyncio.run(run())
