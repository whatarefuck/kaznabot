from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from kaznabot.db import create_tables, make_engine, make_sessionmaker

pytest_plugins = ["tests.tg"]  # фикстура harness — фейковый Telegram с настоящим диспетчером


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = make_engine("sqlite+aiosqlite://")
    await create_tables(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with make_sessionmaker(engine)() as s:
        yield s
