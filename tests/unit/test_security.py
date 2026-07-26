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


class TestPasswordPolicy:
    """A length floor alone admits exactly the passwords attackers try first."""

    def test_accepts_a_reasonable_password(self) -> None:
        from app.domain.passwords import rejection_reason

        assert rejection_reason("correct horse battery") is None

    @pytest.mark.parametrize("weak", ["password", "12345678", "qwerty123", "parol123"])
    def test_rejects_breach_corpus_favourites(self, weak: str) -> None:
        from app.domain.passwords import rejection_reason

        assert rejection_reason(weak) is not None

    def test_rejects_repetition(self) -> None:
        from app.domain.passwords import rejection_reason

        assert rejection_reason("aaaaaaaaaa") is not None

    def test_rejects_a_password_containing_the_email(self) -> None:
        """One leak should not compromise both the account and the inbox."""
        from app.domain.passwords import rejection_reason

        assert rejection_reason("otabek2024x", email="otabek@mail.uz") is not None

    def test_length_floor_still_applies(self) -> None:
        from app.domain.passwords import rejection_reason

        assert rejection_reason("Ab3$xy") is not None


class TestCredentialEncryption:
    """eSIM activation codes are the credential; a database dump must not
    contain working ones."""

    def test_roundtrip(self) -> None:
        from app.core.crypto import decrypt, encrypt

        secret = "LPA:1$rsp.example.com$TOKEN-123"
        stored = encrypt(secret)
        assert stored is not None
        assert secret not in stored
        assert stored.startswith("enc:v1:")
        assert decrypt(stored) == secret

    def test_encrypting_twice_is_a_no_op(self) -> None:
        """Re-saving a row must not double-encrypt it."""
        from app.core.crypto import encrypt

        once = encrypt("value")
        assert encrypt(once) == once

    def test_plaintext_passes_through(self) -> None:
        """Rows written before encryption existed must still be readable."""
        from app.core.crypto import decrypt

        assert decrypt("LPA:1$legacy$row") == "LPA:1$legacy$row"

    def test_empty_values_are_left_alone(self) -> None:
        from app.core.crypto import decrypt, encrypt

        assert encrypt("") == ""
        assert encrypt(None) is None
        assert decrypt(None) is None

    def test_tampering_is_detected(self) -> None:
        """Fernet authenticates, so a modified token must not silently decode."""
        from app.core.crypto import decrypt, encrypt

        stored = encrypt("value")
        assert stored is not None
        assert decrypt(stored[:-6] + "AAAAAA") is None
