"""Tests run against a real, migrated Postgres (compose.yaml locally, a service container in CI).
Each database test runs inside a transaction that is rolled back, so tests leave no data."""

from collections.abc import AsyncGenerator

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.core.db import create_engine
from duit.core.deps import get_db
from duit.core.settings import get_settings
from duit.main_api import app


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


@pytest.fixture
async def client(conn: AsyncConnection) -> AsyncGenerator[httpx2.AsyncClient]:
    """HTTP client for the API, sharing the test's rolled-back transaction: the app's commits
    become savepoints, so every request sees earlier writes but nothing outlives the test."""
    session = AsyncSession(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    app.dependency_overrides[get_db] = lambda: session
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http  # https: the session cookie is Secure
    app.dependency_overrides.clear()
    await session.close()
