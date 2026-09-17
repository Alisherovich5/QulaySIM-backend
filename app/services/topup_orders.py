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

from datetime import timedelta
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

#: How long an unpaid top-up stays the same order rather than becoming a second
#: one. A customer who taps twice, or a client that retries a request it never
#: saw the answer to, means one intent — not two. Fifteen minutes is longer than
#: any payment page stays open and shorter than a price is worth freezing.
REUSE_WINDOW = timedelta(minutes=15)


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
    """Create a pending top-up order and return the link that pays for it.

    Placing the same top-up twice returns the first order rather than a second
    one. The cart endpoint has taken an `Idempotency-Key` since it was written;
    this one never did, and the database shows the cost: eSIM 69 collected three
    orders in 104 seconds (two of them abandoned), and of 93 orders on the live
    database 53 sit unpaid. A second pending order for the same profile and the
    same package is not a second purchase, it is the same tap arriving twice.
    """
    esim = await owned_esim(session, customer, esim_id)

    existing = await _reusable_order(session, customer, esim_id, package_code)
    if existing is not None:
        logger.info(
            "topup.reused",
            order_id=existing["order_id"],
            esim_id=esim_id,
            package_code=package_code,
        )
        return existing

    # Our own `status` column says "expired" both when the allowance is spent and
    # when the validity has elapsed. Only the second one makes a top-up
    # impossible; the first one is the reason top-ups exist. Reading the
    # wholesaler's own state tells the two apart.
    if not topup_service.is_toppable(esim):
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


async def _reusable_order(
    session: AsyncSession,
    customer: Customer,
    esim_id: int,
    package_code: str,
) -> dict[str, object] | None:
    """The customer's own unpaid order for this exact top-up, if it is still fresh.

    Matched on the package code carried by the line's plan rather than on the plan
    id: `_plan_for` creates a row per (esim, package), so the id is stable, but the
    code is what the customer actually asked for and what fulfilment orders
    against.
    """
    from app.db.base import utcnow

    row = (
        await session.execute(
            select(Order, Plan)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Plan, Plan.id == OrderItem.plan_id)
            .where(
                Order.customer_id == customer.id,
                Order.status == OrderStatus.PENDING,
                OrderItem.esim_id == esim_id,
                Plan.provider_package_code == package_code,
                Order.created_at >= utcnow() - REUSE_WINDOW,
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None

    order, plan = row
    if order.amount_uzs is None or order.exchange_rate is None:
        # An order that never got a som amount cannot be paid for, so it is not
        # something to hand back — let the caller place a fresh one.
        return None

    return {
        "order_id": order.id,
        "total_usd": order.total,
        "amount_uzs": order.amount_uzs,
        "exchange_rate": order.exchange_rate,
        "payment_url": await _payment_url_for_lines(
            order.id,
            order.amount_uzs,
            [(plan.title, order.total, 1)],
        ),
    }
