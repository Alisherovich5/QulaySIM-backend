"""Which wholesaler fulfils an order, and what happens when one refuses.

These use lightweight stand-ins rather than the ORM: routing reads only
`offers`, `provider`, `provider_package_code` and `cost_usd`, and building real
Plan rows would test SQLAlchemy instead of the decision being made here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.integrations.suppliers import (
    SupplierError,
    SupplierLine,
    get_supplier,
    register_supplier,
    routes_for,
    usable_routes_for,
)


@dataclass
class FakeOffer:
    provider: str
    package_code: str
    cost_usd: Decimal
    is_available: bool = True


@dataclass
class FakePlan:
    provider: str = "mock"
    provider_package_code: str = ""
    cost_usd: Decimal | None = None
    offers: list[FakeOffer] = field(default_factory=list)


@dataclass
class FakeItem:
    plan: FakePlan
    quantity: int = 1
    plan_id: int = 1


@dataclass
class FakeOrder:
    items: list[FakeItem]
    id: int = 1


def offer(provider: str, cost: str, code: str | None = None, available: bool = True) -> FakeOffer:
    return FakeOffer(
        provider=provider,
        package_code=code or f"{provider}-code",
        cost_usd=Decimal(cost),
        is_available=available,
    )


class TestRouteSelection:
    def test_cheapest_supplier_is_tried_first(self):
        plan = FakePlan(offers=[offer("esimaccess", "4.20"), offer("esimcard", "3.80")])
        routes = routes_for(FakeOrder([FakeItem(plan)]))

        assert [route.provider for route in routes] == ["esimcard", "esimaccess"]
        assert routes[0].total_cost_usd == Decimal("3.80")

    def test_route_carries_that_suppliers_own_package_code(self):
        plan = FakePlan(
            offers=[
                offer("esimaccess", "4.20", code="TR_5_30"),
                offer("esimcard", "3.80", code="tur-5gb-30d"),
            ]
        )
        routes = routes_for(FakeOrder([FakeItem(plan)]))

        # Ordering the right price against the wrong code would deliver the
        # wrong eSIM, so the code has to travel with the route.
        assert routes[0].lines == (SupplierLine(package_code="tur-5gb-30d", quantity=1),)

    def test_unavailable_offer_is_not_a_route(self):
        plan = FakePlan(
            offers=[offer("esimcard", "1.00", available=False), offer("esimaccess", "4.20")]
        )
        routes = routes_for(FakeOrder([FakeItem(plan)]))

        assert [route.provider for route in routes] == ["esimaccess"]

    def test_quantity_multiplies_the_route_cost(self):
        plan = FakePlan(offers=[offer("esimcard", "3.00")])
        routes = routes_for(FakeOrder([FakeItem(plan, quantity=4)]))

        assert routes[0].total_cost_usd == Decimal("12.00")
        assert routes[0].lines[0].quantity == 4

    def test_plan_with_no_offers_still_routes_via_its_denormalised_supplier(self):
        # The catalogue predates supplier offers, so those plans carry only a
        # provider and a code. Dropping them would strand every existing order.
        plan = FakePlan(
            provider="esimaccess", provider_package_code="JP_1_7", cost_usd=Decimal("2.50")
        )
        routes = routes_for(FakeOrder([FakeItem(plan)]))

        assert [route.provider for route in routes] == ["esimaccess"]
        assert routes[0].lines[0].package_code == "JP_1_7"


class TestWholeOrderCoverage:
    def test_only_a_supplier_covering_every_line_is_a_route(self):
        both = FakePlan(offers=[offer("esimaccess", "4.00"), offer("esimcard", "3.00")])
        access_only = FakePlan(offers=[offer("esimaccess", "9.00")])
        routes = routes_for(FakeOrder([FakeItem(both), FakeItem(access_only)]))

        # eSIMCard is cheaper on the first line but cannot supply the second,
        # and an Order records one supplier reference — so it is not a route.
        assert [route.provider for route in routes] == ["esimaccess"]
        assert routes[0].total_cost_usd == Decimal("13.00")

    def test_no_supplier_covers_a_split_order(self):
        card_only = FakePlan(offers=[offer("esimcard", "3.00")])
        access_only = FakePlan(offers=[offer("esimaccess", "4.00")])
        routes = routes_for(FakeOrder([FakeItem(card_only), FakeItem(access_only)]))

        # Reported as "no route" rather than half-fulfilled; the caller logs at
        # error level because the order is already paid.
        assert routes == []

    def test_cheapest_total_wins_not_cheapest_line(self):
        # esimcard is cheaper on line one, dearer on line two, and loses overall.
        first = FakePlan(offers=[offer("esimaccess", "5.00"), offer("esimcard", "4.00")])
        second = FakePlan(offers=[offer("esimaccess", "5.00"), offer("esimcard", "9.00")])
        routes = routes_for(FakeOrder([FakeItem(first), FakeItem(second)]))

        assert routes[0].provider == "esimaccess"
        assert routes[0].total_cost_usd == Decimal("10.00")

    def test_order_with_no_items_has_no_routes(self):
        assert routes_for(FakeOrder([])) == []


class TestUsableRoutes:
    def test_unregistered_supplier_is_dropped_not_used(self):
        # eSIMCard has no client yet: it must not be handed an order it cannot
        # place, and eSIM Access must still fulfil.
        plan = FakePlan(offers=[offer("esimcard", "1.00"), offer("esimaccess", "4.20")])

        class ConfiguredAccess:
            key = "esimaccess"

            def is_configured(self):
                return True

            def place_order(self, *, transaction_id, lines):  # pragma: no cover
                return "n/a"

        register_supplier(ConfiguredAccess())
        try:
            usable = usable_routes_for(FakeOrder([FakeItem(plan)]))
        finally:
            register_supplier(_real_esimaccess)

        assert [route.provider for route in usable] == ["esimaccess"]

    def test_unconfigured_supplier_is_dropped(self):
        plan = FakePlan(offers=[offer("esimaccess", "4.20")])

        class Unconfigured:
            key = "esimaccess"

            def is_configured(self):
                return False

            def place_order(self, *, transaction_id, lines):  # pragma: no cover
                return "n/a"

        register_supplier(Unconfigured())
        try:
            assert usable_routes_for(FakeOrder([FakeItem(plan)])) == []
        finally:
            register_supplier(_real_esimaccess)

    def test_esimcard_is_not_registered_yet(self):
        # Guards against a placeholder client being wired up by accident: an
        # order routed to a stub would be marked ORDERED with nothing bought.
        assert get_supplier("esimcard") is None


_real_esimaccess = get_supplier("esimaccess")


class TestSupplierErrorContract:
    def test_esim_access_failure_surfaces_as_supplier_error(self, monkeypatch):
        from app.integrations import esim_access, suppliers

        class Boom:
            def order_profiles(self, **_):
                raise esim_access.EsimAccessError("supplier down")

        monkeypatch.setattr(esim_access, "EsimAccessClient", lambda: Boom())

        with pytest.raises(SupplierError, match="supplier down"):
            suppliers.EsimAccessSupplier().place_order(
                transaction_id="qs-1", lines=[SupplierLine("TR_5_30", 1)]
            )

    def test_accepted_order_without_a_reference_is_a_failure(self, monkeypatch):
        from app.integrations import esim_access, suppliers

        class Empty:
            def order_profiles(self, **_):
                return {"obj": {}}

        monkeypatch.setattr(esim_access, "EsimAccessClient", lambda: Empty())

        # Storing an empty reference would leave profile sync silently doing
        # nothing, so this must fail loudly and let the fallback run.
        with pytest.raises(SupplierError, match="no order number"):
            suppliers.EsimAccessSupplier().place_order(
                transaction_id="qs-1", lines=[SupplierLine("TR_5_30", 1)]
            )
