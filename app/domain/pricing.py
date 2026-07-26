"""Cart pricing rules — pure functions, no database and no I/O.

Keeping this layer free of SQLAlchemy is what lets the money rules be tested
exhaustively without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.db.models.enums import DiscountType
from app.domain.money import ZERO, money

MAX_LINE_QUANTITY = 10
MAX_CART_LINES = 20


@dataclass(frozen=True, slots=True)
class PricedLine:
    plan_id: int
    title: str
    unit_price: Decimal
    quantity: int

    @property
    def line_total(self) -> Decimal:
        return money(self.unit_price * self.quantity)


@dataclass(frozen=True, slots=True)
class PromoRule:
    code: str
    discount_type: str
    discount_value: Decimal
    max_uses: int
    used_count: int
    is_active: bool
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class Quote:
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    promo_applied: bool
    promo_message: str | None
    lines: tuple[PricedLine, ...]


class PricingError(ValueError):
    """Cart cannot be priced (empty, unavailable plan, absurd quantity)."""


def validate_promo(promo: PromoRule | None, *, now: datetime | None = None) -> str | None:
    """Return a rejection reason, or None when the promo is usable."""
    if promo is None:
        return "Promo code is invalid"
    if not promo.is_active:
        return "Promo code is invalid"
    moment = now or datetime.now(UTC)
    if promo.valid_until is not None:
        deadline = promo.valid_until
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline < moment:
            return "Promo code has expired"
    if promo.max_uses and promo.used_count >= promo.max_uses:
        return "Promo code usage limit reached"
    return None


def compute_discount(subtotal: Decimal, promo: PromoRule) -> Decimal:
    if promo.discount_type == DiscountType.PERCENT:
        raw = subtotal * promo.discount_value / Decimal("100")
    else:
        raw = promo.discount_value
    # A discount can never exceed the cart or turn the total negative.
    return money(max(ZERO, min(money(raw), subtotal)))


def build_quote(
    lines: list[PricedLine],
    promo: PromoRule | None,
    *,
    promo_requested: bool,
    now: datetime | None = None,
) -> Quote:
    if not lines:
        raise PricingError("Cart is empty")
    if len(lines) > MAX_CART_LINES:
        raise PricingError(f"A cart may hold at most {MAX_CART_LINES} different plans")
    for line in lines:
        if line.quantity < 1 or line.quantity > MAX_LINE_QUANTITY:
            raise PricingError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}")
        if line.unit_price < ZERO:
            raise PricingError(f"Plan {line.plan_id} has an invalid price")

    subtotal = money(sum((line.line_total for line in lines), start=ZERO))

    if not promo_requested:
        return Quote(subtotal, ZERO, subtotal, False, None, tuple(lines))

    rejection = validate_promo(promo, now=now)
    if rejection or promo is None:
        return Quote(subtotal, ZERO, subtotal, False, rejection, tuple(lines))

    discount = compute_discount(subtotal, promo)
    return Quote(
        subtotal=subtotal,
        discount=discount,
        total=money(subtotal - discount),
        promo_applied=True,
        promo_message="Promo applied",
        lines=tuple(lines),
    )
