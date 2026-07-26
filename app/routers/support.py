from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.schemas import SupportMessageIn, SupportMessageOut
from app.services.telegram import (
    TelegramDeliveryError,
    TelegramNotConfiguredError,
    send_support_message,
)

router = APIRouter(prefix="/api/support", tags=["support"])

_recent_requests: dict[str, float] = {}
_recent_requests_lock = Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",", 1)[0].strip() if forwarded else (request.client.host if request.client else "unknown")


@router.post("/message", response_model=SupportMessageOut, status_code=status.HTTP_202_ACCEPTED)
def create_support_message(payload: SupportMessageIn, request: Request):
    """Forward a customer request to the private support Telegram chat."""
    client_ip = _client_ip(request)
    now = monotonic()
    with _recent_requests_lock:
        last_request = _recent_requests.get(client_ip, 0)
        if now - last_request < settings.support_message_cooldown_seconds:
            raise HTTPException(status_code=429, detail="Please wait before sending another message")
        _recent_requests[client_ip] = now

    try:
        send_support_message(payload, client_ip)
    except TelegramNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Support chat is being configured") from exc
    except TelegramDeliveryError as exc:
        raise HTTPException(status_code=502, detail="Could not deliver your message. Please try again.") from exc

    return SupportMessageOut()
