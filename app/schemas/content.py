from __future__ import annotations

from pydantic import Field

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
    name: str = Field(min_length=2, max_length=80)
    contact: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=10, max_length=2000)


class SupportMessageOut(APIModel):
    delivered: bool = True
