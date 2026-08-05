"""Checkout must refuse a plan no wholesaler can supply.

Written after finding twenty plans live on the site at $29.90–$35.88 with no
supplier offer and no package code. They were active, they were priced, and the
sourcing engine had nothing to route them to — so a customer could have paid and
no eSIM could ever have been issued.

Everything upstream of this gate is advisory: the admin badge is something a
person has to notice, `Plan.is_active` is a toggle anyone can flip, and the
pricing rules do not care whether a cost came from a real supplier. This is the
one check that has to hold on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.core.config import settings
from app.services.checkout import is_fulfillable


@dataclass
class FakeOffer:
    provider: str
    is_available: bool = True


@dataclass
class FakePlan:
    id: int = 1
    title: str = "Turkey Unlimited · 7 days"
    provider: str = "mock"
    provider_package_code: str = ""
    offers: list = field(default_factory=list)


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    monkeypatch.setattr(settings, "fulfillable_providers_raw", "esimaccess,esimcard")


class TestRefused:
    def test_the_exact_shape_that_was_live_on_the_site(self):
        # provider="mock", no offers, no package code — twenty of these were
        # for sale.
        assert is_fulfillable(FakePlan()) is False

    def test_an_offer_from_a_supplier_we_cannot_order_from_does_not_count(self):
        # A price on file is not an ability to buy. "mock" has no client.
        assert is_fulfillable(FakePlan(offers=[FakeOffer("mock")])) is False

    def test_an_offer_marked_out_of_stock_does_not_count(self):
        plan = FakePlan(offers=[FakeOffer("esimaccess", is_available=False)])
        assert is_fulfillable(plan) is False

    def test_a_named_provider_without_a_package_code_does_not_count(self):
        # Nothing to send the wholesaler, so the order would be placed against
        # an empty code and fail after the card was charged.
        assert is_fulfillable(FakePlan(provider="esimaccess")) is False

    def test_a_plan_with_no_offers_attribute_at_all_is_refused(self):
        class Bare:
            id = 2
            title = "x"
            provider = "mock"
            provider_package_code = ""

        assert is_fulfillable(Bare()) is False


class TestAllowed:
    def test_an_available_offer_from_a_connected_supplier(self):
        assert is_fulfillable(FakePlan(offers=[FakeOffer("esimaccess")])) is True

    def test_either_wholesaler_is_enough(self):
        assert is_fulfillable(FakePlan(offers=[FakeOffer("esimcard")])) is True

    def test_the_denormalised_provider_and_code_are_enough(self):
        # Plans that predate supplier offers still route: they name a supplier
        # and a code directly, and refusing them would take real stock offline.
        plan = FakePlan(provider="esimaccess", provider_package_code="TR_5_30")
        assert is_fulfillable(plan) is True

    def test_one_usable_offer_among_unusable_ones_is_enough(self):
        plan = FakePlan(
            offers=[
                FakeOffer("mock"),
                FakeOffer("esimaccess", is_available=False),
                FakeOffer("esimcard"),
            ]
        )
        assert is_fulfillable(plan) is True


def test_disconnecting_a_supplier_takes_its_plans_out_of_sale(monkeypatch):
    plan = FakePlan(offers=[FakeOffer("esimcard")])
    assert is_fulfillable(plan) is True
    # The setting is the switch: removing a wholesaler must stop its plans
    # selling, which is what makes it safe to pull one in an outage.
    monkeypatch.setattr(settings, "fulfillable_providers_raw", "esimaccess")
    assert is_fulfillable(plan) is False


def test_the_env_var_is_the_one_the_admin_uses():
    # Both services decide "is this supplier connected" from FULFILLABLE_PROVIDERS.
    # If the alias were dropped the backend would silently read a different
    # variable and could sell what the admin thinks is unsellable.
    field = type(settings).model_fields["fulfillable_providers_raw"]
    assert field.validation_alias == "FULFILLABLE_PROVIDERS"
