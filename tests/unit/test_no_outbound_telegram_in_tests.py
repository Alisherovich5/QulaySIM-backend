"""Tests must not be able to message a real person.

A support-form smoke test posts a plausible payload to `/api/support/message`, a
developer's `.env` carries a live bot token, and the two together put
"Test User / customer@example.com" on the shop owner's phone during a routine test
run. Nothing was broken; the suite simply had permission to talk to the outside.

This asserts the guard in conftest is still in place, so the next person to run
the suite cannot repeat it — including by adding an `.env` with a token in it.
"""

from __future__ import annotations

from app.core.config import settings


def test_the_bot_token_is_blank_under_test() -> None:
    assert settings.telegram_bot_token == "", (
        "conftest must clear TELEGRAM_BOT_TOKEN: with a real token the support "
        "endpoint sends an actual message"
    )


def test_the_chat_id_is_blank_under_test() -> None:
    assert settings.telegram_chat_id == ""
