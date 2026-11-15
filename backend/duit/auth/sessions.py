"""Server-side sessions.

The browser holds a random 256-bit token in a cookie; the database holds only its SHA-256.
A plain hash (not argon2) is right here: the token is random, not a human-chosen password, so
there is nothing to guess, and lookups must be fast because they run on every request.

Cookie flags:
- `__Host-` prefix: the browser only accepts it with Secure, Path=/ and no Domain, so a
  sibling subdomain cannot plant or overwrite it.
- HttpOnly: JavaScript cannot read it, so an XSS bug cannot steal the session.
- Secure: HTTPS only (browsers treat http://localhost as secure, so dev still works).
- SameSite=Strict: never sent on requests started by other sites, which blocks CSRF.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from duit.auth.models import User, UserSession

COOKIE_NAME = "__Host-duit_session"
LIFETIME = timedelta(days=14)  # absolute: log in again at least every two weeks
IDLE_TIMEOUT = timedelta(days=2)  # unused for this long: logged out
TOUCH_EVERY = timedelta(minutes=5)  # write last_seen_at at most this often


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_session(db: AsyncSession, user: User, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + LIFETIME,
            user_agent=(user_agent or "")[:200] or None,
        )
    )
    await db.flush()
    return token


async def lookup(db: AsyncSession, token: str) -> tuple[UserSession, User] | None:
    """The live session and its user, or None. Expired or idle sessions are deleted."""
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    session, user = row
    now = datetime.now(UTC)
    if session.expires_at <= now or session.last_seen_at + IDLE_TIMEOUT <= now:
        await db.delete(session)
        await db.commit()
        return None
    if now - session.last_seen_at >= TOUCH_EVERY:
        session.last_seen_at = now
        await db.commit()
    return session, user


async def end_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
    await db.commit()


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(LIFETIME.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
