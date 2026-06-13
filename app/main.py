"""Application entry point."""

import asyncio

from app.config import Settings


async def main() -> None:
    """Start the application."""
    settings = Settings()
    _ = settings


if __name__ == "__main__":
    asyncio.run(main())
