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
    "expire-elapsed-esims": {
        "task": "maintenance.expire_esims",
        "schedule": crontab(minute="*/15"),
    },
    "refresh-currency-rate": {
        "task": "maintenance.refresh_currency_rate",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    "warm-catalog-cache": {
        "task": "maintenance.warm_catalog_cache",
        "schedule": crontab(minute="*/10"),
    },
}
