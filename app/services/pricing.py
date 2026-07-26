"""Pricing / quote calculation shared by the quote and checkout endpoints."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Plan, PromoCode


class PricingError(Exception):
    pass


def _resolve_promo(db: Session, code: str | None):
    if not code:
        return None, None
    promo = db.query(PromoCode).filter(PromoCode.code == code.strip()).first()
    if not promo or not promo.is_active:
        return None, "Promo code is invalid"
    if promo.valid_until and promo.valid_until < datetime.now(timezone.utc):
        return None, "Promo code has expired"
    if promo.max_uses and promo.used_count >= promo.max_uses:
        return None, "Promo code usage limit reached"
    return promo, None


def calculate(db: Session, items: list, promo_code: str | None):
    """items: list of objects with .plan_id and .quantity.

    Returns dict with subtotal, discount, total, promo, promo_message and the
    resolved line items [(plan, quantity, line_total)].
    """
    if not items:
        raise PricingError("Cart is empty")

    lines = []
    subtotal = Decimal("0")
    for item in items:
        plan = db.get(Plan, item.plan_id)
        if not plan or not plan.is_active:
            raise PricingError(f"Plan {item.plan_id} is unavailable")
        line_total = Decimal(str(plan.price_usd)) * item.quantity
        subtotal += line_total
        lines.append((plan, item.quantity, line_total))

    promo, promo_message = _resolve_promo(db, promo_code)
    discount = Decimal("0")
    if promo:
        if promo.discount_type == "percent":
            discount = (subtotal * Decimal(str(promo.discount_value)) / Decimal("100"))
        else:
            discount = Decimal(str(promo.discount_value))
        discount = min(discount, subtotal)
        promo_message = "Promo applied"

    total = (subtotal - discount).quantize(Decimal("0.01"))
    return {
        "subtotal": subtotal.quantize(Decimal("0.01")),
        "discount": discount.quantize(Decimal("0.01")),
        "total": total,
        "promo": promo,
        "promo_message": promo_message,
        "lines": lines,
    }
