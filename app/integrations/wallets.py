"""What each supplier's wallet holds, for code that must not spend blindly.

Fulfilment already falls back: a supplier that refuses an order is logged and
the next one is tried. But it learns the wallet is empty by attempting the
purchase, which costs a round trip, writes a failure into the ledger and
alarms the operations chat — for an order the other supplier could have taken
straight away.

Reading the balance first turns that into a routing decision. It is advisory,
never a veto: a balance that cannot be read, or one that is a few seconds
stale, must not make an order unfulfillable. The worst a wrong answer here can
do is put the routes in a worse order, which is exactly what happens today.

Synchronous, because the caller is a Celery task. The API has its own async
reader; both share this cache key, so whichever asked last serves the other.
"""

from __future__ import annotations

import json

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CACHE_KEY = "qs:bo:wallets"
CACHE_TTL = 30


def _esimcard() -> float | None:
    from app.integrations.esimcard import EsimCardClient

    try:
        return EsimCardClient().balance_usd()
    except Exception as exc:  # noqa: BLE001 - an unreachable supplier is a state
        logger.warning("wallet.unavailable", provider="esimcard", error=str(exc))
        return None


def _esimaccess() -> float | None:
    from app.integrations.esim_access import EsimAccessClient

    try:
        body = EsimAccessClient().balance()
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet.unavailable", provider="esimaccess", error=str(exc))
        return None
    raw = (body.get("obj") or {}).get("balance")
    if raw is None:
        return None
    try:
        # Their balance arrives in ten-thousandths of a dollar.
        return float(raw) / 10000
    except (TypeError, ValueError):
        logger.warning("wallet.unparsable", provider="esimaccess", raw=str(raw)[:40])
        return None


def balances() -> dict[str, float | None]:
    """Both wallets, in dollars. None for a supplier that did not answer."""
    import redis

    client = None
    try:
        client = redis.Redis.from_url(str(settings.redis_url))
        cached = client.get(CACHE_KEY)
        if cached:
            loaded: dict[str, float | None] = json.loads(cached)
            return loaded
    except Exception as exc:  # noqa: BLE001 - Redis down means ask the supplier
        logger.warning("wallet.cache_read_failed", error=str(exc))

    found: dict[str, float | None] = {"esimcard": _esimcard(), "esimaccess": _esimaccess()}

    if client is not None:
        try:
            client.setex(CACHE_KEY, CACHE_TTL, json.dumps(found))
        except Exception as exc:  # noqa: BLE001
            logger.warning("wallet.cache_write_failed", error=str(exc))
    return found


def can_cover(provider: str, cost_usd: float, known: dict[str, float | None]) -> bool:
    """Whether this supplier's wallet covers a purchase of `cost_usd`.

    True when the balance is unknown. A supplier we cannot ask is not a
    supplier we may rule out — it is one we have no opinion about, and the
    purchase attempt is still the authoritative answer.
    """
    balance = known.get(provider)
    if balance is None:
        return True
    return balance >= cost_usd
