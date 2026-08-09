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


async def send_html(text: str, *, chat_id: str | None = None) -> None:
    """Post pre-formatted HTML to the operations chat.

    Separate from `send_support_message` because the caller here has already
    built its own markup — escaping it again would show the tags. Reports are
    assembled from database values, not from anything a visitor typed.

    Raises rather than returning a flag: the only caller is a Celery task, and a
    report that failed to send should fail visibly in the worker log rather than
    look delivered.
    """
    target = chat_id or settings.telegram_chat_id
    if not settings.telegram_bot_token or not target:
        raise ServiceUnavailableError("Telegram is not configured")

    body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 20] + "\n…"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
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
    except Exception as exc:
        logger.warning("telegram.report_delivery_failed", error=str(exc))
        raise UpstreamError("Telegram delivery failed") from exc


async def send_support_message(
    *, name: str, email: str, phone: str, locale: str, message: str, client_ip: str
) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
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
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        response = await get_client().post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=settings.telegram_timeout_seconds,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("telegram.delivery_failed", error=str(exc))
        raise UpstreamError("Could not deliver your message. Please try again.") from exc
