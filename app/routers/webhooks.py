"""Inbound notifications from external suppliers.

The eSIM Access console should be configured with a URL ending in a long,
secret query token. Its notifications may be delivered as JSON or query-string
parameters, so both formats are accepted here.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.database import get_db
from app.models import Order, OrderItem
from app.services.esim_access_sync import synchronise_order
from app.services.providers.esim_access import EsimAccessError

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _content_from_query(query: dict[str, str]) -> dict[str, Any]:
    content: dict[str, Any] = {}
    for key, value in query.items():
        if key.startswith("content[") and key.endswith("]"):
            content[key[8:-1]] = value
    return content


@router.post("/esimaccess", status_code=202)
async def esimaccess_webhook(
    request: Request,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
):
    expected = settings.esimaccess_webhook_token
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        payload = await request.json()
    except Exception:  # eSIM Access can send form/query-based notifications.
        payload = {}
    query = dict(request.query_params)
    notify_type = str(payload.get("notifyType") or query.get("notifyType") or "")
    content = payload.get("content") or _content_from_query(query)
    order_no = str(content.get("orderNo") or "")

    if notify_type != "ORDER_STATUS" or not order_no:
        return {"received": True, "synchronised": 0}

    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.plan))
        .filter(Order.provider == "esimaccess", Order.provider_order_no == order_no)
        .first()
    )
    if not order:
        # Acknowledge unknown supplier notifications so they are not retried.
        return {"received": True, "synchronised": 0}
    try:
        count = synchronise_order(db, order)
    except EsimAccessError as exc:
        # Profile allocation can still be in progress; return 202 for a safe retry.
        if "200010" in str(exc):
            return {"received": True, "provisioning": True, "synchronised": 0}
        raise HTTPException(status_code=502, detail="Supplier profile synchronisation failed") from exc
    return {"received": True, "synchronised": count}
