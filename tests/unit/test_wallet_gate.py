"""Checkout must refuse a plan whose only wholesaler cannot pay for it.

Order #141: a customer paid $5.91 for a plan eSIMCard alone stocked, on a day
eSIMCard's wallet held $0.24. The purchase came back "Insufficient Wallet
Balance", the rescue re-dispatched it every five minutes for three days, and
every one of those dispatches hit the same empty wallet. The money was taken
before anything checked whether it could be spent.

The gate this adds is narrow on purpose, and these tests are mostly about the
edges of that narrowness. A plan with a second wholesaler still sells. A balance
nobody could read still sells. What stops is the exact case above: one supplier,
a balance we know, and not enough of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.core.config import settings
from app.integrations.wallets import can_cover
from app.services.checkout import is_fulfillable


@dataclass
class FakeOffer:
    provider: str
    cost_usd: Decimal = Decimal("4.00")
    is_available: bool = True


@dataclass
class FakePlan:
    id: int = 1
    title: str = "Vietnam Unlimited · 8 days"
    provider: str = "mock"
    provider_package_code: str = ""
    cost_usd: Decimal | None = None
    offers: list = field(default_factory=list)


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    monkeypatch.setattr(settings, "fulfillable_providers_raw", "esimaccess,esimcard")


class TestTheOrderThatFailed:
    def test_a_single_supplier_with_an_empty_wallet_does_not_sell(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", Decimal("3.94"))])
        assert is_fulfillable(plan, {"esimcard": 0.24}) is False

    def test_the_same_plan_sells_once_the_wallet_is_topped_up(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", Decimal("3.94"))])
        assert is_fulfillable(plan, {"esimcard": 50.0}) is True

    def test_a_second_supplier_keeps_it_on_sale(self):
        # The whole point of the narrowness: eSIMCard being broke is not a
        # reason to stop selling something eSIM Access also stocks. This is the
        # shape of order #145, which failed at eSIMCard and was delivered by
        # eSIM Access on the same run.
        plan = FakePlan(
            offers=[
                FakeOffer("esimcard", Decimal("3.94")),
                FakeOffer("esimaccess", Decimal("4.10")),
            ]
        )
        assert is_fulfillable(plan, {"esimcard": 0.24, "esimaccess": 45.48}) is True

    def test_exactly_enough_is_enough(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", Decimal("4.00"))])
        assert is_fulfillable(plan, {"esimcard": 4.0}) is True


class TestFailsOpen:
    """Every way of not knowing has to end in a sale, not in a closed shop."""

    def test_no_balances_at_all_behaves_exactly_as_before(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", Decimal("3.94"))])
        assert is_fulfillable(plan) is True

    def test_a_supplier_missing_from_the_reading_is_not_assumed_broke(self):
        # What a supplier whose balance endpoint timed out looks like: absent,
        # not zero. `integrations.wallets` drops unknowns for this reason.
        plan = FakePlan(offers=[FakeOffer("esimcard", Decimal("3.94"))])
        assert is_fulfillable(plan, {"esimaccess": 45.48}) is True

    def test_a_plan_with_no_known_cost_is_not_judged(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", None)])
        assert is_fulfillable(plan, {"esimcard": 0.24}) is True

    def test_an_unparsable_cost_sells_rather_than_raising(self):
        plan = FakePlan(offers=[FakeOffer("esimcard", "about four dollars")])
        assert is_fulfillable(plan, {"esimcard": 0.24}) is True


class TestDenormalisedPlans:
    """The offer-less plans that predate supplier offers route by provider."""

    def test_the_fallback_path_checks_the_wallet_too(self):
        plan = FakePlan(
            provider="esimcard",
            provider_package_code="VN-8D",
            cost_usd=Decimal("3.94"),
        )
        assert is_fulfillable(plan, {"esimcard": 0.24}) is False
        assert is_fulfillable(plan, {"esimcard": 50.0}) is True

    def test_a_plan_with_neither_offer_nor_code_is_still_refused(self):
        assert is_fulfillable(FakePlan(), {"esimcard": 50.0}) is False


class TestCanCover:
    def test_a_balance_of_zero_is_a_balance_not_an_absence(self):
        # bool(0.0) is False, so a mapping that holds a real zero must not be
        # mistaken for one that holds nothing.
        assert can_cover("esimcard", Decimal("1.00"), {"esimcard": 0.0}) is False

    def test_an_unknown_provider_answers_yes(self):
        assert can_cover("esimcard", Decimal("1.00"), {}) is True
