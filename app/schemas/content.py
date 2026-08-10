from __future__ import annotations

from pydantic import EmailStr, Field

from app.schemas.base import APIModel, Money


class BenefitOut(APIModel):
    id: int
    icon: str
    title: str
    text: str


class TestimonialOut(APIModel):
    id: int
    name: str
    location: str
    text: str
    rating: int


class DeviceOut(APIModel):
    id: int
    name: str


class FaqOut(APIModel):
    id: int
    question: str
    answer: str
    category: str


class PromoOut(APIModel):
    eyebrow: str
    title: str
    text: str
    code: str
    cta_link: str
    # Short line for the bar above the navigation, where the full sentence does
    # not fit. Empty means the storefront uses its own wording.
    strip_text: str = ""
    # Read from the promo code that actually applies the discount, so what the
    # site advertises and what checkout takes off are the same number.
    discount_type: str | None = None
    discount_value: Money | None = None
    # Whether the linked code only works on a first order. The strip is hidden
    # from customers who have already bought when this is set — advertising a
    # discount that checkout will refuse is worse than not advertising it.
    first_order_only: bool = False


class LandingContentOut(APIModel):
    benefits: list[BenefitOut] = []
    testimonials: list[TestimonialOut] = []
    devices: list[DeviceOut] = []
    faqs: list[FaqOut] = []
    promo: PromoOut | None = None


class CurrencyRateOut(APIModel):
    usd_to_uzs: float
    updated_at: str | None = None
    source: str


class SupportMessageIn(APIModel):
    """Mirrors the storefront's support form exactly (see Support.tsx).

    The phone pattern matches `formatUzPhone` on the client, which inserts the
    spaces as the user types — validating a different shape here would reject
    every message the form can actually produce.
    """

    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    phone: str = Field(pattern=r"^\+998 \d{2} \d{3} \d{2} \d{2}$")
    message: str = Field(min_length=10, max_length=2000)
    locale: str = Field(default="uz", min_length=2, max_length=8)


class SupportMessageOut(APIModel):
    sent: bool = True
