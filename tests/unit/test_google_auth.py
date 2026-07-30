"""Every check in the Google ID-token verifier, asserted by its failure.

A verifier is only as good as the tokens it refuses, so each test forges a
token that is valid in every respect but one. Signing with a locally generated
RSA key and pointing the verifier at it keeps this offline and deterministic.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.integrations import google_auth

CLIENT_ID = "1234567890-test.apps.googleusercontent.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "108234567890123456789",
        "email": "Tinchibek.Ibodov@Gmail.com",
        "email_verified": True,
        "name": "Ibodov Tinchibek",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is None]:
        del claims[key]
    return jwt.encode(claims, _KEY, algorithm="RS256")


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)

    class LocalKey:
        key = _KEY.public_key()

    class LocalJwks:
        def get_signing_key_from_jwt(self, _credential):
            return LocalKey()

    monkeypatch.setattr(google_auth, "_jwks", lambda: LocalJwks())


class TestAccepted:
    def test_a_well_formed_token_yields_the_identity(self):
        identity = google_auth.verify_id_token(token())
        assert identity.subject == "108234567890123456789"
        assert identity.full_name == "Ibodov Tinchibek"

    def test_the_email_is_lowercased(self):
        # Addresses arrive in whatever case the user typed at Google; the
        # customer table keys on a lowercase address, so a mixed-case token
        # would otherwise create a second account for the same person.
        assert google_auth.verify_id_token(token()).email == "tinchibek.ibodov@gmail.com"

    def test_email_verified_may_arrive_as_a_string(self):
        # Some Google responses carry "true" rather than true.
        assert google_auth.verify_id_token(token(email_verified="true")).email


class TestRefused:
    def _refuses(self, credential, match):
        with pytest.raises(google_auth.GoogleAuthError, match=match):
            google_auth.verify_id_token(credential)

    def test_unverified_email_is_refused(self):
        # The check that stops account takeover: without it, claiming someone
        # else's address at Google would hand over their orders here.
        self._refuses(token(email_verified=False), "not verified")

    def test_missing_email_verified_claim_is_refused(self):
        self._refuses(token(email_verified=None), "not verified")

    def test_a_token_for_another_client_is_refused(self):
        # A perfectly valid Google token minted for a different site.
        self._refuses(token(aud="999-someone-else.apps.googleusercontent.com"), "Token rejected")

    def test_an_expired_token_is_refused(self):
        past = int(time.time()) - 7200
        self._refuses(token(iat=past, exp=past + 3600), "Token rejected")

    def test_a_foreign_issuer_is_refused(self):
        self._refuses(token(iss="https://accounts.evil.example"), "issuer")

    def test_a_token_with_no_subject_is_refused(self):
        self._refuses(token(sub=None), "Token rejected")

    def test_a_token_with_no_email_is_refused(self):
        self._refuses(token(email=None), "no e-mail")

    def test_a_token_signed_by_another_key_is_refused(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "sub": "1",
                "email": "a@b.com",
                "email_verified": True,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            other,
            algorithm="RS256",
        )
        self._refuses(forged, "Token rejected")

    def test_an_unsigned_token_is_refused(self):
        # alg=none is the classic JWT bypass; PyJWT's algorithms list blocks it,
        # and this asserts the list is actually passed.
        unsigned = jwt.encode({"sub": "1", "aud": CLIENT_ID}, key="", algorithm="none")
        self._refuses(unsigned, "Token rejected|Unknown signing key")

    def test_rubbish_is_refused(self):
        # Which check catches it depends on where it falls apart — the stub JWKS
        # here hands back a key unconditionally, so decoding is what refuses.
        # The guarantee under test is that it is refused, not where.
        self._refuses(
            "not-a-token-at-all-but-long-enough-to-pass-length",
            "Token rejected|Unknown signing key",
        )

    def test_an_oversized_credential_is_refused_before_any_crypto(self):
        self._refuses("x" * 9000, "Malformed")


class TestConfiguration:
    def test_verification_is_refused_when_no_client_id_is_set(self, monkeypatch):
        monkeypatch.setattr(settings, "google_client_id", "")
        assert not google_auth.is_configured()
        with pytest.raises(google_auth.GoogleAuthError, match="not configured"):
            google_auth.verify_id_token(token())
