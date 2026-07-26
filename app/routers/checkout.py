from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_customer
from app.models import Customer
from app.schemas import CheckoutIn, OrderOut, QuoteIn, QuoteOut
from app.services.pricing import PricingError, calculate

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


@router.post("/quote", response_model=QuoteOut)
def quote(payload: QuoteIn, db: Session = Depends(get_db)):
    try:
        result = calculate(db, payload.items, payload.promo_code)
    except PricingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return QuoteOut(
        subtotal=float(result["subtotal"]),
        discount=float(result["discount"]),
        total=float(result["total"]),
        promo_applied=result["promo"] is not None,
        promo_message=result["promo_message"],
    )


@router.post("", response_model=OrderOut)
def checkout(
    payload: CheckoutIn,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Payment confirmation will create and provision real orders here.

    The former test checkout was removed. Supplier orders must only be created
    after a real payment provider confirms the customer's charge.
    """
    if settings.payment_provider == "disabled":
        raise HTTPException(
            status_code=503,
            detail="Online payments are being configured. Please try again soon.",
        )
    raise HTTPException(status_code=501, detail="Configured payment provider is not implemented")
