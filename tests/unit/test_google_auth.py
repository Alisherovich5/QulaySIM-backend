"""Every check in the Google ID-token verifier, asserted by its failure.

A verifier is only as good as the tokens it refuses, so each test forges a
token that is valid in every respect but one. Signing with a locally generated
RSA key and standing in for the key fetch keeps this offline and deterministic.
"""

from __future__ import annotations

import dataclasses
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.integrations import google_auth

CLIENT_ID = "1234567890-test.apps.googleusercontent.com"
KID = "test-signing-key"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(kid: str | None = KID, **overrides) -> str:
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
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": kid} if kid else {})


class FakeGoogle:
    """Stands in for the JWKS endpoint, and counts how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0
        self.unreachable = False
        self.keys = {KID: _KEY.public_key()}

    async def __call__(self) -> dict[str, object]:
        self.calls += 1
        if self.unreachable:
            raise google_auth.GoogleUnavailableError("googleapis is not answering")
        return dict(self.keys)


@pytest.fixture(autouse=True)
def google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    # The key cache is process-wide, so a test that warms it would otherwise
    # decide the outcome of the next one.
    google_auth.reset_key_cache()
    fake = FakeGoogle()
    monkeypatch.setattr(google_auth, "_fetch_keys", fake)
    yield fake
    google_auth.reset_key_cache()


def _age_the_cache(seconds: float) -> None:
    """Pretend the cached key set was fetched `seconds` ago."""
    cached = google_auth._key_set
    assert cached is not None
    google_auth._key_set = dataclasses.replace(cached, fetched_at=cached.fetched_at - seconds)


class TestAccepted:
    async def test_a_well_formed_token_yields_the_identity(self):
        identity = await google_auth.verify_id_token(token())
        assert identity.subject == "108234567890123456789"
        assert identity.full_name == "Ibodov Tinchibek"

    async def test_the_email_is_lowercased(self):
        # Addresses arrive in whatever case the user typed at Google; the
        # customer table keys on a lowercase address, so a mixed-case token
        # would otherwise create a second account for the same person.
        identity = await google_auth.verify_id_token(token())
        assert identity.email == "tinchibek.ibodov@gmail.com"

    async def test_email_verified_may_arrive_as_a_string(self):
        # Some Google responses carry "true" rather than true.
        assert (await google_auth.verify_id_token(token(email_verified="true"))).email

    async def test_a_few_seconds_of_clock_skew_is_tolerated(self):
        # Our clock behind Google's: the token looks issued in the future and
        # would be refused outright without the leeway.
        now = int(time.time())
        assert await google_auth.verify_id_token(token(iat=now + 5, exp=now + 3605))

    async def test_a_token_that_expired_seconds_ago_is_still_accepted(self):
        # The other direction: our clock ahead of Google's. Bounded by the
        # leeway, so this is seconds of grace, not minutes.
        now = int(time.time())
        assert await google_auth.verify_id_token(token(iat=now - 3600, exp=now - 5))


class TestRefused:
    async def _refuses(self, credential, match):
        with pytest.raises(google_auth.GoogleAuthError, match=match):
            await google_auth.verify_id_token(credential)

    async def test_unverified_email_is_refused(self):
        # The check that stops account takeover: without it, claiming someone
        # else's address at Google would hand over their orders here.
        await self._refuses(token(email_verified=False), "not verified")

    async def test_missing_email_verified_claim_is_refused(self):
        await self._refuses(token(email_verified=None), "not verified")

    async def test_a_token_for_another_client_is_refused(self):
        # A perfectly valid Google token minted for a different site.
        await self._refuses(
            token(aud="999-someone-else.apps.googleusercontent.com"), "Token rejected"
        )

    async def test_an_expired_token_is_refused(self):
        past = int(time.time()) - 7200
        await self._refuses(token(iat=past, exp=past + 3600), "Token rejected")

    async def test_a_token_issued_far_in_the_future_is_refused(self):
        # Beyond any credible clock difference, so the leeway must not cover it.
        ahead = int(time.time()) + 900
        await self._refuses(token(iat=ahead, exp=ahead + 3600), "Token rejected")

    async def test_a_foreign_issuer_is_refused(self):
        await self._refuses(token(iss="https://accounts.evil.example"), "issuer")

    async def test_a_token_with_no_subject_is_refused(self):
        await self._refuses(token(sub=None), "Token rejected")

    async def test_a_token_with_no_email_is_refused(self):
        await self._refuses(token(email=None), "no e-mail")

    async def test_a_token_signed_by_another_key_is_refused(self):
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
            headers={"kid": KID},
        )
        await self._refuses(forged, "Token rejected")

    async def test_an_unsigned_token_is_refused(self):
        # alg=none is the classic JWT bypass. Refused on the header, before the
        # key cache is even consulted.
        unsigned = jwt.encode({"sub": "1", "aud": CLIENT_ID}, key="", algorithm="none")
        await self._refuses(unsigned, "Unexpected signing algorithm")

    async def test_a_token_naming_no_key_is_refused(self):
        await self._refuses(token(kid=None), "names no signing key")

    async def test_a_token_naming_an_unknown_key_is_refused(self):
        await self._refuses(token(kid="not-a-google-key"), "Unknown signing key")

    async def test_rubbish_is_refused(self):
        await self._refuses("not-a-token-at-all-but-long-enough-to-pass-length", "Malformed")

    async def test_an_oversized_credential_is_refused_before_any_crypto(self, google):
        await self._refuses("x" * 9000, "Malformed")
        assert google.calls == 0


class TestSigningKeys:
    """Fetching Google's keys is network I/O on the sign-in path; it has to be
    cached, bounded, and honest about failing."""

    async def test_the_key_set_is_fetched_once_for_many_sign_ins(self, google):
        for _ in range(3):
            await google_auth.verify_id_token(token())
        assert google.calls == 1

    async def test_an_unknown_kid_does_not_refetch_on_every_attempt(self, google):
        # A forged header can name any kid it likes. If each one triggered a
        # fetch, the endpoint would be a free way to make us hammer Google.
        await google_auth.verify_id_token(token())
        for _ in range(5):
            with pytest.raises(google_auth.GoogleAuthError):
                await google_auth.verify_id_token(token(kid="forged-kid"))
        assert google.calls == 1

    async def test_a_rotated_key_is_picked_up_once_the_floor_has_passed(self, google):
        await google_auth.verify_id_token(token())
        _age_the_cache(google_auth._JWKS_MIN_REFETCH_SECONDS + 1)

        rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        google.keys = {"rotated-kid": rotated.public_key()}
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "1",
            "email": "a@b.com",
            "email_verified": True,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        signed = jwt.encode(claims, rotated, algorithm="RS256", headers={"kid": "rotated-kid"})

        assert await google_auth.verify_id_token(signed)
        assert google.calls == 2

    async def test_an_unreachable_google_is_not_reported_as_a_bad_token(self, google):
        # 401 would tell the customer their Google account is broken during an
        # outage that has nothing to do with them.
        google.unreachable = True
        with pytest.raises(google_auth.GoogleUnavailableError):
            await google_auth.verify_id_token(token())

    async def test_a_cached_key_still_verifies_while_google_is_down(self, google):
        await google_auth.verify_id_token(token())
        _age_the_cache(google_auth._JWKS_TTL_SECONDS + 1)
        google.unreachable = True

        # Google's published keys outlive our cache by days, so an expired
        # cache plus an outage must not sign everybody out.
        assert await google_auth.verify_id_token(token())
        assert google.calls == 2


class TestConfiguration:
    async def test_verification_is_refused_when_no_client_id_is_set(self, monkeypatch):
        monkeypatch.setattr(settings, "google_client_id", "")
        assert not google_auth.is_configured()
        with pytest.raises(google_auth.GoogleAuthError, match="not configured"):
            await google_auth.verify_id_token(token())

    async def test_the_readout_says_whether_the_keys_are_in_hand(self):
        assert google_auth.describe_configuration()["signing_keys_cached"] == 0
        await google_auth.verify_id_token(token())
        assert google_auth.describe_configuration()["signing_keys_cached"] == 1
