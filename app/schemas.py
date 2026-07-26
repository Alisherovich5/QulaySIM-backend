from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---- Auth ----
class RegisterIn(BaseModel):
    email: EmailStr
    full_name: str = ""
    password: str = Field(min_length=6)
    referral_code: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    full_name: str
    created_at: datetime


# ---- Support ----
class SupportMessageIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    phone: str = Field(pattern=r"^\+998 \d{2} \d{3} \d{2} \d{2}$")
    message: str = Field(min_length=10, max_length=2000)
    locale: str = Field(default="uz", min_length=2, max_length=8)


class SupportMessageOut(BaseModel):
    sent: bool = True


# ---- Referral ----
class ReferralEntry(BaseModel):
    referred_email: str
    status: str
    reward_code: str
    created_at: datetime


class ReferralSummaryOut(BaseModel):
    code: str
    invited: int
    completed: int
    pending: int
    rewards: list[str]
    entries: list[ReferralEntry]


# ---- Catalog ----
class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    scope: str
    title: str
    data_amount_mb: int
    is_unlimited: bool
    data_label: str
    validity_days: int
    price_usd: float
    network_type: str
    supports_hotspot: bool
    is_popular: bool


class CountryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str
    iso2: str
    is_popular: bool
    region: RegionOut | None = None
    starting_price: float | None = None


class CountryDetailOut(CountryOut):
    plans: list[PlanOut] = []


class CurrencyRateOut(BaseModel):
    """Display-only exchange rate for converting the USD catalogue to UZS."""

    usd_to_uzs: float
    updated_at: str | None = None
    source: str


# ---- Checkout ----
class CartItemIn(BaseModel):
    plan_id: int
    quantity: int = Field(default=1, ge=1, le=10)


class CheckoutIn(BaseModel):
    items: list[CartItemIn]
    promo_code: str | None = None


class QuoteIn(BaseModel):
    items: list[CartItemIn]
    promo_code: str | None = None


class QuoteOut(BaseModel):
    subtotal: float
    discount: float
    total: float
    promo_applied: bool
    promo_message: str | None = None


# ---- eSIM / Orders ----
class ESIMOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    iccid: str
    qr_payload: str
    qr_image: str
    status: str
    data_total_mb: int
    data_used_mb: int
    validity_days: int
    activated_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    plan: PlanOut


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    subtotal: float
    discount: float
    total: float
    created_at: datetime
    paid_at: datetime | None
    esims: list[ESIMOut] = []


# ---- Account / profile ----
class PassportCountry(BaseModel):
    iso2: str
    name: str
    esims: int


class AccountSummaryOut(BaseModel):
    full_name: str
    email: EmailStr
    member_since: datetime
    active_esims: int
    total_esims: int
    data_used_mb: int
    data_total_mb: int
    countries_connected: int
    total_spent: float
    orders_count: int
    passport: list[PassportCountry] = []


class ProfileUpdateIn(BaseModel):
    full_name: str | None = None
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=6)


class TopUpIn(BaseModel):
    extra_mb: int = Field(default=1024, ge=512, le=51200)


# ---- Content ----
class FAQOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    question: str
    answer: str
    category: str


class BannerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    subtitle: str
    image_url: str
    cta_text: str
    cta_link: str


# Localized landing content (resolved to one language by the endpoint).
class BenefitOut(BaseModel):
    id: int
    icon: str
    title: str
    text: str


class TestimonialOut(BaseModel):
    id: int
    name: str
    location: str
    text: str
    rating: int


class TestimonialSubmitIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    location: str = Field(min_length=2, max_length=120)
    text: str = Field(min_length=10, max_length=1000)


class TestimonialStatusOut(BaseModel):
    eligible: bool
    status: str | None = None
    rating: int | None = None
    location: str | None = None
    text: str | None = None


class DeviceOut(BaseModel):
    id: int
    name: str


class PromoOut(BaseModel):
    eyebrow: str
    title: str
    text: str
    code: str
    cta_link: str


class LandingOut(BaseModel):
    benefits: list[BenefitOut]
    testimonials: list[TestimonialOut]
    devices: list[DeviceOut]
    faqs: list[FAQOut]
    promo: PromoOut | None = None
