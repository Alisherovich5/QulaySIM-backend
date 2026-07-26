"""Central Bank of Uzbekistan USD/UZS reference rate.

Display only — never used to settle a payment. Catalogue prices stay in USD.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.integrations.http import get_client

logger = get_logger(__name__)


class RateUnavailableError(RuntimeError):
    pass


async def fetch_usd_rate(url: str) -> tuple[float, str | None]:
    try:
        response = await get_client().get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload: Any = response.json()
    except Exception as exc:
        raise RateUnavailableError(str(exc)) from exc

    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RateUnavailableError("Unexpected CBU response shape")

    usd = next((r for r in rows if str(r.get("Ccy", "")).upper() == "USD"), None)
    if not isinstance(usd, dict):
        raise RateUnavailableError("USD rate absent from CBU response")

    try:
        rate = float(str(usd["Rate"]).replace(" ", "").replace(",", "."))
    except (KeyError, ValueError) as exc:
        raise RateUnavailableError("Unparsable CBU rate") from exc

    if rate <= 0:
        raise RateUnavailableError("Non-positive CBU rate")
    return rate, usd.get("Date")
