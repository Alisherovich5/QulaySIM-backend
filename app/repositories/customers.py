from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, Referral


async def get_by_id(session: AsyncSession, customer_id: int) -> Customer | None:
    return await session.get(Customer, customer_id)


async def get_by_email(session: AsyncSession, email: str) -> Customer | None:
    result = await session.execute(
        select(Customer).where(func.lower(Customer.email) == email.strip().lower())
    )
    return result.scalars().first()


async def get_by_referral_code(session: AsyncSession, code: str) -> Customer | None:
    result = await session.execute(select(Customer).where(Customer.referral_code == code))
    return result.scalars().first()


async def referral_code_exists(session: AsyncSession, code: str) -> bool:
    result = await session.execute(
        select(func.count(Customer.id)).where(Customer.referral_code == code)
    )
    return int(result.scalar_one()) > 0


async def list_referrals(session: AsyncSession, referrer_id: int) -> list[Referral]:
    result = await session.execute(
        select(Referral)
        .where(Referral.referrer_id == referrer_id)
        .order_by(Referral.created_at.desc())
    )
    return list(result.scalars().all())


async def find_pending_referral(session: AsyncSession, referred_id: int) -> Referral | None:
    result = await session.execute(
        select(Referral)
        .where(Referral.referred_id == referred_id, Referral.status == "pending")
        .with_for_update(skip_locked=True)
    )
    return result.scalars().first()
