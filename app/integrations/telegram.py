"""Support inbox delivery. Credentials stay server-side only."""

from __future__ import annotations

import html

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


def chat_ids() -> list[str]:
    """Every chat a message should reach.

    `TELEGRAM_CHAT_ID` started as one id and stayed a string, so the reports went
    to a single person. A shop has more than one person who needs to know a sale
    happened or that a payment failed, and the honest way to widen that is to
    accept a list rather than to make everyone share one account.

    Comma or whitespace separated, so `123,-100456` and `123 -100456` both work.
    A negative id is a group: adding the bot to a staff group and using its id
    reaches everyone in it and keeps the list in Telegram rather than in `.env`.
    """
    raw = (settings.telegram_chat_id or "").replace(",", " ")
    return [part for part in raw.split() if part]


async def send_html(text: str, *, chat_id: str | None = None) -> None:
    """Post pre-formatted HTML to the operations chat.

    Separate from `send_support_message` because the caller here has already
    built its own markup — escaping it again would show the tags. Reports are
    assembled from database values, not from anything a visitor typed.

    Raises rather than returning a flag: the only caller is a Celery task, and a
    report that failed to send should fail visibly in the worker log rather than
    look delivered.
    """
    targets = [chat_id] if chat_id else chat_ids()
    if not settings.telegram_bot_token or not targets:
        raise ServiceUnavailableError("Telegram is not configured")

    body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 20] + "\n…"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    delivered = 0
    last_error: Exception | None = None
    for target in targets:
        try:
            response = await get_client().post(
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
    if not settings.telegram_bot_token or not chat_ids():
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
