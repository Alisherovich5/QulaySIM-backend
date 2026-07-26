"""The endpoint Payme calls.

One route, JSON-RPC over POST. It is public by necessity — Payme's servers
reach it directly — so the Basic key is the only thing standing in front of
it, and an unauthorised call must be indistinguishable from a wrong key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import ORJSONResponse

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.payme import (
    INSUFFICIENT_PRIVILEGES,
    METHOD_NOT_FOUND,
    authorised,
    error,
)
from app.services import payme as service

router = APIRouter(prefix="/api/payments", tags=["payments"])
logger = get_logger(__name__)


@router.post("/payme")
async def payme_rpc(
    request: Request,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> ORJSONResponse:
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a protocol error, not a crash
        payload = {}

    request_id = payload.get("id")

    keys = (settings.payme_merchant_key, settings.payme_test_key)
    if not authorised(authorization, keys):
        logger.warning("payme.unauthorised", method=payload.get("method"))
        # Always 200: Payme reads the JSON-RPC error, not the HTTP status.
        return ORJSONResponse(error(request_id, INSUFFICIENT_PRIVILEGES))

    method = payload.get("method")
    if not isinstance(method, str):
        return ORJSONResponse(error(request_id, METHOD_NOT_FOUND, "method"))

    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}

    logger.info("payme.request", method=method)
    body = await service.dispatch(session, request_id, method, params)
    return ORJSONResponse(body)
