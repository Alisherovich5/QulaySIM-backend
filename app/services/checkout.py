"""Quote and checkout orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.db.models import PromoCode
from app.domain.pricing import PricedLine, PricingError, PromoRule, Quote, build_quote
from app.repositories import catalog as catalog_repo
from app.repositories import orders as order_repo
from app.schemas.commerce import CartItemIn
from app.services.supplier_wallets import can_cover, known_balances

logger = get_logger(__name__)


def _to_rule(promo: PromoCode | None, *, paid_orders: int | None = None) -> PromoRule | None:
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
        first_order_only=promo.first_order_only,
        customer_paid_orders=paid_orders,
    )


def sells_at_a_loss(plan: Any) -> bool:
    """Whether this plan would lose money at its current price.

    Compared against the plan's live cost rather than a snapshot, because the
    question is what the wholesaler charges *now* — a sale about to happen is
    paid for at today's cost, not the one recorded when the price was set.

    A plan with no known cost is not judged: zero is what an unsynced row holds,
    and treating "unknown" as "free" would mark the whole catalogue profitable.
    """
    cost = plan.cost_usd
    if cost is None or cost <= Decimal("0"):
        return False
    return bool(plan.price_usd <= cost)


def is_fulfillable(plan: Any, balances: Mapping[str, float] | None = None) -> bool:
    """Whether any wholesaler we can order from could actually supply this plan.

    Checkout's last line of defence, and the one that has to hold: everything
    upstream of it — the admin's badge, the sourcing engine, the operator's
    attention — is advisory, and a plan can slip through all three while being
    active and priced. Twenty did, at $29.90–$35.88, with no supplier offer and
    no package code: the card would have been charged and no eSIM could ever have
    been issued, which is worse than any error message.

    A supplier counts only if it is in FULFILLABLE_PROVIDERS — we have code that
    can buy from it — and either has an available offer for this plan or is the
    plan's denormalised provider with a package code.

    `balances` is the second half of the same question, added after order #141
    took $5.91 for a plan only eSIMCard stocked while eSIMCard's wallet held
    nothing. Having ordering code for a wholesaler is not the same as that
    wholesaler being able to sell today. A supplier whose recorded balance
    cannot cover this plan's cost stops counting, so a plan with a second source
    still sells and only the ones nobody can deliver go quiet. Omitted — as every
    caller outside `price_cart` omits it — it means "do not ask", and the older
    answer stands: a wallet is a bookkeeping state and a sale should not hang on
    one. What must never happen is the reverse, a balance we failed to read
    counting as zero; `supplier_wallets` leaves unknown balances out of the
    mapping entirely, so there is nothing here that can get that wrong.
    """
    fulfillable = set(settings.fulfillable_providers)
    wallets = balances if balances is not None else {}
    # getattr rather than attribute access: this takes `Any` on purpose — the
    # sourcing engine and the admin both hand it their own shapes — and a plan
    # that cannot say what it costs is not the same as one that costs nothing.
    for offer in getattr(plan, "offers", None) or []:
        if (
            offer.provider in fulfillable
            and offer.is_available
            and can_cover(offer.provider, getattr(offer, "cost_usd", None), wallets)
        ):
            return True
    if not (plan.provider in fulfillable and plan.provider_package_code):
        return False
    return can_cover(plan.provider, getattr(plan, "cost_usd", None), wallets)


async def price_cart(
    session: AsyncSession,
    items: list[CartItemIn],
    promo_code: str | None,
    *,
    customer_id: int | None = None,
) -> Quote:
    """Price a cart server-side.

    `customer_id` is what makes a personal reward personal: cashback codes are
    bound to whoever earned them, and without it the quote endpoint would happily
    apply somebody else's code. Optional because an anonymous visitor can still
    price a cart — they just cannot redeem a bound code.
    """
    # One query for the whole cart rather than one per line.
    plans = await catalog_repo.get_active_plans(session, [i.plan_id for i in items])

    # One Redis read for the whole cart, and never a call to a wholesaler: see
    # app/services/supplier_wallets.py for why the balance is read from a cache
    # here rather than asked for at the moment somebody is waiting to pay.
    balances = await known_balances()

    lines: list[PricedLine] = []
    for item in items:
        plan = plans.get(item.plan_id)
        if plan is None:
            raise DomainError(f"Plan {item.plan_id} is unavailable")
        if not is_fulfillable(plan, balances):
            # Refused before any money is involved. The message stays vague on
            # purpose: which wholesaler stocks what is not the customer's
            # business, and "temporarily unavailable" is the honest summary.
            logger.error(
                "checkout.unfulfillable_plan",
                plan_id=plan.id,
                title=plan.title,
                provider=plan.provider,
                offers=len(getattr(plan, "offers", None) or []),
                balances=balances,
            )
            raise DomainError(f"Plan {item.plan_id} is temporarily unavailable")
        if sells_at_a_loss(plan):
            # The wholesaler raised its price above ours. Nothing recomputes a
            # plan whose price the operator locked, so without this the shop
            # keeps selling it and pays the difference on every order — quietly,
            # because a loss looks exactly like a sale until someone reconciles.
            #
            # Refusing is the conservative half of the fix: the plan stops
            # selling rather than silently repricing under a customer who is
            # already at checkout. The report names it so it gets repriced.
            logger.error(
                "checkout.plan_below_cost",
                plan_id=plan.id,
                title=plan.title,
                price_usd=str(plan.price_usd),
                cost_usd=str(plan.cost_usd),
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

    promo = (
        await order_repo.get_promo_by_code(session, promo_code, customer_id=customer_id)
        if promo_code
        else None
    )

    # Counted only when it can change the answer. An anonymous quote has no
    # customer to count for, and a first-order-only code cannot be honoured
    # without knowing — so it is refused rather than granted, which is the safe
    # direction for a discount.
    paid_orders = None
    if promo is not None and promo.first_order_only and customer_id is not None:
        paid_orders = await order_repo.count_paid_orders(session, customer_id)

    try:
        return build_quote(
            lines,
            _to_rule(promo, paid_orders=paid_orders),
            promo_requested=bool(promo_code),
        )
    except PricingError as exc:
        raise DomainError(str(exc)) from exc
