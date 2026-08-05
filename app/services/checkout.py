"""Quote and checkout orchestration."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.db.models import PromoCode
from app.domain.pricing import PricedLine, PricingError, PromoRule, Quote, build_quote
from app.repositories import catalog as catalog_repo
from app.repositories import orders as order_repo
from app.schemas.commerce import CartItemIn

logger = get_logger(__name__)


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


def is_fulfillable(plan) -> bool:
    """Whether any wholesaler we can order from could actually supply this plan.

    Checkout's last line of defence, and the one that has to hold: everything
    upstream of it — the admin's badge, the sourcing engine, the operator's
    attention — is advisory, and a plan can slip through all three while being
    active and priced. Twenty did, at $29.90–$35.88, with no supplier offer and
    no package code: the card would have been charged and no eSIM could ever have
    been issued, which is worse than any error message.

    A supplier counts only if it is in FULFILLABLE_PROVIDERS — we have code that
    can buy from it — and either has an available offer for this plan or is the
    plan's denormalised provider with a package code. Deliberately does not check
    the wholesaler's balance: a wallet can be topped up in a minute, and refusing
    a sale because of it would take the shop offline over a bookkeeping state.
    """
    fulfillable = set(settings.fulfillable_providers)
    for offer in getattr(plan, "offers", None) or []:
        if offer.provider in fulfillable and offer.is_available:
            return True
    return bool(plan.provider in fulfillable and plan.provider_package_code)


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
        if not is_fulfillable(plan):
            # Refused before any money is involved. The message stays vague on
            # purpose: which wholesaler stocks what is not the customer's
            # business, and "temporarily unavailable" is the honest summary.
            logger.error(
                "checkout.unfulfillable_plan",
                plan_id=plan.id,
                title=plan.title,
                provider=plan.provider,
                offers=len(getattr(plan, "offers", None) or []),
            )
            raise DomainError(f"Plan {item.plan_id} is temporarily unavailable")
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
