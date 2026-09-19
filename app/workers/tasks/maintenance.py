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
    from app.db.session import task_sessions
    from app.domain.localisation import SUPPORTED_LANGUAGES
    from app.services.catalog import invalidate_catalog, list_countries, list_regions

    async def run() -> int:
        try:
            await invalidate_catalog()
            total = 0
            # Modul darajasidagi dvigatel emas: u birinchi halqaga bog'lanib
            # qoladi va bu vazifa har safar yangi halqa ochadi.
            async with task_sessions() as sessions, sessions() as session:
                # Every supported language: the catalogue is cached per language
                # now, so warming only English left uz and ru visitors paying the
                # cold query after each invalidation.
                for language in SUPPORTED_LANGUAGES:
                    await list_regions(session, language=language)
                    countries = await list_countries(
                        session,
                        search=None,
                        region_slug=None,
                        popular=None,
                        limit=500,
                        offset=0,
                        language=language,
                    )
                    total += len(countries)
            return total
        finally:
            await close_redis()

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


@celery_app.task(name="maintenance.refresh_esim_usage")
def refresh_esim_usage() -> int:
    """Pull used-data, status and expiry from the wholesaler for live eSIMs.

    `sync_order_profiles` maps `orderUsage` correctly, but it only runs when an
    order is fulfilled — and at that moment nothing has been used yet. Nothing
    refreshed it afterwards, so the admin showed 0% for every eSIM forever and
    customers kept opening their account to look for a number that never moved.
    One of them had spent 471 MB of a 1 GB plan while the page said none.

    The wholesaler's figure is authoritative for the allowance too, so writing
    `data_total_mb` here also repairs a row whose total drifted.

    eSIM Access only. eSIMCard's listing carries no usage field, so its profiles
    are left alone rather than being reset to zero by a source that does not
    know — a wrong number is worse than a stale one.
    """
    from math import ceil

    from app.db.base import utcnow
    from app.integrations.esim_access import (
        EsimAccessClient,
        _local_status,
        _parse_supplier_date,
    )

    client = EsimAccessClient()
    if not client.is_configured:
        return 0

    payload = client.query_profiles(order_no="")
    profiles = {
        str(p.get("esimTranNo")): p
        for p in ((payload.get("obj") or {}).get("esimList") or [])
        if p.get("esimTranNo")
    }
    if not profiles:
        return 0

    updated = 0
    # Bitta o'lchov -- bitta vaqt. Har bir qator uchun alohida olinsa, bir
    # o'tishda yozilgan vaqtlar bir-biridan farq qilib turardi.
    checked_at = utcnow()
    with worker_session() as session:
        rows = (
            session.execute(
                select(ESIM).where(
                    ESIM.provider == "esimaccess",
                    ESIM.status.in_((ESIMStatus.PENDING, ESIMStatus.ACTIVE)),
                    ESIM.provider_esim_tran_no != "",
                )
            )
            .scalars()
            .all()
        )
        for esim in rows:
            profile = profiles.get(esim.provider_esim_tran_no)
            if profile is None:
                continue

            used_bytes = int(profile.get("orderUsage") or 0)
            total_bytes = int(profile.get("totalVolume") or 0)
            fresh = {
                "data_used_mb": ceil(used_bytes / (1024 * 1024)) if used_bytes else 0,
                "provider_status": str(profile.get("esimStatus") or ""),
                "status": _local_status(str(profile.get("esimStatus") or "")),
                "expires_at": _parse_supplier_date(profile.get("expiredTime")),
            }
            if total_bytes:
                fresh["data_total_mb"] = ceil(total_bytes / (1024 * 1024))

            # Only count a row as updated when something actually moved, so the
            # log line means "usage changed" rather than "the task ran".
            if any(getattr(esim, field) != value for field, value in fresh.items()):
                for field, value in fresh.items():
                    setattr(esim, field, value)
                updated += 1

            # Vaqt esa sarf o'zgarmasa ham yoziladi, va ataylab shunday: bu
            # ustun "raqam o'zgardi" degani emas, "biz shu payt tekshirdik"
            # degani. Faqat o'zgarganda yozilsa, umuman internet
            # sarflamagan mijozning sahifasida vaqt hech qachon paydo
            # bo'lmasdi -- ya'ni aynan shikoyat qilgan odam uchun ishlamasdi.
            esim.last_synced_at = checked_at
        session.commit()

    if updated:
        logger.info("maintenance.esim_usage_refreshed", updated=updated, seen=len(profiles))
    return updated


