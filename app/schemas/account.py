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


class ReferralEntry(APIModel):
    referred_email: str
    status: str
    reward_code: str
    created_at: datetime


class ReferralSummaryOut(APIModel):
    code: str
    invited: int
    completed: int
    pending: int
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
