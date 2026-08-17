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
    from sqlalchemy.orm import Session

    from app.db.models import Order, Plan

logger = structlog.get_logger(__name__)


class SupplierError(RuntimeError):
    """A wholesaler refused or failed to fulfil an order.

    Means nothing was bought, so the caller is free to try the next supplier.
    """


class SupplierCommittedError(RuntimeError):
    """Part of the order was bought and the rest was not.

    Deliberately NOT a `SupplierError`: falling back to another wholesaler here
    would buy the whole order a second time while the first purchase stands. The
    only safe move is to stop and retry the same supplier, which the purchase
    ledger makes cheap — the units already bought are skipped.
    """


@dataclass(frozen=True)
class SupplierLine:
    """One package to buy from a wholesaler, in that wholesaler's own code.

    `item_id` is carried so a supplier without idempotency can name the
    individual units it is about to buy. The order item's id and not the plan's:
    it is unique by construction, so two lines of the same plan in one order
    cannot share a name and silently claim each other's units.
    """

    package_code: str
    quantity: int
    item_id: int = 0


class Supplier(Protocol):
    """What fulfilment needs from a wholesaler.

    Deliberately narrow. Catalogue sync and profile polling stay in each
    integration module, because those differ too much between suppliers to
    hide behind one shape without lying about it.
    """

    key: str

    def is_configured(self) -> bool:
        """True when credentials are present, so an unusable route is skipped."""

    def place_order(
        self,
        *,
        db: Session,
        order_id: int,
        transaction_id: str,
        lines: list[SupplierLine],
    ) -> str:
        """Buy the packages and return a reference to look them up by.

        `transaction_id` is derived from our order id so a supplier that
        deduplicates on it — eSIM Access does — refuses a retry instead of
        allocating a second set of profiles we would pay for twice.

        A supplier with no such key gets `db` and `order_id` so it can protect
        itself through the purchase ledger. Suppliers that do not need them
        ignore them; passing them unconditionally keeps the fallback loop from
        having to know which kind of supplier it is talking to.

        Raises `SupplierError` when nothing was bought, and `SupplierCommittedError`
        when some of the order was.
        """


class EsimAccessSupplier:
    """eSIM Access, via the client in `app.integrations.esim_access`."""

    key = "esimaccess"

    def is_configured(self) -> bool:
        from app.integrations.esim_access import EsimAccessClient

        # A property on the client, not a method — calling the bool would raise
        # and take down route selection for every order.
        return EsimAccessClient().is_configured

    def place_order(
        self,
        *,
        db: Session,
        order_id: int,
        transaction_id: str,
        lines: list[SupplierLine],
    ) -> str:
        # db/order_id unused: eSIM Access deduplicates on transaction_id itself,
        # so it needs no ledger of ours.
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


class EsimCardSupplier:
    """eSIMCard, bought one eSIM at a time behind the purchase ledger.

    Three facts about their API decide the shape of this class.

    Their purchase endpoint has **no idempotency key** — a `package_type_id` and
    nothing else — so a repeated call buys a second eSIM at our expense. The
    ledger claims each unit before the money moves, which turns a Celery retry
    from an expensive accident into a no-op.

    It buys **one eSIM per call**, so a line of quantity 3 is three purchases and
    three chances to fail halfway. A half-bought order must never fall through to
    another wholesaler — that would pay for the whole thing twice — hence
    `SupplierCommittedError`.

    A purchase **does not return the activation code**. It returns the eSIM's id;
    the QR payload arrives later as `universal_link` in `/my-esims`. So this
    method finishes with money spent and nothing to hand the customer yet, and
    delivery is completed by `app.integrations.esimcard_sync`.

    The returned reference is our own `transaction_id`: with one supplier id per
    unit there is no single order number to record, and the ledger already holds
    each one.
    """

    key = "esimcard"

    def is_configured(self) -> bool:
        from app.integrations.esimcard import EsimCardClient

        return EsimCardClient().is_configured

    def place_order(
        self,
        *,
        db: Session,
        order_id: int,
        transaction_id: str,
        lines: list[SupplierLine],
    ) -> str:
        from app.integrations.esimcard import (
            EsimCardClient,
            EsimCardError,
            EsimCardPurchaseUncertainError,
        )
        from app.services import supplier_ledger as ledger

        client = EsimCardClient()
        bought = 0
        pending: list[str] = []

        for line in lines:
            for line_key in ledger.line_keys(line.quantity, line.item_id):
                held = ledger.claim(
                    db,
                    order_id=order_id,
                    provider=self.key,
                    line_key=line_key,
                    package_code=line.package_code,
                )
                if not held.ours:
                    if held.existing_state == ledger.DONE:
                        # An earlier attempt already bought this unit.
                        bought += 1
                        continue
                    # Still claimed: an attempt reached the supplier and we do
                    # not know the outcome. Counted as committed rather than
                    # retried, because guessing wrong costs a real purchase.
                    pending.append(line_key)
                    continue

                try:
                    purchased = client.purchase(package_type_id=line.package_code)
                except EsimCardPurchaseUncertainError as exc:
                    # The claim stays "claimed" on purpose: it is the record
                    # that money may have moved, and reconciliation resolves it.
                    ledger.settle(db, held.row_id, state=ledger.CLAIMED, note=str(exc))
                    db.commit()
                    raise SupplierCommittedError(
                        f"eSIMCard did not answer for {line_key}; "
                        "outcome unknown, needs reconciliation"
                    ) from exc
                except EsimCardError as exc:
                    ledger.settle(db, held.row_id, state=ledger.FAILED, note=str(exc))
                    db.commit()
                    if bought:
                        raise SupplierCommittedError(
                            f"eSIMCard bought {bought} of the order before refusing: {exc}"
                        ) from exc
                    # Nothing bought yet, so another wholesaler may still serve
                    # the whole order.
                    raise SupplierError(str(exc)) from exc

                ledger.settle(
                    db,
                    held.row_id,
                    state=ledger.DONE,
                    supplier_ref=purchased.supplier_id,
                    iccid=purchased.iccid,
                    note="" if purchased.applied else purchased.message,
                )
                db.commit()
                bought += 1

        if pending:
            raise SupplierCommittedError(
                f"{len(pending)} unit(s) of order {order_id} are unresolved at eSIMCard"
            )
        if not bought:
            raise SupplierError("no purchasable units in the order")
        return transaction_id


# Registered suppliers, keyed as they appear in `Plan.provider`. A plan sourced
# from an unregistered supplier is reported loudly rather than skipped, because a
# paid order that quietly goes unfulfilled is the failure nobody notices until a
# customer asks. Being registered is not enough to be used: `is_configured()`
# must also find credentials, and Django's FULFILLABLE_PROVIDERS gates whether
# the admin will route to it at all.
_SUPPLIERS: dict[str, Supplier] = {
    EsimAccessSupplier.key: EsimAccessSupplier(),
    EsimCardSupplier.key: EsimCardSupplier(),
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
            lines.append(SupplierLine(package_code=code, quantity=item.quantity, item_id=item.id))
            total += cost * item.quantity
        else:
            routes.append(Route(provider=provider, lines=tuple(lines), total_cost_usd=total))

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
