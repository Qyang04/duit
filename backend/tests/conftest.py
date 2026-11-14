"""Tests run against a real, migrated Postgres (compose.yaml locally, a service container in CI).
Each database test runs inside a transaction that is rolled back, so tests leave no data."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from duit.core.db import create_engine
from duit.core.settings import get_settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def conn() -> AsyncGenerator[AsyncConnection]:
    engine = create_engine(get_settings())
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()
