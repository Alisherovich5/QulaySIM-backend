from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentCustomer, SessionDep
from app.schemas.commerce import QuoteIn, QuoteOut
from app.services import checkout as service

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


@router.post("/quote", response_model=QuoteOut)
async def quote(payload: QuoteIn, session: SessionDep) -> QuoteOut:
    result = await service.price_cart(session, payload.items, payload.promo_code)
    return QuoteOut(
        subtotal=result.subtotal,
        discount=result.discount,
        total=result.total,
        promo_applied=result.promo_applied,
        promo_message=result.promo_message,
    )


@router.post("")
async def checkout(payload: QuoteIn, session: SessionDep, customer: CurrentCustomer) -> None:
    await service.place_order(session, customer.id, payload.items, payload.promo_code)
