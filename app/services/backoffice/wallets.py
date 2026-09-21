"""What is left in each supplier's wallet.

Asked of the suppliers themselves, not read from a table: the number matters
precisely when somebody is about to spend it, and a stale copy is how an order
gets accepted against a wallet that emptied an hour ago.

Two guards around that. The clients are synchronous, so each call goes to a
worker thread — on the event loop a slow supplier would stall every other
request on the API. And the answer is cached for half a minute, so an operator
refreshing the dashboard does not turn into a rate-limit problem upstream.
"""

from __future__ import annotations

import asyncio
import json

from app.core.cache import get_redis
from app.core.logging import get_logger

logger = get_logger(__name__)

CACHE_KEY = "qs:bo:wallets"
CACHE_TTL = 30


def _esimcard() -> float | None:
    from app.integrations.esimcard import EsimCardClient

    try:
        return EsimCardClient().balance_usd()
    except Exception as exc:  # noqa: BLE001 - an unreachable supplier is a state, not a crash
        logger.warning("backoffice.wallet_unavailable", provider="esimcard", error=str(exc))
        return None


def _esimaccess() -> float | None:
    from app.integrations.esim_access import EsimAccessClient

    try:
        body = EsimAccessClient().balance()
    except Exception as exc:  # noqa: BLE001
        logger.warning("backoffice.wallet_unavailable", provider="esimaccess", error=str(exc))
        return None
    obj = body.get("obj") or {}
    # Their balance arrives in ten-thousandths of a dollar.
    raw = obj.get("balance")
    if raw is None:
        return None
    try:
        return float(raw) / 10000
    except (TypeError, ValueError):
        logger.warning("backoffice.wallet_unparsable", provider="esimaccess", raw=str(raw)[:40])
        return None


async def wallet_balances() -> dict[str, float | None]:
    try:
        cached = await get_redis().get(CACHE_KEY)
        if cached:
            loaded: dict[str, float | None] = json.loads(cached)
            return loaded
    except Exception as exc:  # noqa: BLE001 - Redis down means ask the supplier
        logger.warning("backoffice.wallet_cache_read_failed", error=str(exc))

    card, access = await asyncio.gather(
        asyncio.to_thread(_esimcard), asyncio.to_thread(_esimaccess)
    )
    balances: dict[str, float | None] = {"esimcard": card, "esimaccess": access}

    try:
        await get_redis().setex(CACHE_KEY, CACHE_TTL, json.dumps(balances))
    except Exception as exc:  # noqa: BLE001
        logger.warning("backoffice.wallet_cache_write_failed", error=str(exc))
    return balances
