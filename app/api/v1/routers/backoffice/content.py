"""Customer reviews, and whether they are allowed on the site.

A review arrives from the storefront as `pending` and the shop shows nothing
until somebody has read it. That gate had no interface after the Django admin
was retired, which means a review written today would have sat unread forever —
not rejected, just never looked at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models import Customer, Testimonial

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class ReviewRow(BaseModel):
    id: int
    name: str
    location: str
    text: str
    rating: int
    state: str
    is_active: bool
    customer_email: str | None
    created_at: datetime


@router.get("/reviews")
async def list_reviews(
    session: SessionDep,
    staff: CurrentStaff,
    state: str = "",
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    where = [Testimonial.moderation_status == state] if state else []
    rows = (
        await session.execute(
            select(Testimonial, Customer.email)
            .outerjoin(Customer, Customer.id == Testimonial.customer_id)
            .where(*where)
            .order_by(Testimonial.created_at.desc())
            .limit(limit)
        )
    ).all()
    counts_raw = (
        await session.execute(
            select(Testimonial.moderation_status, func.count()).group_by(
                Testimonial.moderation_status
            )
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in counts_raw}
    counts["total"] = sum(counts.values())

    return {
        "items": [
            ReviewRow(
                id=t.id,
                # The uz text is what the shop shows; the base column is the
                # fallback for rows written before the translations existed.
                name=t.name,
                location=t.location_uz or t.location,
                text=t.text_uz or t.text,
                rating=t.rating,
                state=t.moderation_status,
                is_active=t.is_active,
                customer_email=email,
                created_at=t.created_at,
            )
            for t, email in rows
        ],
        "counts": counts,
    }


class Verdict(BaseModel):
    state: Literal["approved", "rejected", "pending"]


@router.post("/reviews/{review_id}/moderate", status_code=204)
async def moderate(
    review_id: int, payload: Verdict, session: SessionDep, staff: CurrentStaff
) -> None:
    """Approve or refuse one review.

    Approving also switches it on, because a review that is approved and
    inactive is invisible for a reason nobody can see on this screen —
    two switches for one decision is how a review ends up approved and still
    missing from the site.
    """
    review = await session.get(Testimonial, review_id)
    if review is None:
        raise NotFoundError("Sharh topilmadi")
    await session.execute(
        update(Testimonial)
        .where(Testimonial.id == review_id)
        .values(
            moderation_status=payload.state,
            is_active=payload.state == "approved",
        )
    )
    await session.commit()
    logger.info(
        "backoffice.review_moderated",
        staff=staff.username,
        review_id=review_id,
        state=payload.state,
    )
