"""Pure functions behind auth: TOTP matching and column encryption."""

import os

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from pyotp import random_base32
from pyotp.totp import TOTP

from duit.auth import totp
from duit.core.crypto import DataKeyError, decrypt, encrypt, load_data_key
from duit.core.settings import get_settings

SECRET = random_base32()
NOW = 1_800_000_000.0  # a fixed instant, so tests do not depend on the clock


def code_at(offset_steps: int) -> str:
    return TOTP(SECRET).at(NOW + offset_steps * totp.STEP_SECONDS)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_codes_within_one_step_of_drift_are_accepted(offset: int) -> None:
    assert (
        totp.match_step(SECRET, code_at(offset), None, now=NOW) == totp.current_step(NOW) + offset
    )


@pytest.mark.parametrize("offset", [-3, -2, 2, 3])
def test_codes_further_away_are_rejected(offset: int) -> None:
    assert totp.match_step(SECRET, code_at(offset), None, now=NOW) is None


def test_used_steps_cannot_be_replayed() -> None:
    step = totp.current_step(NOW)
    assert totp.match_step(SECRET, code_at(0), last_step=step, now=NOW) is None
    assert totp.match_step(SECRET, code_at(1), last_step=step, now=NOW) == step + 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef"])
def test_malformed_codes_are_rejected(code: str) -> None:
    assert totp.match_step(SECRET, code, None, now=NOW) is None


def test_encryption_round_trip_and_binding() -> None:
    key = os.urandom(32)
    blob = encrypt(key, b"JBSWY3DPEHPK3PXP", b"totp:user-a")

    assert decrypt(key, blob, b"totp:user-a") == b"JBSWY3DPEHPK3PXP"
    assert encrypt(key, b"JBSWY3DPEHPK3PXP", b"totp:user-a") != blob  # fresh nonce each time
    with pytest.raises(InvalidTag):  # copied to another user's row: refused
        decrypt(key, blob, b"totp:user-b")
    with pytest.raises(InvalidTag):  # wrong key: refused
        decrypt(os.urandom(32), blob, b"totp:user-a")


@pytest.mark.parametrize("bad", [None, "not base64!", "c2hvcnQ="])
def test_bad_data_keys_are_refused(bad: str | None) -> None:
    key = None if bad is None else SecretStr(bad)
    settings = get_settings().model_copy(update={"data_key": key})
    with pytest.raises(DataKeyError):
        load_data_key(settings)
