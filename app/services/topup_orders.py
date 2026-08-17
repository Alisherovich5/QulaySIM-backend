"""Placing an order for extra data on an eSIM the customer already owns.

Kept beside `orders.place_order` rather than inside it. The two share the parts
that must not diverge — the som freeze and the payment link — and differ in the
parts that matter: a top-up has exactly one line, that line points at an existing
eSIM, and its plan row is created from a price the wholesaler quoted seconds ago
rather than looked up in the catalogue.

The plan row exists because every order line needs one: it is what carries the
title on the invoice, the cost for margin reporting, and the package code
fulfilment orders against. It is created inactive and scoped `topup`, so it never
appears anywhere a customer browses.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, NotFoundError
from app.core.logging import get_logger
from app.db.models import ESIM, Customer, Order, OrderItem, Plan
from app.db.models.enums import OrderStatus
from app.services import topups as topup_service
from app.services.orders import MIN_CHARGEABLE_UZS, _freeze_som_amount, _payment_url_for_lines

logger = get_logger("topup_orders")


async def owned_esim(session: AsyncSession, customer: Customer, esim_id: int) -> ESIM:
    """The customer's own eSIM, or a 404.

    Ownership is checked here rather than trusted from the request: the id comes
    from a browser, and an eSIM is somebody's paid-for property with a QR code
    inside it.
    """
    esim = (
        await session.execute(
            select(ESIM).where(ESIM.id == esim_id, ESIM.customer_id == customer.id)
        )
    ).scalar_one_or_none()
    if esim is None:
        raise NotFoundError("eSIM not found")
    return esim


async def _plan_for(session: AsyncSession, esim: ESIM, option: topup_service.TopUp) -> Plan:
    """The plan row this top-up sells through, created on first use.

    Keyed by the supplier's package code, so the same top-up bought twice reuses
    one row and the cost stays current. The country comes from the eSIM's own
    plan — a top-up covers exactly what the original does, and reporting groups
    by destination.
    """
    existing = (
        await session.execute(
            select(Plan).where(
                Plan.scope == "topup", Plan.provider_package_code == option.package_code
            )
        )
    ).scalar_one_or_none()

    original = await session.get(Plan, esim.plan_id)
    country_id = original.country_id if original else None
    title = f"{option.name or option.data_label} (qo'shimcha)"

    if existing is not None:
        # Refreshed rather than frozen: the wholesaler moves these prices, and a
        # stale cost would quietly misreport the margin on every top-up sold.
        existing.cost_usd = option.cost_usd
        existing.price_usd = option.price_usd
        existing.title = title
        return existing

    plan = Plan(
        scope="topup",
        country_id=country_id,
        title=title,
        data_amount_mb=option.data_mb,
        is_unlimited=False,
        validity_days=option.validity_days,
        cost_usd=option.cost_usd,
        price_usd=option.price_usd,
        provider=esim.provider,
        provider_package_code=option.package_code,
        network_type="4G",
        supports_hotspot=True,
        # Never browsable: a top-up only makes sense next to the eSIM it fills.
        is_active=False,
        sort_order=0,
    )
    session.add(plan)
    await session.flush()
    return plan


async def place(
    session: AsyncSession,
    customer: Customer,
    esim_id: int,
    package_code: str,
) -> dict[str, object]:
    """Create a pending top-up order and return the link that pays for it."""
    esim = await owned_esim(session, customer, esim_id)

    if esim.status in ("expired", "cancelled"):
        # Topping up a dead profile would take money for data the wholesaler will
        # not attach to anything.
        raise DomainError("This eSIM can no longer be topped up", code="esim_not_toppable")

    option = await topup_service.find(session, esim, package_code)
    if option is None:
        # Either the code is stale or the wholesaler withdrew it. Both mean the
        # list the customer is looking at is out of date.
        raise DomainError("This top-up is no longer available", code="topup_unavailable")

    plan = await _plan_for(session, esim, option)
    amount_uzs, rate = await _freeze_som_amount(option.price_usd)
    if amount_uzs < MIN_CHARGEABLE_UZS:
        raise DomainError("Order total is below the minimum payable amount")

    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PENDING,
        subtotal=option.price_usd,
        discount=Decimal("0"),
        total=option.price_usd,
        amount_uzs=amount_uzs,
        exchange_rate=rate,
        provider="",  # set by the payment provider path below
    )
    session.add(order)
    await session.flush()

    session.add(
        OrderItem(
            order_id=order.id,
            plan_id=plan.id,
            esim_id=esim.id,
            unit_price=option.price_usd,
            unit_cost=option.cost_usd,
            quantity=1,
        )
    )

    from app.core.config import settings

    order.provider = settings.payment_provider
    await session.commit()
    await session.refresh(order)

    logger.info(
        "topup.placed",
        order_id=order.id,
        esim_id=esim.id,
        package_code=package_code,
        data_mb=option.data_mb,
        price_usd=str(option.price_usd),
    )

    return {
        "order_id": order.id,
        "total_usd": option.price_usd,
        "amount_uzs": amount_uzs,
        "exchange_rate": rate,
        "payment_url": await _payment_url_for_lines(
            order.id,
            amount_uzs,
            [(plan.title, option.price_usd, 1)],
        ),
    }
