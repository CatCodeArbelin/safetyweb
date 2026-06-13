"""User repository helpers."""

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository:
    """Persist and load Telegram users."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Return a user by Telegram id."""
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def get_or_create(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        """Return an existing user or create a new one."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)

        user.username = username
        user.first_name = first_name
        await self.session.flush()
        return user

    async def get_or_create_from_telegram(self, telegram_user: TelegramUser) -> User:
        """Upsert user fields from aiogram's Telegram user object."""
        return await self.get_or_create(
            telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
        )
