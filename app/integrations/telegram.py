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
