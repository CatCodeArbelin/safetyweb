"""HTTP application for payment provider callbacks."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.payment_service import PLATEGA_PROVIDER_NAME
from app.services.platega_webhook_service import PlategaWebhookService
from app.utils.sanitize import sanitize_dict, sanitize_headers

if TYPE_CHECKING:
    from aiogram import Bot


_INVALID_HEADER_ALERTS: dict[str, datetime] = {}
INVALID_HEADER_ALERT_THROTTLE_SECONDS = 900


def create_app(settings: Settings | None = None, bot: Bot | None = None) -> FastAPI:
    """Create the FastAPI application."""
    app_settings = settings or Settings()
    app = FastAPI(title="SafetyWeb HTTP callbacks")

    @app.post(app_settings.platega_callback_path, status_code=status.HTTP_202_ACCEPTED)
    async def platega_callback(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Persist a Platega callback and enqueue asynchronous processing."""
        raw_body = await request.body()
        payload_hash = hashlib.sha256(b"platega:" + raw_body).hexdigest()
        verification_error = _verify_callback_headers(request, app_settings)
        if verification_error is not None:
            await _notify_rejected_callback_headers(
                bot,
                app_settings,
                request,
                verification_error,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=verification_error.detail,
            )
        payload = sanitize_dict(await _json_payload(request))

        provider_payment_id = _extract_first_str(
            payload,
            "transactionId",
            "transaction_id",
            "id",
            "paymentId",
            "payment_id",
            "uuid",
        )
        event_status = _extract_first_str(
            payload,
            "status",
            "state",
            "transactionStatus",
            "transaction_status",
            "paymentStatus",
            "payment_status",
        )
        headers = _safe_callback_headers(request)

        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            payment_id = _extract_internal_payment_id(payload)
            if provider_payment_id is not None:
                payment = await repository.get_by_provider_payment_id(
                    PLATEGA_PROVIDER_NAME,
                    provider_payment_id,
                )
                payment_id = payment.id if payment is not None else payment_id

            try:
                event = await repository.create_webhook_event(
                    provider=PLATEGA_PROVIDER_NAME,
                    payload_hash=payload_hash,
                    headers=headers,
                    raw_body=raw_body,
                    provider_payment_id=provider_payment_id,
                    payment_id=payment_id,
                    event_status=event_status,
                    payload=payload,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                event = await repository.get_webhook_event_by_payload_hash(
                    PLATEGA_PROVIDER_NAME,
                    payload_hash,
                )
                return {
                    "ok": True,
                    "duplicate": True,
                    "event_id": event.id if event else None,
                }

        background_tasks.add_task(
            PlategaWebhookService(app_settings, bot=bot).process_event,
            event.id,
        )
        return {"ok": True, "duplicate": False, "event_id": event.id}

    return app


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Callback body must be valid JSON",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Callback body must be a JSON object",
        )
    return payload


class CallbackHeaderVerificationError(StrEnum):
    """Reasons a Platega callback header verification can fail."""

    MERCHANT_ID = "merchant_id"
    SECRET = "secret"

    @property
    def detail(self) -> str:
        """Return the HTTP error detail for this verification failure."""
        if self is CallbackHeaderVerificationError.MERCHANT_ID:
            return "Invalid callback merchant id"
        return "Invalid callback secret"


def _verify_callback_headers(
    request: Request,
    settings: Settings,
) -> CallbackHeaderVerificationError | None:
    merchant_id = request.headers.get("x-merchantid", "")
    expected_merchant_id = settings.platega_merchant_id or "\0"
    if not hmac.compare_digest(merchant_id, expected_merchant_id):
        return CallbackHeaderVerificationError.MERCHANT_ID

    secret = request.headers.get("x-secret", "")
    expected_secret = _expected_callback_secret(settings)
    if not hmac.compare_digest(secret, expected_secret):
        return CallbackHeaderVerificationError.SECRET

    return None


async def _notify_rejected_callback_headers(
    bot: Bot | None,
    settings: Settings,
    request: Request,
    reason: CallbackHeaderVerificationError,
) -> None:
    """Notify admins about rejected Platega callback headers without secrets."""
    if bot is None:
        return

    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{reason.value}:{client_ip}"
    now = datetime.now(UTC)
    last_alerted_at = _INVALID_HEADER_ALERTS.get(throttle_key)
    if (
        last_alerted_at is not None
        and now - last_alerted_at
        < timedelta(seconds=INVALID_HEADER_ALERT_THROTTLE_SECONDS)
    ):
        return

    from app.main import notify_admins

    sanitized_merchant_headers = sanitize_headers(
        {"x-merchantid": request.headers.get("x-merchantid") or "missing"}
    )
    text = (
        "⚠️ Rejected Platega callback headers\n"
        f"Reason: invalid {reason.value}\n"
        f"Path: {request.url.path}\n"
        f"Client: {client_ip}\n"
        f"User-Agent: {request.headers.get('user-agent') or 'unknown'}\n"
        f"X-Forwarded-For: {request.headers.get('x-forwarded-for') or 'unknown'}\n"
        f"Merchant ID: {sanitized_merchant_headers['x-merchantid']}"
    )
    await notify_admins(bot, settings, text)
    _INVALID_HEADER_ALERTS[throttle_key] = now


def _expected_callback_secret(settings: Settings) -> str:
    secret = settings.platega_callback_secret or settings.platega_api_key
    return secret.get_secret_value() if secret is not None else "\0"


def _safe_callback_headers(request: Request) -> dict[str, str | bool | None]:
    return sanitize_headers(
        {
            "x-merchantid": request.headers.get("x-merchantid"),
            "x-secret": request.headers.get("x-secret"),
            "authorization": request.headers.get("authorization"),
            "cookie": request.headers.get("cookie"),
            "set-cookie": request.headers.get("set-cookie"),
            "secret_verified": True,
            "user_agent": request.headers.get("user-agent"),
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
        }
    )


def _extract_first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    for nested_key in ("payload", "metadata", "data", "transaction"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            value = _extract_first_str(nested, *keys)
            if value is not None:
                return value
    return None


def _extract_internal_payment_id(data: dict[str, Any]) -> int | None:
    for value in _candidate_values(data, "internalPaymentId", "paymentId"):
        try:
            payment_id = int(value)
        except (TypeError, ValueError):
            continue
        if payment_id > 0:
            return payment_id
    return None


def _candidate_values(data: Any, *keys: str) -> list[Any]:
    values: list[Any] = []
    if not isinstance(data, dict):
        return values
    for key in keys:
        if key in data:
            values.append(data[key])
    for nested_key in ("payload", "metadata", "data", "transaction"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            values.extend(_candidate_values(nested, *keys))
    return values
