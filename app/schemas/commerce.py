from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.schemas.base import APIModel, Money
from app.schemas.catalog import PlanOut


class CartItemIn(APIModel):
    plan_id: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1, le=10)


class QuoteIn(APIModel):
    items: list[CartItemIn] = Field(min_length=1, max_length=20)
    promo_code: str | None = Field(default=None, max_length=40)


class QuoteLineOut(APIModel):
    """Server-side price for one cart line.

    The cart lives in the customer's browser, so the price it holds can be
    hours old. The storefront renders these instead of its stored copy, which
    is what stops a line reading $6.57 while the total is calculated at $20.76.
    """

    plan_id: int
    title: str
    unit_price: Money
    quantity: int
    line_total: Money


class QuoteOut(APIModel):
    subtotal: Money
    discount: Money
    total: Money
    promo_applied: bool
    promo_message: str | None = None
    lines: list[QuoteLineOut] = []


class ESIMOut(APIModel):
    id: int
    iccid: str
    qr_payload: str
    qr_image: str
    status: str
    data_total_mb: int
    data_used_mb: int
    validity_days: int
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    # What was actually paid for this eSIM, frozen at the sale. Null when the
    # order line is gone, which is shown as unknown rather than filled in with
    # the plan's current price — a customer who bought before a repricing would
    # otherwise be told the wrong number about their own receipt.
    paid_usd: Decimal | None = None
    paid_uzs: Decimal | None = None
    plan: PlanOut


class OrderOut(APIModel):
    id: int
    status: str
    subtotal: Money
    discount: Money
    total: Money
    created_at: datetime
    paid_at: datetime | None = None
    esims: list[ESIMOut] = []


class OrderPlacedOut(APIModel):
    """What the storefront needs to send the customer to Payme.

    Both currencies are returned because the customer agreed to a USD price
    but will be charged the som amount frozen here.
    """

    order_id: int
    total_usd: Money
    amount_uzs: Money
    exchange_rate: Money
    payment_url: str
