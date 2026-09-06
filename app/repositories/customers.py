from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Customer, Order, Referral


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


async def list_referrals_with_names(
    session: AsyncSession, referrer_id: int
) -> list[tuple[Referral, str, Decimal | None]]:
    """Referrals with the invitee's own name and what they first paid.

    An email is what the system stored at sign-up; a name is what the agent
    recognises when they are asked "who came through your link?". The join is
    an outer one because an invitation can exist before anyone accepts it, and
    such a row must still be listed rather than silently dropped.

    The third value is the invitee's FIRST paid order in so'm -- the one that
    completed the referral. A percentage-based commission is a percentage of
    that order, so reading it here keeps the money and the person on one row;
    it is None when the invitee has not paid yet, and also when the order
    predates the so'm column.
    """

    referred = aliased(Customer)
    first_paid_uzs = (
        select(Order.amount_uzs)
        .where(Order.customer_id == Referral.referred_id, Order.status == "paid")
        .order_by(Order.id)
        .limit(1)
        .correlate(Referral)
        .scalar_subquery()
    )
    result = await session.execute(
        select(Referral, func.coalesce(referred.full_name, ""), first_paid_uzs)
        .outerjoin(referred, Referral.referred_id == referred.id)
        .where(Referral.referrer_id == referrer_id)
        .order_by(Referral.created_at.desc())
    )
    return [(row[0], row[1], row[2]) for row in result.all()]


async def find_pending_referral(session: AsyncSession, referred_id: int) -> Referral | None:
    result = await session.execute(
        select(Referral)
        .where(Referral.referred_id == referred_id, Referral.status == "pending")
        .with_for_update(skip_locked=True)
    )
    return result.scalars().first()
