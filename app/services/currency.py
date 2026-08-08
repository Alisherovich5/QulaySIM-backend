"""USD→UZS display rate, cached in Redis so every worker shares one value."""

from __future__ import annotations

from decimal import Decimal

from app.core.cache import cache_key, get_or_set, get_redis
from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.cbu import RateUnavailableError, fetch_usd_rate
from app.schemas.base import JSONDict

logger = get_logger(__name__)

# How long a fallback rate is allowed to sit in Redis. Long enough to absorb a
# burst, short enough that a recovered CBU reopens the shop within the minute.
_FALLBACK_TTL = 60


async def usd_to_uzs() -> JSONDict:
    async def produce() -> JSONDict:
        try:
            rate, updated_at = await fetch_usd_rate(settings.cbu_currency_url)
            return {"usd_to_uzs": rate, "updated_at": updated_at, "source": "cbu"}
        except RateUnavailableError as exc:
            # A CBU outage must never stop customers browsing plans.
            logger.warning("currency.fallback_used", error=str(exc))
            return {
                "usd_to_uzs": settings.uzs_per_usd_fallback,
                "updated_at": None,
                "source": "fallback",
            }

    key = cache_key("currency")
    payload = await get_or_set(key, settings.cache_ttl_currency, produce)

    # A real rate is good for six hours. A fallback is not: checkout refuses to
    # price an order against it, so caching one for six hours turns a moment of
    # CBU being unreachable into six hours of nobody being able to pay. That is
    # exactly what happened once — a restart raced the network, the fallback
    # went into Redis, and the shop was closed until someone deleted the key.
    # Shortening the entry instead of skipping the write keeps the stampede
    # protection: a CBU outage still gets one request a minute, not one per
    # visitor.
    if payload.get("source") != "cbu":
        try:
            await get_redis().expire(key, _FALLBACK_TTL)
        except Exception as exc:  # noqa: BLE001
            # Losing the shortening is survivable; failing the request is not.
            logger.warning("currency.fallback_ttl_failed", error=str(exc))

    return payload


# Below this a thousand-som step is bigger than the price itself, and the rule
# would push the figure to nonsense or negative. Nothing in the catalogue comes
# close: the cheapest plan is about 12 000 so'm.
_CHARM_FLOOR = 2_000


def charm_uzs(amount: Decimal | int | float) -> Decimal:
    """The som figure a customer sees, always ending in 999.

    A converted price lands on whatever the day's rate makes it — 220 437 so'm —
    a number nobody chose. Shops here quote 219 999, because a price ending in
    999 is read as belonging to the band below it.

    The rule is one line: round to the nearest thousand, then subtract one. That
    single step also produces every threshold drop by itself, because X 000 minus
    1 is (X-1) 999 — so 100 000 becomes 99 999, 30 000 becomes 29 999, and
    1 000 000 becomes 999 999 without any of them being special-cased.

    It moves the price by at most 500 so'm in either direction, roughly $0.04.
    Unlike the earlier version this one can round *up*, so the som figure is no
    longer bounded above by USD × rate — the "≈ $2.50" beside it is an
    approximation and stays one. What must hold is that the page and the invoice
    agree, and they do: both call this.

    Idempotent by construction — 29 999 rounds to 30 000 and comes back to
    29 999 — which matters because display, checkout and a retried order can all
    apply it to the same amount.
    """
    a = int(amount)
    if a < _CHARM_FLOOR:
        return Decimal(a)
    # Half-up to the nearest thousand, then one below it.
    return Decimal(((a + 500) // 1_000) * 1_000 - 1)
