"""Supplier callbacks.

The previous handler was `async def` but performed synchronous database and
HTTP work inline, blocking the event loop for the whole supplier round trip.
This version only authenticates, records, and hands the work to Celery — the
response is immediate and the retry policy lives in the worker.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Query, Request, status

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.repositories import orders as order_repo

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


def _content_from_query(query: dict[str, str]) -> dict[str, Any]:
    return {k[8:-1]: v for k, v in query.items() if k.startswith("content[") and k.endswith("]")}


@router.post("/esimaccess", status_code=status.HTTP_202_ACCEPTED)
async def esimaccess_webhook(
    request: Request,
    session: SessionDep,
    token: str = Query(default=""),
) -> dict[str, Any]:
    expected = settings.esimaccess_webhook_token
    if not expected or not secrets.compare_digest(token, expected):
        logger.warning("webhook.bad_token")
        raise AuthenticationError("Invalid webhook token")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — supplier may send form/query encoded
        payload = {}

    query = dict(request.query_params)
    notify_type = str(payload.get("notifyType") or query.get("notifyType") or "")
    content = payload.get("content") or _content_from_query(query)
    order_no = str(content.get("orderNo") or "")

    if notify_type != "ORDER_STATUS" or not order_no:
        return {"received": True, "queued": False}

    order = await order_repo.find_by_provider_order_no(session, order_no)
    if order is None:
        # Acknowledge unknown notifications so the supplier stops retrying.
        logger.info("webhook.unknown_order", order_no=order_no)
        return {"received": True, "queued": False}

    from app.workers.tasks.provisioning import synchronise_supplier_order

    synchronise_supplier_order.delay(order.id)
    logger.info("webhook.queued", order_id=order.id, order_no=order_no)
    return {"received": True, "queued": True}
