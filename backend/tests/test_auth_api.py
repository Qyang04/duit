"""The /auth endpoints, end to end against the real database."""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from pyotp.totp import TOTP
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from duit.auth.models import User, UserSession
from duit.auth.sessions import COOKIE_NAME
from duit.core.settings import get_settings
from duit.ledger.models import Category
from duit.main_api import app

pytestmark = pytest.mark.anyio

EMAIL = "ana@example.com"
PASSWORD = "correct horse battery"


async def register(client: httpx2.AsyncClient, email: str = EMAIL) -> httpx2.Response:
    return await client.post("/auth/register", json={"email": email, "password": PASSWORD})


async def login(client: httpx2.AsyncClient, **extra: str) -> httpx2.Response:
    return await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD, **extra})


@pytest.fixture
def open_registration() -> None:
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"registration_open": True}
    )


# --- register --------------------------------------------------------------------------------


async def test_register_stores_lowercase_email_and_argon2_hash(
    client: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    response = await register(client, "Ana@Example.com")

    assert response.status_code == 201
    assert response.json()["email"] == EMAIL
    stored = await conn.scalar(select(User.password_hash).where(User.email == EMAIL))
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


async def test_register_seeds_default_categories(
    client: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    user_id = (await register(client)).json()["id"]

    count = await conn.scalar(select(func.count()).where(Category.user_id == user_id))
    assert count is not None and count > 0


async def test_only_the_first_account_can_register_by_default(client: httpx2.AsyncClient) -> None:
    assert (await register(client)).status_code == 201
    second = await register(client, "mallory@example.com")

    assert second.status_code == 403


@pytest.mark.usefixtures("open_registration")
async def test_duplicate_email_is_rejected(client: httpx2.AsyncClient) -> None:
    await register(client)
    assert (await register(client, "ANA@example.com")).status_code == 409


async def test_short_password_is_rejected(client: httpx2.AsyncClient) -> None:
    response = await client.post("/auth/register", json={"email": EMAIL, "password": "short"})
    assert response.status_code == 422


# --- login, me, logout -----------------------------------------------------------------------


async def test_login_sets_a_hardened_session_cookie(client: httpx2.AsyncClient) -> None:
    await register(client)
    response = await login(client)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag in cookie
    assert "Domain" not in cookie

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


async def test_only_a_hash_of_the_session_token_is_stored(
    client: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    await register(client)
    await login(client)
    token = client.cookies[COOKIE_NAME]

    stored = await conn.scalar(select(UserSession.token_hash))
    assert stored is not None
    assert len(stored) == 32
    assert token.encode() not in stored


async def test_wrong_password_and_unknown_email_look_the_same(client: httpx2.AsyncClient) -> None:
    await register(client)
    wrong = await client.post("/auth/login", json={"email": EMAIL, "password": "nope" * 4})
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_me_without_a_session_is_401(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/auth/me")).status_code == 401


async def test_logout_deletes_the_session(
    client: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    await register(client)
    await login(client)
    token = client.cookies[COOKIE_NAME]

    assert (await client.post("/auth/logout")).status_code == 204
    assert await conn.scalar(select(UserSession.id)) is None
    # Even a client that kept the old cookie is logged out: the server forgot the session.
    client.cookies.set(COOKIE_NAME, token)
    assert (await client.get("/auth/me")).status_code == 401


async def test_idle_session_expires(client: httpx2.AsyncClient, conn: AsyncConnection) -> None:
    await register(client)
    await login(client)
    await conn.execute(
        update(UserSession).values(last_seen_at=datetime.now(UTC) - timedelta(days=3))
    )

    assert (await client.get("/auth/me")).status_code == 401
    assert await conn.scalar(select(UserSession.id)) is None


# --- TOTP ------------------------------------------------------------------------------------


async def enable_totp(client: httpx2.AsyncClient) -> TOTP:
    await register(client)
    await login(client)
    setup = await client.post("/auth/totp/setup")
    assert setup.status_code == 200
    authenticator = TOTP(setup.json()["secret"])
    confirm = await client.post("/auth/totp/confirm", json={"code": authenticator.now()})
    assert confirm.status_code == 204
    await client.post("/auth/logout")
    return authenticator


def next_code(authenticator: TOTP) -> str:
    """The code for the next 30-second step: valid now (drift window), never used yet."""
    return authenticator.at(datetime.now(UTC) + timedelta(seconds=30))


async def test_totp_secret_is_stored_encrypted(
    client: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    await register(client)
    await login(client)
    secret = (await client.post("/auth/totp/setup")).json()["secret"]

    stored = await conn.scalar(select(User.totp_secret_enc))
    assert stored is not None
    assert secret.encode() not in stored


async def test_totp_is_not_enforced_until_confirmed(client: httpx2.AsyncClient) -> None:
    await register(client)
    await login(client)
    await client.post("/auth/totp/setup")
    await client.post("/auth/logout")

    assert (await login(client)).status_code == 200


async def test_wrong_confirmation_code_is_rejected(client: httpx2.AsyncClient) -> None:
    await register(client)
    await login(client)
    await client.post("/auth/totp/setup")

    assert (await client.post("/auth/totp/confirm", json={"code": "000000"})).status_code == 400
    assert (await client.get("/auth/me")).json()["totp_enabled"] is False


async def test_login_with_totp(client: httpx2.AsyncClient) -> None:
    authenticator = await enable_totp(client)

    without_code = await login(client)
    assert without_code.status_code == 401
    assert without_code.json()["detail"] == "TOTP code required"

    assert (await login(client, totp_code="123456")).status_code == 401
    ok = await login(client, totp_code=next_code(authenticator))
    assert ok.status_code == 200
    assert ok.json()["totp_enabled"] is True


async def test_a_totp_code_works_only_once(client: httpx2.AsyncClient) -> None:
    authenticator = await enable_totp(client)
    code = next_code(authenticator)

    assert (await login(client, totp_code=code)).status_code == 200
    assert (await login(client, totp_code=code)).status_code == 401
