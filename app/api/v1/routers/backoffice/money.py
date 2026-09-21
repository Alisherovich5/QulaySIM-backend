"""Payments, supplier wallets and purchases, and the period report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.api.v1.routers.backoffice.orders import code_of
from app.core.logging import get_logger
from app.db.models import Customer, Order, OrderItem, Payment, Plan, SupplierPurchase

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class PaymentRow(BaseModel):
    id: int
    created_at: datetime
    order_code: str
    provider: str
    transaction_id: str
    amount_uzs: float | None
    amount_usd: float | None
    status: str
    ok: bool


class PaymentPage(BaseModel):
    items: list[PaymentRow]
    total_uzs: float
    count: int


@router.get("/payments", response_model=PaymentPage)
async def list_payments(
    session: SessionDep, staff: CurrentStaff, limit: int = Query(default=100, ge=1, le=500)
) -> PaymentPage:
    rows = (
        await session.execute(
            select(Payment, Order)
            .join(Order, Order.id == Payment.order_id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
        )
    ).all()
    totals = (
        await session.execute(
            select(func.coalesce(func.sum(Order.amount_uzs), 0), func.count())
            .select_from(Payment)
            .join(Order, Order.id == Payment.order_id)
            .where(Payment.status == "success")
        )
    ).one()
    return PaymentPage(
        items=[
            PaymentRow(
                id=p.id,
                created_at=p.created_at,
                order_code=code_of(o),
                provider=p.method,
                transaction_id=p.provider_ref,
                amount_uzs=float(o.amount_uzs) if o.amount_uzs is not None else None,
                amount_usd=float(p.amount),
                status=p.status,
                ok=p.status == "success",
            )
            for p, o in rows
        ],
        total_uzs=float(totals[0]),
        count=int(totals[1]),
    )


class Wallet(BaseModel):
    provider: str
    balance_usd: float
    spent_7d_usd: float
    days_left: int | None
    healthy: bool


async def _spent_7d(session: SessionDep) -> dict[str, Decimal]:
    since = datetime.now(UTC) - timedelta(days=7)
    rows = (
        await session.execute(
            select(SupplierPurchase.provider, func.coalesce(func.sum(OrderItem.unit_cost), 0))
            .join(OrderItem, OrderItem.order_id == SupplierPurchase.order_id)
            .where(SupplierPurchase.created_at >= since, SupplierPurchase.state == "done")
            .group_by(SupplierPurchase.provider)
        )
    ).all()
    return {str(row[0]): Decimal(str(row[1])) for row in rows}


async def _wallets(session: SessionDep) -> list[Wallet]:
    """Live balances, asked of each supplier.

    A cached number is worse than none here: the whole reason to look is that
    somebody is about to spend money and needs to know whether it will go
    through. A supplier that does not answer shows as unhealthy with no
    balance rather than as zero, which would read as "empty".
    """
    from app.services.backoffice.wallets import wallet_balances

    spent = await _spent_7d(session)
    out: list[Wallet] = []
    for provider, balance in (await wallet_balances()).items():
        weekly = spent.get(provider, Decimal("0"))
        daily = weekly / 7 if weekly else Decimal("0")
        days = int(Decimal(str(balance)) / daily) if daily > 0 and balance is not None else None
        out.append(
            Wallet(
                provider=provider,
                balance_usd=float(balance if balance is not None else 0),
                spent_7d_usd=float(weekly),
                days_left=days,
                healthy=balance is not None and (days is None or days >= 7),
            )
        )
    return out


@router.get("/suppliers/wallets", response_model=list[Wallet])
async def wallets(session: SessionDep, staff: CurrentStaff) -> list[Wallet]:
    return await _wallets(session)


@router.get("/suppliers/{provider}/wallet")
async def one_wallet(provider: str, session: SessionDep, staff: CurrentStaff) -> dict[str, float]:
    for wallet in await _wallets(session):
        if wallet.provider == provider:
            return {"balance_usd": wallet.balance_usd}
    return {"balance_usd": 0.0}


class Purchase(BaseModel):
    id: int
    created_at: datetime
    order_code: str
    provider: str
    package_code: str
    cost_usd: float
    state: str
    note: str


@router.get("/suppliers/purchases")
async def purchases(
    session: SessionDep, staff: CurrentStaff, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, list[Purchase]]:
    rows = (
        await session.execute(
            select(SupplierPurchase, Order, func.coalesce(func.sum(OrderItem.unit_cost), 0))
            .join(Order, Order.id == SupplierPurchase.order_id)
            .outerjoin(OrderItem, OrderItem.order_id == Order.id)
            .group_by(SupplierPurchase.id, Order.id)
            .order_by(SupplierPurchase.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            Purchase(
                id=p.id,
                created_at=p.created_at,
                order_code=code_of(o),
                provider=p.provider,
                package_code=p.package_code,
                cost_usd=float(cost),
                state=p.state,
                note=p.note,
            )
            for p, o, cost in rows
        ]
    }


class Report(BaseModel):
    period_days: int
    orders: int
    revenue_uzs: float
    supplier_cost_usd: float
    profit_uzs: float
    new_customers: int
    top: list[dict[str, object]]


@router.get("/reports", response_model=Report)
async def report(
    session: SessionDep, staff: CurrentStaff, days: int = Query(default=7, ge=1, le=365)
) -> Report:
    """What the period was worth.

    Revenue counts paid orders only, and complimentary ones are excluded from
    it while their cost still lands in supplier_cost — a giveaway is spend, not
    a sale, and folding it into revenue is how a free eSIM starts looking
    profitable.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    revenue_raw, order_count = (
        await session.execute(
            select(func.coalesce(func.sum(Order.amount_uzs), 0), func.count()).where(
                Order.status == "paid", Order.paid_at >= since, Order.is_complimentary.is_(False)
            )
        )
    ).one()
    revenue = revenue_raw or 0
    cost_raw = (
        await session.execute(
            select(func.coalesce(func.sum(OrderItem.unit_cost), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status == "paid", Order.paid_at >= since)
        )
    ).scalar_one()
    cost = cost_raw or 0
    new_customers = (
        await session.execute(select(func.count()).where(Customer.created_at >= since))
    ).scalar_one()

    top = (
        await session.execute(
            select(
                Plan.title,
                func.count(OrderItem.id),
                func.coalesce(func.sum(Order.amount_uzs), 0),
            )
            .join(OrderItem, OrderItem.plan_id == Plan.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.status == "paid", Order.paid_at >= since, Order.is_complimentary.is_(False)
            )
            .group_by(Plan.title)
            .order_by(func.count(OrderItem.id).desc())
            .limit(8)
        )
    ).all()

    from app.services.currency import usd_to_uzs

    rate = Decimal(str((await usd_to_uzs())["usd_to_uzs"]))
    profit = Decimal(str(revenue)) - Decimal(str(cost)) * rate

    return Report(
        period_days=days,
        orders=int(order_count),
        revenue_uzs=float(revenue),
        supplier_cost_usd=float(cost),
        profit_uzs=float(profit),
        new_customers=int(new_customers),
        top=[
            {"name": name, "orders": int(count), "revenue_uzs": float(amount)}
            for name, count, amount in top
        ],
    )
