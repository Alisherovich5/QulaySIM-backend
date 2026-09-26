"""Proving a Telegram login came from Telegram, and recently.

The widget posts a flat object that anybody can post at us, so the hash is the
whole of the security. These tests are about the ways that can be got wrong:
a signature that is nearly right, a payload replayed a day later, a field added
to smuggle a value past the signed set, and a bot token we do not have.

The freshness half matters as much as the signature. Telegram never revokes a
login it issued, so a captured payload — out of a browser history, a shared
screen, a proxy log — is a permanent credential unless something ages it out.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.integrations.telegram_auth import (
    MAX_AGE_SECONDS,
    TelegramAuthError,
    verify_login,
)

TOKEN = "123456:AAFakeBotTokenForTestsOnly"


def signed(token: str = TOKEN, **fields: str) -> dict[str, str]:
    """A payload signed the way Telegram signs one."""
    data = {
        "id": "42",
        "first_name": "Otabek",
        "last_name": "Qayumov",
        "username": "otabek",
        "photo_url": "https://t.me/i/userpic/320/otabek.jpg",
        "auth_date": str(int(time.time())),
    }
    data.update(fields)
    data = {k: v for k, v in data.items() if v != ""}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


class TestAGenuineLogin:
    def test_is_accepted_and_names_the_person(self) -> None:
        who = verify_login(signed(), bot_token=TOKEN)
        assert who.uid == "42"
        assert who.username == "otabek"
        assert who.full_name == "Otabek Qayumov"

    def test_survives_a_missing_optional_field(self) -> None:
        # Telegram omits last_name and username for accounts that have none,
        # and the signature is computed over what was sent — so a verifier that
        # signs over a fixed list rejects half of real users.
        who = verify_login(signed(last_name="", username=""), bot_token=TOKEN)
        assert who.full_name == "Otabek"
        assert who.username == ""

    def test_carries_no_email_because_telegram_has_none(self) -> None:
        who = verify_login(signed(), bot_token=TOKEN)
        assert not hasattr(who, "email")


class TestForgery:
    def test_a_wrong_hash_is_refused(self) -> None:
        data = signed()
        data["hash"] = "0" * 64
        with pytest.raises(TelegramAuthError):
            verify_login(data, bot_token=TOKEN)

    def test_a_payload_signed_with_another_bot_is_refused(self) -> None:
        with pytest.raises(TelegramAuthError):
            verify_login(signed(token="999:SomeoneElsesBot"), bot_token=TOKEN)

    def test_changing_a_field_after_signing_is_refused(self) -> None:
        # The whole point: an id swapped for somebody else's must not verify.
        data = signed()
        data["id"] = "43"
        with pytest.raises(TelegramAuthError):
            verify_login(data, bot_token=TOKEN)

    def test_an_extra_field_cannot_be_smuggled_in(self) -> None:
        """A key Telegram never sends is ignored rather than signed over, so
        adding one neither breaks a genuine login nor sneaks a value through."""
        data = signed()
        data["is_admin"] = "1"
        who = verify_login(data, bot_token=TOKEN)
        assert who.uid == "42"

    def test_a_payload_with_no_hash_is_refused(self) -> None:
        data = signed()
        del data["hash"]
        with pytest.raises(TelegramAuthError):
            verify_login(data, bot_token=TOKEN)

    def test_nothing_verifies_without_a_configured_token(self) -> None:
        # An unconfigured deployment must refuse every login rather than accept
        # every login, which is what an empty key would do if it were used.
        with pytest.raises(TelegramAuthError):
            verify_login(signed(), bot_token="")


class TestFreshness:
    def test_a_login_from_yesterday_is_refused(self) -> None:
        old = str(int(time.time()) - 24 * 3600)
        with pytest.raises(TelegramAuthError):
            verify_login(signed(auth_date=old), bot_token=TOKEN)

    def test_the_boundary_is_honoured_on_both_sides(self) -> None:
        now = time.time()
        just_inside = signed(auth_date=str(int(now) - (MAX_AGE_SECONDS - 5)))
        assert verify_login(just_inside, bot_token=TOKEN, now=now).uid == "42"

        just_outside = signed(auth_date=str(int(now) - (MAX_AGE_SECONDS + 5)))
        with pytest.raises(TelegramAuthError):
            verify_login(just_outside, bot_token=TOKEN, now=now)

    def test_a_date_in_the_future_is_refused(self) -> None:
        """It cannot be aged out later, so it is refused now."""
        ahead = str(int(time.time()) + 3600)
        with pytest.raises(TelegramAuthError):
            verify_login(signed(auth_date=ahead), bot_token=TOKEN)

    def test_an_unreadable_date_is_refused_rather_than_treated_as_zero(self) -> None:
        with pytest.raises(TelegramAuthError):
            verify_login(signed(auth_date="kecha"), bot_token=TOKEN)