@celery_app.task(name="maintenance.refresh_esimcard_status")
def refresh_esimcard_status() -> int:
    """Move an eSIMCard profile on when the customer installs it.

    The sibling task above deliberately leaves this wholesaler alone, because
    its listing carries no usage figure and a zero written from a source that
    does not know is worse than a stale number. Status is different: the listing
    does carry it, and nothing was reading it. Every profile we have sold through
    eSIMCard sat at "Released" in our database while the supplier had already
    moved one of them to "Installed" — a customer with a working eSIM whose
    account still called it pending.

    The expiry is written here rather than at purchase, and only once: the
    countdown starts when the profile is installed, and a profile bought on
    Monday and installed on Friday does not lose four days.
    """
    from datetime import timedelta

    from app.db.base import utcnow
    from app.integrations.esimcard import EsimCardClient
    from app.integrations.esimcard_sync import _local_status

    client = EsimCardClient()
    if not client.is_configured:
        return 0

    with worker_session() as session:
        rows = (
            session.execute(
                select(ESIM).where(
                    ESIM.provider == "esimcard",
                    ESIM.status.in_((ESIMStatus.PENDING, ESIMStatus.ACTIVE)),
                    ESIM.provider_esim_tran_no != "",
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0

        remote = client.find_esims({esim.provider_esim_tran_no for esim in rows})
        checked_at = utcnow()
        updated = 0
        for esim in rows:
            profile = remote.get(esim.provider_esim_tran_no)
            if profile is None:
                continue
            local = _local_status(profile.status)
            became_live = local == "active" and esim.expires_at is None
            if esim.provider_status != profile.status or esim.status != local or became_live:
                updated += 1
            esim.provider_status = profile.status
            esim.status = local
            if became_live:
                esim.expires_at = checked_at + timedelta(days=esim.validity_days or 0)
            esim.last_synced_at = checked_at
        session.commit()

    if updated:
        logger.info("maintenance.esimcard_status_refreshed", updated=updated, seen=len(rows))
    return updated


#: How long a row image is worth keeping.
#:
#: The audit triggers exist to answer "who changed this order, and to what"
#: after the fact. Ninety days outlives any dispute this shop has had, and an
#: audit table nobody trims is a table that eventually costs more to back up
#: than the data it is auditing — this one reached 31 MB, larger than the entire
#: catalogue, before anyone looked.
AUDIT_RETENTION_DAYS = 90


@celery_app.task(name="maintenance.trim_audit_log")
def trim_audit_log() -> int:
    """Drop audit rows older than the retention window.

    Written as raw SQL against a table Django and SQLAlchemy both know nothing
    about: `audit_row_change` is created by triggers installed outside either
    ORM, so there is no model to delete through. The task tolerates the table
    being absent, because a database restored from before the triggers existed
    is a valid database.
    """
    from sqlalchemy import text

    with worker_session() as session:
        exists = session.execute(
            text("SELECT to_regclass('public.audit_row_change') IS NOT NULL")
        ).scalar()
        if not exists:
            return 0
        # Through the connection rather than the session: `Session.execute` is
        # typed as returning a plain Result, which has no rowcount, and the
        # number deleted is the only thing here worth a log line.
        removed = (
            session.connection()
            .execute(
                text(
                    "DELETE FROM audit_row_change WHERE at < now() - make_interval(days => :days)"
                ),
                {"days": AUDIT_RETENTION_DAYS},
            )
            .rowcount
        )
        session.commit()

    if removed:
        logger.info("maintenance.audit_trimmed", removed=removed, keep_days=AUDIT_RETENTION_DAYS)
    return removed or 0
