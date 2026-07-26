from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.base import APIModel, Money
from app.schemas.catalog import PlanOut


class CartItemIn(APIModel):
    plan_id: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1, le=10)


class QuoteIn(APIModel):
    items: list[CartItemIn] = Field(min_length=1, max_length=20)
    promo_code: str | None = Field(default=None, max_length=40)


class QuoteOut(APIModel):
    subtotal: Money
    discount: Money
    total: Money
    promo_applied: bool
    promo_message: str | None = None


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


class TopUpIn(APIModel):
    extra_mb: int = Field(ge=256, le=51200)
