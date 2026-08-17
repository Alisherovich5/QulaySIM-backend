"""The safety net under a paid order: nothing stays paid and undelivered.

Everything up to here already tries hard. The provisioning task retries with
exponential backoff and jitter, the queue acknowledges late so a killed worker's
job returns to it, and the supplier calls are idempotent. What none of that
covers is the gap between "the money is ours" and "a task exists to spend it":

  * the callback commits the payment and then cannot reach Redis to enqueue;
  * a repeat callback for an already-confirmed transaction answers OK without
    dispatching anything, which is correct for the payment and wrong for the eSIM;
  * the task exhausts its retries because the supplier was down for an hour;
  * something nobody predicted.

In each case the order row itself is the record of unfinished work — paid, with
no eSIM — so this sweeps for exactly that and dispatches again. The order is the
outbox; a separate outbox table would hold the same fact in a second place and
add a way for the two to disagree.

Two thresholds, and both are deliberate. Five minutes before a retry, because a
healthy fulfilment takes seconds and re-dispatching sooner would fight the
task's own backoff. Twenty minutes before the owner's phone buzzes, because by
then a customer is sitting abroad with a receipt and no internet, and that is
worth waking someone for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import ESIM, Order
from app.db.models.enums import OrderStatus
from app.workers.celery_app import celery_app
from app.workers.session import worker_session

logger = get_logger("rescue")

#: A healthy order is fulfilled in seconds. Anything older than this either lost
#: its task or is stuck in backoff, and a second dispatch is harmless because
#: fulfilment is idempotent.
RETRY_AFTER = timedelta(minutes=5)

#: Past this, it is no longer a hiccup. Somebody has paid and has nothing.
ALERT_AFTER = timedelta(minutes=20)

#: A cap, so a genuine outage cannot turn into a thousand queued dispatches or a
#: thousand messages. What was skipped is reported rather than hidden.
MAX_PER_RUN = 25


@celery_app.task(name="rescue.unfulfilled_orders")
def rescue_unfulfilled_orders() -> dict[str, int]:
    """Re-dispatch paid orders that have no eSIM, and escalate the old ones."""
    from app.workers.tasks.provisioning import fulfil_paid_order

    now = datetime.now(UTC)
    with worker_session() as session:
        stale = session.execute(
            select(Order.id, Order.paid_at, Order.created_at)
            .where(
                Order.status == OrderStatus.PAID,
                ~select(ESIM.id).where(ESIM.order_id == Order.id).exists(),
            )
            .order_by(Order.id)
            .limit(MAX_PER_RUN + 1)
        ).all()

    overflow = max(0, len(stale) - MAX_PER_RUN)
    stale = stale[:MAX_PER_RUN]

    redispatched: list[int] = []
    alarming: list[int] = []
    for order_id, paid_at, created_at in stale:
        # paid_at is the honest clock here; older rows predate the column, so
        # created_at stands in rather than making them invisible.
        since = paid_at or created_at
        if since is None:
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        age = now - since
        if age < RETRY_AFTER:
            continue
        redispatched.append(order_id)
        fulfil_paid_order.delay(order_id)
        if age >= ALERT_AFTER:
            alarming.append(order_id)

    if redispatched:
        logger.warning(
            "rescue.redispatched",
            orders=redispatched,
            alarming=alarming,
            skipped=overflow,
        )
    if alarming or overflow:
        _alert(alarming, overflow)

    return {"redispatched": len(redispatched), "alarming": len(alarming), "skipped": overflow}


def _alert(order_ids: list[int], overflow: int) -> None:
    """Tell the owner, now.

    Deliberately not a daily summary: this is the one failure mode where the
    customer already paid. A message that arrives tomorrow morning is a message
    that arrives after the refund request.
    """
    import asyncio

    from app.integrations.telegram import send_html

    lines = ["<b>⚠️ To'landi, lekin eSIM yetkazilmadi</b>"]
    if order_ids:
        listed = ", ".join(f"#{oid}" for oid in order_ids[:10])
        more = f" va yana {len(order_ids) - 10} ta" if len(order_ids) > 10 else ""
        lines.append(f"20 daqiqadan oshgan buyurtmalar: {listed}{more}")
        lines.append("Qayta yuborildi. Agar keyingi tekshiruvda ham chiqsa — ta'minotchida muammo.")
    if overflow:
        lines.append(f"Yana {overflow} ta buyurtma navbatda — bu yugurishda tegilmadi.")

    try:
        asyncio.run(send_html("\n".join(lines)))
    except Exception:
        # An alert that fails must not fail the rescue: the re-dispatch above is
        # the part that actually helps the customer.
        logger.exception("rescue.alert_failed")
