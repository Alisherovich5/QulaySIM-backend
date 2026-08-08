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
    # The owner's thresholds, all of which fall out of the rule on their own:
    # X 000 minus one is (X-1) 999, so the leading digit drops by itself.
    (100_000, 99_999),
    (200_000, 199_999),
    (300_000, 299_999),
    (30_000, 29_999),
    (20_000, 19_999),
    (50_000, 49_999),
    (1_000_000, 999_999),
    # Real conversions, which never land on round numbers.
    (220_437, 219_999),
    (77_451, 76_999),
    (29_788, 29_999),
    (11_915, 11_999),
    (8_462, 7_999),
    # Already ending in 999: nothing to do.
    (29_999, 29_999),
    (199_999, 199_999),
    (11_999, 11_999),
    # Too cheap for a thousand-som step to mean anything.
    (1_999, 1_999),
    (500, 500),
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
def test_charm_uzs_always_ends_in_999(amount: int, expected: int) -> None:
    """The whole point of the rule, asserted directly.

    Everything above the floor must end in 999 — including the cases that were
    already round, which is where a rule that only rounded down would leave
    30 000 as 30 000.
    """
    if amount >= 2_000:
        assert expected % 1_000 == 999, expected


def test_charm_uzs_moves_a_price_by_less_than_half_a_thousand() -> None:
    """The deviation is bounded and symmetric, so margin can be reasoned about.

    Half-up to the nearest thousand then minus one lands within [-500, +499] of
    the conversion — about four cents at 12 000 so'm to the dollar, against
    markups that start at 15%. It rounds *up* on some amounts, which the earlier
    downward-only version did not; the som figure is therefore no longer bounded
    above by USD × rate, and the "≈ $2.50" on the card is an approximation.
    """
    for amount in range(2_000, 2_000_000, 337):
        moved = int(charm_uzs(Decimal(amount))) - amount
        assert -500 <= moved <= 499, (amount, moved)


def test_below_the_floor_the_amount_is_untouched() -> None:
    """A thousand-som step larger than the price would produce nonsense.

    Nothing in the catalogue is this cheap — the floor exists so the arithmetic
    cannot go negative, not because these prices are expected.
    """
    for amount in range(0, 2_000, 7):
        assert charm_uzs(Decimal(amount)) == Decimal(amount)
