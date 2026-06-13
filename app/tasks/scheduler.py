"""Background task scheduler."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler


def create_scheduler() -> AsyncIOScheduler:
    """Create an application scheduler instance."""
    return AsyncIOScheduler()
