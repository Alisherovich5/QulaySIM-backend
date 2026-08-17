"""The hardening pass: masked logs, and a checkout that survives a double tap.

Each of these closes a failure that costs real money or real trust, and each is
cheap to break again by accident — which is why they are pinned here rather than
left as a comment.
"""

from __future__ import annotations

import pytest

from app.core.logging import _mask
from app.services.orders import _idempotency_key


class TestLogsKeepSecrets:
    """A token in a log line is a token that has leaked.

    Logs are read by more people than the database is, pasted into chat when
    something breaks, and kept long after the incident.
    """

    @pytest.mark.parametrize(
        "field",
        ["token", "refresh_token", "password", "api_key", "signature", "activation_code", "qr"],
    )
    def test_a_secret_field_never_reaches_the_line(self, field: str) -> None:
        out = _mask(None, "", {field: "this-is-the-real-value"})
        assert out[field] == "[maskalangan]"
        assert "real-value" not in str(out)

    def test_an_email_inside_a_message_is_masked_too(self) -> None:
        """The common accident: a sentence built with the customer's address."""
        out = _mask(None, "", {"event": "support.sent", "detail": "from customer@example.com"})
        assert "customer@example.com" not in out["detail"]

    def test_a_phone_number_is_masked_in_any_shape(self) -> None:
        for shape in ("+998901234567", "998 90 123 45 67", "+998(90)123-45-67"):
            out = _mask(None, "", {"detail": f"called {shape} today"})
            assert "1234567" not in out["detail"].replace(" ", ""), shape

    def test_a_card_shaped_number_is_masked(self) -> None:
        out = _mask(None, "", {"detail": "pan 4111111111111111 declined"})
        assert "4111111111111111" not in out["detail"]

    def test_a_jwt_is_masked(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
        out = _mask(None, "", {"detail": f"bearer {jwt}"})
        assert jwt not in out["detail"]

    def test_ordinary_operational_fields_survive(self) -> None:
        """Masking that eats the useful fields is masking nobody will keep."""
        out = _mask(None, "", {"event": "orders.placed", "order_id": 61, "status": "paid"})
        assert out == {"event": "orders.placed", "order_id": 61, "status": "paid"}


class TestCheckoutKeyIsScopedToTheCustomer:
    def test_the_same_key_from_two_customers_does_not_collide(self) -> None:
        """Two people can each pick "1" as their key.

        Without the customer in the key, the second one would be handed the
        first one's order and its payment link.
        """
        assert _idempotency_key(1, "abc") != _idempotency_key(2, "abc")

    def test_the_key_is_not_stored_verbatim(self) -> None:
        """It is client-supplied text; hashing keeps it out of Redis key names
        and caps its length."""
        key = _idempotency_key(1, "customer-chose-this")
        assert "customer-chose-this" not in key

    def test_whitespace_does_not_make_a_new_key(self) -> None:
        assert _idempotency_key(1, " abc ") == _idempotency_key(1, "abc")
