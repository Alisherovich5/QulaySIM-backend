"""Catalogue reads.

The previous implementation used `joinedload(Country.plans)` on the list
endpoint purely to compute each country's cheapest price — loading every plan
row for every country. This uses a grouped subquery instead, so the payload
stays proportional to the number of countries.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Subquery, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Country, Plan, Region


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


async def list_countries(
    session: AsyncSession,
    *,
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
        stmt = stmt.where(or_(Country.name.ilike(like), Country.iso2.ilike(like)))
    if popular is not None:
        stmt = stmt.where(Country.is_popular.is_(popular))
    if region_slug:
        stmt = stmt.join(Region, Region.id == Country.region_id).where(Region.slug == region_slug)

    stmt = stmt.order_by(Country.sort_order, Country.name).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def count_countries(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(Country.id)).where(Country.is_active.is_(True))
    )
    return int(result.scalar_one())


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
    """Single query for a whole cart — avoids one SELECT per line item."""
    if not plan_ids:
        return {}
    result = await session.execute(
        select(Plan).where(Plan.id.in_(set(plan_ids)), Plan.is_active.is_(True))
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
