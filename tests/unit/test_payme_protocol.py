"""Payme protocol details that are easy to get subtly wrong."""

from __future__ import annotations

import base64

import pytest

from app.integrations.payme import (
    INSUFFICIENT_PRIVILEGES,
    MESSAGES,
    authorised,
    checkout_url,
    error,
    result,
)


def basic(login: str, key: str) -> str:
    return "Basic " + base64.b64encode(f"{login}:{key}".encode()).decode()


class TestAuthorisation:
    KEYS = ("live-key", "sandbox-key")

    def test_accepts_the_live_key(self) -> None:
        assert authorised(basic("Paycom", "live-key"), self.KEYS)

    def test_accepts_the_sandbox_key(self) -> None:
        """The same deployment has to pass Payme's test suite."""
        assert authorised(basic("Paycom", "sandbox-key"), self.KEYS)

    def test_rejects_a_wrong_key(self) -> None:
        assert not authorised(basic("Paycom", "nope"), self.KEYS)

    def test_rejects_a_missing_header(self) -> None:
        assert not authorised(None, self.KEYS)

    def test_rejects_an_empty_key(self) -> None:
        assert not authorised(basic("Paycom", ""), self.KEYS)

    def test_rejects_a_non_basic_scheme(self) -> None:
        assert not authorised("Bearer live-key", self.KEYS)

    def test_rejects_malformed_base64(self) -> None:
        assert not authorised("Basic !!!not-base64!!!", self.KEYS)

    def test_ignores_the_login_part(self) -> None:
        """Payme sends 'Paycom' but the key is what authenticates."""
        assert authorised(basic("anything", "live-key"), self.KEYS)

    def test_empty_configured_keys_authorise_nobody(self) -> None:
        assert not authorised(basic("Paycom", ""), ("", ""))
        assert not authorised(basic("Paycom", "x"), ("", ""))


class TestErrorShape:
    def test_message_is_an_object_not_a_string(self) -> None:
        """Payme requires per-language messages; a plain string is rejected."""
        body = error(7, INSUFFICIENT_PRIVILEGES)["error"]
        assert isinstance(body["message"], dict)
        assert set(body["message"]) == {"ru", "uz", "en"}

    def test_request_id_is_echoed(self) -> None:
        assert error("abc", INSUFFICIENT_PRIVILEGES)["id"] == "abc"
        assert result("abc", {"allow": True})["id"] == "abc"

    def test_data_is_omitted_when_absent(self) -> None:
        assert "data" not in error(1, INSUFFICIENT_PRIVILEGES)["error"]
        assert error(1, INSUFFICIENT_PRIVILEGES, "amount")["error"]["data"] == "amount"

    @pytest.mark.parametrize("code", list(MESSAGES))
    def test_every_code_has_all_three_languages(self, code: int) -> None:
        assert set(MESSAGES[code]) == {"ru", "uz", "en"}


class TestCheckoutUrl:
    def test_encodes_the_documented_parameter_string(self) -> None:
        url = checkout_url(
            base_url="https://checkout.paycom.uz",
            merchant_id="abc123",
            account_field="order_id",
            account_value="42",
            amount_tiyin=5445828,
        )
        decoded = base64.b64decode(url.rsplit("/", 1)[1]).decode()
        assert decoded == "m=abc123;ac.order_id=42;a=5445828"

    def test_return_url_is_appended_when_given(self) -> None:
        url = checkout_url(
            base_url="https://checkout.paycom.uz",
            merchant_id="abc123",
            account_field="order_id",
            account_value="42",
            amount_tiyin=100,
            return_url="https://qulaysim.uz/account",
        )
        assert "c=https://qulaysim.uz/account" in base64.b64decode(url.rsplit("/", 1)[1]).decode()

    def test_trailing_slash_does_not_double_up(self) -> None:
        url = checkout_url(
            base_url="https://checkout.paycom.uz/",
            merchant_id="m",
            account_field="order_id",
            account_value="1",
            amount_tiyin=1,
        )
        assert "//" not in url.removeprefix("https://")
