from __future__ import annotations

from app.schemas.base import APIModel, Money


class RegionOut(APIModel):
    id: int
    name: str
    slug: str
    # How many countries one regional eSIM covers, and what the cheapest one
    # costs. "Europe 5 GB" tells a traveller nothing about whether their stop is
    # included; "55 countries, from $5" tells them everything they need to click.
    country_count: int = 0
    starting_price: Money | None = None


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


class RegionDetailOut(RegionOut):
    """A region and the multi-country eSIMs sold for it.

    `country_count` is the reason to buy one: "Yevropa 5 GB" tells a customer
    nothing about whether their stop is covered, and "41 countries" tells them
    everything. It counts the destinations we sell in the region, which is the
    honest number to show — the underlying package may cover more, but those are
    countries we cannot otherwise sell them anyway.
    """

    plans: list[PlanOut] = []
    starting_price: Money | None = None
    country_count: int = 0


class PopularPlanOut(PlanOut):
    """A plan with enough of its destination attached to stand on its own.

    The landing page shows plans outside any country page, so the country name,
    slug and flag have to travel with each one — otherwise the card cannot say
    where it is for or link anywhere.
    """

    country_name: str
    country_slug: str
    country_iso2: str
