"""Quote and checkout orchestration."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.db.models import PromoCode
from app.domain.pricing import PricedLine, PricingError, PromoRule, Quote, build_quote
from app.repositories import catalog as catalog_repo
from app.repositories import orders as order_repo
from app.schemas.commerce import CartItemIn


def _to_rule(promo: PromoCode | None) -> PromoRule | None:
    if promo is None:
        return None
    return PromoRule(
        code=promo.code,
        discount_type=promo.discount_type,
        discount_value=promo.discount_value,
        max_uses=promo.max_uses,
        used_count=promo.used_count,
        is_active=promo.is_active,
        valid_until=promo.valid_until,
        min_order_usd=promo.min_order_usd or Decimal("0"),
    )


async def price_cart(
    session: AsyncSession,
    items: list[CartItemIn],
    promo_code: str | None,
) -> Quote:
    # One query for the whole cart rather than one per line.
    plans = await catalog_repo.get_active_plans(session, [i.plan_id for i in items])

    lines: list[PricedLine] = []
    for item in items:
        plan = plans.get(item.plan_id)
        if plan is None:
            raise DomainError(f"Plan {item.plan_id} is unavailable")
        lines.append(
            PricedLine(
                plan_id=plan.id,
                title=plan.title,
                unit_price=plan.price_usd,
                unit_cost=plan.cost_usd,
                quantity=item.quantity,
            )
        )

    promo = await order_repo.get_promo_by_code(session, promo_code) if promo_code else None

    try:
        return build_quote(lines, _to_rule(promo), promo_requested=bool(promo_code))
    except PricingError as exc:
        raise DomainError(str(exc)) from exc
