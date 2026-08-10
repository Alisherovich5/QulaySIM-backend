"""Scheduled sales reports, delivered to the operations Telegram chat.

One task parameterised by window length rather than five near-identical ones:
the periods the owner asked for — daily, three-day, weekly, monthly, quarterly —
differ only in how far back they look and what the heading says.

Reports are sent even when nothing happened. A reporting bot that goes quiet on
a slow day is indistinguishable from one that has stopped working, and the
difference only surfaces when somebody goes looking for a number that never
arrived.
"""

from __future__ import annotations

import asyncio

import redis

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.telegram import send_html
from app.services.reporting import (
    PERIOD_LABELS,
    build_report,
    build_sale_note,
    format_report,
)
from app.workers.celery_app import celery_app
from app.workers.session import worker_session

logger = get_logger(__name__)


@celery_app.task(name="reports.send_period", bind=True, max_retries=3, default_retry_delay=300)
def send_period_report(self, days: int) -> str:
    """Build the report for the last `days` and post it.

    Retries on delivery failure rather than dropping the report: Telegram being
    briefly unreachable should postpone the message, not cancel it. The report
    is rebuilt on each attempt, so a retry an hour later reports the same window
    with any late-arriving rows included.
    """
    with worker_session() as session:
        report = build_report(session, days=days)

    text = format_report(report)
    try:
        asyncio.run(send_html(text))
    except Exception as exc:  # noqa: BLE001 - retried, and logged with its window
        logger.warning("reports.delivery_failed", days=days, error=str(exc))
        raise self.retry(exc=exc) from exc

    logger.info(
        "reports.sent",
        days=days,
        orders=report.orders,
        new_customers=report.new_customers,
        unfulfilled=len(report.unfulfilled_orders),
    )
    return PERIOD_LABELS.get(days, f"{days}d")


#: How long the "already announced" marker lives. Long enough that a retry an
#: hour later is still recognised as the same sale, short enough that the keys
#: do not accumulate forever.
ANNOUNCED_TTL_SECONDS = 7 * 24 * 3600


def _claim_announcement(order_id: int) -> bool:
    """True the first time this order is claimed, False afterwards.

    `fulfil_paid_order` retries, and a retry that reaches the end again would
    otherwise announce the same sale twice. SETNX makes the claim atomic, so two
    workers racing on the same order still produce one message.

    A Redis outage returns True: announcing twice is a nuisance, staying silent
    about a sale is worse.
    """
    try:
        client = redis.from_url(str(settings.redis_url))
        return bool(client.set(f"qs:announced:order:{order_id}", "1", nx=True, ex=ANNOUNCED_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("reports.announce_claim_failed", order_id=order_id, error=str(exc))
        return True


@celery_app.task(name="reports.announce_sale", bind=True, max_retries=3, default_retry_delay=120)
def announce_sale(self, order_id: int) -> str:
    """Post a message the moment a sale completes.

    Scheduled with a short delay by the fulfilment task so the supplier has
    usually returned a profile by the time this runs — but the message reports
    whatever it actually finds, so an eSIM that has not arrived says so rather
    than being left out.
    """
    if not _claim_announcement(order_id):
        logger.info("reports.already_announced", order_id=order_id)
        return "duplicate"

    with worker_session() as session:
        note = build_sale_note(session, order_id)

    if note is None:
        logger.info("reports.nothing_to_announce", order_id=order_id)
        return "skipped"

    try:
        asyncio.run(send_html(note))
    except Exception as exc:  # noqa: BLE001
        logger.warning("reports.announce_failed", order_id=order_id, error=str(exc))
        raise self.retry(exc=exc) from exc

    logger.info("reports.announced", order_id=order_id)
    return "sent"
