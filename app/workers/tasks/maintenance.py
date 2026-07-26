"""Scheduled housekeeping."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.db.models import ESIM
from app.db.models.enums import ESIMStatus
from app.workers.celery_app import celery_app
from app.workers.session import worker_session

logger = get_logger(__name__)


@celery_app.task(name="maintenance.expire_esims")
def expire_esims() -> int:
    """Flip active eSIMs to 'expired' once their window closes.

    Nothing did this before: an activated eSIM stayed 'active' indefinitely, so
    the account dashboard and the admin both over-reported active profiles.
    """
    now = datetime.now(UTC)
    with worker_session() as session:
        result = session.execute(
            update(ESIM)
            .where(
                ESIM.status == ESIMStatus.ACTIVE,
                ESIM.expires_at.is_not(None),
                ESIM.expires_at < now,
            )
            .values(status=ESIMStatus.EXPIRED)
        )
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if count:
        logger.info("maintenance.esims_expired", count=count)
    return count


@celery_app.task(name="maintenance.refresh_currency_rate")
def refresh_currency_rate() -> float:
    """Refresh the CBU rate ahead of TTL expiry so no customer request ever
    pays the latency of the upstream call."""
    from app.core.cache import close_redis, invalidate
    from app.services.currency import usd_to_uzs

    async def run() -> float:
        try:
            await invalidate("qs:currency*")
            payload = await usd_to_uzs()
            return float(payload["usd_to_uzs"])
        finally:
            await close_redis()

    rate = asyncio.run(run())
    logger.info("maintenance.currency_refreshed", rate=rate)
    return rate


@celery_app.task(name="maintenance.warm_catalog_cache")
def warm_catalog_cache() -> int:
    from app.core.cache import close_redis
    from app.db.session import SessionFactory, dispose_engine
    from app.services.catalog import invalidate_catalog, list_countries, list_regions

    async def run() -> int:
        try:
            await invalidate_catalog()
            async with SessionFactory() as session:
                await list_regions(session)
                countries = await list_countries(
                    session, search=None, region_slug=None, popular=None, limit=500, offset=0
                )
            return len(countries)
        finally:
            await close_redis()
            await dispose_engine()

    count = asyncio.run(run())
    logger.info("maintenance.catalog_warmed", countries=count)
    return count


@celery_app.task(name="maintenance.count_stale_pending_esims")
def count_stale_pending_esims() -> int:
    """Observability hook: pending eSIMs that never got a supplier profile."""
    with worker_session() as session:
        rows = session.execute(
            select(ESIM.id).where(
                ESIM.status == ESIMStatus.PENDING,
                ESIM.provider == "esimaccess",
                ESIM.provider_esim_tran_no == "",
            )
        ).all()
    if rows:
        logger.warning("maintenance.stale_pending_esims", count=len(rows))
    return len(rows)
