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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.config import settings


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
        self.access_code = (access_code if access_code is not None else settings.esimaccess_access_code).strip()
        self.secret_key = (secret_key if secret_key is not None else settings.esimaccess_secret_key).strip()
        self.base_url = (base_url if base_url is not None else settings.esimaccess_base_url).rstrip("/")
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
            signature = hmac.new(
                self.secret_key.encode(), sign_data.encode(), hashlib.sha256
            ).hexdigest().lower()
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
        request = Request(
            f"{self.base_url}{path}",
            data=body.encode(),
            headers=self._headers(body),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - fixed supplier URL
                result = json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise EsimAccessError(f"eSIM Access HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
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

    def query_profiles(self, *, order_no: str) -> dict[str, Any]:
        return self._post(
            "/api/v1/open/esim/query",
            {"orderNo": order_no, "pager": {"pageNum": 1, "pageSize": 50}},
        )

    def cancel_profile(self, *, esim_tran_no: str) -> dict[str, Any]:
        return self._post("/api/v1/open/esim/cancel", {"esimTranNo": esim_tran_no})
