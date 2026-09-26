"""Proving that a Telegram login really came from Telegram.

The login widget hands the browser a flat set of fields and a `hash`. Anyone
can post that object at us, so the hash is the whole of the security: it is an
HMAC-SHA256 over the other fields, keyed by SHA256 of the bot token. Nobody who
lacks the token can produce it, and nobody who has it needs to.

Two checks, not one. The signature says the data came from Telegram; it does
not say *when*. Without the freshness check a payload captured once — out of a
browser history, a shared screen, a proxy log — stays a valid login forever,
because Telegram never revokes it. `auth_date` is inside the signed set, so an
attacker cannot move it.

Deliberately no network call: unlike Google's, this verification is local
arithmetic. That is why it cannot fail with "provider unavailable" and why the
endpoint above it has no such branch.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)


class TelegramAuthError(Exception):
    """The payload is not a login Telegram issued to us, or is too old."""


#: How long a signed login stays usable. Telegram suggests a day; a day is a
#: long time for a credential that is replayable until it expires, and the
#: widget posts within seconds of the person pressing the button. Five minutes
#: covers a slow phone and a tab left open on the confirmation screen.
MAX_AGE_SECONDS = 300

#: The fields the widget sends. Anything else in the body is ignored rather than
#: signed over, so an attacker cannot smuggle a value past the hash by adding a
#: key Telegram never sends.
SIGNED_FIELDS = (
    "auth_date",
    "first_name",
    "id",
    "last_name",
    "photo_url",
    "username",
)


@dataclass(frozen=True)
class TelegramIdentity:
    """Who Telegram says this is. No e-mail — Telegram does not have one."""

    uid: str
    first_name: str
    last_name: str
    username: str
    photo_url: str

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()


def verify_login(
    data: dict[str, str], *, bot_token: str, now: float | None = None
) -> TelegramIdentity:
    """Check the widget's signature and freshness, and say who signed in.

    Raises `TelegramAuthError` for every failure, with the reason in the message
    for the log and never for the caller: which check failed is not something
    the person at the keyboard can act on, and naming it helps only somebody
    probing for one.
    """
    if not bot_token:
        raise TelegramAuthError("no bot token configured")

    supplied = data.get("hash") or ""
    if not supplied:
        raise TelegramAuthError("no hash")

    # Alphabetical, `key=value`, newline-joined — Telegram's own recipe, and the
    # order is part of the signature rather than a convention.
    check = "\n".join(
        f"{key}={data[key]}" for key in SIGNED_FIELDS if data.get(key) not in (None, "")
    )
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

    # Constant time: a byte-by-byte comparison leaks how much of a guess was
    # right, which is enough to forge one character at a time.
    if not hmac.compare_digest(expected, supplied):
        raise TelegramAuthError("signature mismatch")

    try:
        issued = int(data.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise TelegramAuthError("unreadable auth_date") from None

    age = (now if now is not None else time.time()) - issued
    if age > MAX_AGE_SECONDS:
        raise TelegramAuthError(f"stale login, {int(age)}s old")
    # A login from the future is a clock problem or a forged date; either way it
    # cannot be aged out later, so it is refused now.
    if age < -MAX_AGE_SECONDS:
        raise TelegramAuthError("auth_date is in the future")

    uid = str(data.get("id") or "").strip()
    if not uid:
        raise TelegramAuthError("no id")

    return TelegramIdentity(
        uid=uid,
        first_name=str(data.get("first_name") or "").strip(),
        last_name=str(data.get("last_name") or "").strip(),
        username=str(data.get("username") or "").strip(),
        photo_url=str(data.get("photo_url") or "").strip(),
    )
