"""Which supplier an order is offered to when one wallet is empty.

The rule is a preference, not a veto. Everything here is about what must still
happen when the balance is wrong, stale or unreadable — because the money has
already been taken from the customer by the time this code runs, and an order
that cannot be routed at all is worse than one routed to a supplier that
refuses it.
"""

from __future__ import annotations

from app.integrations.wallets import can_cover


class TestCanCover:
    def test_a_wallet_with_enough_covers_it(self) -> None:
        assert can_cover("esimcard", 3.94, {"esimcard": 10.0}) is True

    def test_a_wallet_with_exactly_enough_covers_it(self) -> None:
        # Not `>`: a balance equal to the price buys the thing.
        assert can_cover("esimcard", 3.94, {"esimcard": 3.94}) is True

    def test_an_empty_wallet_does_not(self) -> None:
        assert can_cover("esimcard", 3.94, {"esimcard": 0.24}) is False

    def test_a_balance_we_could_not_read_is_not_an_answer(self) -> None:
        """The supplier did not reply, or Redis did not. That is not a reason
        to rule it out — it is a reason to have no opinion, and the purchase
        attempt is still the authority."""
        assert can_cover("esimcard", 3.94, {"esimcard": None}) is True

    def test_a_supplier_nobody_asked_about_is_not_ruled_out(self) -> None:
        assert can_cover("esimaccess", 3.94, {}) is True
        assert can_cover("esimaccess", 3.94, {"esimcard": 0.0}) is True


class TestTheOrderOfRoutes:
    """The reordering itself, as fulfilment does it — cheapest first, then the
    ones that cannot be paid for pushed to the back."""

    @staticmethod
    def _reorder(routes: list[tuple[str, float]], known: dict[str, float | None]) -> list[str]:
        affordable = [r for r in routes if can_cover(r[0], r[1], known)]
        short = [r for r in routes if r not in affordable]
        return [r[0] for r in affordable + short]

    def test_the_broke_supplier_goes_last_even_when_cheaper(self) -> None:
        # eSIMCard is cheaper, so it is first by cost. It has 24 cents.
        routes = [("esimcard", 3.94), ("esimaccess", 4.60)]
        assert self._reorder(routes, {"esimcard": 0.24, "esimaccess": 45.48}) == [
            "esimaccess",
            "esimcard",
        ]

    def test_nothing_is_dropped(self) -> None:
        """Both wallets empty: the order still has two routes to try. Dropping
        them would turn a paid order into one with nowhere to go, which is the
        state this whole path exists to avoid."""
        routes = [("esimcard", 3.94), ("esimaccess", 4.60)]
        assert set(self._reorder(routes, {"esimcard": 0.0, "esimaccess": 0.0})) == {
            "esimcard",
            "esimaccess",
        }

    def test_cheapest_first_survives_when_everyone_can_pay(self) -> None:
        routes = [("esimcard", 3.94), ("esimaccess", 4.60)]
        assert self._reorder(routes, {"esimcard": 50.0, "esimaccess": 50.0}) == [
            "esimcard",
            "esimaccess",
        ]

    def test_an_unreadable_balance_changes_nothing(self) -> None:
        routes = [("esimcard", 3.94), ("esimaccess", 4.60)]
        assert self._reorder(routes, {"esimcard": None, "esimaccess": None}) == [
            "esimcard",
            "esimaccess",
        ]
