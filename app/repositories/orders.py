from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ESIM, Country, Order, OrderItem, Plan, PromoCode
from app.repositories.catalog import localised_country_name


async def list_orders(session: AsyncSession, customer_id: int, limit: int = 100) -> list[Order]:
    stmt = (
        select(Order)
        .options(selectinload(Order.esims).selectinload(ESIM.plan))
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def list_esims(session: AsyncSession, customer_id: int) -> list[ESIM]:
    stmt = (
        select(ESIM)
        .options(selectinload(ESIM.plan))
        .where(ESIM.customer_id == customer_id)
        .order_by(ESIM.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_owned_esim(
    session: AsyncSession, esim_id: int, customer_id: int, *, lock: bool = False
) -> ESIM | None:
    """Always scoped by customer_id — ownership is enforced in the query, not
    after the fact, so an IDOR cannot slip through a forgotten check."""
    stmt = (
        select(ESIM)
        .options(selectinload(ESIM.plan))
        .where(ESIM.id == esim_id, ESIM.customer_id == customer_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=ESIM)
    result = await session.execute(stmt)
    return result.scalars().first()


async def lock_promo_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    """SELECT ... FOR UPDATE so two concurrent redemptions cannot both pass the
    `used_count < max_uses` check."""
    result = await session.execute(
        select(PromoCode).where(PromoCode.code == code.strip().upper()).with_for_update()
    )
    return result.scalars().first()


async def get_promo_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    result = await session.execute(select(PromoCode).where(PromoCode.code == code.strip().upper()))
    return result.scalars().first()


async def count_paid_orders(session: AsyncSession, customer_id: int) -> int:
    result = await session.execute(
        select(func.count(Order.id)).where(Order.customer_id == customer_id, Order.status == "paid")
    )
    return int(result.scalar_one())


async def find_by_provider_order_no(session: AsyncSession, order_no: str) -> Order | None:
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.plan))
        .where(Order.provider == "esimaccess", Order.provider_order_no == order_no)
    )
    result = await session.execute(stmt)
    return result.scalars().unique().first()


async def account_summary_rows(
    session: AsyncSession, customer_id: int, *, language: str = "en"
) -> dict[str, Any]:
    """All account KPIs in three queries instead of a per-metric round trip."""
    esim_stats = await session.execute(
        select(
            func.count(ESIM.id),
            func.coalesce(func.sum(ESIM.data_used_mb), 0),
            func.coalesce(func.sum(ESIM.data_total_mb), 0),
            func.count(ESIM.id).filter(ESIM.status == "active"),
        ).where(ESIM.customer_id == customer_id)
    )
    total_esims, used_mb, total_mb, active = esim_stats.one()

    order_stats = await session.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total).filter(Order.status == "paid"), 0),
        ).where(Order.customer_id == customer_id)
    )
    orders_count, total_spent = order_stats.one()

    # The same localised name the catalogue serves, or the passport would say
    # "Turkey" on the page next to a destinations list that says "Turkiya".
    country_name = localised_country_name(language)
    passport = await session.execute(
        select(Country.iso2, country_name, func.count(ESIM.id))
        .join(Plan, Plan.id == ESIM.plan_id)
        .join(Country, Country.id == Plan.country_id)
        .where(ESIM.customer_id == customer_id)
        .group_by(Country.iso2, country_name)
        .order_by(func.count(ESIM.id).desc())
    )
    passport_rows = passport.all()

    return {
        "total_esims": int(total_esims),
        "active_esims": int(active),
        "data_used_mb": int(used_mb),
        "data_total_mb": int(total_mb),
        "orders_count": int(orders_count),
        "total_spent": total_spent,
        "passport": [
            {"iso2": iso2, "name": name, "esims": int(count)} for iso2, name, count in passport_rows
        ],
    }
