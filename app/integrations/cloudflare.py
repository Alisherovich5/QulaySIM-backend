"""Tell the edge that a price changed.

The catalogue answers already carry `s-maxage=3600`, which is what lets a CDN
serve them for an hour without asking us. That is the point — and it is also the
risk: an hour is a long time to advertise a price that is no longer real.

So the same moment that clears the Redis cache clears the edge. Purge by URL
rather than everything, because a full purge throws away the HTML and the assets
too and the next visitor pays for all of it.

Unconfigured by default. Without a zone id this module does nothing at all, which
is exactly what should happen while there is no CDN in front.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("cloudflare")

_API = "https://api.cloudflare.com/client/v4"

#: Cloudflare accepts up to 30 URLs per call on the free plan.
_BATCH = 30


def is_configured() -> bool:
    return bool(settings.cloudflare_zone_id and settings.cloudflare_api_token)


async def purge(urls: list[str]) -> int:
    """Drop these URLs from the edge cache. Returns how many were accepted.

    Never raises. A failed purge means the edge serves a stale price for up to an
    hour, which is bad; a failed purge that also fails the sync that triggered it
    would leave the price wrong in the database as well, which is worse.
    """
    if not is_configured() or not urls:
        return 0

    accepted = 0
    headers = {
        "Authorization": f"Bearer {settings.cloudflare_api_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        for start in range(0, len(urls), _BATCH):
            batch = urls[start : start + _BATCH]
            try:
                response = await client.post(
                    f"{_API}/zones/{settings.cloudflare_zone_id}/purge_cache",
                    headers=headers,
                    json={"files": batch},
                )
                if response.status_code == 200 and response.json().get("success"):
                    accepted += len(batch)
                else:
                    logger.warning(
                        "cloudflare.purge_refused",
                        status=response.status_code,
                        count=len(batch),
                    )
            except Exception as exc:  # noqa: BLE001 — see the docstring
                logger.warning("cloudflare.purge_failed", error=str(exc), count=len(batch))
    if accepted:
        logger.info("cloudflare.purged", count=accepted)
    return accepted


async def purge_catalogue(slugs: list[str] | None = None) -> int:
    """Purge the catalogue endpoints, and the pages that render them.

    The HTML is included because destination pages carry their prices baked in:
    an edge holding yesterday's HTML would show yesterday's price even with the
    API purged.
    """
    base = (
        str(settings.site_url).rstrip("/")
        if hasattr(settings, "site_url")
        else "https://qulaysim.uz"
    )
    urls = [
        f"{base}/api/countries",
        f"{base}/api/regions",
        f"{base}/api/regions/global",
        f"{base}/api/plans/popular",
        f"{base}/destinations",
    ]
    for slug in slugs or []:
        urls.append(f"{base}/api/countries/{slug}")
        urls.append(f"{base}/destinations/{slug}")
        # Each language edition is its own URL at the edge.
        for lang in ("ru", "en"):
            urls.append(f"{base}/{lang}/destinations/{slug}")
    return await purge(urls)
