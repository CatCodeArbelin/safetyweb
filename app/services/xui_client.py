"""HTTP client for the X-UI panel."""

import httpx


class XuiClient:
    """Minimal asynchronous X-UI API client."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
