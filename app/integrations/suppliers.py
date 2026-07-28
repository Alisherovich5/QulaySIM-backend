"""Choosing which wholesaler fulfils an order, and in what order to try them.

The catalogue can be sourced from more than one wholesaler, and which one is
cheapest varies by destination. The Django admin already resolves the winner per
plan and denormalises it onto `Plan.provider`, so pricing and the storefront do
not consult this module at all — it exists for the one job that genuinely needs
the full picture: deciding where to send a paid order, and where to send it
instead when the first supplier refuses.

Two rules shape everything here.

A route must cover the **whole** order. `Order` records a single
`provider_order_no`, so splitting one order across two wholesalers would leave
half of it with no tracking reference and no way to sync profiles back. A
supplier that cannot supply every line is not a candidate, however cheap it is.

Routes are tried **cheapest first, then in the order returned**. A paid order
that cannot be fulfilled is the worst outcome in the system — worse than paying
a few cents more — so falling back to a dearer supplier is always preferred to
giving up.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.models import Order, Plan

logger = structlog.get_logger(__name__)


class SupplierError(RuntimeError):
    """A wholesaler refused or failed to fulfil an order."""


@dataclass(frozen=True)
class SupplierLine:
    """One package to buy from a wholesaler, in that wholesaler's own code."""

    package_code: str
    quantity: int


class Supplier(Protocol):
    """What fulfilment needs from a wholesaler.

    Deliberately narrow. Catalogue sync and profile polling stay in each
    integration module, because those differ too much between suppliers to
    hide behind one shape without lying about it.
    """

    key: str

    def is_configured(self) -> bool:
        """True when credentials are present, so an unusable route is skipped."""

    def place_order(self, *, transaction_id: str, lines: list[SupplierLine]) -> str:
        """Buy the packages and return the supplier's own order reference.

        `transaction_id` must be derived from our order id so a retry is
        deduplicated by the supplier rather than allocating a second set of
        profiles we would pay for twice.
        """


class EsimAccessSupplier:
    """eSIM Access, via the client in `app.integrations.esim_access`."""

    key = "esimaccess"

    def is_configured(self) -> bool:
        from app.integrations.esim_access import EsimAccessClient

        # A property on the client, not a method — calling the bool would raise
        # and take down route selection for every order.
        return EsimAccessClient().is_configured

    def place_order(self, *, transaction_id: str, lines: list[SupplierLine]) -> str:
        from app.integrations.esim_access import (
            EsimAccessClient,
            EsimAccessError,
            EsimAccessPackage,
        )

        packages = [
            EsimAccessPackage(slug=line.package_code, count=line.quantity) for line in lines
        ]
        try:
            response = EsimAccessClient().order_profiles(
                transaction_id=transaction_id, packages=packages
            )
        except EsimAccessError as exc:
            # Re-raised as SupplierError so the caller can try the next route
            # without knowing which supplier it just spoke to.
            raise SupplierError(str(exc)) from exc

        order_no = str((response.get("obj") or {}).get("orderNo") or "")
        if not order_no:
            raise SupplierError("supplier accepted the order but returned no order number")
        return order_no


# Registered suppliers, keyed as they appear in `Plan.provider`. eSIMCard is
# absent until its API is connected: a plan sourced from an unregistered
# supplier is reported loudly rather than skipped, because a paid order that
# quietly goes unfulfilled is the failure nobody notices until a customer asks.
_SUPPLIERS: dict[str, Supplier] = {
    EsimAccessSupplier.key: EsimAccessSupplier(),
}


def get_supplier(key: str) -> Supplier | None:
    return _SUPPLIERS.get(key)


def register_supplier(supplier: Supplier) -> None:
    """Add a wholesaler. Used by tests and by future integrations."""
    _SUPPLIERS[supplier.key] = supplier


@dataclass(frozen=True)
class Route:
    """One way to fulfil an entire order: a supplier and every line's code there."""

    provider: str
    lines: tuple[SupplierLine, ...]
    total_cost_usd: Decimal


def _offer_for(plan: Plan, provider: str) -> tuple[str, Decimal] | None:
    """This provider's package code and cost for a plan, if it can supply it.

    Falls back to the denormalised `provider_package_code` so a catalogue that
    predates supplier offers still routes: those plans have no offer rows, but
    they do name a supplier and a code.
    """
    for offer in plan.offers:
        if offer.provider == provider and offer.is_available:
            return offer.package_code, offer.cost_usd

    if plan.provider == provider and plan.provider_package_code:
        return plan.provider_package_code, plan.cost_usd or Decimal("0")

    return None


def routes_for(order: Order) -> list[Route]:
    """Every supplier that can fulfil the whole order, cheapest total first.

    Returns an empty list when no single supplier covers every line — which is
    a real possibility with a mixed cart, and one the caller must report rather
    than paper over.
    """
    items = [item for item in order.items if item.plan is not None]
    if not items:
        return []

    # Start from the providers that could supply the first line, then keep only
    # those that can supply every subsequent one.
    candidates: set[str] | None = None
    for item in items:
        providers = {offer.provider for offer in item.plan.offers if offer.is_available}
        if item.plan.provider_package_code:
            providers.add(item.plan.provider)
        candidates = providers if candidates is None else candidates & providers

    routes: list[Route] = []
    for provider in sorted(candidates or set()):
        lines: list[SupplierLine] = []
        total = Decimal("0")
        for item in items:
            offer = _offer_for(item.plan, provider)
            if offer is None:  # pragma: no cover - excluded by the intersection
                break
            code, cost = offer
            lines.append(SupplierLine(package_code=code, quantity=item.quantity))
            total += cost * item.quantity
        else:
            routes.append(
                Route(provider=provider, lines=tuple(lines), total_cost_usd=total)
            )

    # Cheapest first, provider name only to keep the order stable between runs.
    routes.sort(key=lambda route: (route.total_cost_usd, route.provider))
    return routes


def usable_routes_for(order: Order) -> list[Route]:
    """Routes whose supplier is registered and configured, cheapest first.

    Unusable routes are logged rather than dropped silently: an operator who
    added eSIMCard prices needs to know the reason orders are still going to
    eSIM Access.
    """
    usable: list[Route] = []
    for route in routes_for(order):
        supplier = get_supplier(route.provider)
        if supplier is None:
            logger.warning(
                "supplier.route_unregistered", order_id=order.id, provider=route.provider
            )
            continue
        if not supplier.is_configured():
            logger.warning(
                "supplier.route_unconfigured", order_id=order.id, provider=route.provider
            )
            continue
        usable.append(route)
    return usable
