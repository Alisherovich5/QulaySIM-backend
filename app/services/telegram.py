"""Small server-side Telegram transport for customer support requests."""

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.core.config import settings
from app.schemas import SupportMessageIn


class TelegramNotConfiguredError(RuntimeError):
    """Raised when the environment has not yet been connected to Telegram."""


class TelegramDeliveryError(RuntimeError):
    """Raised when Telegram cannot accept a configured support request."""


def send_support_message(payload: SupportMessageIn, client_ip: str) -> None:
    """Deliver a single support request without exposing bot credentials to clients."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise TelegramNotConfiguredError

    message = "\n".join(
        (
            "📩 QulaySIM — yangi yordam so‘rovi",
            f"Ism: {payload.name}",
            f"Email: {payload.email}",
            f"Telefon: {payload.phone}",
            f"Til: {payload.locale}",
            f"IP: {client_ip}",
            "",
            "Xabar:",
            payload.message,
        )
    )
    body = urlencode(
        {
            "chat_id": settings.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = Request(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=settings.telegram_timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise TelegramDeliveryError
    except (HTTPError, URLError, TimeoutError) as exc:
        raise TelegramDeliveryError from exc
