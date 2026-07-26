"""Column types that encrypt at rest.

The Django side declares these columns with EncryptedCharField /
EncryptedTextField; this is the SQLAlchemy half of the same contract, so both
services see plaintext in Python and ciphertext in Postgres.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, TypeDecorator

from app.core.crypto import decrypt, encrypt


class EncryptedText(TypeDecorator[str]):
    """Text encrypted on the way in, decrypted on the way out."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        return decrypt(value)


class EncryptedString(TypeDecorator[str]):
    """As above, for conceptually short values.

    The column is TEXT regardless of the declared length: ciphertext is longer
    than its plaintext, so a VARCHAR sized for the plaintext would truncate it.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        return decrypt(value)
