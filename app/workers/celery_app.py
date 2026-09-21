"""Celery application.

Background work the API previously had no way to do:
  * supplier order/profile synchronisation (was inline in the webhook);
  * expiring eSIMs past their validity window (nothing did this before, so
    `status` stayed 'active' forever);
  * warming the currency and catalogue caches;
  * referral reward granting off the request path.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "qulaysim",
    broker=str(settings.redis_url),
    backend=str(settings.redis_url),
    include=[
        "app.workers.tasks.provisioning",
        "app.workers.tasks.maintenance",
        "app.workers.tasks.diagnostics",
        "app.workers.tasks.rescue",
        "app.workers.tasks.reports",
        "app.workers.tasks.wallets",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=240,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_default_retry_delay=30,
    task_annotations={"*": {"max_retries": 5}},
)

celery_app.conf.beat_schedule = {
    # The safety net under a paid order. Every five minutes, because the
    # customer is the one waiting and the daily report is twenty-four hours too
    # late for money that has already changed hands.
    "rescue-unfulfilled-orders": {
        "task": "rescue.unfulfilled_orders",
        "schedule": crontab(minute="*/5"),
    },
    # Ten minutes, for two different reasons that happen to agree. It is the
    # freshness checkout needs — the balance it refuses a sale on should not be
    # an hour old — and it is often enough that a wallet emptying mid-morning is
    # announced mid-morning. Two HTTP calls, so the cadence costs nothing.
    "check-supplier-wallets": {
        "task": "maintenance.check_wallets",
        "schedule": crontab(minute="*/10"),
    },
    "expire-elapsed-esims": {
        "task": "maintenance.expire_esims",
        "schedule": crontab(minute="*/15"),
    },
    "refresh-currency-rate": {
        "task": "maintenance.refresh_currency_rate",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # Usage is what customers open their account to check, so it is polled often
    # enough that the number they see is roughly current, and rarely enough that
    # the wholesaler is not asked once a minute for data that changes slowly.
    "refresh-esim-usage": {
        "task": "maintenance.refresh_esim_usage",
        "schedule": crontab(minute="*/20"),
    },
    # eSIMCard sends no usage, but it does send status — so this asks only that,
    # and on the same cadence, so an installed profile stops reading "pending"
    # within twenty minutes rather than never.
    "refresh-esimcard-status": {
        "task": "maintenance.refresh_esimcard_status",
        "schedule": crontab(minute="*/20"),
    },
    "warm-catalog-cache": {
        "task": "maintenance.warm_catalog_cache",
        "schedule": crontab(minute="*/10"),
    },
    # Reports land at 07:00 Tashkent, which is 02:00 UTC — the schedule runs in
    # UTC, and writing the conversion here beats rediscovering it every time a
    # report arrives at the wrong hour. Each period is offset by five minutes so
    # a day that is also the 1st of the month does not fire four reports in the
    # same second and race for the same Telegram rate limit.
    # Weekly, at an hour nothing else uses: the audit log grows only while
    # somebody is changing orders, and a week of it is a few dozen rows.
    "trim-audit-log": {
        "task": "maintenance.trim_audit_log",
        "schedule": crontab(minute=40, hour=3, day_of_week=0),
    },
    "report-daily": {
        "task": "reports.send_period",
        "schedule": crontab(minute=0, hour=2),
        "args": (1,),
    },
    "report-3-day": {
        "task": "reports.send_period",
        "schedule": crontab(minute=5, hour=2, day_of_month="*/3"),
        "args": (3,),
    },
    "report-weekly": {
        "task": "reports.send_period",
        "schedule": crontab(minute=10, hour=2, day_of_week=1),
        "args": (7,),
    },
    "report-monthly": {
        "task": "reports.send_period",
        "schedule": crontab(minute=15, hour=2, day_of_month=1),
        "args": (30,),
    },
    "report-quarterly": {
        "task": "reports.send_period",
        "schedule": crontab(minute=20, hour=2, day_of_month=1, month_of_year="1,4,7,10"),
        "args": (90,),
    },
    "report-yearly": {
        "task": "reports.send_period",
        "schedule": crontab(minute=25, hour=2, day_of_month=1, month_of_year=1),
        "args": (365,),
    },
}
