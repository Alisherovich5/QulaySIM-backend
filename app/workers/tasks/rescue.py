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

from sqlalchemy import and_, or_, select

from app.core.logging import get_logger
from app.db.models import ESIM, Order, OrderItem
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
        # Two shapes of unfinished work, because a top-up creates no eSIM row: an
        # ordinary order is undelivered when it has no eSIM, and a top-up is
        # undelivered when its line has no applied-at stamp. Without the second
        # clause every top-up ever sold would look unfulfilled forever and the
        # alert would cry wolf until nobody read it.
        no_esim = ~select(ESIM.id).where(ESIM.order_id == Order.id).exists()
        pending_topup = (
            select(OrderItem.id)
            .where(
                OrderItem.order_id == Order.id,
                OrderItem.esim_id.is_not(None),
                OrderItem.topup_applied_at.is_(None),
            )
            .exists()
        )
        has_topup_line = (
            select(OrderItem.id)
            .where(OrderItem.order_id == Order.id, OrderItem.esim_id.is_not(None))
            .exists()
        )
        stale = session.execute(
            select(Order.id, Order.paid_at, Order.created_at)
            .where(
                Order.status == OrderStatus.PAID,
                or_(pending_topup, and_(no_esim, ~has_topup_line)),
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


#: How long one order stays quiet after it has been reported. The rescue runs
#: every five minutes, so without this a single stuck order sends the same
#: message twelve times an hour — which is how an alert stops being read. A day
#: is the floor rather than silence: somebody has paid and has nothing, and an
#: order that nobody fixes should keep asking once, not stop asking.
ALERT_SILENCE = 24 * 60 * 60


def _first_alert(order_ids: list[int]) -> list[int]:
    """The ones not reported recently, marked as reported on the way out.

    Redis rather than a column: the marker is about the message, not about the
    order, and it should disappear on its own. A failure to reach Redis reports
    everything, because a duplicate alert is a smaller problem than a silent
    one.
    """
    import redis

    from app.core.config import settings

    try:
        client = redis.Redis.from_url(str(settings.redis_url))
        fresh = []
        for oid in order_ids:
            if client.set(f"rescue:alerted:{oid}", b"1", ex=ALERT_SILENCE, nx=True):
                fresh.append(oid)
        return fresh
    except Exception:  # noqa: BLE001 - any Redis failure must fall back to alerting
        logger.warning("rescue.alert_dedupe_unavailable")
        return order_ids


def _failure_note(order_ids: list[int]) -> dict[int, str]:
    """The supplier's own words for why each one failed, where there are any.

    "Check the supplier" is what the message used to say, and it is the one
    thing the reader cannot do from their phone. The ledger already holds the
    reason — an empty wallet reads as an empty wallet — so it goes in the
    message and the owner knows whether to top up or to wait.
    """
    from app.db.models import SupplierPurchase

    if not order_ids:
        return {}
    notes: dict[int, str] = {}
    with worker_session() as session:
        rows = session.execute(
            select(SupplierPurchase.order_id, SupplierPurchase.note)
            .where(SupplierPurchase.order_id.in_(order_ids), SupplierPurchase.note != "")
            .order_by(SupplierPurchase.id.desc())
        ).all()
    for order_id, note in rows:
        if note:
            notes.setdefault(order_id, note.strip()[:120])
    return notes


def _alert(order_ids: list[int], overflow: int) -> None:
    """Tell the owner, once.

    Deliberately not a daily summary: this is the one failure mode where the
    customer already paid. A message that arrives tomorrow morning is a message
    that arrives after the refund request. Equally deliberately not every five
    minutes for the same order — an alert repeated until it is background noise
    protects nobody.
    """

    from app.integrations.telegram import send_html_blocking

    fresh = _first_alert(order_ids)
    if not fresh and not overflow:
        return

    notes = _failure_note(fresh)
    lines = ["<b>⚠️ To'landi, lekin eSIM yetkazilmadi</b>"]
    if fresh:
        listed = ", ".join(f"#{oid}" for oid in fresh[:10])
        more = f" va yana {len(fresh) - 10} ta" if len(fresh) > 10 else ""
        lines.append(f"20 daqiqadan oshgan buyurtmalar: {listed}{more}")
        for oid in fresh[:10]:
            if oid in notes:
                lines.append(f"#{oid} — ta'minotchi: <i>{notes[oid]}</i>")
        lines.append(
            "Qayta yuborish har 5 daqiqada davom etadi — sabab bartaraf bo'lsa, o'zi yetkaziladi."
        )
        lines.append("Keyingi eslatma 24 soatdan keyin.")
    if overflow:
        lines.append(f"Yana {overflow} ta buyurtma navbatda — bu yugurishda tegilmadi.")

    try:
        send_html_blocking("\n".join(lines))
    except Exception:
        # An alert that fails must not fail the rescue: the re-dispatch above is
        # the part that actually helps the customer.
        logger.exception("rescue.alert_failed")
