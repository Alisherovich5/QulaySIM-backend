"""Customers, staff, and the search box at the top of every page."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.api.v1.routers.backoffice.orders import code_of
from app.core.errors import NotFoundError
from app.db.models import (
    ESIM,
    Customer,
    Order,
    OrderItem,
    Payment,
    Plan,
    SocialAccount,
    Staff,
    TOTPDevice,
)

router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class CustomerRow(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime
    orders: int
    spent_uzs: float


class CustomerPage(BaseModel):
    items: list[CustomerRow]
    total: int
    page: int
    pages: int


@router.get("/customers", response_model=CustomerPage)
async def list_customers(
    session: SessionDep,
    staff: CurrentStaff,
    q: str = "",
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> CustomerPage:
    where = []
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(Customer.email).like(needle), func.lower(Customer.full_name).like(needle)
            )
        )

    total = (
        await session.execute(
            select(func.count()).select_from(select(Customer.id).where(*where).subquery())
        )
    ).scalar_one()

    # Paid orders only. Counting a pending basket as spend turns a browsing
    # visitor into the biggest customer on the page.
    spend = (
        select(
            Order.customer_id.label("cid"),
            func.count().label("orders"),
            func.coalesce(func.sum(Order.amount_uzs), 0).label("spent"),
        )
        .where(Order.status == "paid")
        .group_by(Order.customer_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Customer, spend.c.orders, spend.c.spent)
            .outerjoin(spend, spend.c.cid == Customer.id)
            .where(*where)
            .order_by(Customer.created_at.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    ).all()

    return CustomerPage(
        items=[
            CustomerRow(
                id=c.id,
                name=c.full_name or c.email.split("@")[0],
                email=c.email,
                created_at=c.created_at,
                orders=int(orders or 0),
                spent_uzs=float(spent or 0),
            )
            for c, orders, spent in rows
        ],
        total=int(total),
        page=page,
        pages=max(1, -(-int(total) // size)),
    )


class CustomerDetail(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime
    sign_in: str
    orders_count: int
    spent_uzs: float
    orders: list[dict[str, object]]
    esims: list[dict[str, object]]
    payments: list[dict[str, object]]
    referred_by: str | None


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
async def customer_detail(
    customer_id: int, session: SessionDep, staff: CurrentStaff
) -> CustomerDetail:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("Mijoz topilmadi")

    orders = (
        await session.execute(
            select(Order, func.min(Plan.title))
            .outerjoin(OrderItem, OrderItem.order_id == Order.id)
            .outerjoin(Plan, Plan.id == OrderItem.plan_id)
            .where(Order.customer_id == customer_id)
            .group_by(Order.id)
            .order_by(Order.created_at.desc())
            .limit(50)
        )
    ).all()
    esims = (
        await session.execute(
            select(ESIM, Plan.title)
            .join(Plan, Plan.id == ESIM.plan_id)
            .where(ESIM.customer_id == customer_id)
            .order_by(ESIM.created_at.desc())
            .limit(50)
        )
    ).all()
    payments = (
        await session.execute(
            select(Payment, Order)
            .join(Order, Order.id == Payment.order_id)
            .where(Order.customer_id == customer_id)
            .order_by(Payment.created_at.desc())
            .limit(50)
        )
    ).all()

    social = (
        (
            await session.execute(
                select(SocialAccount.provider).where(SocialAccount.customer_id == customer_id)
            )
        )
        .scalars()
        .first()
    )
    referrer = None
    if customer.referred_by_id:
        referrer = (
            await session.execute(
                select(Customer.email).where(Customer.id == customer.referred_by_id)
            )
        ).scalar_one_or_none()

    paid = [o for o, _ in orders if o.status == "paid"]
    return CustomerDetail(
        id=customer.id,
        name=customer.full_name or customer.email.split("@")[0],
        email=customer.email,
        created_at=customer.created_at,
        sign_in=social or ("parol" if customer.hashed_password else "—"),
        orders_count=len(paid),
        spent_uzs=float(sum((o.amount_uzs or 0) for o in paid)),
        orders=[
            {
                "id": o.id,
                "code": code_of(o),
                "plan_title": title or "—",
                "amount_uzs": float(o.amount_uzs) if o.amount_uzs is not None else None,
                "stage": o.status,
                "created_at": o.created_at.isoformat(),
            }
            for o, title in orders
        ],
        esims=[
            {
                "id": e.id,
                "iccid": e.iccid,
                "plan_title": title,
                "status": e.status,
                "data_total_mb": e.data_total_mb,
                "data_used_mb": e.data_used_mb,
            }
            for e, title in esims
        ],
        payments=[
            {
                "id": p.id,
                "created_at": p.created_at.isoformat(),
                "order_code": code_of(o),
                "provider": p.method,
                "amount_uzs": float(o.amount_uzs) if o.amount_uzs is not None else None,
            }
            for p, o in payments
        ],
        referred_by=referrer,
    )


class StaffRow(BaseModel):
    id: int
    name: str
    email: str
    role: str
    last_login_at: datetime | None
    two_factor: bool


@router.get("/staff")
async def list_staff(session: SessionDep, staff: CurrentStaff) -> dict[str, list[StaffRow]]:
    people = list(
        (await session.execute(select(Staff).where(Staff.is_staff.is_(True)).order_by(Staff.id)))
        .scalars()
        .all()
    )
    with_totp = set(
        (
            await session.execute(
                select(TOTPDevice.user_id).where(TOTPDevice.confirmed.is_(True)).distinct()
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            StaffRow(
                id=p.id,
                name=p.display_name,
                email=p.email or p.username,
                role=p.role,
                last_login_at=p.last_login,
                two_factor=p.id in with_totp,
            )
            for p in people
        ]
    }


class SearchOut(BaseModel):
    customers: list[dict[str, object]]
    orders: list[dict[str, object]]
    esims: list[dict[str, object]]


@router.get("/search", response_model=SearchOut)
async def search(q: str, session: SessionDep, staff: CurrentStaff) -> SearchOut:
    """One box, three tables. An operator holding a phone has an email, an
    ICCID or an order number, and no idea which page it belongs to."""
    term = q.strip().lower()
    if len(term) < 2:
        return SearchOut(customers=[], orders=[], esims=[])
    needle = f"%{term}%"

    customers = (
        (
            await session.execute(
                select(Customer)
                .where(
                    or_(
                        func.lower(Customer.email).like(needle),
                        func.lower(Customer.full_name).like(needle),
                    )
                )
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    orders = (
        await session.execute(
            select(Order, Customer)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                or_(
                    cast(Order.id, String).like(needle),
                    func.lower(Customer.email).like(needle),
                    func.lower(Order.provider_transaction_id).like(needle),
                )
            )
            .order_by(Order.created_at.desc())
            .limit(10)
        )
    ).all()
    esims = (
        await session.execute(
            select(ESIM, Customer, Plan.title)
            .join(Customer, Customer.id == ESIM.customer_id)
            .join(Plan, Plan.id == ESIM.plan_id)
            .where(
                or_(func.lower(ESIM.iccid).like(needle), func.lower(Customer.email).like(needle))
            )
            .order_by(ESIM.created_at.desc())
            .limit(10)
        )
    ).all()

    return SearchOut(
        customers=[
            {"id": c.id, "name": c.full_name or c.email.split("@")[0], "email": c.email}
            for c in customers
        ],
        orders=[
            {
                "id": o.id,
                "code": code_of(o),
                "customer_name": c.full_name or c.email.split("@")[0],
                "amount_uzs": float(o.amount_uzs) if o.amount_uzs is not None else None,
            }
            for o, c in orders
        ],
        esims=[
            {
                "id": e.id,
                "iccid": e.iccid,
                "customer_name": c.full_name or c.email.split("@")[0],
                "plan_title": title,
            }
            for e, c, title in esims
        ],
    )
