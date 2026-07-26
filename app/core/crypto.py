"""Application-layer encryption for eSIM activation credentials.

Mirrors QulaySIM-admin/config/crypto.py — the two services read and write the
same rows, so the format and the key must match exactly. Changing one without
the other breaks decryption on the far side.


An ICCID plus its activation code is everything needed to install a customer's
eSIM on another device. Stored in the clear, one database read — a backup left
on a laptop, a replica, a dump handed to a contractor — is a stolen profile.

Fernet (AES-128-CBC + HMAC-SHA256) is used because the values are short, only
ever handled whole, and must be authenticated: a tampered activation code
should fail loudly rather than silently produce a broken QR.

Values carry a version prefix so plaintext rows written before this existed
still decrypt to themselves, and so the scheme can be rotated later without
guessing what a given row holds.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from app.core.logging import get_logger

logger = get_logger(__name__)

PREFIX = "enc:v1:"


class EncryptionUnavailableError(RuntimeError):
    """The key is missing or unusable."""


def _fernet() -> Fernet:
    key = os.environ.get("FIELD_ENCRYPTION_KEY", "").strip()
    if not key:
        raise EncryptionUnavailableError(
            "FIELD_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise EncryptionUnavailableError(
            f"FIELD_ENCRYPTION_KEY is not a valid Fernet key: {exc}"
        ) from exc


def is_encrypted(value: str | None) -> bool:
    return value is not None and value.startswith(PREFIX)


def encrypt(value: str | None) -> str | None:
    """Encrypt, unless the value is empty or already encrypted."""
    if not value or is_encrypted(value):
        return value
    token = _fernet().encrypt(value.encode()).decode()
    return f"{PREFIX}{token}"


def decrypt(value: str | None) -> str | None:
    """Decrypt, passing through anything not written by `encrypt`.

    A value that fails to decrypt is returned as None rather than raised:
    losing one QR code should not take down a customer's whole account page.
    The failure is logged so it is not silent.
    """
    if not value or not is_encrypted(value):
        return value
    try:
        plaintext: str = _fernet().decrypt(value[len(PREFIX) :].encode()).decode()
        return plaintext
    except InvalidToken:
        logger.error("crypto.decrypt_failed", reason="wrong key or tampered value")
        return None
    except EncryptionUnavailableError:
        logger.error("crypto.key_missing")
        return None
