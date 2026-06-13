"""VPN provisioning service."""

from app.config import Settings


class VpnService:
    """Coordinate VPN account provisioning and updates."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    async def create_client(self, telegram_id: int, months: int) -> str:
        """Create a VPN client and return a connection link for the user.

        The X-UI integration point is intentionally isolated here so bot handlers
        only depend on this service while the concrete panel API can evolve.
        """
        return (
            f"vless://user-{telegram_id}-{months}m@"
            f"{self.settings.xui_base_url.removeprefix('https://').removeprefix('http://')}"
            f"?type=tcp&security=none#SafetyWeb-{months}m"
        )
