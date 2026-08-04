"""ATMOS payment gateway — the outbound half.

We only ever use the hosted checkout: create an invoice, hand the customer the
checkout.atmos.uz link, and let card data live entirely on their side. The
inbound half — ATMOS's Callback API asking us to confirm the charge — lives in
app/services/atmos.py, because answering it is business logic, not transport.

Protocol facts that shape this code:
  * OAuth2 client_credentials against /token, Basic auth with the consumer
    key/secret pair; the bearer expires, so it is cached with a safety margin
    and refreshed once on a 401 rather than trusted forever;
  * amounts are integers in tiyin (1 UZS = 100 tiyin), same as Payme;
  * every invoice line wants an OFD classification code (ИКПУ) — fiscal law,
    not an API whim. The code comes from settings; the business supplies it.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT = 30.0


class AtmosError(RuntimeError):
    """An ATMOS request failed or answered outside its contract."""


# One token per process, like the supplier clients: workers and API processes
# each hold their own, and a token is cheap to mint compared to the bookkeeping
# of sharing one.
_token: str | None = None
_token_expires_at: float = 0.0


def _clear_token() -> None:
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


async def _get_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    if _token and time.monotonic() < _token_expires_at:
        return _token

    response = await client.post(
        f"{settings.atmos_base_url}/token",
        data={"grant_type": "client_credentials"},
        auth=(settings.atmos_consumer_key, settings.atmos_consumer_secret),
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        # The body may describe the failure but may also echo credentials-ish
        # detail; the status alone is enough for the operator to act on.
        raise AtmosError(f"token request failed with HTTP {response.status_code}")
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise AtmosError("token response carried no access_token")

    # Refresh a minute early: a token that expires mid-request costs a retry,
    # one that is refreshed early costs nothing.
    _token = str(token)
    _token_expires_at = time.monotonic() + max(int(body.get("expires_in") or 3600) - 60, 60)
    return _token


async def _authorised_post(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any]
) -> httpx.Response:
    """POST with the cached bearer, refreshing it once if ATMOS says expired."""
    token = await _get_token(client)
    response = await client.post(
        f"{settings.atmos_base_url}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 401:
        _clear_token()
        token = await _get_token(client)
        response = await client.post(
            f"{settings.atmos_base_url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    return response


def _invoice_items(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order lines in the shape checkout/invoice/create wants.

    The doc's sample also nests a `details` array of fiscal attributes
    (package_code, mark_code, tin). Those values do not exist for a digital
    service until the business registers them; the sandbox will say whether
    the array may be omitted, and this is the one place to add it if not.
    """
    items = []
    for index, line in enumerate(lines, start=1):
        items.append(
            {
                "items_id": str(index),
                "code": settings.atmos_ikpu_code,
                # ATMOS renders this on the payment page and the fiscal receipt.
                "name": str(line["name"])[:120],
                "amount": int(line["amount_tiyin"]),
                "quantity": int(line["quantity"]),
            }
        )
    return items


async def create_invoice(
    *,
    account: str,
    amount_tiyin: int,
    lines: list[dict[str, Any]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Create a hosted-checkout invoice and return the URL to send the customer to.

    `account` is the value ATMOS will echo back in the callback — it is how the
    callback finds the order, so it must be the order id and nothing cleverer.
    """
    payload: dict[str, Any] = {
        # Unique per attempt, not per order: a retried create must not collide
        # with the invoice a lost response already created.
        "request_id": uuid.uuid4().hex,
        "store_id": settings.atmos_store_id,
        "account": account,
        "amount": amount_tiyin,
        "success_url": settings.atmos_success_url,
    }
    # Only with a real fiscal code. Proven against the DEV store (11035):
    # any items array — empty code or plausible 17-digit one — answers
    # -999999 "System error", while the same invoice without items succeeds.
    # So until the business supplies the ИКПУ (and ATMOS enables the fiscal
    # module for the store), the invoice goes up as a single total.
    if settings.atmos_ikpu_code:
        payload["items"] = _invoice_items(lines)

    async with httpx.AsyncClient(transport=transport) as client:
        response = await _authorised_post(client, "/checkout/invoice/create", payload)

    if response.status_code != 200:
        raise AtmosError(f"invoice create failed with HTTP {response.status_code}")
    body = response.json()
    url = body.get("url")
    if not url:
        status = body.get("status")
        code = status.get("code") if isinstance(status, dict) else None
        raise AtmosError(f"invoice create answered without a url (status code {code!r})")

    logger.info("atmos.invoice_created", account=account, amount_tiyin=amount_tiyin)
    return str(url)
