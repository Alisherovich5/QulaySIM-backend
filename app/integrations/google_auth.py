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

Google's signing keys are fetched over the shared async HTTP client and cached
in-process. `PyJWKClient` was used here before; it fetches with blocking
`urllib` from inside an async request handler, which stalls every other request
in the worker for as long as googleapis takes to answer — up to its 30-second
default timeout. Reaching Google is Google's problem; freezing the API while we
wait is ours.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
import structlog
from jwt import PyJWKSet

from app.core.config import settings
from app.integrations.http import get_client

logger = structlog.get_logger(__name__)

_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Wider than any plausible server/Google clock difference, narrow enough that a
# captured token is not usefully extended. Applies to exp, iat and nbf alike.
_LEEWAY_SECONDS = 30
# How long a fetched key set is trusted without asking Google again. Google
# publishes each key well before it signs with it and keeps it published well
# after, so an hour is conservative.
_JWKS_TTL_SECONDS = 3600
# Floor between fetches. An unknown `kid` is the signal that Google has rotated,
# and refetching on it is what keeps rotation invisible — but a caller can put
# any `kid` they like in an unsigned header, so without a floor every forged
# token would become an outbound request to Google, at our expense and theirs.
_JWKS_MIN_REFETCH_SECONDS = 60
# Google's own docs allow only RS256 here. Pinning it in the header check as
# well as in `decode` means junk never reaches the key cache.
_ALGORITHM = "RS256"


class GoogleAuthError(RuntimeError):
    """The token did not verify."""


class GoogleUnavailableError(GoogleAuthError):
    """Google could not be reached, so the token could not be judged at all.

    Distinct from `GoogleAuthError` because the answers differ: a rejected token
    means "this sign-in is not valid" (401), an unreachable Google means "ask
    again shortly" (503). Collapsing the two tells a customer their Google
    account is broken during an outage that has nothing to do with them, and
    hides the outage from whoever is watching the error rates.
    """


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    full_name: str


@dataclass(frozen=True)
class _KeySet:
    """Signing keys by `kid`, and when they were fetched (monotonic)."""

    keys: dict[str, Any]
    fetched_at: float


_key_set: _KeySet | None = None


def is_configured() -> bool:
    return bool(settings.google_client_id)


def reset_key_cache() -> None:
    """Forget the cached signing keys. For tests and for operator recovery."""
    global _key_set
    _key_set = None


async def _fetch_keys() -> dict[str, Any]:
    """Fetch Google's current signing keys, keyed by `kid`."""
    try:
        response = await get_client().get(_JWKS_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # Transport, status and JSON failures all mean the same thing to the
        # caller — we have no keys — so they are not worth telling apart.
        raise GoogleUnavailableError(f"Could not reach Google for its signing keys: {exc}") from exc

    try:
        key_set = PyJWKSet.from_dict(payload)
    except Exception as exc:
        # PyJWT raises several unrelated types out of key parsing.
        # Reached Google but got something unusable. Treated as an outage rather
        # than a bad token: the token was never judged.
        raise GoogleUnavailableError(f"Google returned an unusable key set: {exc}") from exc

    return {
        key.key_id: key.key
        for key in key_set.keys
        if key.key_id and key.public_key_use in ("sig", None)
    }


async def _signing_key(kid: str) -> Any:
    """The public key for `kid`, fetching Google's key set when needed.

    Deliberately unsynchronised: a burst of cold requests may each fetch, which
    costs a few duplicate GETs and settles immediately. A lock would be bound to
    whichever event loop first took it, which is a worse failure than a
    duplicate request.
    """
    global _key_set

    now = time.monotonic()
    cached = _key_set
    if cached is not None:
        age = now - cached.fetched_at
        if kid in cached.keys and age < _JWKS_TTL_SECONDS:
            return cached.keys[kid]
        if age < _JWKS_MIN_REFETCH_SECONDS:
            # Just refreshed and the kid is still unknown: it is not a rotation
            # we have missed, it is a token we should refuse. Refuse it without
            # touching the network.
            if kid in cached.keys:
                return cached.keys[kid]
            raise GoogleAuthError("Unknown signing key")

    try:
        keys = await _fetch_keys()
    except GoogleUnavailableError:
        # Google's published keys outlive our cache lifetime by days, so an
        # expired cache plus an unreachable Google is not a reason to sign
        # everybody out — the key we already hold still verifies the signature.
        if cached is not None and kid in cached.keys:
            logger.warning("google.jwks_stale_cache_used", kid=kid)
            return cached.keys[kid]
        raise

    _key_set = _KeySet(keys=keys, fetched_at=now)
    key = keys.get(kid)
    if key is None:
        raise GoogleAuthError("Unknown signing key")
    return key


async def verify_id_token(credential: str) -> GoogleIdentity:
    """Verify a Google ID token and return the identity it asserts."""
    if not is_configured():
        raise GoogleAuthError("Google sign-in is not configured")
    if not credential or len(credential) > 8192:
        # A token that size is not a token; refuse before doing any crypto.
        raise GoogleAuthError("Malformed credential")

    try:
        header = jwt.get_unverified_header(credential)
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"Malformed credential: {type(exc).__name__}") from exc

    if header.get("alg") != _ALGORITHM:
        # `alg: none` is the classic JWT bypass, and any other algorithm is not
        # something Google issues. Refused here so it never reaches the cache.
        raise GoogleAuthError("Unexpected signing algorithm")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise GoogleAuthError("Token names no signing key")

    signing_key = await _signing_key(kid)

    try:
        claims: dict[str, Any] = jwt.decode(
            credential,
            signing_key,
            algorithms=[_ALGORITHM],
            audience=settings.google_client_id,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"Token rejected: {type(exc).__name__}") from exc

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


def describe_configuration() -> dict[str, Any]:
    """Small readout for the health endpoint and for operators."""
    cached = _key_set
    return {
        "configured": is_configured(),
        "client_id_tail": settings.google_client_id[-12:] if is_configured() else "",
        # Whether Google's keys are already in hand: the first sign-in after a
        # restart is the one that needs the network.
        "signing_keys_cached": 0 if cached is None else len(cached.keys),
        "checked_at": int(time.time()),
    }
