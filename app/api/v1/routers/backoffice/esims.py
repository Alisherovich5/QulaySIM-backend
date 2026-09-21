"""eSIMs: what was issued, how much is left on it, and the QR itself."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.api.v1.routers.backoffice.orders import EsimOut, code_of, days_left
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models import ESIM, Customer, Order, Plan

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])


class EsimPage(BaseModel):
    items: list[EsimOut]
    total: int
    page: int
    pages: int
    counts: dict[str, int]


class EsimDetail(EsimOut):
    order_code: str
    provider: str
    qr_image: str
    qr_payload: str
    created_at: datetime


def _live(status: str, expires_at: datetime | None) -> str:
    """What to call it on screen.

    The stored status lags: a profile expires by the clock, not by anybody
    running an update, so a row still marked active three days after its
    validity ran out would otherwise be shown as fine.
    """
    if status == "active" and expires_at is not None and expires_at <= datetime.now(UTC):
        return "expired"
    return status


@router.get("/esims", response_model=EsimPage)
async def list_esims(
    session: SessionDep,
    staff: CurrentStaff,
    holat: str = "",
    q: str = "",
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> EsimPage:
    where = []
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(ESIM.iccid).like(needle),
                func.lower(Customer.email).like(needle),
                func.lower(Customer.full_name).like(needle),
            )
        )

    joined = (
        select(ESIM, Customer, Plan.title)
        .join(Customer, Customer.id == ESIM.customer_id)
        .join(Plan, Plan.id == ESIM.plan_id)
        .where(*where)
    )
    total = (
        await session.execute(
            select(func.count()).select_from(
                select(ESIM.id)
                .join(Customer, Customer.id == ESIM.customer_id)
                .where(*where)
                .subquery()
            )
        )
    ).scalar_one()

    counts_raw = (
        await session.execute(select(ESIM.status, func.count()).group_by(ESIM.status))
    ).all()
    counts = {str(row[0]): int(row[1]) for row in counts_raw}
    counts["total"] = sum(counts.values())

    rows = (
        await session.execute(
            joined.order_by(ESIM.created_at.desc()).limit(size).offset((page - 1) * size)
        )
    ).all()

    items = [
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
        for esim, customer, title in rows
    ]
    if holat:
        items = [item for item in items if item.status == holat]
    return EsimPage(
        items=items,
        total=int(total),
        page=page,
        pages=max(1, -(-int(total) // size)),
        counts=counts,
    )


@router.get("/esims/{esim_id}", response_model=EsimDetail)
async def esim_detail(esim_id: int, session: SessionDep, staff: CurrentStaff) -> EsimDetail:
    """Includes the QR.

    The payload is stored encrypted and comes back decrypted by the column type,
    which is exactly what this page is for — the operator has somebody on the
    phone who cannot install their eSIM. It is deliberately not in the list
    response: one profile at a time, behind a sign-in, and the read is in the
    audit log like any other.
    """
    row = (
        await session.execute(
            select(ESIM, Customer, Plan.title, Order)
            .join(Customer, Customer.id == ESIM.customer_id)
            .join(Plan, Plan.id == ESIM.plan_id)
            .join(Order, Order.id == ESIM.order_id)
            .where(ESIM.id == esim_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("eSIM topilmadi")
    esim, customer, title, order = row

    logger.info("backoffice.qr_read", esim_id=esim.id, staff=staff.username)
    image = esim.qr_image or ""
    if image and not image.startswith("data:"):
        image = f"data:image/png;base64,{image}"

    return EsimDetail(
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
        order_code=code_of(order),
        provider=esim.provider,
        qr_image=image,
        qr_payload=esim.qr_payload,
        created_at=esim.created_at,
    )


@router.post("/esims/{esim_id}/refresh", status_code=202)
async def refresh_esim(esim_id: int, session: SessionDep, staff: CurrentStaff) -> dict[str, str]:
    """Ask the supplier what this profile's usage actually is, right now.

    The scheduled sweep runs on its own clock; when a customer is on the phone
    saying their data is gone, "it refreshes every few hours" is not an answer.
    """
    esim = await session.get(ESIM, esim_id)
    if esim is None:
        raise NotFoundError("eSIM topilmadi")

    from app.workers.tasks.maintenance import refresh_esim_usage

    refresh_esim_usage.delay()
    return {"status": "queued"}
