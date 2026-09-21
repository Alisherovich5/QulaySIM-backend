"""Which supplier an order is offered to, and when none of them is asked at all.

Two rules that look contradictory and are not. Preferring the wallet that can
pay is advisory: a balance that is wrong, stale or unreadable must never make a
paid order unfulfillable, so an unknown balance changes nothing and the purchase
attempt stays the authority.

Refusing to attempt anything is the one exception, and it needs every balance to
be *known* and every one of them too small. That is not a guess about what the
supplier will say — it is arithmetic. Order #141 is what the old behaviour cost:
one route, an empty eSIMCard wallet, a refusal raised back to Celery, ten
retries with backoff, and the rescue re-dispatching the whole thing every five
minutes for three days.

These drive the real `_place_supplier_order` rather than a copy of its logic in
the test file. A reimplementation passes forever after production stops matching
it, which is the failure this module is here to prevent.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.core.config import settings
from app.integrations.suppliers import Route, SupplierError, SupplierLine
from app.integrations.wallets import can_cover
from app.workers.tasks import provisioning


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
        to rule it out — it is a reason to have no opinion."""
        assert can_cover("esimcard", 3.94, {"esimcard": None}) is True

    def test_a_supplier_nobody_asked_about_is_not_ruled_out(self) -> None:
        assert can_cover("esimaccess", 3.94, {}) is True
        assert can_cover("esimaccess", 3.94, {"esimcard": 0.0}) is True

    def test_a_decimal_cost_is_compared_not_rejected(self) -> None:
        # Route totals are Decimal; a float-only comparison would raise.
        assert can_cover("esimcard", Decimal("3.94"), {"esimcard": 0.24}) is False


# --------------------------------------------------------------------------
# Enough of an order for `_place_supplier_order` to work on. Routing reads the
# order only to find its items, and the routes themselves are injected.
# --------------------------------------------------------------------------


@dataclass
class FakeItem:
    id: int = 1
    plan_id: int = 1


@dataclass
class FakeOrder:
    id: int = 141
    items: list[FakeItem] = field(default_factory=lambda: [FakeItem()])
    provider: str | None = None
    provider_order_no: str | None = None
    provider_status: str | None = None


class FakeSession:
    def __init__(self, order: FakeOrder) -> None:
        self.order = order

    def get(self, _model: object, _pk: int) -> FakeOrder:
        return self.order


def route(provider: str, cost: str) -> Route:
    return Route(
        provider=provider,
        lines=(SupplierLine(package_code=f"{provider}-PKG", quantity=1, item_id=1),),
        total_cost_usd=Decimal(cost),
    )


class RecordingSupplier:
    """A wholesaler that counts how often it was asked to sell something."""

    def __init__(self, key: str, *, refuses: bool = False) -> None:
        self.key = key
        self.refuses = refuses
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def place_order(self, **_: object) -> str:
        self.calls += 1
        if self.refuses:
            raise SupplierError("Insufficient Wallet Balance")
        return f"{self.key}-order-1"


@pytest.fixture
def wired(monkeypatch):
    """`_place_supplier_order` against fakes, with the knobs it reads."""
    order = FakeOrder()
    suppliers: dict[str, RecordingSupplier] = {}
    state: dict[str, object] = {"routes": [], "balances": {}}

    @contextmanager
    def fake_session():
        yield FakeSession(order)

    monkeypatch.setattr(settings, "esim_provider", "live")
    monkeypatch.setattr(provisioning, "worker_session", fake_session)
    monkeypatch.setattr(provisioning, "_pinned_provider", lambda *_: None)
    monkeypatch.setattr(
        provisioning, "get_supplier_or_none", lambda provider: suppliers.get(provider)
    )
    monkeypatch.setattr(
        "app.integrations.suppliers.usable_routes_for", lambda _order: state["routes"]
    )
    monkeypatch.setattr("app.integrations.wallets.balances", lambda: state["balances"])
    return {"order": order, "suppliers": suppliers, "state": state}


