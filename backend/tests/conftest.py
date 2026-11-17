"""Tests run against a real, migrated Postgres (compose.yaml locally, a service container in CI).
Each database test runs inside a transaction that is rolled back, so tests leave no data."""

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.core.db import create_engine
from duit.core.deps import get_db
from duit.core.settings import get_settings
from duit.main_api import app


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg (Procrastinate's driver) cannot use Windows' default Proactor event loop.
    options = {"loop_factory": asyncio.SelectorEventLoop} if os.name == "nt" else {}
    return "asyncio", options


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


# --- signed-in users (ledger tests) ----------------------------------------------------------

PASSWORD = "correct horse battery"


async def sign_in(http: httpx2.AsyncClient, email: str) -> None:
    creds = {"email": email, "password": PASSWORD}
    assert (await http.post("/api/auth/register", json=creds)).status_code == 201
    assert (await http.post("/api/auth/login", json=creds)).status_code == 200


@pytest.fixture
async def ana(client: httpx2.AsyncClient) -> httpx2.AsyncClient:
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"registration_open": True}
    )
    await sign_in(client, "ana@example.com")
    return client


@pytest.fixture
async def bob(ana: httpx2.AsyncClient) -> AsyncGenerator[httpx2.AsyncClient]:
    """A second browser, with its own cookie jar, on the same app and test transaction."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="https://testserver") as http:
        await sign_in(http, "bob@example.com")
        yield http
