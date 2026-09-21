"""The first screen: what needs a person right now."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.catalogue import STALE_AFTER
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.api.v1.routers.backoffice.esims import _live
from app.api.v1.routers.backoffice.money import Wallet, _wallets
from app.api.v1.routers.backoffice.orders import EsimOut, OrderRow, _rows, _shape, days_left
from app.db.models import ESIM, CatalogSyncRun, Customer, Order, Plan

router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])

# An order that has been paid for this long without an eSIM is not "in
# progress", it is stuck. Fulfilment normally takes seconds.
STUCK_AFTER = timedelta(minutes=10)
EXPIRING_WITHIN = timedelta(days=3)


class Today(BaseModel):
    orders: int
    revenue_uzs: float
    pending: int
    delivered: int
    failed: int


class Catalogue(BaseModel):
    synced_at: datetime | None
    stale: bool


class DashboardOut(BaseModel):
    attention: list[OrderRow]
    wallets: list[Wallet]
    today: Today
    catalogue: Catalogue
    expiring: list[EsimOut]


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(session: SessionDep, staff: CurrentStaff) -> DashboardOut:
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Paid, old enough to be late, and with no eSIM against it. The NOT EXISTS
    # is what keeps a delivered order off this list without a second pass.
    delivered = select(ESIM.order_id).where(ESIM.order_id == Order.id)
    stuck = list(
        (
            await session.execute(
                select(Order)
                .options(selectinload(Order.customer))
                .where(
                    Order.status == "paid",
                    Order.paid_at.isnot(None),
                    Order.paid_at < now - STUCK_AFTER,
                    ~delivered.exists(),
                )
                .order_by(Order.paid_at.desc())
                .limit(25)
            )
        )
        .scalars()
        .all()
    )
    extra = await _rows(session, stuck)
    attention = [_shape(order, extra[order.id]) for order in stuck]
    # A top-up leaves no eSIM row by design, so the query above catches every
    # one of them. Only the ones still unapplied actually need a person.
    attention = [row for row in attention if row.stage != "delivered"]

    orders_today, revenue_today = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Order.amount_uzs), 0)).where(
                Order.created_at >= midnight
            )
        )
    ).one()
    paid_today = (
        await session.execute(
            select(func.count()).where(Order.status == "paid", Order.paid_at >= midnight)
        )
    ).scalar_one()
    esims_today = (
        await session.execute(select(func.count()).where(ESIM.created_at >= midnight))
    ).scalar_one()

    last_ok = (
        await session.execute(
            select(func.max(CatalogSyncRun.finished_at)).where(CatalogSyncRun.status == "ok")
        )
    ).scalar_one_or_none()

    expiring_rows = (
        await session.execute(
            select(ESIM, Customer, Plan.title)
            .join(Customer, Customer.id == ESIM.customer_id)
            .join(Plan, Plan.id == ESIM.plan_id)
            .where(
                ESIM.status == "active",
                ESIM.expires_at.isnot(None),
                ESIM.expires_at > now,
                ESIM.expires_at <= now + EXPIRING_WITHIN,
            )
            .order_by(ESIM.expires_at)
            .limit(10)
        )
    ).all()

    return DashboardOut(
        attention=attention,
        wallets=await _wallets(session),
        today=Today(
            orders=int(orders_today),
            revenue_uzs=float(revenue_today),
            pending=max(0, int(paid_today) - int(esims_today)),
            delivered=int(esims_today),
            failed=len([row for row in attention if row.stage == "failed"]),
        ),
        catalogue=Catalogue(
            synced_at=last_ok,
            stale=last_ok is None or (now - last_ok) > STALE_AFTER,
        ),
        expiring=[
            EsimOut(
                id=esim.id,
                iccid=esim.iccid,
                customer_name=customer.full_name or customer.email.split("@")[0],
                customer_email=customer.email,
                plan_title=title,
                status=_live(esim.status, esim.expires_at),
                data_total_mb=esim.data_total_mb,
                data_used_mb=esim.data_used_mb,
                days_left=days_left(esim),
                last_synced_at=esim.last_synced_at,
            )
            for esim, customer, title in expiring_rows
        ],
    )
