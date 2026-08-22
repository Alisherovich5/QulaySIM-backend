"""The single ATMOS-facing route: their Callback API knocks here.

There is deliberately no "create checkout" endpoint — the payment link is
minted inside place_order like Payme's, so the storefront needs no knowledge
of which provider is live.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.core.ratelimit import client_ip
from app.services import atmos as service

logger = get_logger(__name__)

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.post("/atmos/callback")
async def atmos_callback(request: Request, session: SessionDep) -> dict[str, Any]:
    """Always HTTP 200; the verdict travels in the body's status field.

    ATMOS treats anything but a clean 200 as "ask again later", so transport
    errors would only earn us retries — refusals belong in the payload.
    """
    # Read the body before judging the caller. Nothing here acts on it — the
    # address still decides whether the callback is processed — but it decides
    # whether a human is woken: a refusal only matters if somebody may have been
    # charged, and an empty probe from an internet scanner charges nobody. An
    # alarm that fires on scanner traffic is an alarm that stops being read.
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    looks_like_a_payment = isinstance(payload, dict) and bool(
        payload.get("transaction_id") and payload.get("account")
    )

    ip = client_ip(request)
    if not service.caller_allowed(ip):
        # The signature alone would catch a forgery; the doc still demands the
        # source-range check, and it costs the attacker information to learn
        # nothing more than "no".
        logger.warning("atmos.callback_bad_ip", ip=ip, payment=looks_like_a_payment)
        if looks_like_a_payment:
            # This is the one that cost a customer their eSIM: the address
            # changed the day Cloudflare went in front, the callback was
            # refused, and the only trace was this line.
            await service.alarm_bad_ip(ip)
        return {"status": 0, "message": "Forbidden"}

    if not isinstance(payload, dict):
        return {"status": 0, "message": "Malformed callback"}

    return await service.handle_callback(session, payload)
