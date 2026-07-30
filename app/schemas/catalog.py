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
    # Empty for almost every plan; the storefront renders it only when set.
    price_note: str = ""
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


class PopularPlanOut(PlanOut):
    """A plan with enough of its destination attached to stand on its own.

    The landing page shows plans outside any country page, so the country name,
    slug and flag have to travel with each one — otherwise the card cannot say
    where it is for or link anywhere.
    """

    country_name: str
    country_slug: str
    country_iso2: str
