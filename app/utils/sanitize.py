"""Helpers for removing secrets from diagnostic data before storage/logging."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping

MASK = "***"

SENSITIVE_KEY_MARKERS = (
    "x-secret",
    "secret",
    "token",
    "password",
    "authorization",
    "cookie",
    "set-cookie",
    "api-key",
    "api_key",
    "apikey",
    "api-token",
    "private_key",
    "private-key",
    "private key",
    "connection_string",
    "postgres_password",
    "xui_password",
    "platega_api_key",
    "bot_token",
    "api_token",
)

PARTIAL_KEY_MARKERS = ("x-merchantid", "merchant_id", "merchantid")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s,;\"'<>]+")
_ASSIGNMENT_RE = re.compile(
    r'(?i)("?(?:x-secret|x-merchantid|authorization|cookie|set-cookie|password|token|secret|api[_-]?key|api[_-]?token|private[_ -]?key)"?\s*[:=]\s*)"?[^",&;\s}]+"?'
)


def sensitive_values_from(*values: Any) -> list[str]:
    """Return configured secret values as plain strings, omitting empty values."""
    secrets: list[str] = []
    for value in values:
        if value is None:
            continue
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        text = str(value)
        if text:
            secrets.append(text)
    return secrets


def sanitize_mapping(
    data: Mapping[Any, Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Sanitize a mapping recursively and stringify keys for JSON storage."""
    sanitized = sanitize_value(data, secrets=secrets)
    return sanitized if isinstance(sanitized, dict) else {}


def sanitize_dict(
    data: Mapping[Any, Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Backward-compatible alias for :func:`sanitize_mapping`."""
    return sanitize_mapping(data, secrets=secrets)


def sanitize_list(
    data: list[Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> list[Any]:
    """Sanitize a list recursively."""
    sanitized = sanitize_value(data, secrets=secrets)
    return sanitized if isinstance(sanitized, list) else []


def sanitize_headers(
    headers: Mapping[str, Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Sanitize HTTP headers, masking sensitive headers and partial merchant IDs."""
    return sanitize_mapping(headers, secrets=secrets)


def sanitize_exception(
    error: Exception,
    secrets: list[str] | tuple[str, ...] = (),
    limit: int = 2000,
) -> str:
    """Return compact exception text with known sensitive values removed."""
    message = " ".join(str(error).split()) or error.__class__.__name__
    return sanitize_string(message, secrets=secrets)[:limit]


def sanitize_string(value: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Mask known secret values and common credential patterns in a string."""
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, MASK)
    sanitized = _PRIVATE_KEY_RE.sub(MASK, sanitized)
    sanitized = _BOT_TOKEN_RE.sub(MASK, sanitized)
    sanitized = _BEARER_RE.sub(r"\1***", sanitized)
    sanitized = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{MASK}", sanitized)
    return sanitized


def sanitize_value(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    """Sanitize dictionaries, lists, strings, SecretStr-like values, and Decimals."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in value.items():
            key = str(item_key)
            if _is_sensitive_key(key):
                sanitized[key] = MASK
            elif _is_partial_key(key):
                sanitized[key] = _partial_mask(item_value)
            else:
                sanitized[key] = sanitize_value(item_value, secrets=secrets)
        return sanitized
    if isinstance(value, list):
        return [sanitize_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return sanitize_string(value, secrets=secrets)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return str(value)
    if hasattr(value, "get_secret_value"):
        return MASK
    return value


def _normalize_key_marker(value: str) -> str:
    return value.lower().replace("_", "-")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key_marker(key)
    return any(
        _normalize_key_marker(marker) in normalized
        for marker in SENSITIVE_KEY_MARKERS
    )


def _is_partial_key(key: str) -> bool:
    normalized = _normalize_key_marker(key)
    return any(_normalize_key_marker(marker) in normalized for marker in PARTIAL_KEY_MARKERS)


def _partial_mask(value: Any) -> str:
    text = str(value) if value is not None else ""
    if len(text) <= 4:
        return MASK
    return f"{text[:2]}{MASK}{text[-2:]}"
