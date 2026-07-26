"""Money helpers. Every amount in this service is a Decimal quantised to cents."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: Decimal | int | str) -> Decimal:
    """Quantise to two decimal places using banker-safe half-up rounding."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)
