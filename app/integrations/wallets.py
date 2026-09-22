"""What each wholesaler's wallet holds, for every part of the shop that spends it.

One module because there used to be three, with two `can_cover` implementations
and two cache keys for the same fact — the shape duplication takes when the
checkout guard, the routing preference and the dashboard are each built on a
different day. A second copy of "how much money is left" is a second answer, and
the one that goes stale is always the one somebody is about to spend against.

Three readers, and they want genuinely different things, which is why there are
two caches rather than one:

* **Checkout** (`known_balances`) must never call a wholesaler. A quote is a
  customer waiting on a page and a balance endpoint is a third party that can
  hang, so checkout reads only what the wallet watch left behind and treats a
  missing key as "no opinion".
* **Fulfilment and the dashboard** (`balances`, `wallet_balances`) may ask,
  because by then somebody is either spending the money or looking straight at
  the number. Half a minute of cache keeps a refreshed dashboard from turning
  into a rate-limit problem upstream.
* **The wallet watch** (`record`) writes the long-lived key the first reader
  depends on.

Every unknown resolves the same way everywhere: a balance that could not be
read, a cache that is gone, a supplier that did not answer — none of them is a
supplier we may rule out. It is one we have no opinion about, and the purchase
attempt stays the authority.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Asked of the wholesaler when it is missing. Short, because the readers of
#: this one are about to act on it.
LIVE_KEY = "qs:wallet:live"
LIVE_TTL = 30

#: Written only by the wallet watch and read without ever asking a supplier.
#: Comfortably longer than the watch's ten-minute cadence, so one missed run
#: does not blind checkout, and short enough that a worker down for an hour
#: stops being quoted as fact.
WATCH_KEY = "qs:wallet:watch"
WATCH_TTL = 45 * 60


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


def fetch_balances() -> dict[str, float | None]:
    """Ask both wholesalers, in this thread. `None` is "we could not find out"."""
    return {"esimcard": _esimcard(), "esimaccess": _esimaccess()}


def _redis() -> Any:
    """A synchronous client. Typed loosely because redis-py's generics differ
    between the sync and async clients and nothing here needs the distinction."""
    import redis

    return redis.Redis.from_url(str(settings.redis_url))


def balances() -> dict[str, float | None]:
    """Both wallets, in dollars, for a synchronous caller. Asks if the cache is cold.

    A supplier that does not answer falls back to whatever the watch last
    recorded, rather than to "unknown". The difference is worth the extra read:
    unknown means attempt the purchase, and a worker restarted with a cold cache
    asks the supplier live — so one timed-out balance read turned into a burst
    of six refused orders at 08:45 on 22 September, for a wallet the watch had
    correctly recorded as holding 24 cents ten minutes earlier.

    A number from the watch is at most WATCH_TTL old, and being wrong with it
    costs one five-minute rescue cycle, because by the next sweep the watch has
    refreshed. Being wrong the other way costs a round trip, a failed row in the
    ledger and an alarm, every time.
    """
    client = None
    try:
        client = _redis()
        cached = client.get(LIVE_KEY)
        if cached:
            loaded: dict[str, float | None] = json.loads(cached)
            return loaded
    except Exception as exc:  # noqa: BLE001 - Redis down means ask the supplier
        logger.warning("wallet.cache_read_failed", error=str(exc))

    found = fetch_balances()

    if any(value is None for value in found.values()):
        recorded = _recorded()
        for provider, value in found.items():
            if value is None and provider in recorded:
                found[provider] = recorded[provider]
                logger.info("wallet.fell_back_to_watch", provider=provider)

    if client is not None:
        try:
            client.setex(LIVE_KEY, LIVE_TTL, json.dumps(found))
        except Exception as exc:  # noqa: BLE001
            logger.warning("wallet.cache_write_failed", error=str(exc))
    return found


def _recorded() -> dict[str, float]:
    """What the watch last wrote. Empty when there is nothing to fall back to."""
    try:
        raw = _redis().get(WATCH_KEY)
    except Exception:  # noqa: BLE001 - no fallback is the same as no record
        return {}
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: float(value)
        for key, value in loaded.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


async def wallet_balances() -> dict[str, float | None]:
    """The same answer for the dashboard, off the event loop.

    The supplier clients are synchronous, so each goes to a worker thread: on
    the loop a slow wholesaler would stall every other request on the API.
    """
    from app.core.cache import get_redis

    try:
        cached = await get_redis().get(LIVE_KEY)
        if cached:
            loaded: dict[str, float | None] = json.loads(cached)
            return loaded
    except Exception as exc:  # noqa: BLE001 - Redis down means ask the supplier
        logger.warning("wallet.cache_read_failed", error=str(exc))

    card, access = await asyncio.gather(
        asyncio.to_thread(_esimcard), asyncio.to_thread(_esimaccess)
    )
    found: dict[str, float | None] = {"esimcard": card, "esimaccess": access}

    try:
        await get_redis().setex(LIVE_KEY, LIVE_TTL, json.dumps(found))
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet.cache_write_failed", error=str(exc))
    return found


def record(found: Mapping[str, float | None]) -> None:
    """Leave the known balances where checkout can read them without asking anyone.

    Only the numbers we actually have: an unreadable balance is left out rather
    than written as zero, so `known_balances` cannot hand checkout a "wallet is
    empty" that really means "the supplier did not answer".

    A failure here is logged and swallowed. Checkout falls back to selling,
    which is the safe direction.
    """
    keep = {key: float(value) for key, value in found.items() if value is not None}
    try:
        _redis().setex(WATCH_KEY, WATCH_TTL, json.dumps(keep))
    except Exception:  # noqa: BLE001
        logger.warning("wallet.record_failed")


async def known_balances() -> dict[str, float]:
    """What the watch last recorded. Never asks a supplier; unknowns are absent."""
    from app.core.cache import get_redis

    try:
        raw = await get_redis().get(WATCH_KEY)
    except Exception as exc:  # noqa: BLE001 - a cache outage must not block a sale
        logger.warning("wallet.cache_read_failed", error=str(exc))
        return {}
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        logger.warning("wallet.cache_unparsable")
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: float(value)
        for key, value in loaded.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def can_cover(
    provider: str,
    cost_usd: Decimal | float | None,
    known: Mapping[str, float | None],
) -> bool:
    """Whether this wallet covers one purchase of `cost_usd`.

    True when the balance is unknown, and true when the cost is: the question
    is "do we already know this will fail", not "are we sure it will work".
    """
    balance = known.get(provider)
    if balance is None:
        return True
    try:
        needed = float(cost_usd or 0)
    except (TypeError, ValueError):
        return True
    return needed <= balance
