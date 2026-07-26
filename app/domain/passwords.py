"""Password strength rules.

A length floor alone lets through the passwords that are actually used —
"password", "12345678", "qwerty123". These are the ones credential-stuffing
tries first, so they are refused outright.

Deliberately not a complexity rule (one upper, one digit, one symbol): those
push people toward "Password1!" and are weaker in practice than length.
"""

from __future__ import annotations

MIN_LENGTH = 8
MAX_LENGTH = 128

# The passwords that dominate every breach corpus, plus the local variants a
# short list would otherwise miss.
_BANNED = frozenset(
    [
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty",
        "qwerty123",
        "qwertyuiop",
        "111111",
        "000000",
        "iloveyou",
        "admin",
        "admin123",
        "welcome",
        "welcome1",
        "letmein",
        "monkey",
        "dragon",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "trustno1",
        "abc12345",
        "1q2w3e4r",
        "zaq12wsx",
        "changeme",
        "secret",
        "parol",
        "parol123",
        "parol1234",
        "salom123",
        "tashkent",
        "uzbekistan",
        "qulaysim",
    ]
)


def rejection_reason(password: str, *, email: str = "", full_name: str = "") -> str | None:
    """Return why the password is unacceptable, or None when it is fine."""
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"Password must be at most {MAX_LENGTH} characters"

    lowered = password.lower()
    if lowered in _BANNED:
        return "That password is too common — please choose another"

    # A password that is mostly one repeated character survives a length check.
    if len(set(password)) < 4:
        return "Password is too repetitive — please choose another"

    # Reusing the e-mail or name means one leak compromises both.
    local_part = email.split("@", 1)[0].lower() if email else ""
    for personal in (local_part, full_name.lower()):
        if personal and len(personal) >= 4 and personal in lowered:
            return "Password must not contain your name or e-mail"

    return None
