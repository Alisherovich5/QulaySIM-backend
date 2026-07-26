"""Referral code generation and reward rules — pure logic."""

from __future__ import annotations

import secrets
import string

ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8
REWARD_PERCENT = 10
REWARD_PREFIX = "REF-"


def new_referral_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_reward_code() -> str:
    return REWARD_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(6))


def normalise_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = code.strip().upper()
    return cleaned or None
