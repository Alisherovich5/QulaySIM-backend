"""Referral code generation and reward rules — pure logic."""

from __future__ import annotations

import secrets
import string

ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8
REWARD_PERCENT = 10
REWARD_PREFIX = "REF-"

# Repeat-purchase cashback. A separate prefix so a glance at a code says which
# scheme paid for it, and so support can tell a customer why they have it.
LOYALTY_PREFIX = "QAYT-"


def new_referral_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_reward_code() -> str:
    return REWARD_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(6))


def new_loyalty_code() -> str:
    return LOYALTY_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(6))


def normalise_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = code.strip().upper()
    return cleaned or None
