"""Catalogue reads.

The previous implementation used `joinedload(Country.plans)` on the list
endpoint purely to compute each country's cheapest price — loading every plan
row for every country. This uses a grouped subquery instead, so the payload
stays proportional to the number of countries.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import SQLColumnExpression, Subquery, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Country, Plan, Region


def localised_country_name(language: str) -> SQLColumnExpression[str]:
    """The country name the customer is actually reading.

    Sorting on `Country.name` alone puts Yaponiya under J on the Uzbek page,
    which reads as an unsorted list. An empty translation falls back to the
    English base, exactly as the payload does.
    """
    column = getattr(Country, f"name_{language}", None)
    if column is None:
        return Country.name
    return func.coalesce(func.nullif(column, ""), Country.name)


def _cheapest_price_subquery() -> Subquery:
    return (
        select(Plan.country_id, func.min(Plan.price_usd).label("starting_price"))
        .where(Plan.is_active.is_(True), Plan.country_id.is_not(None))
        .group_by(Plan.country_id)
        .subquery()
    )


async def list_regions(session: AsyncSession) -> list[Region]:
    result = await session.execute(select(Region).order_by(Region.sort_order, Region.name))
    return list(result.scalars().all())


async def region_summaries(
    session: AsyncSession,
) -> list[tuple[Region, int, Decimal | None]]:
    """Each region with its active country count and cheapest regional plan.

    Two aggregates rather than a query per region: the list is rendered on the
    destinations page, and a card per region each fetching its own count would be
    a dozen round trips for a header.

    A region with no regional plan of its own gets None, not zero — "from $0"
    would advertise a price that does not exist.
    """
    regions = (
        (await session.execute(select(Region).order_by(Region.sort_order, Region.name)))
        .scalars()
        .all()
    )

    counts = dict(
        (
            await session.execute(
                select(Country.region_id, func.count(Country.id))
                .where(Country.is_active.is_(True), Country.region_id.is_not(None))
                .group_by(Country.region_id)
            )
        ).all()
    )

    # Only multi-country plans count towards the headline price. A region's
    # cheapest *local* plan is a single-country eSIM and would undercut the
    # regional one it is meant to advertise.
    prices = dict(
        (
            await session.execute(
                select(Plan.region_id, func.min(Plan.price_usd))
                .where(
                    Plan.is_active.is_(True),
                    Plan.region_id.is_not(None),
                    Plan.scope.in_(("regional", "global")),
                )
                .group_by(Plan.region_id)
            )
        ).all()
    )

    return [(r, counts.get(r.id, 0), prices.get(r.id)) for r in regions]


async def list_countries(
    session: AsyncSession,
    *,
    language: str = "en",
    search: str | None = None,
    region_slug: str | None = None,
    popular: bool | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[tuple[Country, Decimal | None]]:
    cheapest = _cheapest_price_subquery()
    stmt = (
        select(Country, cheapest.c.starting_price)
        .outerjoin(cheapest, cheapest.c.country_id == Country.id)
        .options(selectinload(Country.region))
        .where(Country.is_active.is_(True))
    )
    if search:
        like = f"%{search.strip()}%"
        # Every language's name, not just the page's: somebody browsing in
        # Russian may still type "Turkey", and a search that only matched the
        # active language would tell them the destination does not exist.
        stmt = stmt.where(
            or_(
                Country.name.ilike(like),
                Country.name_ru.ilike(like),
                Country.name_uz.ilike(like),
                Country.iso2.ilike(like),
            )
        )
    if popular is not None:
        stmt = stmt.where(Country.is_popular.is_(popular))
    if region_slug:
        stmt = stmt.join(Region, Region.id == Country.region_id).where(Region.slug == region_slug)

    stmt = (
        stmt.order_by(Country.sort_order, localised_country_name(language))
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def count_countries(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(Country.id)).where(Country.is_active.is_(True))
    )
    return int(result.scalar_one())


async def get_region_by_slug(session: AsyncSession, slug: str) -> Region | None:
    """A region with its multi-country plans and its destination count.

    `Region.plans` is the multi-country side — plans with a region and no
    country. Countries are loaded too, because how many destinations a region
    covers is the headline number on the page.
    """
    stmt = (
        select(Region)
        .options(selectinload(Region.plans), selectinload(Region.countries))
        .where(Region.slug == slug)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_country_by_slug(session: AsyncSession, slug: str) -> Country | None:
    stmt = (
        select(Country)
        .options(selectinload(Country.region), selectinload(Country.plans))
        .where(Country.slug == slug, Country.is_active.is_(True))
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_active_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    result = await session.execute(select(Plan).where(Plan.id == plan_id, Plan.is_active.is_(True)))
    return result.scalars().first()


async def get_active_plans(session: AsyncSession, plan_ids: list[int]) -> dict[int, Plan]:
    """Single query for a whole cart — avoids one SELECT per line item.

    Loads each plan's supplier offers with it, because `is_active` alone is not
    enough to decide a plan may be sold: a plan can be active, priced, and have
    no wholesaler able to supply it. Twenty such plans were live at $29.90–$35.88
    with no offer and no package code, so the money would have been taken and no
    eSIM could ever have been issued. See `is_fulfillable`.
    """
    if not plan_ids:
        return {}
    result = await session.execute(
        select(Plan)
        .options(selectinload(Plan.offers))
        .where(Plan.id.in_(set(plan_ids)), Plan.is_active.is_(True))
    )
    return {plan.id: plan for plan in result.scalars().all()}


async def list_popular_plans(session: AsyncSession, *, limit: int) -> list[tuple[Plan, Country]]:
    """Popular, active plans with their destination, cheapest first.

    Joined rather than loaded through the relationship so the query returns
    exactly the rows rendered — one statement, no per-plan follow-up.

    Only one plan per destination: six cards showing six tariffs for the same
    country is a worse landing page than six countries, and the flags are what
    make the section scannable.
    """
    rows = (
        await session.execute(
            select(Plan, Country)
            .join(Country, Plan.country_id == Country.id)
            .where(
                Plan.is_active.is_(True),
                Plan.is_popular.is_(True),
                Country.is_active.is_(True),
                # The destination must be promoted too, or the landing page
                # shows one set of countries in its destinations grid and a
                # different set in its plans row. Requiring both means the two
                # agree by construction instead of depending on somebody keeping
                # two flags in step.
                Country.is_popular.is_(True),
            )
            .order_by(Plan.price_usd, Plan.id)
        )
    ).all()

    seen: set[int] = set()
    out: list[tuple[Plan, Country]] = []
    for plan, country in rows:
        if country.id in seen:
            continue
        seen.add(country.id)
        out.append((plan, country))
        if len(out) >= limit:
            break
    return out
