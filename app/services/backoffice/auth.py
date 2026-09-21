"""Signing in to the backoffice.

The accounts are the ones that already existed: `auth_user`, with Django's
password hashes and django-otp's TOTP devices. Nobody has to be re-invited and
nobody's password changes, because the alternative — a fresh user table — would
have meant handing out new credentials over Telegram at midnight.

Three deliberate differences from the storefront's auth:

  * Passwords are verified against Django's PBKDF2 format, not bcrypt. The hash
    on the live account runs 1,000,000 iterations, which costs about a second
    of CPU, so the check runs in a worker thread: on the event loop it would
    stall every other request for the duration.
  * The subject of every token is `staff:<id>`. A customer's access token has a
    bare numeric subject, so the two token families cannot be swapped even
    though they are signed with the same key.
  * Sessions are short. Twelve hours of refresh, not thirty days: this is the
    console that can hand out eSIMs and read QR codes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta

from fastapi import status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_redis
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.db.models import AccessFailureLog, AccessLog, Staff, TOTPDevice

logger = get_logger(__name__)

STAFF_SUBJECT_PREFIX = "staff:"
REFRESH_TTL = timedelta(hours=12)
ACCESS_TTL = timedelta(minutes=30)

# Five wrong passwords and that name is out for fifteen minutes. django-axes
# enforced the same rule for the Django admin; losing it along with the Django
# login page would have quietly removed the only brute-force brake we had.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


# --------------------------------------------------------------------------
# Django's password format
# --------------------------------------------------------------------------


def _verify_django_password(plain: str, encoded: str) -> bool:
    """Check a password against Django's `pbkdf2_sha256$rounds$salt$hash`.

    Returns False for any other algorithm rather than raising: an account
    stored with argon2 or an unusable `!` placeholder simply cannot sign in
    here, and it must not be a 500 on the login page.
    """
    try:
        algorithm, rounds, salt, stored = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        logger.warning("backoffice.unsupported_password_hash", algorithm=algorithm)
        return False
    try:
        derived = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), int(rounds))
    except ValueError:
        return False
    return hmac.compare_digest(base64.b64encode(derived).decode(), stored)


# A decoy hash with the live account's cost, so a username that does not exist
# takes the same second to refuse as one that does. Built at import from random
# bytes; the plaintext is never known to anyone.
_DUMMY_SALT = secrets.token_hex(8)
_DUMMY_DIGEST = base64.b64encode(
    hashlib.pbkdf2_hmac("sha256", secrets.token_bytes(16), secrets.token_bytes(8), 1)
).decode()
_DUMMY_ENCODED = f"pbkdf2_sha256$1000000${_DUMMY_SALT}${_DUMMY_DIGEST}"


async def _verify_password(plain: str, encoded: str | None) -> bool:
    target = encoded or _DUMMY_ENCODED
    ok = await asyncio.to_thread(_verify_django_password, plain, target)
    return ok and encoded is not None


# --------------------------------------------------------------------------
# django-otp's TOTP
# --------------------------------------------------------------------------


def _totp_matches(device: TOTPDevice, code: str) -> int | None:
    """The counter the code belongs to, or None.

    django-otp stores the shared secret as hex in `key` and the last accepted
    counter in `last_t`. Anything at or below `last_t` is refused, so a code
    seen once cannot be replayed inside the thirty seconds it stays valid.
    """
    if not code.isdigit() or len(code) != device.digits:
        return None
    try:
        secret = bytes.fromhex(device.key)
    except ValueError:
        return None
    now = int(time.time())
    # django-otp counts steps from t0, not from the epoch. It is zero on every
    # device we have, which is exactly why getting this wrong would go unnoticed
    # until the day somebody enrolled one that was not.
    base = (now - device.t0) // device.step + device.drift
    for offset in range(-device.tolerance, device.tolerance + 1):
        counter = base + offset
        if counter <= device.last_t:
            continue
        digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[index : index + 4])[0] & 0x7FFFFFFF
        candidate = str(value % (10**device.digits)).zfill(device.digits)
        if hmac.compare_digest(candidate, code):
            return counter
    return None


async def _devices(session: AsyncSession, staff_id: int) -> list[TOTPDevice]:
    rows = await session.execute(
        select(TOTPDevice).where(TOTPDevice.user_id == staff_id, TOTPDevice.confirmed.is_(True))
    )
    return list(rows.scalars())


# --------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------


def _lock_key(username: str) -> str:
    return f"qs:bo:lock:{username.lower()}"


async def _failures(username: str) -> int:
    try:
        raw = await get_redis().get(_lock_key(username))
    except Exception as exc:  # noqa: BLE001 - Redis down must not block sign-in
        logger.warning("backoffice.lockout_read_failed", error=str(exc))
        return 0
    return int(raw) if raw else 0


async def _record_failure(username: str) -> None:
    try:
        client = get_redis()
        count = await client.incr(_lock_key(username))
        if count == 1:
            await client.expire(_lock_key(username), LOCKOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("backoffice.lockout_write_failed", error=str(exc))


async def _clear_failures(username: str) -> None:
    try:
        await get_redis().delete(_lock_key(username))
    except Exception as exc:  # noqa: BLE001
        logger.warning("backoffice.lockout_clear_failed", error=str(exc))


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------


class TwoFactorRequiredError(AuthenticationError):
    """Password was right; the account has a confirmed device and no code came.

    428 rather than 401, because the two mean different things to the login
    form: 401 is "those credentials are wrong, try again", 428 is "they were
    right, now show the code field". Answering 401 here would tell somebody
    holding the correct password that it was wrong.
    """

    status_code = status.HTTP_428_PRECONDITION_REQUIRED

    def __init__(self) -> None:
        super().__init__("Ikki bosqichli kod kerak", code="totp_required")


async def sign_in(
    session: AsyncSession,
    *,
    login: str,
    password: str,
    code: str | None,
    ip: str | None,
    user_agent: str,
) -> Staff:
    """Return the signed-in person, or raise. Writes the attempt to the journal."""
    login = login.strip()
    if await _failures(login) >= MAX_FAILURES:
        await _log_failure(session, login, ip, user_agent, locked_out=True)
        raise AuthenticationError(
            "Juda ko‘p urinish. 15 daqiqadan keyin qayta urinib ko‘ring.", code="locked_out"
        )

    # The sign-in box asks for an email because that is what people know their
    # account by; the column that is unique is `username`. Accept either.
    row = await session.execute(
        select(Staff).where(
            (Staff.username == login) | (Staff.email == login), Staff.is_staff.is_(True)
        )
    )
    staff = row.scalars().first()

    if not await _verify_password(password, staff.password if staff else None):
        await _record_failure(login)
        await _log_failure(session, login, ip, user_agent, locked_out=False)
        raise AuthenticationError("Login yoki parol xato", code="bad_credentials")

    assert staff is not None  # _verify_password returns False without one
    if not staff.is_active:
        await _log_failure(session, login, ip, user_agent, locked_out=False)
        raise AuthenticationError("Bu hisob o‘chirilgan", code="inactive")

    devices = await _devices(session, staff.id)
    if devices:
        if not code:
            raise TwoFactorRequiredError()
        accepted = next(
            ((d, t) for d in devices if (t := _totp_matches(d, code)) is not None), None
        )
        if accepted is None:
            await _record_failure(login)
            await _log_failure(session, login, ip, user_agent, locked_out=False)
            raise AuthenticationError("Kod xato", code="bad_code")
        device, counter = accepted
        await session.execute(
            update(TOTPDevice)
            .where(TOTPDevice.id == device.id)
            .values(last_t=counter, last_used_at=datetime.now(UTC))
        )

    await _clear_failures(login)
    await session.execute(
        update(Staff).where(Staff.id == staff.id).values(last_login=datetime.now(UTC))
    )
    session.add(
        AccessLog(
            username=staff.username,
            ip_address=ip,
            user_agent=user_agent[:255],
            path_info="/api/v1/backoffice/auth/login",
            http_accept="",
            session_hash="",
        )
    )
    await session.commit()
    return staff


async def _log_failure(
    session: AsyncSession, username: str, ip: str | None, user_agent: str, *, locked_out: bool
) -> None:
    session.add(
        AccessFailureLog(
            username=username[:255],
            ip_address=ip,
            user_agent=user_agent[:255],
            path_info="/api/v1/backoffice/auth/login",
            http_accept="",
            locked_out=locked_out,
        )
    )
    await session.commit()


async def load(session: AsyncSession, staff_id: int) -> Staff | None:
    staff = await session.get(Staff, staff_id)
    if staff is None or not staff.is_active or not staff.is_staff:
        return None
    return staff


def subject(staff: Staff) -> str:
    return f"{STAFF_SUBJECT_PREFIX}{staff.id}"


def staff_id_from(sub: str) -> int:
    if not sub.startswith(STAFF_SUBJECT_PREFIX):
        raise AuthenticationError("Bu token backoffice uchun emas")
    try:
        return int(sub.removeprefix(STAFF_SUBJECT_PREFIX))
    except ValueError as exc:
        raise AuthenticationError("Token buzilgan") from exc


__all__ = [
    "ACCESS_TTL",
    "REFRESH_TTL",
    "TwoFactorRequiredError",
    "load",
    "settings",
    "sign_in",
    "staff_id_from",
    "subject",
]
