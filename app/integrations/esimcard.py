"""eSIMCard — the second wholesaler.

A reseller API in the Laravel idiom, and it differs from eSIM Access in three
ways that shape every function below.

**HTTP 200 is not success.** A refused purchase answers `200` with
`{"status": false, "message": "Insufficient Wallet Balance…"}`. Code that trusts
the status line treats a failed purchase as a completed one, so the truth is the
`status` field and nothing else. This is verified against the live API, not
inferred from the doc.

**There is no idempotency key.** `POST /package/purchase` takes a
`package_type_id` and nothing more: call it twice and you have bought two eSIMs
and been charged for both. Nothing on their side can undo that, so the guard
lives on ours — see `app.services.supplier_ledger`.

**A purchase does not return the activation code.** The response carries the
eSIM's id and ICCID; the QR payload appears later, as `universal_link` in
`GET /my-esims`. Sometimes it is not even that fast — `sim_applied: false` means
the profile is still being cut and the money is already spent. So delivery is
always two steps: buy, then poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EsimCardError(RuntimeError):
    """eSIMCard refused a request or answered outside its contract."""


class EsimCardPurchaseUncertainError(EsimCardError):
    """The purchase may or may not have gone through.

    Raised for a timeout or a transport failure on `/package/purchase` — the
    one case where we genuinely do not know whether money left the account.
    Distinct from `EsimCardError` because the two demand opposite handling: a
    clean refusal is safe to retry elsewhere, this is not safe to retry at all.
    """


@dataclass(frozen=True)
class PurchasedEsim:
    """What a purchase yields immediately.

    `applied` false means bought-but-not-yet-cut: `supplier_id` may be empty,
    and the eSIM has to be found later by polling. Recorded either way, because
    the charge has already happened.
    """

    applied: bool
    supplier_id: str
    iccid: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class RemoteEsim:
    """One eSIM as eSIMCard lists it, with the activation payload."""

    supplier_id: str
    iccid: str
    status: str
    universal_link: str
    bundle: str


class EsimCardClient:
    """Thin HTTP client. Synchronous, because fulfilment runs in Celery."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(settings.esimcard_api_token)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise EsimCardError("eSIMCard API token is not configured")

        with httpx.Client(transport=self._transport) as client:
            response = client.request(
                method,
                f"{settings.esimcard_base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.esimcard_api_token}",
                    "Accept": "application/json",
                },
                timeout=settings.esimcard_timeout_seconds,
            )

        # 410 is what the retired host answers on every path. Named explicitly
        # so an operator reads "the API moved" instead of "purchase failed".
        if response.status_code == 410:
            raise EsimCardError(
                f"eSIMCard answered HTTP 410 for {path} — the base URL is stale "
                "(the API moved to portal.esimcard.com)"
            )
        if response.status_code == 401:
            raise EsimCardError("eSIMCard rejected the API token")

        try:
            body = response.json()
        except ValueError as exc:
            raise EsimCardError(
                f"{path} answered HTTP {response.status_code} with a non-JSON body"
            ) from exc
        if not isinstance(body, dict):
            raise EsimCardError(f"{path} answered with {type(body).__name__}, expected an object")

        # The contract's real success flag. Checked before the status code
        # because eSIMCard reports business failures inside a 200.
        if body.get("status") is not True:
            raise EsimCardError(str(body.get("message") or f"{path} answered status=false"))
        if response.status_code >= 400:
            raise EsimCardError(f"{path} answered HTTP {response.status_code}")
        return body

    def balance_usd(self) -> float:
        """Wallet balance. Zero means every purchase will be refused."""
        return float(self._request("GET", "/balance").get("balance") or 0)

    def purchase(self, *, package_type_id: str) -> PurchasedEsim:
        """Buy one eSIM. NOT idempotent — never call without holding a claim.

        A timeout here is the dangerous case: the request may have completed on
        their side after we stopped listening, so it is raised as
        `EsimCardPurchaseUncertainError` and must not be retried blindly.
        """
        try:
            body = self._request("POST", "/package/purchase", {"package_type_id": package_type_id})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise EsimCardPurchaseUncertainError(
                f"purchase of {package_type_id} did not answer: {exc}"
            ) from exc

        data = body.get("data") or {}
        sim = data.get("sim") or {}
        applied = bool(data.get("sim_applied"))
        purchased = PurchasedEsim(
            applied=applied,
            supplier_id=str(sim.get("id") or ""),
            iccid=str(sim.get("iccid") or ""),
            status=str(sim.get("status") or ""),
            message=str(data.get("message") or ""),
        )
        logger.info(
            "esimcard.purchased",
            package_type_id=package_type_id,
            applied=applied,
            supplier_id=purchased.supplier_id or None,
        )
        return purchased

    def list_esims(self, *, page: int = 1) -> tuple[list[RemoteEsim], int]:
        """One page of our eSIMs, plus the last page number.

        Paginated at 15 per page, so a reconciliation sweep has to walk it —
        the newest eSIM is not guaranteed to be on page one forever.
        """
        body = self._request("GET", f"/my-esims?page={page}")
        meta = body.get("meta") or {}
        rows = [
            RemoteEsim(
                supplier_id=str(row.get("id") or ""),
                iccid=str(row.get("iccid") or ""),
                status=str(row.get("status") or ""),
                universal_link=str(row.get("universal_link") or ""),
                bundle=str(row.get("last_bundle") or ""),
            )
            for row in (body.get("data") or [])
            if isinstance(row, dict)
        ]
        return rows, int(meta.get("lastPage") or 1)

    def find_esims(self, supplier_ids: set[str], *, max_pages: int = 20) -> dict[str, RemoteEsim]:
        """Look up specific eSIMs by id, stopping as soon as all are found.

        Bounded rather than open-ended: an account with thousands of eSIMs would
        otherwise let one stuck order walk the entire history on every retry.
        """
        found: dict[str, RemoteEsim] = {}
        wanted = set(supplier_ids)
        page, last_page = 1, 1
        while page <= min(last_page, max_pages) and wanted:
            rows, last_page = self.list_esims(page=page)
            for row in rows:
                if row.supplier_id in wanted:
                    found[row.supplier_id] = row
                    wanted.discard(row.supplier_id)
            page += 1
        return found
