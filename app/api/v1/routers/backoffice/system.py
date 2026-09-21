"""Support tickets, the Telegram recipients, and the journals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.api.v1.routers.backoffice.orders import code_of
from app.core.errors import NotFoundError, UpstreamError
from app.db.models import (
    AccessFailureLog,
    AccessLog,
    Customer,
    Order,
    OrderItem,
    Plan,
    RowChange,
    Staff,
    SupportNote,
    SupportTicket,
    TelegramRecipient,
)

router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])

State = Literal["new", "answered", "closed"]


class TicketRow(BaseModel):
    id: int
    created_at: datetime
    customer_name: str
    customer_email: str
    phone: str
    subject: str
    state: State
    assignee: str | None


def _subject(message: str) -> str:
    """The first line, trimmed. A list of forty-word messages is unreadable."""
    first = (message or "").strip().splitlines()
    head = first[0] if first else ""
    return head[:80] + ("…" if len(head) > 80 else "") or "(bo‘sh xabar)"


def _state(ticket: SupportTicket) -> State:
    if ticket.state == "closed":
        return "closed"
    return "answered" if ticket.state == "answered" else "new"


@router.get("/tickets")
async def list_tickets(
    session: SessionDep,
    staff: CurrentStaff,
    holat: str = "",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    where = [SupportTicket.state == holat] if holat else []
    rows = (
        await session.execute(
            select(SupportTicket, Staff.first_name, Staff.last_name, Staff.username)
            .outerjoin(Staff, Staff.id == SupportTicket.assignee_id)
            .where(*where)
            .order_by(SupportTicket.created_at.desc())
            .limit(limit)
        )
    ).all()
    counts_raw = (
        await session.execute(
            select(SupportTicket.state, func.count()).group_by(SupportTicket.state)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in counts_raw}
    counts["total"] = sum(counts.values())

    return {
        "items": [
            TicketRow(
                id=t.id,
                created_at=t.created_at,
                customer_name=t.name or t.email.split("@")[0],
                customer_email=t.email,
                phone=t.phone,
                subject=_subject(t.message),
                state=_state(t),
                assignee=(f"{first} {last}".strip() or username) if username else None,
            )
            for t, first, last, username in rows
        ],
        "counts": counts,
    }


class Message(BaseModel):
    id: int
    from_: Literal["customer", "staff"] = Field(serialization_alias="from")
    author: str
    body: str
    at: datetime

    model_config = {"populate_by_name": True}


class TicketDetail(BaseModel):
    id: int
    code: str
    state: State
    customer: dict[str, object]
    messages: list[Message]
    orders: list[dict[str, object]]


@router.get("/tickets/{ticket_id}", response_model=TicketDetail, response_model_by_alias=True)
async def ticket_detail(ticket_id: int, session: SessionDep, staff: CurrentStaff) -> TicketDetail:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError("Murojaat topilmadi")

    notes = (
        await session.execute(
            select(SupportNote, Staff)
            .outerjoin(Staff, Staff.id == SupportNote.author_id)
            .where(SupportNote.ticket_id == ticket_id)
            .order_by(SupportNote.created_at)
        )
    ).all()

    # The account is matched by address, so a ticket sent from an address that
    # never signed up still opens — with no order history, which is itself the
    # answer to "have they bought anything from us".
    customer = None
    if ticket.customer_id:
        customer = await session.get(Customer, ticket.customer_id)
    elif ticket.email:
        customer = (
            (
                await session.execute(
                    select(Customer).where(func.lower(Customer.email) == ticket.email.lower())
                )
            )
            .scalars()
            .first()
        )

    orders: list[dict[str, object]] = []
    if customer is not None:
        rows = (
            await session.execute(
                select(Order, func.min(Plan.title))
                .outerjoin(OrderItem, OrderItem.order_id == Order.id)
                .outerjoin(Plan, Plan.id == OrderItem.plan_id)
                .where(Order.customer_id == customer.id)
                .group_by(Order.id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
        ).all()
        orders = [
            {
                "id": o.id,
                "code": code_of(o),
                "plan_title": title or "—",
                "amount_uzs": float(o.amount_uzs) if o.amount_uzs is not None else None,
                "stage": o.status,
            }
            for o, title in rows
        ]

    messages = [
        Message(
            id=0,
            from_="customer",
            author=ticket.name or ticket.email,
            body=ticket.message,
            at=ticket.created_at,
        )
    ] + [
        Message(
            id=note.id,
            from_="staff",
            author=author.display_name if author else "—",
            body=note.body,
            at=note.created_at,
        )
        for note, author in notes
    ]

    return TicketDetail(
        id=ticket.id,
        code=f"MR-{ticket.id:04d}",
        state=_state(ticket),
        customer={
            "id": customer.id if customer else 0,
            "name": ticket.name or (customer.full_name if customer else "") or ticket.email,
            "email": ticket.email,
            "phone": ticket.phone,
            "since": (customer.created_at if customer else ticket.created_at).isoformat(),
        },
        messages=messages,
        orders=orders,
    )


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/reply", status_code=201)
async def add_note(
    ticket_id: int, payload: NoteIn, session: SessionDep, staff: CurrentStaff
) -> dict[str, str]:
    """Record what was done about this ticket.

    It is a note, not a reply: nothing is sent to the customer from here,
    because this system has no outbound email and pretending otherwise would
    have someone believe an answer went out when it did not. Answering happens
    on the phone or in Telegram; this is where it gets written down.
    """
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError("Murojaat topilmadi")
    session.add(SupportNote(ticket_id=ticket_id, author_id=staff.id, body=payload.body.strip()))
    if ticket.state == "new":
        await session.execute(
            update(SupportTicket)
            .where(SupportTicket.id == ticket_id)
            .values(state="answered", assignee_id=staff.id)
        )
    await session.commit()
    return {"status": "saved"}


@router.post("/tickets/{ticket_id}/close", status_code=204)
async def close_ticket(ticket_id: int, session: SessionDep, staff: CurrentStaff) -> None:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError("Murojaat topilmadi")
    await session.execute(
        update(SupportTicket)
        .where(SupportTicket.id == ticket_id)
        .values(
            state="closed", closed_at=datetime.now(UTC), assignee_id=ticket.assignee_id or staff.id
        )
    )
    await session.commit()


class Recipient(BaseModel):
    id: int
    chat_id: str
    label: str
    is_active: bool


@router.get("/telegram")
async def telegram_recipients(
    session: SessionDep, staff: CurrentStaff
) -> dict[str, list[Recipient]]:
    rows = list(
        (await session.execute(select(TelegramRecipient).order_by(TelegramRecipient.id)))
        .scalars()
        .all()
    )
    return {
        "items": [
            Recipient(id=r.id, chat_id=r.chat_id, label=r.label, is_active=r.is_active)
            for r in rows
        ]
    }


@router.post("/telegram/{recipient_id}/test", status_code=202)
async def telegram_test(
    recipient_id: int, session: SessionDep, staff: CurrentStaff
) -> dict[str, str]:
    """Send one message to one chat.

    Worth its own button: "the bot is configured" and "this chat receives
    messages" are different facts, and the second one is the one that fails —
    somebody leaves the group, the chat id changes, the bot gets blocked.
    """
    recipient = await session.get(TelegramRecipient, recipient_id)
    if recipient is None:
        raise NotFoundError("Qabul qiluvchi topilmadi")

    from app.integrations import telegram

    try:
        who = telegram._escape(staff.display_name)
        await telegram.send_html(
            f"✅ Sinov xabari — <b>{who}</b> backoffice'dan yubordi.",
            chat_id=recipient.chat_id,
        )
    except Exception as exc:
        raise UpstreamError(f"Yuborilmadi: {exc}") from exc
    return {"status": "sent"}


class LoginRow(BaseModel):
    id: int
    at: datetime
    username: str
    ip: str
    ok: bool


@router.get("/journal/logins")
async def login_journal(
    session: SessionDep, staff: CurrentStaff, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, list[LoginRow]]:
    """Successes and refusals in one list, newest first.

    Two tables, because django-axes wrote them that way; merged here because
    what you want to see is the sequence — five refusals then a success is the
    shape that matters, and it is invisible if they are on separate pages.
    """
    ok_rows = (
        (
            await session.execute(
                select(AccessLog).order_by(AccessLog.attempt_time.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    bad_rows = (
        (
            await session.execute(
                select(AccessFailureLog).order_by(AccessFailureLog.attempt_time.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    merged = [
        LoginRow(
            id=row.id * 2,
            at=row.attempt_time,
            username=row.username or "—",
            ip=str(row.ip_address or "—"),
            ok=True,
        )
        for row in ok_rows
    ] + [
        LoginRow(
            id=row.id * 2 + 1,
            at=row.attempt_time,
            username=row.username or "—",
            ip=str(row.ip_address or "—"),
            ok=False,
        )
        for row in bad_rows
    ]
    merged.sort(key=lambda r: r.at, reverse=True)
    return {"items": merged[:limit]}


class ChangeRow(BaseModel):
    id: int
    at: datetime
    who: str
    table: str
    row_pk: str
    op: str
    summary: str


# Columns worth naming in a one-line summary. A dump of every changed field is
# unreadable, and some of them (qr_payload) must not be echoed at all.
_INTERESTING = (
    "status",
    "state",
    "is_active",
    "price_usd",
    "cost_usd",
    "amount_uzs",
    "note",
    "email",
)
_NEVER_SHOWN = ("qr_payload", "qr_image", "password", "hashed_password", "key")


def _summarise(old: dict[str, object] | None, new: dict[str, object] | None) -> str:
    if old is None:
        return "yangi qator"
    if new is None:
        return "o‘chirildi"
    parts = []
    for column in _INTERESTING:
        if column in _NEVER_SHOWN:
            continue
        before, after = old.get(column), new.get(column)
        if column in old and column in new and before != after:
            parts.append(f"{column}: {before} → {after}")
    return ", ".join(parts[:4]) or "o‘zgardi"


@router.get("/journal/changes")
async def change_journal(
    session: SessionDep, staff: CurrentStaff, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, list[ChangeRow]]:
    rows = (
        (await session.execute(select(RowChange).order_by(RowChange.at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "items": [
            ChangeRow(
                id=row.id,
                at=row.at,
                who=row.app_name or row.db_user,
                table=row.tbl,
                row_pk=row.row_pk or "—",
                op=row.op,
                summary=_summarise(row.old_row, row.new_row),
            )
            for row in rows
        ]
    }
