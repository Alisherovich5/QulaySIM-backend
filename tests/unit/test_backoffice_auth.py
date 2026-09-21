"""The two credential checks the backoffice does itself.

Both are re-implementations of somebody else's format — Django's password hash
and django-otp's TOTP — which is exactly the kind of code that looks right and
accepts everything. So both are tested against values the original libraries
produced, not against this module's own output.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.models import TOTPDevice
from app.services.backoffice.auth import _totp_matches, _verify_django_password

# Produced by `django.contrib.auth.hashers.make_password` on the live admin
# image (Django 5, PBKDF2-SHA256, 1,000,000 iterations — the same cost the real
# account carries). Copied verbatim; if this module ever stops accepting them,
# nobody signs in.
DJANGO_HASHES = [
    (
        "salom-dunyo-123",
        "pbkdf2_sha256$1000000$cH0MggGcVgtUvFW0ggXLnb$Ueno5Yd3HOJ4IZjmIuj8UnTImXdBEewGHDXKj6RJy1A=",
    ),
    (
        "a",
        "pbkdf2_sha256$1000000$qEnt077zwlqZYHigm8Ee09$L8Jqlfl/DB+sm87rncdIeofDOiNAv2BNvzxgwdE3Ym4=",
    ),
    # Non-ASCII on purpose: a byte-vs-character slip only shows up here.
    (
        "Ω-uzbek-парол",
        "pbkdf2_sha256$1000000$4xzKVUPDOgEdk5azPJhHTF$gqFkzD7edSJpil0v9jvlItZ8btqD2hudKyeKfFT0oPI=",
    ),
]


class TestDjangoPasswords:
    @pytest.mark.parametrize(("password", "encoded"), DJANGO_HASHES)
    def test_the_right_password_is_accepted(self, password: str, encoded: str) -> None:
        assert _verify_django_password(password, encoded) is True

    @pytest.mark.parametrize(("password", "encoded"), DJANGO_HASHES)
    def test_a_wrong_password_is_refused(self, password: str, encoded: str) -> None:
        assert _verify_django_password(password + "x", encoded) is False
        assert _verify_django_password("", encoded) is False

    def test_an_algorithm_we_do_not_implement_is_refused_not_guessed(self) -> None:
        # An argon2 account must fail closed. Returning True on "I cannot check
        # this" is the one outcome that must never happen.
        assert (
            _verify_django_password("x", "argon2$argon2id$v=19$m=102400,t=2,p=8$abc$def") is False
        )

    def test_an_unusable_password_marker_is_refused(self) -> None:
        # Django writes "!" for an account that may not sign in with a password.
        assert _verify_django_password("", "!") is False
        assert _verify_django_password("!", "!") is False

    def test_a_malformed_hash_does_not_raise(self) -> None:
        for junk in ("", "pbkdf2_sha256", "pbkdf2_sha256$abc$salt$hash", "$$$"):
            assert _verify_django_password("x", junk) is False


def _device(**kwargs: object) -> TOTPDevice:
    base: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "key": b"12345678901234567890".hex(),
        "step": 30,
        "digits": 8,
        "tolerance": 0,
        "drift": 0,
        "last_t": -1,
    }
    base.update(kwargs)
    return TOTPDevice(**base)  # type: ignore[arg-type]


class TestTotp:
    """RFC 6238's own test vectors, SHA-1, secret '12345678901234567890'."""

    @pytest.mark.parametrize(
        ("now", "code", "counter"),
        [
            (59, "94287082", 1),
            (1111111109, "07081804", 37037036),
            (1234567890, "89005924", 41152263),
        ],
    )
    def test_the_published_vectors_are_accepted(self, now: int, code: str, counter: int) -> None:
        with patch("app.services.backoffice.auth.time.time", return_value=now):
            assert _totp_matches(_device(), code) == counter

    def test_a_wrong_code_is_refused(self) -> None:
        with patch("app.services.backoffice.auth.time.time", return_value=59):
            assert _totp_matches(_device(), "00000000") is None

    def test_a_code_cannot_be_used_twice(self) -> None:
        # The whole point of storing last_t: a code stays valid for 30 seconds,
        # and anyone who reads it over a shoulder must not get a second use.
        with patch("app.services.backoffice.auth.time.time", return_value=59):
            assert _totp_matches(_device(last_t=1), "94287082") is None

    def test_tolerance_accepts_the_neighbouring_window(self) -> None:
        # One step early, which is what a phone with a slow clock sends.
        with patch("app.services.backoffice.auth.time.time", return_value=89):
            assert _totp_matches(_device(tolerance=1), "94287082") == 1
        with patch("app.services.backoffice.auth.time.time", return_value=89):
            assert _totp_matches(_device(tolerance=0), "94287082") is None

    def test_a_non_numeric_or_short_code_is_refused(self) -> None:
        with patch("app.services.backoffice.auth.time.time", return_value=59):
            assert _totp_matches(_device(), "9428708") is None
            assert _totp_matches(_device(), "abcdefgh") is None

    def test_a_corrupt_secret_is_refused_rather_than_raising(self) -> None:
        with patch("app.services.backoffice.auth.time.time", return_value=59):
            assert _totp_matches(_device(key="not-hex"), "94287082") is None
