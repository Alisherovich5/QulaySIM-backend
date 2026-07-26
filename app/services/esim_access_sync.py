"""Map eSIM Access profiles into QulaySIM's provider-neutral eSIM records."""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import ESIM, Order, OrderItem, Plan
from app.services.esim import render_qr_data_url
from app.services.providers.esim_access import EsimAccessClient


def _parse_supplier_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_status(provider_status: str) -> str:
    if provider_status == "IN_USE":
        return "active"
    if provider_status in {"USED_UP", "UNUSED_EXPIRED", "USED_EXPIRED", "CANCEL", "REVOKE"}:
        return "expired"
    return "pending"


def _plan_by_package_code(order: Order) -> dict[str, Plan]:
    result: dict[str, Plan] = {}
    for item in order.items:
        if item.plan.provider == "esimaccess" and item.plan.provider_package_code:
            result[item.plan.provider_package_code] = item.plan
    return result


def synchronise_order(db: Session, order: Order, client: EsimAccessClient | None = None) -> int:
    """Fetch an allocated supplier order and upsert its eSIM profiles.

    eSIM Access can return `200010` while SM-DP+ is allocating profiles. The
    caller should treat that as still provisioning and retry after ORDER_STATUS
    webhook instead of failing a customer order.
    """
    if order.provider != "esimaccess" or not order.provider_order_no:
        return 0

    if not order.items:
        order = (
            db.query(Order)
            .options(joinedload(Order.items).joinedload(OrderItem.plan))
            .filter(Order.id == order.id)
            .one()
        )

    response = (client or EsimAccessClient()).query_profiles(order_no=order.provider_order_no)
    profiles: list[dict[str, Any]] = response.get("obj", {}).get("esimList", [])
    plans = _plan_by_package_code(order)
    created_or_updated = 0

    for profile in profiles:
        transaction_no = str(profile.get("esimTranNo") or "")
        iccid = str(profile.get("iccid") or "")
        activation_code = str(profile.get("ac") or "")
        package_list = profile.get("packageList") or []
        package_code = str(package_list[0].get("packageCode") or "") if package_list else ""
        plan = plans.get(package_code)
        if not transaction_no or not iccid or not activation_code or not plan:
            # Do not create an incomplete or incorrectly mapped customer eSIM.
            continue

        existing = (
            db.query(ESIM)
            .filter(ESIM.provider == "esimaccess", ESIM.provider_esim_tran_no == transaction_no)
            .first()
        )
        provider_status = str(profile.get("esimStatus") or "")
        total_bytes = int(profile.get("totalVolume") or 0)
        used_bytes = int(profile.get("orderUsage") or 0)
        values = {
            "plan_id": plan.id,
            "iccid": iccid,
            "qr_payload": activation_code,
            "qr_image": render_qr_data_url(activation_code),
            "provider": "esimaccess",
            "provider_esim_tran_no": transaction_no,
            "provider_status": provider_status,
            "provider_qr_url": str(profile.get("qrCodeUrl") or ""),
            "status": _local_status(provider_status),
            "data_total_mb": ceil(total_bytes / (1024 * 1024)) if total_bytes else 0,
            "data_used_mb": ceil(used_bytes / (1024 * 1024)) if used_bytes else 0,
            "validity_days": int(profile.get("totalDuration") or plan.validity_days),
            "expires_at": _parse_supplier_date(profile.get("expiredTime")),
        }
        if existing:
            for field, value in values.items():
                setattr(existing, field, value)
        else:
            db.add(
                ESIM(
                    order_id=order.id,
                    customer_id=order.customer_id,
                    **values,
                )
            )
        created_or_updated += 1

    if profiles:
        order.provider_status = str(profiles[0].get("esimStatus") or "GOT_RESOURCE")
    db.commit()
    return created_or_updated
