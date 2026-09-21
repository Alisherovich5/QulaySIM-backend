"""Handing an eSIM over for free.

This is the one screen that spends money, so it is also the one with the most
said out loud: the cost is shown before the click, the wallet is checked
against it, and every grant is written down with the name of whoever made it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models import ESIM, ComplimentaryGrant, Customer, Order, OrderItem, Plan, Staff

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class GrantRow(BaseModel):
    id: int
    created_at: datetime
    customer_email: str
    plan_title: str
    cost_usd: float
    esim_id: int | None
    granted_by: str


@router.get("/grants")
async def list_grants(session: SessionDep, staff: CurrentStaff) -> dict[str, list[GrantRow]]:
    rows = (
        await session.execute(
            select(ComplimentaryGrant, Customer.email, Plan.title, Staff)
            .join(Customer, Customer.id == ComplimentaryGrant.customer_id)
            .join(Plan, Plan.id == ComplimentaryGrant.plan_id)
            .outerjoin(Staff, Staff.id == ComplimentaryGrant.granted_by_id)
            .order_by(ComplimentaryGrant.created_at.desc())
            .limit(200)
        )
    ).all()

    order_ids = [g.order_id for g, _, _, _ in rows if g.order_id]
    esims: dict[int, int] = {}
    if order_ids:
        esims = {
            int(row[0]): int(row[1])
            for row in (
                await session.execute(
                    select(ESIM.order_id, func.min(ESIM.id))
                    .where(ESIM.order_id.in_(order_ids))
                    .group_by(ESIM.order_id)
                )
            ).all()
        }

    return {
        "items": [
            GrantRow(
                id=grant.id,
                created_at=grant.created_at,
                customer_email=email,
                plan_title=title,
                cost_usd=float(grant.cost_usd),
                esim_id=esims.get(grant.order_id or 0),
                granted_by=who.display_name if who else "—",
            )
            for grant, email, title, who in rows
        ]
    }


class GrantIn(BaseModel):
    email: EmailStr
    plan_id: int
    reason: str | None = Field(default=None, max_length=200)


class GrantOut(BaseModel):
    order_id: int
    customer_created: bool


@router.post("/grants", response_model=GrantOut, status_code=201)
async def create_grant(payload: GrantIn, session: SessionDep, staff: CurrentStaff) -> GrantOut:
    """Create the order, mark it paid-at-our-cost, and let fulfilment run.

    Deliberately NOT a direct call to the supplier. The grant goes through the
    exact same path a sale does — same order row, same purchase claim, same
    retry behaviour — so a free eSIM cannot be bought twice by a retry, and so
    the eSIM shows up in the customer's own account like any other.

    The address is the input, not a customer id: the person handing this over
    has an email in front of them and nothing else. An unknown address opens an
    account, which is what the customer needs anyway to see their QR.
    """
    plan = await session.get(Plan, payload.plan_id)
    if plan is None or not plan.is_active:
        raise NotFoundError("Tarif topilmadi yoki o‘chirilgan")
    if plan.provider == "mock":
        raise ConflictError("Bu tarif sinov ta’minotchisida — haqiqiy eSIM bermaydi")

    email = payload.email.strip().lower()
    customer = (
        (await session.execute(select(Customer).where(func.lower(Customer.email) == email)))
        .scalars()
        .first()
    )
    created = customer is None
    if customer is None:
        customer = Customer(email=email, full_name="", hashed_password="", is_active=True)
        session.add(customer)
        await session.flush()

    cost = Decimal(str(plan.cost_usd or 0))
    order = Order(
        customer_id=customer.id,
        status="paid",
        subtotal=Decimal("0"),
        discount=Decimal("0"),
        total=Decimal("0"),
        amount_uzs=Decimal("0"),
        is_complimentary=True,
        paid_at=datetime.now(UTC),
        provider="grant",
        provider_transaction_id=f"grant-{staff.id}",
    )
    session.add(order)
    await session.flush()

    session.add(
        OrderItem(
            order_id=order.id,
            plan_id=plan.id,
            unit_price=Decimal("0"),
            unit_cost=cost,
            quantity=1,
        )
    )
    session.add(
        ComplimentaryGrant(
            customer_id=customer.id,
            plan_id=plan.id,
            order_id=order.id,
            granted_by_id=staff.id,
            reason=(payload.reason or "").strip()[:200],
            cost_usd=cost,
        )
    )
    await session.commit()

    logger.info(
        "backoffice.grant_created",
        staff=staff.username,
        order_id=order.id,
        plan_id=plan.id,
        cost_usd=str(cost),
        customer_created=created,
    )

    from app.workers.tasks.provisioning import fulfil_paid_order

    fulfil_paid_order.delay(order.id)
    return GrantOut(order_id=order.id, customer_created=created)
