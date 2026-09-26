"""Promo codes and the referral programme.

Both of these hand money away, so both are owner-only to change and both are
written down in the log with the name of whoever changed them. Reading them is
open to any operator: knowing which code a customer is quoting is support work,
not an approval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff, OwnerOnly
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Customer, PromoCode, Referral

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class PromoRow(BaseModel):
    id: int
    code: str
    discount_type: str
    discount_value: float
    min_order_usd: float
    max_uses: int
    used_count: int
    valid_until: datetime | None
    is_active: bool
    reason: str
    first_order_only: bool
    issued_to: str | None
    created_at: datetime
    # Derived, because "is this code still usable" is the question the screen is
    # opened to answer and it is three fields away from any one column.
    spent: bool
    expired: bool


def _shape(code: PromoCode, issued_to: str | None) -> PromoRow:
    expired = code.valid_until is not None and code.valid_until <= datetime.now(UTC)
    spent = code.max_uses > 0 and code.used_count >= code.max_uses
    return PromoRow(
        id=code.id,
        code=code.code,
        discount_type=code.discount_type,
        discount_value=float(code.discount_value),
        min_order_usd=float(code.min_order_usd),
        max_uses=code.max_uses,
        used_count=code.used_count,
        valid_until=code.valid_until,
        is_active=code.is_active,
        reason=code.reason,
        first_order_only=code.first_order_only,
        issued_to=issued_to,
        created_at=code.created_at,
        spent=spent,
        expired=expired,
    )


@router.get("/promocodes")
async def list_promocodes(
    session: SessionDep, staff: CurrentStaff, limit: int = Query(default=200, ge=1, le=500)
) -> dict[str, object]:
    rows = (
        await session.execute(
            select(PromoCode, Customer.email)
            .outerjoin(Customer, Customer.id == PromoCode.issued_to_id)
            .order_by(PromoCode.created_at.desc())
            .limit(limit)
        )
    ).all()
    items = [_shape(code, email) for code, email in rows]
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "usable": len([i for i in items if i.is_active and not i.spent and not i.expired]),
            "cashback": len([i for i in items if i.reason != "manual"]),
        },
    }


class PromoIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    discount_type: str = Field(default="percent", pattern="^(percent|fixed)$")
    discount_value: float = Field(gt=0, le=100000)
    min_order_usd: float = Field(default=0, ge=0)
    max_uses: int = Field(default=0, ge=0, le=100000)
    valid_until: datetime | None = None
    first_order_only: bool = False


@router.post("/promocodes", status_code=201)
async def create_promocode(
    payload: PromoIn, session: SessionDep, staff: OwnerOnly
) -> dict[str, object]:
    """Mint a code by hand.

    Owner only. A percentage code with no expiry and no use limit is a
    permanent discount on every order anybody pastes it into, which is how a
    loyalty scheme becomes a site-wide sale nobody approved.
    """
    code = payload.code.strip().upper()
    if payload.discount_type == "percent" and payload.discount_value > 100:
        raise ConflictError("Foiz chegirmasi 100 dan oshmaydi")

    existing = (
        await session.execute(select(PromoCode.id).where(func.upper(PromoCode.code) == code))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Bunday kod allaqachon bor")

    row = PromoCode(
        code=code,
        discount_type=payload.discount_type,
        discount_value=Decimal(str(payload.discount_value)),
        min_order_usd=Decimal(str(payload.min_order_usd)),
        max_uses=payload.max_uses,
        used_count=0,
        valid_until=payload.valid_until,
        is_active=True,
        reason="manual",
        first_order_only=payload.first_order_only,
    )
    session.add(row)
    await session.commit()
    logger.info("backoffice.promo_created", staff=staff.username, code=code)
    return {"id": row.id, "code": code}


class PromoPatch(BaseModel):
    is_active: bool


@router.patch("/promocodes/{promo_id}", status_code=204)
async def toggle_promocode(
    promo_id: int, payload: PromoPatch, session: SessionDep, staff: OwnerOnly
) -> None:
    """Switch a code off — the thing you reach for when one is being shared.

    Switching off rather than deleting: the orders that used it still point at
    the row, and the history of what a customer was charged has to stay
    readable.
    """
    code = await session.get(PromoCode, promo_id)
    if code is None:
        raise NotFoundError("Kod topilmadi")
    await session.execute(
        update(PromoCode).where(PromoCode.id == promo_id).values(is_active=payload.is_active)
    )
    await session.commit()
    logger.info(
        "backoffice.promo_toggled",
        staff=staff.username,
        code=code.code,
        is_active=payload.is_active,
    )


class ReferralRow(BaseModel):
    id: int
    referrer_email: str
    referrer_id: int
    referred_email: str
    referred_id: int | None
    status: str
    reward_code: str
    created_at: datetime
    completed_at: datetime | None


class AgentRow(BaseModel):
    customer_id: int
    email: str
    name: str
    invited: int
    completed: int


@router.get("/referrals")
async def list_referrals(
    session: SessionDep,
    staff: CurrentStaff,
    q: str = "",
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    """Who invited whom, and who is actually bringing people in.

    Two lists, because they answer different questions. The rows are the
    invitations; the agents are the handful of customers those rows come from —
    which is invisible in a list of two hundred lines, and is the thing worth
    knowing.
    """
    referrer = Customer.__table__.alias("referrer")
    invited = Customer.__table__.alias("invited")
    # Either side of the invitation. Somebody arrives here with one address and
    # does not know whether that person invited or was invited.
    where = []
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(referrer.c.email).like(needle),
                func.lower(invited.c.email).like(needle),
                func.lower(Referral.referred_email).like(needle),
            )
        )
    rows = (
        await session.execute(
            select(Referral, referrer.c.email, invited.c.email)
            .join(referrer, referrer.c.id == Referral.referrer_id)
            .outerjoin(invited, invited.c.id == Referral.referred_id)
            .where(*where)
            .order_by(Referral.created_at.desc())
            .limit(limit)
        )
    ).all()

    agent_where = []
    if q.strip():
        agent_where.append(func.lower(Customer.email).like(f"%{q.strip().lower()}%"))
    agents = (
        await session.execute(
            select(
                Customer.id,
                Customer.email,
                Customer.full_name,
                func.count(Referral.id),
                func.count(Referral.id).filter(Referral.status == "completed"),
            )
            .join(Referral, Referral.referrer_id == Customer.id)
            .where(*agent_where)
            .group_by(Customer.id, Customer.email, Customer.full_name)
            # Busiest first, then newest — so a tie does not reorder itself
            # between two loads of the same page.
            .order_by(func.count(Referral.id).desc(), Customer.id.desc())
            .limit(200)
        )
    ).all()

    return {
        "items": [
            ReferralRow(
                id=r.id,
                referrer_email=from_email,
                referrer_id=r.referrer_id,
                referred_email=to_email or r.referred_email,
                referred_id=r.referred_id,
                status=r.status,
                reward_code=r.reward_code,
                created_at=r.created_at,
                completed_at=r.completed_at,
            )
            for r, from_email, to_email in rows
        ],
        "agents": [
            AgentRow(
                customer_id=cid,
                email=email,
                name=name or (email.split("@")[0] if email else "Mijoz"),
                invited=int(count),
                completed=int(done),
            )
            for cid, email, name, count, done in agents
        ],
    }
