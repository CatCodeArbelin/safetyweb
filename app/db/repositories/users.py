"""User repository helpers."""

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository:
    """Persist and load Telegram users."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Return a user by Telegram id."""
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def upsert_telegram_user(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
    ) -> tuple[User, bool]:
        """Atomically insert or update a Telegram user via PostgreSQL upsert."""
        existing_id = await self.session.scalar(
            select(User.id).where(User.telegram_id == telegram_id)
        )

        statement = (
            insert(User)
            .values(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=[User.telegram_id],
                set_={
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "language_code": language_code,
                    "is_active": True,
                    "updated_at": func.now(),
                },
            )
            .returning(User.id)
        )
        user_id = await self.session.scalar(statement)
        user = await self.session.get(User, user_id)
        if user is None:
            msg = f"Upserted Telegram user {telegram_id} was not found"
            raise RuntimeError(msg)
        return user, existing_id is None

    async def get_or_create(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        """Return an existing user or create a new one atomically."""
        user, _ = await self.upsert_telegram_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        return user

    async def get_or_create_from_telegram(
        self, telegram_user: TelegramUser
    ) -> tuple[User, bool]:
        """Upsert user fields from aiogram's Telegram user object.

        Returns the user and whether it was absent before this call's upsert.
        """
        return await self.upsert_telegram_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )
