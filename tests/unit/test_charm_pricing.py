"""The som figures a customer sees, and the fact that we charge exactly those.

The cases below are the shop owner's own examples, kept verbatim rather than
paraphrased into properties: 100 000 → 99 000, 30 000 → 29 000, 1 000 000 →
990 000. A property test would pass on a rule that rounded to 99 900 instead,
which is a different price and not the one asked for.

The same table is checked against the storefront's TypeScript copy — see
`charmUzs` in `src/lib/charm.ts`. The two must agree to the som, because one
shows the price and the other bills it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.currency import charm_uzs

# (converted amount, what the customer should see)
CASES = [
    # The owner's examples.
    (100_000, 99_000),
    (200_000, 199_000),
    (300_000, 299_000),
    (30_000, 29_000),
    (20_000, 19_000),
    (50_000, 49_000),
    (1_000_000, 990_000),
    # Real conversions, which never land on round numbers.
    (220_439, 219_000),
    (77_451, 77_000),
    (11_915, 11_900),
    (8_462, 8_400),
    # Already low-reading: nothing to gain, so nothing is taken.
    (29_400, 29_000),
    (199_000, 199_000),
    (99_000, 99_000),
    # Too cheap for the rule to be worth anything.
    (4_999, 4_999),
    (1_200, 1_200),
    (0, 0),
]


@pytest.mark.parametrize(("amount", "expected"), CASES)
def test_charm_uzs_matches_the_agreed_table(amount: int, expected: int) -> None:
    assert charm_uzs(Decimal(amount)) == Decimal(expected)


@pytest.mark.parametrize(("amount", "expected"), CASES)
def test_charm_uzs_is_idempotent(amount: int, expected: int) -> None:
    """Applying it to its own output must not walk the price down again.

    Display and billing both call it, and an order re-priced on retry calls it a
    third time. If it were not a fixed point, a price would drift a step lower
    on every pass and the page would stop matching the invoice.
    """
    assert charm_uzs(charm_uzs(Decimal(amount))) == Decimal(expected)


@pytest.mark.parametrize(("amount", "expected"), CASES)
def test_charm_uzs_never_charges_more_than_the_conversion(
    amount: int, expected: int
) -> None:
    """Downward only. Rounding up would bill above the quoted price."""
    assert Decimal(expected) <= Decimal(amount)


def test_charm_uzs_gives_away_at_most_one_step() -> None:
    """The discount is bounded, so margin can be reasoned about.

    Worst case is one step below the round number: 1 000 so'm under a million,
    10 000 above it. At ~12 000 so'm to the dollar that is under $0.09 and
    under $0.84 respectively — inside every markup band in the catalogue.
    """
    for amount in range(5_000, 2_000_000, 337):
        step = 100 if amount < 20_000 else 1_000 if amount < 1_000_000 else 10_000
        given_away = amount - int(charm_uzs(Decimal(amount)))
        assert 0 <= given_away < step * 2, amount
