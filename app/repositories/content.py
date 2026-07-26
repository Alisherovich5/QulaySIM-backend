from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FAQ, Benefit, Device, PromoBanner, Testimonial


async def active_benefits(session: AsyncSession) -> list[Benefit]:
    result = await session.execute(
        select(Benefit).where(Benefit.is_active.is_(True)).order_by(Benefit.sort_order, Benefit.id)
    )
    return list(result.scalars().all())


async def approved_testimonials(session: AsyncSession, limit: int = 12) -> list[Testimonial]:
    """Only moderator-approved reviews ever reach the storefront."""
    result = await session.execute(
        select(Testimonial)
        .where(
            Testimonial.is_active.is_(True),
            Testimonial.moderation_status == "approved",
        )
        .order_by(Testimonial.sort_order, Testimonial.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def active_devices(session: AsyncSession) -> list[Device]:
    result = await session.execute(
        select(Device).where(Device.is_active.is_(True)).order_by(Device.sort_order, Device.id)
    )
    return list(result.scalars().all())


async def active_faqs(session: AsyncSession) -> list[FAQ]:
    result = await session.execute(
        select(FAQ).where(FAQ.is_active.is_(True)).order_by(FAQ.sort_order, FAQ.id)
    )
    return list(result.scalars().all())


async def current_promo_banner(session: AsyncSession) -> PromoBanner | None:
    result = await session.execute(
        select(PromoBanner)
        .where(PromoBanner.is_active.is_(True))
        .order_by(PromoBanner.updated_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_customer_testimonial(session: AsyncSession, customer_id: int) -> Testimonial | None:
    result = await session.execute(
        select(Testimonial).where(Testimonial.customer_id == customer_id)
    )
    return result.scalars().first()
