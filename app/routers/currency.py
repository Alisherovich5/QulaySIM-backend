"""Public display exchange-rate endpoint.

Prices in the catalogue and checkout remain USD.  This endpoint only gives the
storefront a recent UZS conversion rate, so it must never be used to settle a
payment or determine an order amount.
"""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Response

from app.core.config import settings
from app.schemas import CurrencyRateOut

router = APIRouter(prefix="/api", tags=["currency"])

_cached_rate: CurrencyRateOut | None = None
_cache_expires_at = 0.0


def _fallback_rate() -> CurrencyRateOut:
    return CurrencyRateOut(
        usd_to_uzs=settings.uzs_per_usd_fallback,
        source="fallback",
    )


def _fetch_cbu_usd_rate() -> CurrencyRateOut:
    request = Request(
        settings.cbu_currency_url,
        headers={"Accept": "application/json", "User-Agent": "QulaySIM/1.0"},
    )
    with urlopen(request, timeout=10) as response:  # nosec B310 - URL is app configuration
        payload = json.loads(response.read().decode("utf-8"))

    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Unexpected CBU exchange-rate response")

    usd = next(
        (item for item in rows if str(item.get("Ccy", "")).upper() == "USD"),
        None,
    )
    if not isinstance(usd, dict):
        raise ValueError("USD rate is absent from the CBU response")

    rate = float(str(usd["Rate"]).replace(" ", "").replace(",", "."))
    if rate <= 0:
        raise ValueError("Invalid CBU USD rate")

    return CurrencyRateOut(
        usd_to_uzs=rate,
        updated_at=usd.get("Date"),
        source="cbu",
    )


@router.get("/currency", response_model=CurrencyRateOut)
def currency_rate(response: Response) -> CurrencyRateOut:
    global _cached_rate, _cache_expires_at

    now = time.monotonic()
    if _cached_rate is None or now >= _cache_expires_at:
        try:
            _cached_rate = _fetch_cbu_usd_rate()
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            # A temporary upstream failure must not prevent browsing plans.
            _cached_rate = _fallback_rate()
        _cache_expires_at = now + settings.currency_rate_cache_seconds

    response.headers["Cache-Control"] = (
        f"public, max-age={settings.currency_rate_cache_seconds}"
    )
    return _cached_rate
