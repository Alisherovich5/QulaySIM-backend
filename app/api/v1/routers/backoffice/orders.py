"""Orders: the list the day starts from, one order in full, and the two
recoveries an operator can run without opening a shell."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.core.errors import ConflictError, NotFoundError, UpstreamError
from app.db.models import (
    ESIM,
    Customer,
    Order,
    OrderItem,
    Payment,
    Plan,
    SupplierPurchase,
)
from app.integrations import telegram

router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])

Stage = Literal["pending", "paid", "delivered", "failed"]


class OrderRow(BaseModel):
    id: int
    code: str
    customer_name: str
    customer_email: str
    plan_title: str
    amount_uzs: float | None
    stage: Stage
    failure: str
    is_topup: bool
    created_at: datetime


class PaymentOut(BaseModel):
    provider: str
    transaction_id: str
    amount_uzs: float | None
    status: str
    paid_at: datetime | None


class SupplierOut(BaseModel):
    provider: str
    package_code: str
    cost_usd: float
    state: str
    note: str


class EsimOut(BaseModel):
    id: int
    iccid: str
    customer_name: str
    customer_email: str
    plan_title: str
    status: str
    data_total_mb: int
    data_used_mb: int
    days_left: int | None
    last_synced_at: datetime | None


class Beat(BaseModel):
    at: datetime
    label: str
    detail: str
    ok: bool


class OrderDetail(OrderRow):
    customer_id: int
    payment: PaymentOut | None
    supplier: SupplierOut | None
    esim: EsimOut | None
    timeline: list[Beat]


class Paged(BaseModel):
    items: list[OrderRow]
    total: int
    page: int
    pages: int
    counts: dict[str, int]


def code_of(order: Order) -> str:
    return f"QS-{order.id:05d}"


def stage_of(order: Order, *, has_esim: bool, topup_done: bool, is_topup: bool) -> Stage:
    """The four words an operator actually uses, derived rather than stored.

    `orders_order.status` says paid/pending/cancelled; whether the customer
    RECEIVED anything lives in another table entirely. Collapsing both into one
    word here is the whole point of the list: "paid" on this screen means paid
    and still waiting, which is the row that needs a human.
    """
    if order.status == "cancelled":
        return "failed"
    if order.status != "paid":
        return "pending"
    if is_topup:
        return "delivered" if topup_done else "paid"
    return "delivered" if has_esim else "paid"


def days_left(esim: ESIM) -> int | None:
    if esim.expires_at is None:
        return None
    return max(0, (esim.expires_at - datetime.now(UTC)).days)


async def _rows(session: SessionDep, orders: list[Order]) -> dict[int, dict[str, object]]:
    """Everything the list needs that does not live on the order itself.

    One query per fact rather than per row: the orders page used to be the
    slowest thing in the admin because Django walked a relation for each line.
    """
    ids = [o.id for o in orders]
    if not ids:
        return {}

    titles: dict[int, str] = {
        int(row[0]): str(row[1])
        for row in (
            await session.execute(
                select(OrderItem.order_id, func.min(Plan.title))
                .join(Plan, Plan.id == OrderItem.plan_id)
                .where(OrderItem.order_id.in_(ids))
                .group_by(OrderItem.order_id)
            )
        ).all()
    }
    lines = {
        row[0]: (row[1], row[2])
        for row in (
            await session.execute(
                select(
                    OrderItem.order_id,
                    func.bool_or(OrderItem.esim_id.isnot(None)),
                    func.bool_and(OrderItem.topup_applied_at.isnot(None)),
                )
                .where(OrderItem.order_id.in_(ids))
                .group_by(OrderItem.order_id)
            )
        ).all()
    }
    esims = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(ESIM.order_id, func.count())
                .where(ESIM.order_id.in_(ids))
                .group_by(ESIM.order_id)
            )
        ).all()
    }
    notes = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(SupplierPurchase.order_id, func.max(SupplierPurchase.note))
                .where(SupplierPurchase.order_id.in_(ids), SupplierPurchase.state != "done")
                .group_by(SupplierPurchase.order_id)
            )
        ).all()
    }
    return {
        oid: {
            "title": titles.get(oid, "—"),
            "is_topup": bool(lines.get(oid, (False, False))[0]),
            "topup_done": bool(lines.get(oid, (False, False))[1]),
            "has_esim": esims.get(oid, 0) > 0,
            "note": notes.get(oid) or "",
        }
        for oid in ids
    }


def _shape(order: Order, extra: dict[str, object]) -> OrderRow:
    is_topup = bool(extra["is_topup"])
    stage = stage_of(
        order,
        has_esim=bool(extra["has_esim"]),
        topup_done=bool(extra["topup_done"]),
        is_topup=is_topup,
    )
    return OrderRow(
        id=order.id,
        code=code_of(order),
        customer_name=order.customer.full_name or order.customer.email.split("@")[0],
        customer_email=order.customer.email,
        plan_title=str(extra["title"]),
        amount_uzs=float(order.amount_uzs) if order.amount_uzs is not None else None,
        stage=stage,
        failure=str(extra["note"]) if stage in ("failed", "paid") else "",
        is_topup=is_topup,
        created_at=order.created_at,
    )


async def _stage_counts(session: SessionDep) -> dict[str, int]:
    """How many orders are in each of the four states, over the whole table.

    The tiles above the list must not count only the page being looked at — the
    number an operator acts on is "how many are stuck", not "how many are stuck
    among the fifty I can see". Derived in SQL for the same reason the list is:
    delivery is recorded in another table, so `status` alone cannot say it.
    """
    delivered = select(ESIM.order_id).where(ESIM.order_id == Order.id)
    topup_open = select(OrderItem.id).where(
        OrderItem.order_id == Order.id,
        OrderItem.esim_id.isnot(None),
        OrderItem.topup_applied_at.is_(None),
    )
    total, cancelled, paid, done = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Order.status == "cancelled"),
                func.count().filter(Order.status == "paid"),
                func.count().filter(
                    Order.status == "paid", delivered.exists(), ~topup_open.exists()
                ),
            )
        )
    ).one()
    return {
        "total": int(total),
        "failed": int(cancelled),
        "delivered": int(done),
        # Paid and not yet delivered — the row that needs somebody.
        "pending": int(paid) - int(done),
    }


@router.get("/orders", response_model=Paged)
async def list_orders(
    session: SessionDep,
    staff: CurrentStaff,
    stage: str = "",
    q: str = "",
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> Paged:
    where = []
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(Customer.email).like(needle),
                func.lower(Customer.full_name).like(needle),
                cast(Order.id, String).like(needle),
            )
        )

    base = select(Order).join(Customer, Customer.id == Order.customer_id).where(*where)
    total = (
        await session.execute(
            select(func.count()).select_from(
                select(Order.id)
                .join(Customer, Customer.id == Order.customer_id)
                .where(*where)
                .subquery()
            )
        )
    ).scalar_one()

    rows = list(
        (
            await session.execute(
                base.options(selectinload(Order.customer))
                .order_by(Order.created_at.desc())
                .limit(size)
                .offset((page - 1) * size)
            )
        )
        .scalars()
        .all()
    )
    extra = await _rows(session, rows)
    items = [_shape(order, extra[order.id]) for order in rows]
    if stage:
        items = [item for item in items if item.stage == stage]
    return Paged(
        items=items,
        total=int(total),
        page=page,
        pages=max(1, -(-int(total) // size)),
        counts=await _stage_counts(session),
    )


async def _load(session: SessionDep, order_id: int) -> Order:
    order = (
        await session.execute(
            select(Order).options(selectinload(Order.customer)).where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Buyurtma topilmadi")
    return order


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def order_detail(order_id: int, session: SessionDep, staff: CurrentStaff) -> OrderDetail:
    order = await _load(session, order_id)
    extra = (await _rows(session, [order]))[order.id]
    row = _shape(order, extra)

    payment = (
        (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == order.id)
                .order_by(Payment.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    purchase = (
        (
            await session.execute(
                select(SupplierPurchase)
                .where(SupplierPurchase.order_id == order.id)
                .order_by(SupplierPurchase.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    esim = (
        (
            await session.execute(
                select(ESIM).where(ESIM.order_id == order.id).order_by(ESIM.id.desc())
            )
        )
        .scalars()
        .first()
    )

    spent = (
        await session.execute(
            select(func.coalesce(func.sum(OrderItem.unit_cost), 0)).where(
                OrderItem.order_id == order.id
            )
        )
    ).scalar_one()

    esim_out = None
    if esim is not None:
        plan_title = (
            await session.execute(select(Plan.title).where(Plan.id == esim.plan_id))
        ).scalar_one_or_none() or "—"
        esim_out = EsimOut(
            id=esim.id,
            iccid=esim.iccid,
            customer_name=row.customer_name,
            customer_email=row.customer_email,
            plan_title=plan_title,
            status=esim.status,
            data_total_mb=esim.data_total_mb,
            data_used_mb=esim.data_used_mb,
            days_left=days_left(esim),
            last_synced_at=esim.last_synced_at,
        )

    timeline = [
        Beat(at=order.created_at, label="Buyurtma yaratildi", detail=row.plan_title, ok=True)
    ]
    if order.paid_at:
        timeline.append(
            Beat(
                at=order.paid_at,
                label="To‘lov o‘tdi",
                detail=f"{order.provider} · {order.provider_transaction_id or '—'}",
                ok=True,
            )
        )
    if purchase is not None:
        timeline.append(
            Beat(
                at=purchase.updated_at,
                label=f"Ta’minotchi: {purchase.provider}",
                detail=purchase.note or purchase.state,
                ok=purchase.state == "done",
            )
        )
    if esim is not None:
        timeline.append(Beat(at=esim.created_at, label="eSIM berildi", detail=esim.iccid, ok=True))

    return OrderDetail(
        **row.model_dump(),
        customer_id=order.customer_id,
        payment=(
            PaymentOut(
                provider=payment.method,
                transaction_id=payment.provider_ref,
                amount_uzs=float(order.amount_uzs) if order.amount_uzs is not None else None,
                status=payment.status,
                paid_at=order.paid_at,
            )
            if payment is not None
            else None
        ),
        supplier=(
            SupplierOut(
                provider=purchase.provider,
                package_code=purchase.package_code,
                cost_usd=float(spent or 0),
                state=purchase.state,
                note=purchase.note,
            )
            if purchase is not None
            else None
        ),
        esim=esim_out,
        timeline=sorted(timeline, key=lambda b: b.at),
    )


@router.post("/orders/{order_id}/retry", status_code=202)
async def retry_order(order_id: int, session: SessionDep, staff: CurrentStaff) -> dict[str, str]:
    """Run fulfilment again for an order that was paid and never delivered.

    Handed to the same Celery task the payment callback uses, so the retry takes
    the identical path — including the supplier-purchase claim that stops a
    double buy. Doing it any other way is how you pay twice for one eSIM.
    """
    order = await _load(session, order_id)
    if order.status != "paid":
        raise ConflictError("Faqat to‘langan buyurtmani qayta yuborish mumkin")

    from app.workers.tasks.provisioning import fulfil_paid_order

    fulfil_paid_order.delay(order.id)
    return {"status": "queued"}


@router.post("/orders/{order_id}/send-qr", status_code=202)
async def send_qr(order_id: int, session: SessionDep, staff: CurrentStaff) -> dict[str, str]:
    """Push the QR image into the operations chat.

    There is no outbound email in this system, and the customer's own copy is
    behind a sign-in they may not have. What actually happens when someone is
    stuck is that an operator sends the picture over Telegram — so this puts it
    there, captioned with who it belongs to, instead of leaving them to
    screenshot a browser tab.
    """
    order = await _load(session, order_id)
    esim = (
        (
            await session.execute(
                select(ESIM).where(ESIM.order_id == order.id).order_by(ESIM.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if esim is None:
        raise ConflictError("Bu buyurtmada hali eSIM yo‘q")

    payload = esim.qr_image or ""
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        image = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpstreamError("QR rasmi o‘qilmadi") from exc
    if not image:
        raise ConflictError("QR rasmi saqlanmagan")

    caption = (
        f"<b>{code_of(order)}</b> · {telegram._escape(order.customer.email)}\n"
        f"ICCID <code>{telegram._escape(esim.iccid)}</code>\n"
        f"{telegram._escape(staff.display_name)} yubordi"
    )
    await telegram.send_photo(image, caption=caption, filename=f"qulaysim-{esim.iccid}.png")
    return {"status": "sent"}
