"""USD→UZS display rate, cached in Redis so every worker shares one value."""

from __future__ import annotations

from app.core.cache import cache_key, get_or_set
from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.cbu import RateUnavailableError, fetch_usd_rate
from app.schemas.base import JSONDict

logger = get_logger(__name__)


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

    return await get_or_set(cache_key("currency"), settings.cache_ttl_currency, produce)
