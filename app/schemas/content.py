from __future__ import annotations

from pydantic import EmailStr, Field

from app.schemas.base import APIModel


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
