"""What happens when somebody uses the contact form.

Two things now, in this order: the message is written down, then Telegram is
told about it. It used to be Telegram only — which worked right up until the
message arrived while nobody was looking, or the person who saw it was not the
person who could answer, and then there was no record it had ever come in.

Storing first is deliberate. A Telegram outage must not lose the message; a
failed notification is recoverable because the ticket is already in the list.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Customer, SupportTicket
from app.integrations.telegram import send_support_message

logger = get_logger(__name__)


async def submit_support_message(
    *,
    session: AsyncSession,
    name: str,
    email: str,
    phone: str,
    locale: str,
    message: str,
    client_ip: str,
) -> None:
    customer_id = (
        await session.execute(
            select(Customer.id).where(func.lower(Customer.email) == email.lower())
        )
    ).scalar_one_or_none()

    session.add(
        SupportTicket(
            name=name[:120],
            email=email[:254],
            phone=phone[:40],
            locale=locale[:8],
            message=message,
            client_ip=client_ip[:45],
            state="new",
            customer_id=customer_id,
        )
    )
    await session.commit()

    try:
        await send_support_message(
            name=name, email=email, phone=phone, locale=locale, message=message, client_ip=client_ip
        )
    except Exception as exc:  # noqa: BLE001 - the ticket is saved; delivery is best effort
        # Not re-raised: the visitor's message is stored and visible in the
        # backoffice. Failing their form submission because our bot is down
        # would ask them to send it again for nothing.
        logger.warning("support.telegram_failed", error=str(exc))
