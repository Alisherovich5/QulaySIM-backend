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
    # Carried alongside the price so the order can freeze both sides of the
    # margin; None when the plan has no supplier cost (manually priced).
    unit_cost: Decimal | None = None

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
    # 0 means no floor. Checked against the subtotal, so a fixed discount cannot
    # be spent on a cart smaller than the discount itself.
    min_order_usd: Decimal = ZERO
    first_order_only: bool = False
    #: Paid orders this customer already has. Only consulted when the code is
    #: first-order-only; None means the caller did not establish it, which is
    #: treated as "not eligible" rather than waved through — a discount rule
    #: that fails open is a discount rule that does not exist.
    customer_paid_orders: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    promo_applied: bool
    promo_message: str | None
    lines: tuple[PricedLine, ...]
    #: Stable slug for why a code was refused, so the storefront can translate
    #: it instead of showing the English prose in `promo_message`.
    promo_reason: str | None = None
    #: Set only when the refusal was the minimum-order rule, because that is the
    #: one message a translation cannot write without the figure.
    promo_min_order_usd: Decimal | None = None


class PricingError(ValueError):
    """Cart cannot be priced (empty, unavailable plan, absurd quantity)."""


#: Rejection reasons, as stable slugs rather than prose.
#:
#: The storefront showed whatever text the API returned, so an Uzbek customer
#: read "Promo code has expired" in English. Prose cannot be translated on the
#: client without matching strings, which breaks the moment the wording changes;
#: a slug can. `promo_message_for` still renders English for anything that has no
#: translation of its own.
PROMO_INVALID = "promo_invalid"
PROMO_EXPIRED = "promo_expired"
PROMO_LIMIT_REACHED = "promo_limit_reached"
PROMO_MIN_ORDER = "promo_min_order"
PROMO_FIRST_ORDER_ONLY = "promo_first_order_only"


def promo_message_for(reason: str | None, promo: PromoRule | None = None) -> str | None:
    """English text for a rejection slug, for callers that want prose."""
    if reason is None:
        return None
    if reason == PROMO_MIN_ORDER and promo is not None:
        return f"This code applies to orders of ${promo.min_order_usd:g} or more"
    return {
        PROMO_INVALID: "Promo code is invalid",
        PROMO_EXPIRED: "Promo code has expired",
        PROMO_LIMIT_REACHED: "Promo code usage limit reached",
        PROMO_MIN_ORDER: "This code applies to a larger order",
        PROMO_FIRST_ORDER_ONLY: "This code is for your first order",
    }.get(reason, reason)


def validate_promo(
    promo: PromoRule | None,
    *,
    now: datetime | None = None,
    subtotal: Decimal | None = None,
) -> str | None:
    """Return a rejection reason, or None when the promo is usable.

    `subtotal` is optional so callers that only check the code itself still work,
    but passing it is what enforces the minimum order — without it a $20 code
    applies to a $1.99 cart and the plan is free.
    """
    if promo is None:
        return PROMO_INVALID
    if not promo.is_active:
        return PROMO_INVALID
    moment = now or datetime.now(UTC)
    if promo.valid_until is not None:
        deadline = promo.valid_until
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline < moment:
            return PROMO_EXPIRED
    if promo.max_uses and promo.used_count >= promo.max_uses:
        return PROMO_LIMIT_REACHED
    if subtotal is not None and promo.min_order_usd > ZERO and subtotal < promo.min_order_usd:
        return PROMO_MIN_ORDER
    if promo.first_order_only and (promo.customer_paid_orders or 0) > 0:
        return PROMO_FIRST_ORDER_ONLY
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
        return Quote(subtotal, ZERO, subtotal, False, None, tuple(lines))  # no code asked for

    # The subtotal is known by here, so the minimum-order rule can be applied.
    rejection = validate_promo(promo, now=now, subtotal=subtotal)
    if rejection or promo is None:
        return Quote(
            subtotal=subtotal,
            discount=ZERO,
            total=subtotal,
            promo_applied=False,
            # Prose for anything that cannot translate the slug, slug for the
            # storefront, which can.
            promo_message=promo_message_for(rejection, promo),
            lines=tuple(lines),
            promo_reason=rejection,
            promo_min_order_usd=(
                promo.min_order_usd if rejection == PROMO_MIN_ORDER and promo else None
            ),
        )

    discount = compute_discount(subtotal, promo)
    return Quote(
        subtotal=subtotal,
        discount=discount,
        total=money(subtotal - discount),
        promo_applied=True,
        promo_message="Promo applied",
        lines=tuple(lines),
    )
