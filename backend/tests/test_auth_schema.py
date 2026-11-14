"""Guarantees the database enforces on its own, even if app code has a bug."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from duit.core.auth import User, UserSession

pytestmark = pytest.mark.anyio


async def add_user(conn: AsyncConnection, email: str) -> object:
    result = await conn.execute(
        insert(User).values(email=email, password_hash="x").returning(User.id)
    )
    return result.scalar_one()


async def test_email_must_be_stored_lowercase(conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"):
        await add_user(conn, "Ana@Example.com")


async def test_email_is_unique(conn: AsyncConnection) -> None:
    await add_user(conn, "ana@example.com")
    with pytest.raises(IntegrityError, match="uq_users_email"):
        await add_user(conn, "ana@example.com")


async def test_deleting_a_user_deletes_their_sessions(conn: AsyncConnection) -> None:
    user_id = await add_user(conn, "ana@example.com")
    await conn.execute(
        insert(UserSession).values(
            user_id=user_id,
            token_hash=b"\x00" * 32,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )

    await conn.execute(delete(User).where(User.id == user_id))

    remaining = await conn.scalar(select(func.count()).select_from(UserSession))
    assert remaining == 0
