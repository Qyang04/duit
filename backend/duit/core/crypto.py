"""Application-level encryption for sensitive columns (TOTP secrets now; bank credentials later).

AES-256-GCM with the data key from DUIT_DATA_KEY (32 random bytes, base64). Each value gets a
fresh 12-byte nonce, stored in front of the ciphertext. The associated data binds a ciphertext
to its purpose and owner, so an encrypted value copied into another row or column fails to
decrypt instead of being silently accepted.
"""

import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from duit.core.settings import Settings

NONCE_BYTES = 12


class DataKeyError(RuntimeError):
    pass


def load_data_key(settings: Settings) -> bytes:
    if settings.data_key is None:
        raise DataKeyError("DUIT_DATA_KEY is not set")
    try:
        key = base64.b64decode(settings.data_key.get_secret_value(), validate=True)
    except binascii.Error as exc:
        raise DataKeyError("DUIT_DATA_KEY is not valid base64") from exc
    if len(key) != 32:
        raise DataKeyError("DUIT_DATA_KEY must decode to exactly 32 bytes")
    return key


def encrypt(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated_data)


def decrypt(key: bytes, blob: bytes, associated_data: bytes) -> bytes:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data)  # raises InvalidTag
