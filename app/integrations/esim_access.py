"""Small, synchronous client for the eSIM Access Partner API.

The client intentionally returns the upstream JSON without reshaping it. The
checkout/synchronisation layer is the single place that maps supplier fields to
our database. This keeps a supplier change from leaking into the storefront.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any

import httpx
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.models import ESIM, Order, OrderItem, Plan
from app.integrations.qr import render_qr_data_url


class EsimAccessError(RuntimeError):
    """An eSIM Access request or business-rule failure."""


@dataclass(frozen=True)
class EsimAccessPackage:
    """Supplier package reference stored against a local plan."""

    slug: str
    count: int = 1
    price: int | None = None  # supplier price is USD × 10,000
    period_num: int | None = None


class EsimAccessClient:
    """Authenticated eSIM Access v1 API client.

    Header-only API key authentication is supported by eSIM Access. When the
    optional SecretKey is present, the stronger documented HMAC headers are
    sent as well.
    """

    def __init__(
        self,
        *,
        access_code: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.access_code = (
            access_code if access_code is not None else settings.esimaccess_access_code
        ).strip()
        self.secret_key = (
            secret_key if secret_key is not None else settings.esimaccess_secret_key
        ).strip()
        self.base_url = (base_url if base_url is not None else settings.esimaccess_base_url).rstrip(
            "/"
        )
        self.timeout_seconds = timeout_seconds or settings.esimaccess_timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.access_code)

    def _headers(self, body: str) -> dict[str, str]:
        if not self.access_code:
            raise EsimAccessError("eSIM Access is not configured: missing ESIMACCESS_ACCESS_CODE")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "RT-AccessCode": self.access_code,
        }
        if self.secret_key:
            timestamp = str(int(time.time() * 1000))
            request_id = str(uuid.uuid4())
            sign_data = f"{timestamp}{request_id}{self.access_code}{body}"
            signature = (
                hmac.new(self.secret_key.encode(), sign_data.encode(), hashlib.sha256)
                .hexdigest()
                .lower()
            )
            headers.update(
                {
                    "RT-Timestamp": timestamp,
                    "RT-RequestID": request_id,
                    "RT-Signature": signature,
                }
            )
        return headers

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False)
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                content=body.encode(),
                headers=self._headers(body),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            status = exc.response.status_code
            raise EsimAccessError(f"eSIM Access HTTP {status}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise EsimAccessError("Unable to reach eSIM Access") from exc
        except json.JSONDecodeError as exc:
            raise EsimAccessError("eSIM Access returned invalid JSON") from exc

        if not result.get("success"):
            code = result.get("errorCode", "unknown")
            message = result.get("errorMessage", "Unknown eSIM Access error")
            raise EsimAccessError(f"eSIM Access {code}: {message}")
        return result

    def balance(self) -> dict[str, Any]:
        return self._post("/api/v1/open/balance/query")

    def list_packages(
        self,
        *,
        location_code: str = "",
        package_type: str = "BASE",
        slug: str = "",
        iccid: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/v1/open/package/list",
            {
                "locationCode": location_code.upper(),
                "type": package_type,
                "slug": slug,
                "iccid": iccid,
            },
        )

    def order_profiles(
        self, *, transaction_id: str, packages: list[EsimAccessPackage]
    ) -> dict[str, Any]:
        if not packages:
            raise ValueError("At least one eSIM package is required")
        package_info_list = []
        amount = 0
        has_complete_prices = True
        for item in packages:
            entry: dict[str, Any] = {"packageCode": item.slug, "count": item.count}
            if item.price is not None:
                entry["price"] = item.price
                amount += item.price * item.count
            else:
                has_complete_prices = False
            if item.period_num is not None:
                entry["periodNum"] = item.period_num
            package_info_list.append(entry)

        payload: dict[str, Any] = {
            "transactionId": transaction_id,
            "packageInfoList": package_info_list,
        }
        # Sending price + amount protects us from a changed supplier price, but
        # only when every package was synchronised and has a supplier price.
        if has_complete_prices:
            payload["amount"] = amount
        return self._post("/api/v1/open/esim/order", payload)

    def topup(self, *, transaction_id: str, package_code: str, iccid: str) -> dict[str, Any]:
        """Add data to an eSIM that already exists.

        `transactionId` is ours and is what makes a retry safe: the wholesaler
        deduplicates on it, so a task that timed out after the charge went through
        cannot buy the same gigabytes twice. The contract was confirmed against
        the live API — the endpoint answers 200042 with neither iccid nor
        esimTranNo, and 310409 for an ICCID that is not ours.
        """
        return self._post(
            "/api/v1/open/esim/topup",
            {"transactionId": transaction_id, "packageCode": package_code, "iccid": iccid},
        )

    PROFILE_PAGE_SIZE = 50
    # Bir chaqiruvda cheksiz sahifa so'ramaslik uchun. 40 sahifa = 2000 profil;
    # bundan oshsa halqa emas, ta'minotchi tomonda nimadir noto'g'ri.
    PROFILE_PAGE_LIMIT = 40

    def query_profiles(self, *, order_no: str) -> dict[str, Any]:
        """Barcha profillar -- bitta sahifa emas.

        Ilgari faqat 1-sahifa (50 ta) so'ralardi. Bugun 17 ta profil bor,
        ya'ni hammasi bitta sahifaga sig'adi va hech narsa sezilmaydi. 50 dan
        oshgan kunda esa eng eskilari ro'yxatdan tushib qolar edi va ularning
        traffigi JIMGINA yangilanmay qolardi -- mijoz "qancha qolganini
        ko'rsatmayapti" deb yozardi, sabab esa hech qayerda ko'rinmasdi.

        Javob eski shaklda qaytariladi (bitta `esimList`), chunki chaqiruvchi
        kod uni shunday o'qiydi.
        """

        merged: list[dict[str, Any]] = []
        last: dict[str, Any] = {}
        for page in range(1, self.PROFILE_PAGE_LIMIT + 1):
            last = self._post(
                "/api/v1/open/esim/query",
                {
                    "orderNo": order_no,
                    "pager": {"pageNum": page, "pageSize": self.PROFILE_PAGE_SIZE},
                },
            )
            batch = ((last.get("obj") or {}).get("esimList") or []) if last else []
            merged.extend(batch)
            # To'liq bo'lmagan sahifa -- oxirgisi. Bo'sh sahifa ham shu.
            if len(batch) < self.PROFILE_PAGE_SIZE:
                break

        obj = dict(last.get("obj") or {}) if last else {}
        obj["esimList"] = merged
        return {**last, "obj": obj}

    def cancel_profile(self, *, esim_tran_no: str) -> dict[str, Any]:
        return self._post("/api/v1/open/esim/cancel", {"esimTranNo": esim_tran_no})


# --- Order → local eSIM mapping (moved from services/esim_access_sync.py) ---


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


def sync_order_profiles(db: Session, order: Order, client: EsimAccessClient | None = None) -> int:
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
