"""Order placement.

An order is created *before* payment and left pending; only Payme performing
the transaction marks it paid. Nothing is provisioned until then, so an
abandoned checkout costs us nothing.

The som amount is frozen here. The catalogue is priced in USD and the rate
moves, so recomputing at payment time would charge a different figure from
the one the customer agreed to — and Payme validates the amount against what
we stored, so a drifting rate would simply fail every payment.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, DomainError, ServiceUnavailableError
from app.core.logging import get_logger
from app.db.models import Customer, Order, OrderItem, PromoCode
from app.db.models.enums import OrderStatus
from app.domain.pricing import Quote
from app.integrations.payme import checkout_url
from app.schemas.commerce import CartItemIn
from app.services.checkout import price_cart
from app.services.currency import usd_to_uzs

logger = get_logger(__name__)

# Payme rejects an amount of zero, and a free order has nothing to charge for.
MIN_CHARGEABLE_UZS = Decimal("1000")


async def _freeze_som_amount(total_usd: Decimal) -> tuple[Decimal, Decimal]:
    """Return (amount_uzs, rate).

    Refuses to price an order against the fallback rate: charging real money
    at a hard-coded 12000 would over- or under-bill every customer until the
    rate service came back.
    """
    rate_payload = await usd_to_uzs()
    if rate_payload.get("source") != "cbu":
        logger.error("orders.rate_unavailable", source=rate_payload.get("source"))
        raise ServiceUnavailableError(
            "Exchange rate is temporarily unavailable. Please try again shortly."
        )

    rate = Decimal(str(rate_payload["usd_to_uzs"]))
    amount = (total_usd * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount, rate


async def place_order(
    session: AsyncSession,
    customer: Customer,
    items: list[CartItemIn],
    promo_code: str | None,
) -> dict[str, object]:
    if settings.payment_provider == "disabled":
        raise ServiceUnavailableError(
            "Online payments are being configured. Please try again soon."
        )
    if settings.payment_provider not in ("payme", "atmos"):
        raise DomainError("Configured payment provider is not implemented", code="not_implemented")
    if settings.payment_provider == "payme" and not settings.payme_merchant_id:
        logger.error("orders.payme_unconfigured")
        raise ServiceUnavailableError("Payments are not configured. Please try again soon.")
    if settings.payment_provider == "atmos" and not (
        settings.atmos_consumer_key and settings.atmos_consumer_secret and settings.atmos_store_id
    ):
        logger.error("orders.atmos_unconfigured")
        raise ServiceUnavailableError("Payments are not configured. Please try again soon.")

    # Price server-side: the cart came from the customer's browser and its
    # prices may be stale or tampered with.
    quote = await price_cart(session, items, promo_code)

    amount_uzs, rate = await _freeze_som_amount(quote.total)
    if amount_uzs < MIN_CHARGEABLE_UZS:
        raise DomainError("Order total is below the minimum payable amount")

    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PENDING,
        subtotal=quote.subtotal,
        discount=quote.discount,
        total=quote.total,
        amount_uzs=amount_uzs,
        exchange_rate=rate,
        provider=settings.payment_provider,
    )

    if quote.promo_applied and promo_code:
        promo = (
            (
                await session.execute(
                    select(PromoCode).where(PromoCode.code == promo_code.strip().upper())
                )
            )
            .scalars()
            .first()
        )
        if promo is not None:
            order.promo_code_id = promo.id

    session.add(order)
    await session.flush()

    for line in quote.lines:
        session.add(
            OrderItem(
                order_id=order.id,
                plan_id=line.plan_id,
                unit_price=line.unit_price,
                unit_cost=line.unit_cost,
                quantity=line.quantity,
            )
        )

    await session.commit()
    await session.refresh(order)

    logger.info(
        "orders.placed",
        order_id=order.id,
        customer_id=customer.id,
        total_usd=str(quote.total),
        amount_uzs=str(amount_uzs),
    )

    return {
        "order_id": order.id,
        "total_usd": quote.total,
        "amount_uzs": amount_uzs,
        "exchange_rate": rate,
        "payment_url": await _payment_url(order.id, amount_uzs, quote),
    }


async def _payment_url(order_id: int, amount_uzs: Decimal, quote: Quote) -> str:
    """The link the customer pays at, from whichever provider is live.

    Built after the order is committed so a provider hiccup can never leave a
    paid-for order unrecorded — the customer just retries the checkout.
    """
    amount_tiyin = int((amount_uzs * 100).to_integral_value())
    if settings.payment_provider == "atmos":
        from app.integrations.atmos import create_invoice

        # The invoice's fiscal lines must sum to its amount exactly, but the
        # lines are priced in USD and the amount was frozen in som — so each
        # line takes its proportional share of the tiyin total and the last
        # line absorbs the rounding remainder. The discount, if any, spreads
        # itself across the lines the same way, which is also what the tax
        # receipt should say.
        line_totals = [line.unit_price * line.quantity for line in quote.lines]
        grand = sum(line_totals) or 1
        shares = [int(amount_tiyin * (t / grand)) for t in line_totals]
        shares[-1] += amount_tiyin - sum(shares)
        return await create_invoice(
            account=str(order_id),
            amount_tiyin=amount_tiyin,
            lines=[
                {"name": line.title, "amount_tiyin": share, "quantity": line.quantity}
                for line, share in zip(quote.lines, shares, strict=True)
            ],
        )
    return checkout_url(
        base_url=settings.payme_checkout_url,
        merchant_id=settings.payme_merchant_id,
        account_field=settings.payme_account_field,
        account_value=str(order_id),
        amount_tiyin=amount_tiyin,
        return_url=settings.payme_return_url,
    )


async def cancel_unpaid_order(session: AsyncSession, customer: Customer, order_id: int) -> None:
    """Let a customer abandon a pending order so the account page stays clean.

    Scoped by customer_id in the query so one customer cannot cancel another's.
    """
    order = (
        (
            await session.execute(
                select(Order)
                .where(Order.id == order_id, Order.customer_id == customer.id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    if order is None:
        raise DomainError("Order not found", code="not_found")
    if order.status != OrderStatus.PENDING:
        raise ConflictError("Only a pending order can be cancelled")

    order.status = OrderStatus.CANCELLED
    await session.commit()
    logger.info("orders.cancelled_by_customer", order_id=order_id)
