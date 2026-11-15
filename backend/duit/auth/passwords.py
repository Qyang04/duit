"""Password hashing with argon2id.

Parameters follow the OWASP minimum (19 MiB memory, 2 passes, 1 lane): each hash costs ~19 MiB,
which keeps several concurrent logins well inside the API's 192 MiB memory limit, while still
making offline guessing of a leaked hash slow. Raising them later is safe: old hashes keep
verifying and are upgraded on the next successful login (needs_rehash).

Hashing is CPU-bound and blocks for tens of milliseconds, so the async API runs it in a worker
thread instead of on the event loop.
"""

from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi.concurrency import run_in_threadpool

_hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)

MIN_LENGTH = 12
MAX_LENGTH = 128  # long enough for passphrases; caps the work an attacker can make us do


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_hasher.hash, password)


def _verify(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(stored_hash: str, password: str) -> bool:
    return await run_in_threadpool(_verify, stored_hash, password)


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash("not-a-real-password")


async def burn_time(password: str) -> None:
    """Verify against a dummy hash when the email is unknown, so "no such user" takes as long
    as "wrong password" and response times do not reveal which emails have accounts."""
    await run_in_threadpool(_verify, _dummy_hash(), password)
