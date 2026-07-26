"""Catalogue reads with a Redis read-through cache.

The catalogue changes only when an admin edits it, so it is cached for
`cache_ttl_catalog` seconds and invalidated explicitly.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_key, get_or_set, invalidate
from app.core.config import settings
from app.core.errors import NotFoundError
from app.repositories import catalog as repo
from app.schemas.base import JSONDict, JSONList
from app.schemas.catalog import CountryDetailOut, CountryOut, PlanOut, RegionOut


async def list_regions(session: AsyncSession) -> JSONList:
    async def produce() -> JSONList:
        regions = await repo.list_regions(session)
        return [RegionOut.model_validate(r).model_dump(mode="json") for r in regions]

    return await get_or_set(cache_key("regions"), settings.cache_ttl_catalog, produce)


async def list_countries(
    session: AsyncSession,
    *,
    search: str | None,
    region_slug: str | None,
    popular: bool | None,
    limit: int,
    offset: int,
) -> JSONList:
    key = cache_key(
        "countries",
        search=search,
        region=region_slug,
        popular=popular,
        limit=limit,
        offset=offset,
    )

    async def produce() -> JSONList:
        rows = await repo.list_countries(
            session,
            search=search,
            region_slug=region_slug,
            popular=popular,
            limit=limit,
            offset=offset,
        )
        out: JSONList = []
        for country, starting_price in rows:
            model = CountryOut.model_validate(country)
            model.starting_price = starting_price
            out.append(model.model_dump(mode="json"))
        return out

    return await get_or_set(key, settings.cache_ttl_catalog, produce)


async def get_country(session: AsyncSession, slug: str) -> JSONDict:
    async def produce() -> JSONDict:
        country = await repo.get_country_by_slug(session, slug)
        if country is None:
            raise NotFoundError("Country not found")
        active = sorted(
            (p for p in country.plans if p.is_active),
            key=lambda p: (p.sort_order, p.price_usd),
        )
        detail = CountryDetailOut.model_validate(country)
        detail.plans = [PlanOut.model_validate(p) for p in active]
        detail.starting_price = min((p.price_usd for p in active), default=None)
        return detail.model_dump(mode="json")

    return await get_or_set(cache_key("country", slug=slug), settings.cache_ttl_catalog, produce)


async def invalidate_catalog() -> int:
    return await invalidate("qs:regions*", "qs:countries*", "qs:country*")
