from __future__ import annotations

from fastapi import APIRouter, Header, Request, status

from app.api.deps import CurrentCustomer, OptionalCustomer, SessionDep
from app.core.config import settings
from app.core.ratelimit import client_ip, enforce
from app.schemas.commerce import (
    OrderPlacedOut,
    QuoteIn,
    QuoteLineOut,
    QuoteOut,
)
from app.services import checkout as service
from app.services import orders as order_service

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


@router.post("/quote", response_model=QuoteOut)
async def quote(
    payload: QuoteIn, request: Request, session: SessionDep, customer: OptionalCustomer
) -> QuoteOut:
    # A quote is the only endpoint that will tell you whether a promo code
    # exists, which makes it the endpoint someone points a script at to find
    # one. Limited per address and per signed-in customer, so sharing an office
    # NAT does not lock a real buyer out of their own account.
    if payload.promo_code:
        await enforce("promo_ip", client_ip(request), settings.rate_limit_promo)
        if customer:
            await enforce("promo_user", str(customer.id), settings.rate_limit_promo)
    result = await service.price_cart(
        session,
        payload.items,
        payload.promo_code,
        # A cashback code is bound to whoever earned it. Without this the quote
        # would apply someone else's reward to an anonymous cart.
        customer_id=customer.id if customer else None,
    )
    return QuoteOut(
        subtotal=result.subtotal,
        discount=result.discount,
        total=result.total,
        promo_applied=result.promo_applied,
        promo_message=result.promo_message,
        promo_reason=result.promo_reason,
        promo_min_order_usd=result.promo_min_order_usd,
        lines=[
            QuoteLineOut(
                plan_id=line.plan_id,
                title=line.title,
                unit_price=line.unit_price,
                quantity=line.quantity,
                line_total=line.line_total,
            )
            for line in result.lines
        ],
    )


@router.post("", response_model=OrderPlacedOut, status_code=status.HTTP_201_CREATED)
async def place_order(
    payload: QuoteIn,
    session: SessionDep,
    customer: CurrentCustomer,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrderPlacedOut:
    """Create a pending order and return the link that pays for it.

    The order carries no eSIM until the provider confirms the charge, so
    abandoning this step costs nothing.

    `Idempotency-Key` is honoured when the client sends one: a double tap on a
    slow connection — and this shop is served over a route that loses packets in
    bursts — otherwise opens two orders and two payment links for one cart, and
    the customer can pay both.
    """
    placed = await order_service.place_order(
        session,
        customer,
        payload.items,
        payload.promo_code,
        idempotency_key=idempotency_key,
    )
    return OrderPlacedOut.model_validate(placed)


@router.post("/{order_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_order(order_id: int, session: SessionDep, customer: CurrentCustomer) -> None:
    await order_service.cancel_unpaid_order(session, customer, order_id)
