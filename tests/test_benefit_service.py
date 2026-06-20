import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "bot-token")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres-password")

from app.config import Settings
from app.db.models import CustomerBenefit, User
from app.db.session import async_session_maker
from app.services.benefit_service import BenefitService, EARLY_BUYER_BENEFIT_TYPE


def test_grant_early_buyer_discount_on_start_is_idempotent_and_race_safe() -> None:
    async def run() -> None:
        telegram_id = 670831478
        settings = Settings(
            early_buyer_discount_enabled=True,
            early_buyer_limit=100,
            early_buyer_discount_percent=15,
        )
        service = BenefitService(settings=settings)

        async def delete_test_user() -> None:
            async with async_session_maker() as session, session.begin():
                user_id = await session.scalar(
                    select(User.id).where(User.telegram_id == telegram_id)
                )
                if user_id is not None:
                    await session.execute(
                        delete(CustomerBenefit).where(CustomerBenefit.user_id == user_id)
                    )
                    await session.execute(delete(User).where(User.id == user_id))

        async def benefit_count() -> int:
            async with async_session_maker() as session:
                count = await session.scalar(
                    select(func.count(CustomerBenefit.id))
                    .join(User, CustomerBenefit.user_id == User.id)
                    .where(
                        User.telegram_id == telegram_id,
                        CustomerBenefit.benefit_type == EARLY_BUYER_BENEFIT_TYPE,
                    )
                )
                return int(count or 0)

        try:
            await delete_test_user()
        except OSError as exc:
            pytest.skip(f"test database is unavailable: {exc}")

        try:
            assert (
                await service.grant_early_buyer_discount_on_start_if_eligible(
                    telegram_id
                )
                is True
            )
            assert (
                await service.grant_early_buyer_discount_on_start_if_eligible(
                    telegram_id
                )
                is False
            )
            assert await benefit_count() == 1

            await delete_test_user()

            results = await asyncio.gather(
                *(
                    service.grant_early_buyer_discount_on_start_if_eligible(
                        telegram_id
                    )
                    for _ in range(2)
                )
            )

            assert sorted(results) == [False, True]
            assert await benefit_count() == 1
        finally:
            await delete_test_user()

    asyncio.run(run())
