"""Safe X-UI health diagnostics."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings, XuiNodeConfig
from app.services.xui_client import XuiClient, XuiRequestError


@dataclass(frozen=True, slots=True)
class XuiHealthResult:
    ok: bool
    node_key: str
    node_name: str
    auth_mode: str
    base_url_safe: str
    public_host: str | None
    inbound_ids: list[int]
    checked_endpoint: str
    error_type: str | None = None
    error_message: str | None = None
    hint: str | None = None


def safe_base_url(value: str) -> str:
    """Return a base URL summary without exposing private panel web path."""
    parsed = urlsplit(value)
    if not parsed.netloc:
        return "configured" if value else "not configured"
    return f"{parsed.scheme}://{parsed.netloc}/…"


def xui_auth_hint(node: XuiNodeConfig, error: BaseException | None = None) -> str | None:
    """Return a safe admin hint for X-UI auth diagnostics."""
    token_configured = bool(
        node.xui_api_token and node.xui_api_token.get_secret_value().strip()
    )
    if token_configured and node.xui_auth_mode == "session_cookie":
        return (
            "В .env указан XUI_API_TOKEN, но не указан XUI_AUTH_MODE=api_token. "
            "Сейчас бот пытается войти через username/password. Добавьте "
            "XUI_AUTH_MODE=api_token или проверьте логин/пароль панели."
        )
    if node.xui_auth_mode == "api_token":
        return (
            "XUI_AUTH_MODE=api_token: проверьте XUI_API_TOKEN или переключите "
            "XUI_AUTH_MODE=session_cookie, если панель ожидает вход через cookie."
        )
    if node.xui_auth_mode == "session_cookie":
        return "Проверьте XUI_USERNAME/XUI_PASSWORD и XUI_BASE_URL панели."
    if node.xui_auth_mode == "auto":
        return "XUI_AUTH_MODE=auto: token не сработал или недоступен cookie-login."
    return str(error) if error else None


def _extract_inbound_ids(data: dict[str, Any]) -> set[int]:
    obj = data.get("obj", data.get("inbounds", data))
    if isinstance(obj, dict):
        maybe = obj.get("inbounds") or obj.get("list") or obj.get("items")
        obj = maybe if maybe is not None else [obj]
    if not isinstance(obj, list):
        return set()
    ids: set[int] = set()
    for item in obj:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                ids.add(int(item["id"]))
            except (TypeError, ValueError):
                pass
    return ids


async def check_node_health(settings: Settings, node: XuiNodeConfig) -> XuiHealthResult:
    """Check one X-UI node and return safe diagnostics."""
    endpoint = "/panel/api/inbounds/list"
    client: XuiClient | None = None
    try:
        client = XuiClient(settings=settings, node=node)
        try:
            data = await client.list_inbounds()
        except XuiRequestError as error:
            if "status=404" not in str(error):
                raise
            endpoint = "/panel/api/openapi.json"
            await client.get_openapi()
            data = {}
        found_ids = _extract_inbound_ids(data)
        if found_ids and not set(node.xui_inbound_ids).issubset(found_ids):
            missing = sorted(set(node.xui_inbound_ids) - found_ids)
            raise ValueError(f"Configured inbound IDs are missing in panel: {missing}")
        return XuiHealthResult(
            ok=True,
            node_key=node.key,
            node_name=node.name or node.key,
            auth_mode=node.xui_auth_mode,
            base_url_safe=safe_base_url(node.xui_base_url),
            public_host=node.xui_public_host,
            inbound_ids=list(node.xui_inbound_ids),
            checked_endpoint=endpoint,
        )
    except Exception as error:
        return XuiHealthResult(
            ok=False,
            node_key=node.key,
            node_name=node.name or node.key,
            auth_mode=node.xui_auth_mode,
            base_url_safe=safe_base_url(node.xui_base_url),
            public_host=node.xui_public_host,
            inbound_ids=list(node.xui_inbound_ids),
            checked_endpoint=endpoint,
            error_type=type(error).__name__,
            error_message=str(error),
            hint=xui_auth_hint(node, error),
        )
    finally:
        if client is not None:
            await client.close()
