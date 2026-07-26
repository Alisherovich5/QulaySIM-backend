"""Password hashing and JWT issuing/verification.

Changes from the previous implementation:
  * PyJWT instead of python-jose (jose is unmaintained and carries CVEs).
  * Short-lived access tokens plus refresh tokens, instead of one 7-day token.
  * Every token carries a `jti` so it can be revoked through a Redis denylist.
  * `verify_password` always runs a real bcrypt comparison, so a missing
    account and a wrong password take the same time (no user enumeration).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.cache import get_redis
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)

TokenType = Literal["access", "refresh"]

# Comparing against this when the account does not exist keeps login timing
# constant. Generated once at import; the plaintext is never used.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt())

_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str | None) -> bool:
    candidate = plain.encode()[:_BCRYPT_MAX_BYTES]
    if not hashed:
        bcrypt.checkpw(candidate, _DUMMY_HASH)  # constant-time decoy
        return False
    try:
        return bcrypt.checkpw(candidate, hashed.encode())
    except (ValueError, TypeError):
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False


_CURRENT_BCRYPT_ROUNDS = int(bcrypt.gensalt().decode().split("$")[2])


def needs_rehash(hashed: str) -> bool:
    """True when a stored hash was produced with a weaker cost factor than we
    now use, so it should be upgraded on the next successful login."""
    try:
        return int(hashed.split("$")[2]) < _CURRENT_BCRYPT_ROUNDS
    except (IndexError, ValueError):
        return False


def _encode(subject: str, token_type: TokenType, ttl: timedelta) -> tuple[str, str]:
    now = datetime.now(UTC)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "jti": jti,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "iss": settings.service_name,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), jti


def create_access_token(subject: str) -> str:
    token, _ = _encode(subject, "access", timedelta(minutes=settings.access_token_ttl_minutes))
    return token


def create_refresh_token(subject: str) -> tuple[str, str]:
    return _encode(subject, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.service_name,
            options={"require": ["exp", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Session expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Could not validate credentials") from exc

    if payload.get("typ") != expected_type:
        raise AuthenticationError("Wrong token type")
    return payload


async def revoke(jti: str, ttl_seconds: int) -> None:
    try:
        await get_redis().setex(f"qs:jwt:revoked:{jti}", max(ttl_seconds, 1), b"1")
    except Exception as exc:  # noqa: BLE001
        logger.warning("token.revoke_failed", jti=jti, error=str(exc))


async def is_revoked(jti: str) -> bool:
    """Fails CLOSED on a Redis outage for refresh tokens is too aggressive
    (it would log everyone out), so we fail open and log loudly instead."""
    try:
        return await get_redis().exists(f"qs:jwt:revoked:{jti}") == 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("token.revocation_check_failed", error=str(exc))
        return False
