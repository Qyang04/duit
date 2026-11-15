"""TOTP (RFC 6238) second factor: 6 digits, 30-second steps, the default of every authenticator app.

Codes from the previous and next step are accepted to tolerate clock drift, and the last
accepted step is stored per user so a code cannot be used twice (replay protection).
"""

import hmac
import time

from pyotp import random_base32
from pyotp.totp import TOTP

STEP_SECONDS = 30
ISSUER = "Duit"


def new_secret() -> str:
    return random_base32()  # 160 bits, as RFC 4226 recommends


def provisioning_uri(secret: str, email: str) -> str:
    """otpauth:// URI; authenticator apps import it from a QR code or by pasting the secret."""
    uri: str = TOTP(secret).provisioning_uri(  # pyright: ignore[reportUnknownMemberType]
        name=email, issuer_name=ISSUER
    )  # (untyped **kwargs in pyotp; the return type is declared)
    return uri


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def match_step(
    secret: str, code: str, last_step: int | None, now: float | None = None
) -> int | None:
    """The time step `code` belongs to, or None if it is wrong, expired or already used."""
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    totp = TOTP(secret)
    step_now = current_step(now)
    for step in (step_now - 1, step_now, step_now + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            return step
    return None
