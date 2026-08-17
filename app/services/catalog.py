"""Catalogue reads with a Redis read-through cache.

The catalogue changes only when an admin edits it, so it is cached for
`cache_ttl_catalog` seconds and invalidated explicitly.

Destination and region names are localised here rather than in the storefront:
they are admin-owned data, not UI copy, so the same table that holds "Turkey"
holds "Turkiya" and "Турция". Every cache key carries the language — without it
the first caller's language would be served to everyone for the whole TTL.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_key, get_or_set, invalidate
from app.core.config import settings
from app.core.errors import NotFoundError
from app.db.models import Country
from app.domain.localisation import DEFAULT_LANGUAGE, localise
from app.repositories import catalog as repo
from app.schemas.base import JSONDict, JSONList
from app.schemas.catalog import (
    CountryDetailOut,
    CountryOut,
    PlanOut,
    PopularPlanOut,
    RegionDetailOut,
    RegionOut,
)


def _localise_country(model: CountryOut, country: Country, language: str) -> None:
    """Swap the English base names for the requested language, in place.

    Applied after `model_validate` so the schema stays the one place deciding
    which columns leave the service. The nested region needs the same treatment
    or a destination reads "Turkiya" under the heading "Europe".
    """
    model.name = localise(country, "name", language)
    if model.region is not None and country.region is not None:
        model.region.name = localise(country.region, "name", language)


async def list_regions(session: AsyncSession, language: str = DEFAULT_LANGUAGE) -> JSONList:
    async def produce() -> JSONList:
        out: JSONList = []
        for region, country_count, starting_price in await repo.region_summaries(session):
            model = RegionOut.model_validate(region)
            model.name = localise(region, "name", language)
            model.country_count = country_count
            model.starting_price = starting_price
            out.append(model.model_dump(mode="json"))
        return out

    return await get_or_set(
        cache_key("regions", lang=language), settings.cache_ttl_catalog, produce
    )


async def list_countries(
    session: AsyncSession,
    *,
    # The API always resolves a language before calling; the default is for the
    # cache-warming worker, which has no request to read one from.
    language: str = DEFAULT_LANGUAGE,
    search: str | None,
    region_slug: str | None,
    popular: bool | None,
    limit: int,
    offset: int,
) -> JSONList:
    key = cache_key(
        "countries",
        lang=language,
        search=search,
        region=region_slug,
        popular=popular,
        limit=limit,
        offset=offset,
    )

    async def produce() -> JSONList:
        rows = await repo.list_countries(
            session,
            language=language,
            search=search,
            region_slug=region_slug,
            popular=popular,
            limit=limit,
            offset=offset,
        )
        out: JSONList = []
        for country, starting_price in rows:
            model = CountryOut.model_validate(country)
            _localise_country(model, country, language)
            model.starting_price = starting_price
            out.append(model.model_dump(mode="json"))
        return out

    return await get_or_set(key, settings.cache_ttl_catalog, produce)


async def get_country(
    session: AsyncSession, slug: str, language: str = DEFAULT_LANGUAGE
) -> JSONDict:
    async def produce() -> JSONDict:
        country = await repo.get_country_by_slug(session, slug)
        if country is None:
            raise NotFoundError("Country not found")
        active = sorted(
            (p for p in country.plans if p.is_active),
            key=lambda p: (p.sort_order, p.price_usd),
        )
        detail = CountryDetailOut.model_validate(country)
        _localise_country(detail, country, language)
        detail.plans = [PlanOut.model_validate(p) for p in active]
        detail.starting_price = min((p.price_usd for p in active), default=None)
        return detail.model_dump(mode="json")

    return await get_or_set(
        cache_key("country", slug=slug, lang=language), settings.cache_ttl_catalog, produce
    )


async def get_region(
    session: AsyncSession, slug: str, language: str = DEFAULT_LANGUAGE
) -> JSONDict:
    """One region with the multi-country eSIMs sold for it.

    The counterpart of get_country, for the product a traveller doing three
    countries actually wants. Same shape on purpose, so the storefront renders a
    region page with the component it already has for a destination.
    """
    async def produce() -> JSONDict:
        region = await repo.get_region_by_slug(session, slug)
        if region is None:
            raise NotFoundError("Region not found")
        active = sorted(
            (p for p in region.plans if p.is_active),
            key=lambda p: (p.sort_order, p.price_usd),
        )
        detail = RegionDetailOut.model_validate(region)
        detail.name = localise(region, "name", language)
        detail.plans = [PlanOut.model_validate(p) for p in active]
        detail.starting_price = min((p.price_usd for p in active), default=None)
        detail.country_count = sum(1 for c in region.countries if c.is_active)
        return detail.model_dump(mode="json")

    return await get_or_set(
        cache_key("region", slug=slug, lang=language), settings.cache_ttl_catalog, produce
    )


async def list_popular_plans(
    session: AsyncSession, *, language: str = DEFAULT_LANGUAGE, limit: int
) -> JSONList:
    async def produce() -> JSONList:
        rows = await repo.list_popular_plans(session, limit=limit)
        out: JSONList = []
        for plan, country in rows:
            # Validate the plan through PlanOut first so the field whitelist is
            # the one place deciding what leaves the service, then attach the
            # destination. Building the dict by hand would let a future column
            # on Plan slip out without passing that whitelist.
            model = PopularPlanOut(
                **PlanOut.model_validate(plan).model_dump(),
                country_name=localise(country, "name", language),
                country_slug=country.slug,
                country_iso2=country.iso2,
            )
            out.append(model.model_dump(mode="json"))
        return out

    return await get_or_set(
        cache_key("popular_plans", lang=language, limit=limit),
        settings.cache_ttl_catalog,
        produce,
    )


async def invalidate_catalog(slugs: list[str] | None = None) -> int:
    """Clear our cache, and the edge's.

    One function rather than two calls at every site: a price change that clears
    Redis but leaves the CDN holding the old number for an hour is worse than no
    caching, because it is wrong in a way nobody can see from here.
    """
    cleared = await invalidate(
        "qs:regions*", "qs:countries*", "qs:country*", "qs:popular_plans*"
    )
    from app.integrations.cloudflare import is_configured, purge_catalogue

    if is_configured():
        await purge_catalogue(slugs)
    return cleared
