from __future__ import annotations

from app.schemas.base import APIModel, Money


class RegionOut(APIModel):
    id: int
    name: str
    slug: str


class PlanOut(APIModel):
    id: int
    scope: str
    title: str
    data_amount_mb: int
    is_unlimited: bool
    data_label: str
    validity_days: int
    price_usd: Money
    network_type: str
    supports_hotspot: bool
    is_popular: bool


class CountryOut(APIModel):
    id: int
    name: str
    slug: str
    iso2: str
    is_popular: bool
    region: RegionOut | None = None
    starting_price: Money | None = None


class CountryDetailOut(CountryOut):
    plans: list[PlanOut] = []
