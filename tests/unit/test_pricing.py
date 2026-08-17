"""Money rules — no database, no Redis, no event loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.pricing import (
    MAX_LINE_QUANTITY,
    PROMO_EXPIRED,
    PROMO_INVALID,
    PROMO_LIMIT_REACHED,
    PricedLine,
    PricingError,
    PromoRule,
    build_quote,
    compute_discount,
    validate_promo,
)


def line(price: str, qty: int = 1, plan_id: int = 1) -> PricedLine:
    return PricedLine(plan_id=plan_id, title="Plan", unit_price=Decimal(price), quantity=qty)


def promo(**kw: object) -> PromoRule:
    base = {
        "code": "SAVE10",
        "discount_type": "percent",
        "discount_value": Decimal("10"),
        "max_uses": 0,
        "used_count": 0,
        "is_active": True,
        "valid_until": None,
    }
    base.update(kw)
    return PromoRule(**base)  # type: ignore[arg-type]


class TestSubtotal:
    def test_sums_lines_and_quantises(self) -> None:
        quote = build_quote([line("9.99", 3), line("4.005", 1, 2)], None, promo_requested=False)
        # 29.97 + 4.01 (half-up) = 33.98
        assert quote.subtotal == Decimal("33.98")
        assert quote.total == quote.subtotal
        assert quote.discount == Decimal("0.00")

    def test_empty_cart_rejected(self) -> None:
        with pytest.raises(PricingError, match="empty"):
            build_quote([], None, promo_requested=False)

    def test_quantity_ceiling_enforced(self) -> None:
        with pytest.raises(PricingError, match="Quantity"):
            build_quote([line("5.00", MAX_LINE_QUANTITY + 1)], None, promo_requested=False)

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(PricingError, match="invalid price"):
            build_quote([line("-1.00")], None, promo_requested=False)


class TestDiscount:
    def test_percent(self) -> None:
        assert compute_discount(Decimal("100.00"), promo()) == Decimal("10.00")

    def test_fixed(self) -> None:
        rule = promo(discount_type="fixed", discount_value=Decimal("7.50"))
        assert compute_discount(Decimal("100.00"), rule) == Decimal("7.50")

    def test_fixed_discount_cannot_exceed_cart(self) -> None:
        """A 50 USD voucher on a 10 USD cart must not produce a negative total."""
        rule = promo(discount_type="fixed", discount_value=Decimal("50.00"))
        quote = build_quote([line("10.00")], rule, promo_requested=True)
        assert quote.discount == Decimal("10.00")
        assert quote.total == Decimal("0.00")

    def test_percent_rounds_half_up(self) -> None:
        rule = promo(discount_value=Decimal("33.333"))
        assert compute_discount(Decimal("10.00"), rule) == Decimal("3.33")


class TestPromoValidation:
    def test_missing_code(self) -> None:
        assert validate_promo(None) == PROMO_INVALID

    def test_inactive(self) -> None:
        assert validate_promo(promo(is_active=False)) == PROMO_INVALID

    def test_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(days=1)
        assert validate_promo(promo(valid_until=past)) == PROMO_EXPIRED

    def test_naive_datetime_treated_as_utc(self) -> None:
        """Django can hand back naive datetimes; comparing them must not crash."""
        naive_future = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
        assert validate_promo(promo(valid_until=naive_future)) is None

    def test_usage_cap_reached(self) -> None:
        rule = promo(max_uses=5, used_count=5)
        assert validate_promo(rule) == PROMO_LIMIT_REACHED

    def test_unlimited_uses(self) -> None:
        assert validate_promo(promo(max_uses=0, used_count=999)) is None


class TestQuoteAssembly:
    def test_rejected_promo_still_returns_full_price(self) -> None:
        quote = build_quote([line("20.00")], promo(is_active=False), promo_requested=True)
        assert quote.promo_applied is False
        assert quote.total == Decimal("20.00")
        assert quote.promo_message == "Promo code is invalid"

    def test_applied_promo(self) -> None:
        quote = build_quote([line("50.00", 2)], promo(), promo_requested=True)
        assert quote.promo_applied is True
        assert quote.subtotal == Decimal("100.00")
        assert quote.discount == Decimal("10.00")
        assert quote.total == Decimal("90.00")

    def test_no_promo_requested_skips_validation(self) -> None:
        quote = build_quote([line("15.00")], None, promo_requested=False)
        assert quote.promo_message is None


class TestMinimumOrder:
    """A fixed discount without a floor is a free-plan coupon.

    $20 off a $1.99 tariff is $0.00 — the discount is capped at the cart, so
    nothing errors and nothing warns. The minimum is what makes a fixed amount
    usable at all.
    """

    @staticmethod
    def _rule(**kwargs):
        from decimal import Decimal

        from app.domain.pricing import PromoRule

        defaults = {
            "code": "SAVE20",
            "discount_type": "fixed",
            "discount_value": Decimal("20"),
            "max_uses": 0,
            "used_count": 0,
            "is_active": True,
            "valid_until": None,
            "min_order_usd": Decimal("25"),
        }
        defaults.update(kwargs)
        return PromoRule(**defaults)

    def test_a_cart_below_the_minimum_is_refused(self):
        from decimal import Decimal

        from app.domain.pricing import PROMO_MIN_ORDER, promo_message_for, validate_promo

        rule = self._rule()
        reason = validate_promo(rule, subtotal=Decimal("1.99"))
        assert reason == PROMO_MIN_ORDER
        # The slug alone cannot name the figure, and the customer cannot act on
        # "your order is too small" without it — so the rendered message must.
        assert "25" in (promo_message_for(reason, rule) or "")

    def test_a_cart_at_the_minimum_is_allowed(self):
        from decimal import Decimal

        from app.domain.pricing import validate_promo

        assert validate_promo(self._rule(), subtotal=Decimal("25")) is None

    def test_no_minimum_means_no_floor(self):
        from decimal import Decimal

        from app.domain.pricing import validate_promo

        assert (
            validate_promo(self._rule(min_order_usd=Decimal("0")), subtotal=Decimal("0.50")) is None
        )

    def test_omitting_the_subtotal_skips_the_check(self):
        # Callers that only vet the code itself must keep working.
        from app.domain.pricing import validate_promo

        assert validate_promo(self._rule()) is None

    def test_the_quote_refuses_and_charges_full_price(self):
        from decimal import Decimal

        from app.domain.pricing import PricedLine, build_quote

        line = PricedLine(plan_id=1, title="Turkey 1 GB", unit_price=Decimal("1.99"), quantity=1)
        quote = build_quote([line], self._rule(), promo_requested=True)
        assert quote.promo_applied is False
        assert quote.discount == Decimal("0")
        # Without the floor this was 0.00 — the plan given away.
        assert quote.total == Decimal("1.99")

    def test_without_a_minimum_a_big_fixed_discount_still_zeroes_a_cheap_cart(self):
        from decimal import Decimal

        from app.domain.pricing import PricedLine, build_quote

        line = PricedLine(plan_id=1, title="Turkey 1 GB", unit_price=Decimal("1.99"), quantity=1)
        quote = build_quote([line], self._rule(min_order_usd=Decimal("0")), promo_requested=True)
        # Documented, not desired: the clamp keeps the total at zero rather than
        # negative, and the minimum is the only thing that prevents this.
        assert quote.total == Decimal("0")
