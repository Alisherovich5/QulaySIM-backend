"""Verifying a Google ID token.

The storefront uses Google Identity Services, which hands the browser a signed
JWT and nothing else. That token is the entire proof of identity, so every check
below is load-bearing — a verifier that skips one is not a weaker verifier, it
is an open door:

* **Signature** against Google's published keys. Without it the token is just
  JSON the caller wrote themselves.
* **`aud` equals our client id.** A token minted for another site is a valid
  Google token; accepting it lets that site's operator sign in as any of our
  customers who use the same address.
* **`iss`** is one of Google's two documented issuers.
* **`exp` / `iat`** with a small clock allowance, so a captured token stops
  working.
* **`email_verified`.** This is the one people leave out. Google will issue a
  token for an unverified address, so without this check anyone could claim
  someone else's e-mail at Google and take over the matching account here.

No client secret is involved: this is the ID-token flow, where verification is
done against public keys. The client id is not a secret either — it ships in the
page — but it must still match, for the reason above.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import jwt
import structlog
from jwt import PyJWKClient

from app.core.config import settings

logger = structlog.get_logger(__name__)

_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Google's keys rotate; PyJWKClient caches them and refetches on an unknown kid.
_LEEWAY_SECONDS = 30


class GoogleAuthError(RuntimeError):
    """The token did not verify, or Google is unreachable."""


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    full_name: str


_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def is_configured() -> bool:
    return bool(settings.google_client_id)


def verify_id_token(credential: str) -> GoogleIdentity:
    """Verify a Google ID token and return the identity it asserts."""
    if not is_configured():
        raise GoogleAuthError("Google sign-in is not configured")
    if not credential or len(credential) > 8192:
        # A token that size is not a token; refuse before doing any crypto.
        raise GoogleAuthError("Malformed credential")

    try:
        signing_key = _jwks().get_signing_key_from_jwt(credential)
    except urllib.error.URLError as exc:  # pragma: no cover - network
        raise GoogleAuthError(f"Could not reach Google to verify: {exc}") from exc
    except Exception as exc:
        raise GoogleAuthError(f"Unknown signing key: {exc}") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"Token rejected: {exc}") from exc

    if claims.get("iss") not in _ISSUERS:
        raise GoogleAuthError("Unexpected issuer")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Token carries no e-mail")
    if claims.get("email_verified") not in (True, "true"):
        # Refused rather than downgraded: an unverified address is a claim, not
        # an identity, and this is the check that stops account takeover.
        raise GoogleAuthError("Google has not verified this e-mail address")

    subject = str(claims.get("sub") or "")
    if not subject:
        raise GoogleAuthError("Token carries no subject")

    return GoogleIdentity(
        subject=subject,
        email=email,
        full_name=(claims.get("name") or "").strip()[:150],
    )


def _fetch_json(url: str, timeout: int = 10) -> dict[str, Any]:  # pragma: no cover - network
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def describe_configuration() -> dict[str, Any]:
    """Small readout for the health endpoint and for operators."""
    return {
        "configured": is_configured(),
        "client_id_tail": settings.google_client_id[-12:] if is_configured() else "",
        "checked_at": int(time.time()),
    }
