"""Support inbox delivery. Credentials stay server-side only."""

from __future__ import annotations

import asyncio
import html

import httpx

from app.core.config import settings
from app.core.errors import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.integrations.http import get_client

logger = get_logger(__name__)


def _escape(value: str) -> str:
    return html.escape(value.strip())[:2000]


#: Telegram rejects anything longer, and truncating loudly beats a delivery that
#: silently fails once the shop is busy enough for the report to grow.
MAX_MESSAGE_CHARS = 4096


def env_chat_ids() -> list[str]:
    """The fallback list, from `TELEGRAM_CHAT_ID`.

    Comma or whitespace separated, so `123,-100456` and `123 -100456` both work.
    Kept as the fallback rather than removed: it is what makes the bot able to
    speak before anyone has opened the admin, and it is the only route left if
    the database is unreachable — which is exactly when an alert matters most.
    """
    raw = (settings.telegram_chat_id or "").replace(",", " ")
    return [part for part in raw.split() if part]


async def _recipients_from(session: object) -> list[str] | None:
    """The active chat ids on one session, or None if they cannot be read."""
    from sqlalchemy import select

    from app.db.models import TelegramRecipient

    try:
        rows = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(TelegramRecipient.chat_id).where(TelegramRecipient.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - falls back rather than failing
        logger.warning("telegram.recipients_unreadable", error=str(exc))
        return None
    return [str(r) for r in rows] or None


async def chat_ids() -> list[str]:
    """Every chat a message should reach.

    The list lives in the admin, because adding a colleague used to mean an SSH
    session and an `.env` edit — so in practice nobody was ever added. Rows the
    operator switched off are skipped without being deleted.

    Falls back to the environment when the table is empty or cannot be read. A
    reporting bot that goes silent because a query failed is worse than one that
    writes to a slightly stale list, and the database being down is precisely the
    moment somebody should be told something.
    """
    from app.db.session import session_scope

    try:
        async with session_scope() as session:
            found = await _recipients_from(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram.recipients_unreadable", error=str(exc))
        found = None
    return found or env_chat_ids()


async def send_html(
    text: str,
    *,
    chat_id: str | None = None,
    targets: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Post pre-formatted HTML to the operations chat.

    Separate from `send_support_message` because the caller here has already
    built its own markup — escaping it again would show the tags. Reports are
    assembled from database values, not from anything a visitor typed.

    Raises rather than returning a flag: the only caller is a Celery task, and a
    report that failed to send should fail visibly in the worker log rather than
    look delivered.
    """
    if chat_id:
        targets = [chat_id]
    elif targets is None:
        targets = await chat_ids()
    if not settings.telegram_bot_token or not targets:
        raise ServiceUnavailableError("Telegram is not configured")

    body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 20] + "\n…"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    delivered = 0
    last_error: Exception | None = None
    for target in targets:
        try:
            response = await (client or get_client()).post(
                url,
                json={
                    "chat_id": target,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=settings.telegram_timeout_seconds,
            )
            response.raise_for_status()
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - reported per recipient below
            last_error = exc
            # One person having blocked the bot must not stop the report reaching
            # everyone else, so this is logged and the loop continues.
            logger.warning("telegram.delivery_failed_for_chat", chat_id=target, error=str(exc))

    if delivered == 0:
        raise UpstreamError("Telegram delivery failed") from last_error


async def send_photo(
    image: bytes,
    *,
    caption: str,
    filename: str = "qr.png",
    targets: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Post an image to the operations chat.

    This exists for one job: getting a QR code onto the phone of the person who
    is going to hand it over. The customer reads their QR in their account on
    the site; when they cannot — the account is under a different address, the
    order was a manual grant, they are on the phone right now — the operator
    needs the picture where they are already typing, which is Telegram.

    The caption is escaped here because it carries a customer's email and plan
    title, i.e. text that came from outside.
    """
    targets = targets if targets is not None else await chat_ids()
    if not settings.telegram_bot_token or not targets:
        raise ServiceUnavailableError("Telegram is not configured")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    delivered = 0
    last_error: Exception | None = None
    for target in targets:
        try:
            response = await (client or get_client()).post(
                url,
                data={"chat_id": target, "caption": caption[:1024], "parse_mode": "HTML"},
                files={"photo": (filename, image, "image/png")},
                timeout=settings.telegram_timeout_seconds,
            )
            response.raise_for_status()
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - reported per recipient below
            last_error = exc
            logger.warning("telegram.photo_failed_for_chat", chat_id=target, error=str(exc))

    if delivered == 0:
        raise UpstreamError("Telegram photo delivery failed") from last_error


def send_html_blocking(text: str) -> None:
    """`send_html`, for a Celery task — which has no event loop of its own.

    `asyncio.run()` opens a NEW loop on every call, and both of the things
    `send_html` reaches for are process-global and bound to whichever loop
    created them: the SQLAlchemy engine behind the recipient list, and the
    shared httpx client. So the second call in a worker process fails with
    "got Future … attached to a different loop", then "Event loop is closed" —
    which is exactly what swallowed the sale announcement for order #129 while
    the customer's two eSIMs were delivered normally.

    This owns the loop, and gives that loop its own engine and its own client,
    both disposed of when it returns. Same reasoning as `task_sessions()`,
    applied to the other half of the problem.
    """

    asyncio.run(_send_html_standalone(text))


async def _send_html_standalone(text: str) -> None:
    from app.db.session import task_sessions

    targets: list[str] | None = None
    try:
        async with task_sessions() as factory, factory() as session:
            targets = await _recipients_from(session)
    except Exception as exc:  # noqa: BLE001 - the env list is the fallback
        logger.warning("telegram.recipients_unreadable", error=str(exc))
    targets = targets or env_chat_ids()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "QulaySIM/2.0"},
        follow_redirects=True,
    ) as client:
        await send_html(text, targets=targets, client=client)


async def send_support_message(
    *, name: str, email: str, phone: str, locale: str, message: str, client_ip: str
) -> None:
    """Deliver a support request to whoever is on the operations list.

    Delegates the sending to `send_html` so it reaches every configured chat.
    Doing its own single-recipient POST meant a list like "123,-100456" was passed
    to Telegram as one id and rejected — support requests would have stopped
    arriving the moment a second person was added.

    Every field is escaped here because all of them are typed by a stranger, and
    the body is HTML by the time it reaches Telegram.
    """
    if not settings.telegram_bot_token or not await chat_ids():
        raise ServiceUnavailableError("Support chat is being configured")

    body = (
        "<b>New QulaySIM support request</b>\n\n"
        f"<b>Name:</b> {_escape(name)}\n"
        f"<b>Email:</b> {_escape(email)}\n"
        f"<b>Phone:</b> {_escape(phone)}\n"
        f"<b>Locale:</b> {_escape(locale)}\n"
        f"<b>IP:</b> {_escape(client_ip)}\n\n"
        f"{_escape(message)}"
    )
    try:
        await send_html(body)
    except Exception as exc:
        # The customer is told their message did not go through; the reason is
        # already in the log with the chat it failed for.
        raise UpstreamError("Could not deliver your message. Please try again.") from exc
