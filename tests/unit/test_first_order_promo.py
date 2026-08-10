"""A first-order code must stop applying after the first order.

WELCOME10 is advertised on every page as "10% off your first eSIM" and applied
to every later one as well, so the same customer kept getting the discount
indefinitely. Nothing enforced it: `count_paid_orders` existed in the repository
and had no callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.pricing import (
    PROMO_FIRST_ORDER_ONLY,
    PricedLine,
    PromoRule,
    build_quote,
    validate_promo,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _rule(**overrides) -> PromoRule:
    base = {
        "code": "WELCOME10",
        "discount_type": "percent",
        "discount_value": Decimal("10"),
        "max_uses": 0,
        "used_count": 0,
        "is_active": True,
        "valid_until": None,
        "min_order_usd": Decimal("0"),
        "first_order_only": True,
    }
    return PromoRule(**{**base, **overrides})


def _line(price: str = "10.00") -> PricedLine:
    return PricedLine(
        plan_id=1,
        title="Turkey 1 GB",
        unit_price=Decimal(price),
        unit_cost=Decimal("1.00"),
        quantity=1,
    )


def test_a_first_time_buyer_gets_the_discount() -> None:
    quote = build_quote(
        [_line()], _rule(customer_paid_orders=0), promo_requested=True, now=NOW
    )
    assert quote.promo_applied is True
    assert quote.discount == Decimal("1.00")
    assert quote.total == Decimal("9.00")


def test_a_returning_buyer_does_not() -> None:
    """The bug: this used to discount the second purchase too."""
    quote = build_quote(
        [_line()], _rule(customer_paid_orders=1), promo_requested=True, now=NOW
    )
    assert quote.promo_applied is False
    assert quote.discount == Decimal("0")
    assert quote.total == Decimal("10.00")
    assert quote.promo_message == "This code is for your first order"
    # The slug is what the storefront translates; the prose is the fallback.
    assert quote.promo_reason == PROMO_FIRST_ORDER_ONLY


def test_an_anonymous_visitor_still_sees_the_advertised_discount() -> None:
    """None means "no customer to count", and that is allowed on purpose.

    The quote endpoint accepts an anonymous cart, and "10% off your first eSIM"
    is advertised on every page precisely to first-time buyers — who are the
    people browsing without an account. Refusing the discount until they log in
    would hide it from exactly its audience.

    It costs nothing, because buying requires an account: `price_cart` is the
    only path that builds this rule, and once a customer is attached it always
    counts their paid orders. A returning customer therefore never gets the
    discount on an order, whatever an anonymous quote showed them earlier.
    """
    quote = build_quote(
        [_line()], _rule(customer_paid_orders=None), promo_requested=True, now=NOW
    )
    assert quote.promo_applied is True


@pytest.mark.parametrize("paid", [1, 2, 17])
def test_any_previous_purchase_blocks_it(paid: int) -> None:
    assert (
        validate_promo(_rule(customer_paid_orders=paid), now=NOW, subtotal=Decimal("10"))
        == PROMO_FIRST_ORDER_ONLY
    )


def test_a_code_without_the_flag_is_unaffected() -> None:
    """Cashback and referral codes are earned by returning customers.

    Applying the first-order rule to every code would silently cancel the two
    loyalty schemes, whose whole point is a second purchase.
    """
    rule = _rule(first_order_only=False, customer_paid_orders=5)
    quote = build_quote([_line()], rule, promo_requested=True, now=NOW)
    assert quote.promo_applied is True