class TestNoWalletCanPay:
    """Order #141: the purchase that could not succeed, attempted 1,700 times a day."""

    def test_the_supplier_is_not_called_at_all(self, wired) -> None:
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard", refuses=True)
        wired["state"]["routes"] = [route("esimcard", "3.94")]
        wired["state"]["balances"] = {"esimcard": 0.24}

        assert provisioning._place_supplier_order(141, attempt=0) is False
        assert wired["suppliers"]["esimcard"].calls == 0

    def test_it_returns_rather_than_raising(self, wired) -> None:
        """An exception is a promise to Celery that a retry might work. Here it
        cannot: the retry places the same call against the same empty wallet."""
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard", refuses=True)
        wired["state"]["routes"] = [route("esimcard", "3.94")]
        wired["state"]["balances"] = {"esimcard": 0.24}

        # No pytest.raises: returning False is the whole point.
        assert provisioning._place_supplier_order(141, attempt=0) is False

    def test_every_route_short_stops_all_of_them(self, wired) -> None:
        for key in ("esimcard", "esimaccess"):
            wired["suppliers"][key] = RecordingSupplier(key, refuses=True)
        wired["state"]["routes"] = [route("esimcard", "3.94"), route("esimaccess", "4.60")]
        wired["state"]["balances"] = {"esimcard": 0.24, "esimaccess": 1.00}

        assert provisioning._place_supplier_order(141, attempt=0) is False
        assert all(s.calls == 0 for s in wired["suppliers"].values())


class TestItStillTriesWhenItMightWork:
    """Everything that must keep happening, because the money is already taken."""

    def test_one_readable_shortfall_does_not_stop_the_other(self, wired) -> None:
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard")
        wired["suppliers"]["esimaccess"] = RecordingSupplier("esimaccess")
        wired["state"]["routes"] = [route("esimcard", "3.94"), route("esimaccess", "4.60")]
        wired["state"]["balances"] = {"esimcard": 0.24, "esimaccess": 45.48}

        assert provisioning._place_supplier_order(141, attempt=0) is True
        # The one that can pay was asked; the broke one was never touched.
        assert wired["suppliers"]["esimaccess"].calls == 1
        assert wired["suppliers"]["esimcard"].calls == 0
        assert wired["order"].provider == "esimaccess"

    def test_an_unreadable_balance_is_attempted(self, wired) -> None:
        """The supplier did not answer when asked for its balance. That is not
        a reason to leave a paid order unfulfilled."""
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard")
        wired["state"]["routes"] = [route("esimcard", "3.94")]
        wired["state"]["balances"] = {"esimcard": None}

        assert provisioning._place_supplier_order(141, attempt=0) is True
        assert wired["suppliers"]["esimcard"].calls == 1

    def test_no_balances_at_all_is_attempted(self, wired) -> None:
        """Redis empty, or the watch has not run yet."""
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard")
        wired["state"]["routes"] = [route("esimcard", "3.94")]
        wired["state"]["balances"] = {}

        assert provisioning._place_supplier_order(141, attempt=0) is True
        assert wired["suppliers"]["esimcard"].calls == 1

    def test_a_refusal_from_a_funded_wallet_still_raises(self, wired) -> None:
        """The balance said yes and the supplier said no. That is the case
        Celery's retry exists for, and it must not be swallowed."""
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard", refuses=True)
        wired["state"]["routes"] = [route("esimcard", "3.94")]
        wired["state"]["balances"] = {"esimcard": 50.0}

        with pytest.raises(SupplierError):
            provisioning._place_supplier_order(141, attempt=0)
        assert wired["suppliers"]["esimcard"].calls == 1

    def test_the_cheaper_funded_wallet_still_wins(self, wired) -> None:
        wired["suppliers"]["esimcard"] = RecordingSupplier("esimcard")
        wired["suppliers"]["esimaccess"] = RecordingSupplier("esimaccess")
        wired["state"]["routes"] = [route("esimcard", "3.94"), route("esimaccess", "4.60")]
        wired["state"]["balances"] = {"esimcard": 50.0, "esimaccess": 50.0}

        assert provisioning._place_supplier_order(141, attempt=0) is True
        assert wired["suppliers"]["esimcard"].calls == 1
        assert wired["suppliers"]["esimaccess"].calls == 0
