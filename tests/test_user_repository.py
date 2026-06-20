import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")

from app.db.models import User
from app.db.repositories.users import UserRepository
from app.db.session import async_session_maker


@pytest.mark.asyncio
async def test_upsert_telegram_user_is_race_safe_across_sessions() -> None:
    telegram_id = 670831477

    async def delete_test_user() -> None:
        async with async_session_maker() as session, session.begin():
            existing = await session.scalar(
                select(User).where(User.telegram_id == telegram_id)
            )
            if existing is not None:
                await session.delete(existing)

    try:
        await delete_test_user()
    except OSError as exc:
        pytest.skip(f"test database is unavailable: {exc}")

    try:
        async with async_session_maker() as session, session.begin():
            repo = UserRepository(session)
            await repo.upsert_telegram_user(
                telegram_id=telegram_id,
                username="before_race",
                first_name="Before",
                last_name="Race",
                language_code="en",
            )

        async def upsert_once() -> tuple[User, bool]:
            async with async_session_maker() as session, session.begin():
                repo = UserRepository(session)
                return await repo.upsert_telegram_user(
                    telegram_id=telegram_id,
                    username="thekingdomton",
                    first_name="Kingdom",
                    last_name="Ton",
                    language_code="ru",
                )

        results = await asyncio.gather(*(upsert_once() for _ in range(8)))

        assert all(user.telegram_id == telegram_id for user, _ in results)
        assert User.__table__.c.telegram_id.unique is True

        async with async_session_maker() as session:
            user_count = await session.scalar(
                select(func.count(User.id)).where(User.telegram_id == telegram_id)
            )
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id)
            )

        assert user_count == 1
        assert user is not None
        assert user.username == "thekingdomton"
        assert user.first_name == "Kingdom"
        assert user.last_name == "Ton"
        assert user.language_code == "ru"
        assert user.is_active is True
    finally:
        await delete_test_user()

