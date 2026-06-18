"""HTTP application for payment provider callbacks."""

import hashlib
import hmac
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.repositories.payments import PaymentRepository
from app.db.session import async_session_maker
from app.services.payment_service import PLATEGA_PROVIDER_NAME
from app.services.platega_webhook_service import PlategaWebhookService

SENSITIVE_HEADER_MARKERS = (
    "authorization",
    "cookie",
    "secret",
    "token",
    "api-key",
    "apikey",
    "x-secret",
)


def create_app(settings: Settings | None = None) -> FastAPI:
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
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        payload = await _json_payload(request)
        _verify_callback_secret(request, app_settings)

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
        headers = _sanitize_headers(request.headers)

        async with async_session_maker() as session:
            repository = PaymentRepository(session)
            payment_id = None
            if provider_payment_id is not None:
                payment = await repository.get_by_provider_payment_id(
                    PLATEGA_PROVIDER_NAME,
                    provider_payment_id,
                )
                payment_id = payment.id if payment is not None else None

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
            PlategaWebhookService(app_settings).process_event,
            event.id,
        )
        return {"ok": True, "duplicate": False, "event_id": event.id}

    return app


app = create_app()


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


def _verify_callback_secret(request: Request, settings: Settings) -> None:
    if settings.platega_callback_secret is None:
        return

    expected = settings.platega_callback_secret.get_secret_value()
    provided = _extract_secret_header(request)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid callback secret",
        )


def _extract_secret_header(request: Request) -> str | None:
    for header_name in ("x-secret", "x-callback-secret", "x-platega-secret"):
        value = request.headers.get(header_name)
        if value:
            return value
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _sanitize_headers(headers: Any) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.lower()
        if any(marker in normalized for marker in SENSITIVE_HEADER_MARKERS):
            sanitized[key] = "***"
        else:
            sanitized[key] = value
    return sanitized


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
