from __future__ import annotations

import pytest

from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


class TestPasswords:
    def test_roundtrip(self) -> None:
        assert verify_password("correct horse", hash_password("correct horse"))

    def test_wrong_password(self) -> None:
        assert not verify_password("nope", hash_password("correct horse"))

    def test_missing_hash_is_false_not_crash(self) -> None:
        """Login for a non-existent account must return False, not raise —
        that is what keeps timing constant."""
        assert verify_password("anything", None) is False

    def test_corrupt_hash_is_false(self) -> None:
        assert verify_password("anything", "not-a-bcrypt-hash") is False

    def test_over_72_bytes_does_not_raise(self) -> None:
        long_password = "a" * 200
        assert verify_password(long_password, hash_password(long_password))


class TestTokens:
    def test_access_token_roundtrip(self) -> None:
        payload = decode_token(create_access_token("42"), "access")
        assert payload["sub"] == "42"
        assert payload["typ"] == "access"
        assert payload["jti"]

    def test_refresh_token_has_distinct_type(self) -> None:
        token, jti = create_refresh_token("42")
        payload = decode_token(token, "refresh")
        assert payload["typ"] == "refresh"
        assert payload["jti"] == jti

    def test_access_token_rejected_as_refresh(self) -> None:
        """Token confusion: an access token must never be spendable as a
        refresh token."""
        with pytest.raises(AuthenticationError, match="Wrong token type"):
            decode_token(create_access_token("42"), "refresh")

    def test_tampered_token_rejected(self) -> None:
        token = create_access_token("42")
        with pytest.raises(AuthenticationError):
            decode_token(token[:-4] + "AAAA", "access")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(AuthenticationError):
            decode_token("not.a.token", "access")

    def test_jti_is_unique_per_token(self) -> None:
        a = decode_token(create_access_token("1"), "access")
        b = decode_token(create_access_token("1"), "access")
        assert a["jti"] != b["jti"]
