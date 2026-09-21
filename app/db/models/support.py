"""Support tickets — the one part of the schema this service owns.

Everything else in `app/db/models` mirrors a table Django created. These two are
ours: created by `alembic/versions/0002_support_tickets.py` and touched by
nothing else.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class SupportTicket(Base):
    __tablename__ = "support_ticket"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    locale: Mapped[str] = mapped_column(String(8), default="uz")
    message: Mapped[str] = mapped_column(Text, default="")
    client_ip: Mapped[str] = mapped_column(String(45), default="")
    state: Mapped[str] = mapped_column(String(12), default="new")
    # Filled when the address matches an account, so the ticket page can show
    # what this person has bought without a second search.
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers_customer.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("auth_user.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupportNote(Base):
    """What staff wrote on a ticket. Internal — the customer never sees it."""

    __tablename__ = "support_note"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_ticket.id"))
    author_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("auth_user.id"))
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
