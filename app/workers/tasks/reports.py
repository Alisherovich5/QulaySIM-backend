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

from app.core.logging import get_logger
from app.integrations.telegram import send_html
from app.services.reporting import PERIOD_LABELS, build_report, format_report
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
