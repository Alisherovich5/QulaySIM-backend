from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.base import APIModel, Money


class PassportCountry(APIModel):
    iso2: str
    name: str
    esims: int


class AccountSummaryOut(APIModel):
    full_name: str
    email: str
    member_since: datetime
    active_esims: int
    total_esims: int
    data_used_mb: int
    data_total_mb: int
    countries_connected: int
    total_spent: Money
    orders_count: int
    # Inline data URI or null. Sent with the summary rather than served from a
    # URL: there is then no public endpoint to enumerate, and no authenticated
    # image request for an <img> tag to fail — the access token lives in memory,
    # so it cannot ride along on one.
    avatar_url: str | None = None
    passport: list[PassportCountry] = []
    # Referal bo'limi shu mijozga ko'rinadimi. Sayt shu bayroqqa qarab
    # bo'limni umuman chizmaydi -- yopiq bo'lim ko'rinib turib "ruxsat yo'q"
    # deyishdan ko'ra, umuman ko'rinmagani tinch.
    referral_enabled: bool = True


class ReferralEntry(APIModel):
    referred_email: str
    referred_name: str = ""
    status: str
    reward_code: str
    created_at: datetime
    completed_at: datetime | None = None
    # Aynan shu odam uchun tegadigan summa. Umumiy summani odamlar soniga
    # bo'lish endi to'g'ri javob bermaydi: stavka pog'onali va har bir mijoz
    # o'zi kelgan paytdagi stavkani saqlab qoladi.
    commission_uzs: int = 0


class ReferralRate(APIModel):
    """Hozirgi stavka. `label` -- ekranda ko'rsatiladigan yagona haqiqat."""

    label: str
    percent: float | None = None
    flat_uzs: int | None = None


class ReferralNextRate(APIModel):
    label: str
    percent: float | None = None
    flat_uzs: int | None = None
    at: int
    needed: int


class ReferralSummaryOut(APIModel):
    code: str
    invited: int
    completed: int
    pending: int
    # So'mda, chunki agentga naqd shu valyutada to'lanadi.
    earned_uzs: int = 0
    rate: ReferralRate
    next_rate: ReferralNextRate | None = None
    rewards: list[str] = []
    entries: list[ReferralEntry] = []


class TestimonialStatusOut(APIModel):
    eligible: bool
    status: str | None = None
    rating: int | None = None
    location: str | None = None
    text: str | None = None


class TestimonialSubmitIn(APIModel):
    rating: int = Field(ge=1, le=5)
    location: str = Field(min_length=2, max_length=120)
    text: str = Field(min_length=10, max_length=1000)
