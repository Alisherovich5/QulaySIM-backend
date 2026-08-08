"""Account use cases: summary, eSIM lifecycle, profile, referrals, reviews."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.security import hash_password, verify_password
from app.db.models import ESIM, Customer, Testimonial
from app.db.models.enums import ESIMStatus
from app.domain import avatars
from app.domain.referral import new_referral_code
from app.repositories import content as content_repo
from app.repositories import customers as customer_repo
from app.repositories import orders as order_repo
from app.schemas.base import JSONDict

logger = get_logger(__name__)



async def summary(
    session: AsyncSession, customer: Customer, *, language: str = "en"
) -> JSONDict:
    rows = await order_repo.account_summary_rows(session, customer.id, language=language)
    passport = rows["passport"]
    return {
        "full_name": customer.full_name,
        "email": customer.email,
        "member_since": customer.created_at,
        "active_esims": rows["active_esims"],
        "total_esims": rows["total_esims"],
        "data_used_mb": rows["data_used_mb"],
        "data_total_mb": rows["data_total_mb"],
        "countries_connected": len(passport),
        "total_spent": rows["total_spent"],
        "orders_count": rows["orders_count"],
        "avatar_url": avatars.to_data_uri(customer.avatar_webp),
        "passport": passport,
    }


async def set_avatar(session: AsyncSession, customer: Customer, raw: bytes) -> None:
    """Store a re-encoded avatar. Raises AvatarRejectedError for anything unsuitable.

    Only the re-encoded bytes are kept — never what was uploaded. The rejection
    carries a code so the storefront can say what was wrong in the customer's own
    language instead of "upload failed".
    """
    from datetime import datetime

    built = avatars.build(raw)
    customer.avatar_webp = built.webp
    customer.avatar_updated_at = datetime.now(UTC)
    await session.commit()
    logger.info("account.avatar_set", customer_id=customer.id, bytes=len(built.webp))


async def clear_avatar(session: AsyncSession, customer: Customer) -> None:
    customer.avatar_webp = None
    customer.avatar_updated_at = None
    await session.commit()
    logger.info("account.avatar_cleared", customer_id=customer.id)


async def activate_esim(session: AsyncSession, customer: Customer, esim_id: int) -> ESIM:
    esim = await order_repo.get_owned_esim(session, esim_id, customer.id, lock=True)
    if esim is None:
        raise NotFoundError("eSIM not found")
    if esim.status == ESIMStatus.EXPIRED:
        raise ConflictError("This eSIM has expired")
    if esim.status == ESIMStatus.ACTIVE:
        return esim  # idempotent — a double click must not shift the expiry

    now = datetime.now(UTC)
    esim.status = ESIMStatus.ACTIVE
    esim.activated_at = now
    esim.expires_at = now + timedelta(days=esim.validity_days)
    await session.commit()
    await session.refresh(esim)
    logger.info("esim.activated", esim_id=esim.id, customer_id=customer.id)
    return esim


async def update_profile(
    session: AsyncSession,
    customer: Customer,
    *,
    full_name: str | None,
    current_password: str | None,
    new_password: str | None,
) -> Customer:
    if new_password:
        if not current_password:
            raise DomainError("Enter your current password to set a new one")
        if not verify_password(current_password, customer.hashed_password):
            raise PermissionDeniedError("Current password is incorrect")
        customer.hashed_password = hash_password(new_password)
        logger.info("account.password_changed", customer_id=customer.id)

    if full_name is not None:
        customer.full_name = full_name.strip()[:150]

    await session.commit()
    await session.refresh(customer)
    return customer


async def ensure_referral_code(session: AsyncSession, customer: Customer) -> str:
    if customer.referral_code:
        return customer.referral_code
    for _ in range(8):
        code = new_referral_code()
        if not await customer_repo.referral_code_exists(session, code):
            customer.referral_code = code
            await session.commit()
            await session.refresh(customer)
            return code
    raise DomainError("Could not allocate a referral code, please retry")


async def referral_summary(session: AsyncSession, customer: Customer) -> JSONDict:
    code = await ensure_referral_code(session, customer)
    rows = await customer_repo.list_referrals(session, customer.id)
    completed = [r for r in rows if r.status == "completed"]
    return {
        "code": code,
        "invited": len(rows),
        "completed": len(completed),
        "pending": len(rows) - len(completed),
        "rewards": [r.reward_code for r in completed if r.reward_code],
        "entries": rows,
    }


async def _has_purchased(session: AsyncSession, customer_id: int) -> bool:
    esims = await order_repo.list_esims(session, customer_id)
    return bool(esims)


async def testimonial_status(session: AsyncSession, customer: Customer) -> JSONDict:
    review = await content_repo.get_customer_testimonial(session, customer.id)
    return {
        "eligible": await _has_purchased(session, customer.id),
        "status": review.moderation_status if review else None,
        "rating": review.rating if review else None,
        "location": review.location if review else None,
        "text": review.text if review else None,
    }


async def submit_testimonial(
    session: AsyncSession, customer: Customer, *, rating: int, location: str, text: str
) -> JSONDict:
    if not await _has_purchased(session, customer.id):
        raise PermissionDeniedError("Only customers who purchased an eSIM can leave a review")

    review = await content_repo.get_customer_testimonial(session, customer.id)
    if review and review.moderation_status == "approved":
        raise ConflictError("Your approved review is already published")

    if review is None:
        review = Testimonial(customer_id=customer.id)
        session.add(review)

    review.name = (customer.full_name.strip() or customer.email.split("@", 1)[0])[:80]
    review.location = location.strip()
    review.text = text.strip()
    review.rating = rating
    # Clear stale translations: a moderator re-translates after approval.
    review.location_ru = review.text_ru = review.location_uz = review.text_uz = ""
    # Every edit re-enters moderation — reviews never bypass a human.
    review.moderation_status = "pending"
    review.is_active = True

    await session.commit()
    await session.refresh(review)

    from app.services.content import invalidate_landing

    await invalidate_landing()
    logger.info("testimonial.submitted", customer_id=customer.id, rating=rating)

    return {
        "eligible": True,
        "status": review.moderation_status,
        "rating": review.rating,
        "location": review.location,
        "text": review.text,
    }
