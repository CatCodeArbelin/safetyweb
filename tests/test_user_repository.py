import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")

from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.sql.selectable import Select

from app.db.models import User
from app.db.repositories.users import UserRepository


class FakeSession:
    def __init__(self) -> None:
        self.users: dict[int, User] = {}
        self.upsert_count = 0

    def _value(self, statement, name: str):
        for column, value in statement._values.items():
            if column.name == name:
                return value.value
        raise AssertionError(f"Missing value for {name}")

    async def scalar(self, statement):
        if isinstance(statement, Select):
            return None
        if isinstance(statement, Insert):
            self.upsert_count += 1
            telegram_id = self._value(statement, "telegram_id")
            user = self.users.get(telegram_id)
            if user is None:
                user = User(id=1, telegram_id=telegram_id)
                self.users[telegram_id] = user
            user.username = self._value(statement, "username")
            user.first_name = self._value(statement, "first_name")
            user.last_name = self._value(statement, "last_name")
            user.language_code = self._value(statement, "language_code")
            user.is_active = self._value(statement, "is_active")
            return user.id
        raise AssertionError(f"Unexpected statement: {statement!r}")

    async def get(self, model, user_id: int):
        assert model is User
        return next(user for user in self.users.values() if user.id == user_id)


def test_get_or_create_from_telegram_is_race_safe() -> None:
    async def run() -> None:
        session = FakeSession()
        telegram_user = SimpleNamespace(
            id=670831477,
            username="thekingdomton",
            first_name="Kingdom",
            last_name="Ton",
            language_code="ru",
        )

        async def create_once():
            repo = UserRepository(session)  # type: ignore[arg-type]
            return await repo.get_or_create_from_telegram(telegram_user)  # type: ignore[arg-type]

        results = await asyncio.gather(create_once(), create_once(), create_once())

        assert all(user.telegram_id == 670831477 for user, _ in results)
        assert len(session.users) == 1
        assert session.upsert_count == 3

    asyncio.run(run())
