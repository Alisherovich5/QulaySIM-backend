"""Supplier provisioning, moved off the request path.

The webhook now enqueues these tasks, so a slow or failing supplier costs a
worker retry instead of a blocked API worker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from celery import Task

from app.core.logging import get_logger
from app.db.models import ESIM, Order, OrderItem
from app.workers.celery_app import celery_app
from app.workers.session import worker_session

if TYPE_CHECKING:
    from app.integrations.suppliers import Supplier

logger = get_logger(__name__)


@celery_app.task(
    name="provisioning.synchronise_supplier_order",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=8,
)
def synchronise_supplier_order(self: Task, order_id: int) -> int:
    """Pull supplier profiles for one order and persist them.

    Supplier code 200010 means "allocation still in progress" — that is a
    retry, not a failure, so it is raised and left to the backoff policy.
    """
    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            logger.warning("provisioning.order_missing", order_id=order_id)
            return 0
        if not order.provider_order_no:
            return 0
        # Profile polling is supplier-specific: each wholesaler hands profiles
        # over in its own shape and on its own schedule. An order routed to a
        # supplier with no sync has been paid for and placed, so silence here
        # would mean a customer waiting on an eSIM nobody is fetching.
        if order.provider == "esimaccess":
            from app.integrations.esim_access import EsimAccessError as SupplierSyncError
            from app.integrations.esim_access import sync_order_profiles
        elif order.provider == "esimcard":
            # Same names, different suppliers — the branch picks one pair, and
            # mypy cannot express "either of these two shapes" here.
            from app.integrations.esimcard import (  # type: ignore[assignment]
                EsimCardError as SupplierSyncError,
            )
            from app.integrations.esimcard_sync import (  # type: ignore[assignment]
                sync_order_profiles,
            )
        else:
            logger.error(
                "provisioning.no_sync_for_provider",
                order_id=order_id,
                provider=order.provider,
                provider_order_no=order.provider_order_no,
            )
            return 0

        try:
            count = sync_order_profiles(session, order)
        except SupplierSyncError as exc:
            logger.warning(
                "provisioning.supplier_error",
                order_id=order_id,
                attempt=self.request.retries,
                error=str(exc),
            )
            raise

    logger.info("provisioning.synchronised", order_id=order_id, profiles=count)
    return count


@celery_app.task(
    name="provisioning.grant_loyalty_cashback",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def grant_loyalty_cashback(customer_id: int, order_id: int) -> str | None:
    """Cashback for coming back — a single-use code from the second order on.

    Earned per order, so a customer who buys five times gets four codes. The
    percentage, the qualifying order and the expiry are all settings, because
    they are marketing levers the business will move and moving them should not
    need new logic.

    Two things keep it from paying twice for the same purchase. The code carries
    the order id, and the unique constraint on `PromoCode.code` means a retried
    task collides instead of minting a second reward — the same trick the ATMOS
    callback uses. And the code is bound to the customer who earned it, so it
    cannot be passed around; an unbound reward posted in a group chat is a
    site-wide sale nobody approved.

    Silent when the scheme is off (percent 0) or the order is their first.
    """
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from app.core.config import settings
    from app.db.base import utcnow
    from app.db.models import Order, PromoCode
    from app.domain.referral import LOYALTY_PREFIX

    if settings.loyalty_cashback_percent <= 0:
        return None

    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None or order.customer_id != customer_id:
            return None

        # This order's rank in the customer's history — not how many they have
        # now. The two differ whenever the task runs late: a retry a week after
        # the fact would otherwise see five paid orders and reward the customer's
        # very first purchase, which is exactly what the scheme is not for.
        # Ranked by id because ids are monotonic and paid_at can be null on rows
        # that predate the column.
        rank = session.scalar(
            select(func.count(Order.id)).where(
                Order.customer_id == customer_id,
                Order.status == "paid",
                Order.id <= order_id,
            )
        )
        if (rank or 0) < settings.loyalty_cashback_from_order:
            return None

        # Derived from the order, not random: this is what makes the whole task
        # idempotent. A retry builds the same code and the unique index refuses
        # it, instead of quietly handing out a second discount.
        code = f"{LOYALTY_PREFIX}{order_id}"
        valid_until = utcnow() + timedelta(days=settings.loyalty_cashback_valid_days)
        session.add(
            PromoCode(
                code=code,
                discount_type="percent",
                discount_value=settings.loyalty_cashback_percent,
                max_uses=1,
                used_count=0,
                is_active=True,
                valid_until=valid_until,
                reason="loyalty",
                issued_to_id=customer_id,
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            logger.info("loyalty.already_granted", customer_id=customer_id, order_id=order_id)
            return None

    logger.info(
        "loyalty.granted",
        customer_id=customer_id,
        order_id=order_id,
        code=code,
        percent=settings.loyalty_cashback_percent,
    )
    return code


@celery_app.task(
    name="provisioning.grant_referral_reward",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def grant_referral_reward(customer_id: int) -> str | None:
    """Grant the referrer a single-use code after the invitee's first paid order.

    Runs inside one transaction with `SELECT ... FOR UPDATE` on the referral row
    so two concurrent paid orders cannot both mint a reward.
    """
    from sqlalchemy import select

    from app.db.models import Customer, Order, PromoCode, Referral
    from app.domain.referral import REWARD_PERCENT, new_reward_code

    with worker_session() as session:
        customer = session.get(Customer, customer_id)
        if customer is None or not customer.referred_by_id:
            return None

        paid = session.scalar(
            select(Order.id)
            .where(Order.customer_id == customer_id, Order.status == "paid")
            .limit(2)
        )
        if paid is None:
            return None

        referral = session.scalars(
            select(Referral)
            .where(Referral.referred_id == customer_id, Referral.status == "pending")
            .with_for_update(skip_locked=True)
        ).first()
        if referral is None:
            return None  # already rewarded, or another worker holds the row

        code = new_reward_code()
        session.add(
            PromoCode(
                code=code,
                discount_type="percent",
                discount_value=REWARD_PERCENT,
                max_uses=1,
                used_count=0,
                is_active=True,
            )
        )
        referral.status = "completed"
        referral.reward_code = code
        from app.db.base import utcnow

        referral.completed_at = utcnow()

    logger.info("referral.rewarded", customer_id=customer_id, code=code)
    return code


@celery_app.task(
    name="provisioning.fulfil_paid_order",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=10,
)
def fulfil_paid_order(self: Task, order_id: int) -> str:
    """Everything that happens once money is captured.

    Runs off the request because Payme is waiting on the PerformTransaction
    response: a slow supplier here would become a timeout Payme retries, and
    the retry would find the order already paid.

    Ordered so the customer-visible part comes first and the reward last —
    if the reward fails the retry must not re-place a supplier order, which is
    why the supplier step is skipped once provider_order_no is set.
    """
    from app.db.models.enums import OrderStatus

    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            logger.warning("fulfil.order_missing", order_id=order_id)
            return "missing"
        if order.status != OrderStatus.PAID:
            logger.warning("fulfil.order_not_paid", order_id=order_id, status=order.status)
            return "not_paid"

        redeemed = _redeem_promo(session, order)
        customer_id = order.customer_id
        already_ordered = bool(order.provider_order_no)
        # A top-up adds data to a profile that already exists: there is no new
        # eSIM to buy, no QR to deliver, and the supplier call is a different
        # endpoint. Detected from the line's own plan rather than a flag on the
        # order, so a mixed cart could not be half-handled.
        topup_lines = [
            (
                item.id,
                item.esim_id,
                item.plan.provider_package_code,
                item.plan.data_amount_mb,
                item.plan.validity_days,
            )
            for item in order.items
            if item.esim_id and item.plan is not None and item.plan.scope == "topup"
        ]

    if topup_lines:
        _apply_topups(order_id, topup_lines)
        from app.workers.tasks.reports import announce_sale

        announce_sale.apply_async((order_id,), countdown=25)
        logger.info("fulfil.topup_done", order_id=order_id, lines=len(topup_lines))
        return "topup"

    if not already_ordered:
        placed = _place_supplier_order(order_id, attempt=self.request.retries)
        if placed:
            synchronise_supplier_order.delay(order_id)
    else:
        synchronise_supplier_order.delay(order_id)

    grant_referral_reward.delay(customer_id)
    grant_loyalty_cashback.delay(customer_id, order_id)

    # Announce the sale to the operations chat. Delayed so the supplier has
    # usually returned a profile by the time the message is built — the note
    # reports whatever it finds, and "eSIM still coming" reads worse than it
    # needs to when the profile lands two seconds later. Guarded against
    # duplicates in the task itself, because this function retries.
    from app.workers.tasks.reports import announce_sale

    announce_sale.apply_async((order_id,), countdown=25)

    logger.info("fulfil.done", order_id=order_id, promo_redeemed=redeemed)
    return "ok"


def _redeem_promo(session: object, order: Order) -> bool:
    """Count the promo use now that the money is real.

    Uses an atomic UPDATE with the cap in the WHERE clause, so two orders
    redeeming the last remaining use cannot both succeed.
    """
    from sqlalchemy import or_, update

    from app.db.models import PromoCode

    if not order.promo_code_id:
        return False

    result = session.execute(  # type: ignore[attr-defined]
        update(PromoCode)
        .where(
            PromoCode.id == order.promo_code_id,
            or_(PromoCode.max_uses == 0, PromoCode.used_count < PromoCode.max_uses),
        )
        .values(used_count=PromoCode.used_count + 1)
    )
    redeemed = bool(result.rowcount)
    if not redeemed:
        # The order is already paid, so this is reported rather than refused.
        logger.warning("fulfil.promo_over_limit", order_id=order.id)
    return redeemed


def _apply_topups(order_id: int, lines: list[tuple[int, int, str, int, int]]) -> None:
    """Buy the extra data and put it on the customer's profile.

    Ordered before the local row is updated, and the local row is only updated
    once the wholesaler has confirmed: the alternative is telling a customer they
    have 5 GB more when nothing was bought, which is worse than telling them
    nothing yet — the retry will fix a missing update, but it cannot take back a
    promise the network already made.

    `transaction_id` is the order line's own id, so a retry after a timeout is
    deduplicated by the wholesaler instead of buying twice.
    """
    from app.core.config import settings
    from app.db.base import utcnow
    from app.integrations.esim_access import EsimAccessClient, EsimAccessError

    client = EsimAccessClient()
    if not client.is_configured or not settings.supplier_calls_enabled:
        logger.info("topup.supplier_skipped", order_id=order_id)
        return

    for item_id, esim_id, package_code, data_mb, days in lines:
        with worker_session() as session:
            item = session.get(OrderItem, item_id)
            if item is None or item.topup_applied_at is not None:
                # Already applied on an earlier attempt. The retry is meant to be
                # cheap here, not to buy a second package.
                continue
            esim = session.get(ESIM, esim_id)
            if esim is None or not esim.iccid:
                logger.error("topup.esim_missing", order_id=order_id, esim_id=esim_id)
                continue
            iccid = esim.iccid

        response = client.topup(
            transaction_id=f"topup-{item_id}", package_code=package_code, iccid=iccid
        )
        if not response.get("success"):
            # Raised, not swallowed: the customer has paid, so this must reach the
            # retry policy and then the alert rather than ending here quietly.
            raise EsimAccessError(
                f"top-up refused: {response.get('errorCode')} {response.get('errorMsg')}"
            )

        with worker_session() as session:
            esim = session.get(ESIM, esim_id)
            item = session.get(OrderItem, item_id)
            if esim is None or item is None:
                continue
            esim.data_total_mb = (esim.data_total_mb or 0) + data_mb
            # The package carries its own validity. Extending from whichever is
            # later means a top-up bought early does not shorten the window, and
            # one bought after expiry starts a fresh one.
            base = esim.expires_at or utcnow()
            esim.expires_at = max(base, utcnow()) + timedelta(days=days)
            if esim.status == "expired":
                esim.status = "active"
            item.topup_applied_at = utcnow()
            session.commit()
            logger.info(
                "topup.applied",
                order_id=order_id,
                esim_id=esim_id,
                added_mb=data_mb,
                total_mb=esim.data_total_mb,
            )


def _place_supplier_order(order_id: int, *, attempt: int) -> bool:
    """Order the eSIM profiles, from the cheapest supplier that can fulfil them.

    `transaction_id` is our order id so the supplier deduplicates a retry
    instead of allocating a second set of profiles we would pay for twice. That
    also makes the fallback safe: if the cheapest supplier failed *after*
    accepting, retrying it later cannot double-allocate.

    Falls through to dearer suppliers rather than giving up, because a paid
    order left unfulfilled costs more than the price difference. The exception
    is only re-raised once every route has refused, so Celery's retry covers a
    genuine outage rather than a single supplier having a bad minute.
    """
    from app.core.config import settings
    from app.integrations.suppliers import SupplierCommittedError, SupplierError, usable_routes_for

    if not settings.supplier_calls_enabled:
        logger.info("fulfil.supplier_skipped", order_id=order_id, provider="mock")
        return False

    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            return False

        routes = usable_routes_for(order)

        # Once a wholesaler has been paid for part of this order, it is the only
        # candidate. Cheapest-first is the right rule for a fresh order and the
        # wrong one for a retry: the ledger stops eSIMCard from being charged
        # twice, but nothing stops a *different* supplier from selling us the
        # whole order again, and after a rollback `order.provider` cannot be
        # trusted to remember where the money went.
        pinned = _pinned_provider(session, order_id)
        if pinned:
            routes = [route for route in routes if route.provider == pinned]
            if not routes:
                logger.error("fulfil.pinned_provider_unusable", order_id=order_id, provider=pinned)
                return False

        if not routes:
            # Either nothing is mapped to a supplier, or a mixed cart has no
            # single supplier able to cover every line. Both leave a paid order
            # unfulfilled, so both are reported at error level.
            logger.error(
                "fulfil.no_supplier_route",
                order_id=order_id,
                plans=[item.plan_id for item in order.items],
            )
            return False

        last_error: SupplierError | None = None
        for index, route in enumerate(routes):
            supplier = get_supplier_or_none(route.provider)
            if supplier is None:  # pragma: no cover - filtered by usable_routes_for
                continue
            try:
                order_no = supplier.place_order(
                    db=session,
                    order_id=order_id,
                    transaction_id=f"qs-{order_id}",
                    lines=list(route.lines),
                )
            except SupplierCommittedError as exc:
                # Part of the order is already bought. Trying the next supplier
                # would buy the whole order again, so the only safe move is to
                # stop; the purchase ledger makes the Celery retry skip whatever
                # was already paid for, and `_pinned_provider` keeps that retry
                # at this supplier instead of letting a cheaper one win again.
                logger.error(
                    "fulfil.supplier_partially_committed",
                    order_id=order_id,
                    provider=route.provider,
                    attempt=attempt,
                    error=str(exc),
                )
                raise
            except SupplierError as exc:
                last_error = exc
                logger.warning(
                    "fulfil.supplier_order_failed",
                    order_id=order_id,
                    provider=route.provider,
                    attempt=attempt,
                    fallbacks_left=len(routes) - index - 1,
                    error=str(exc),
                )
                continue

            order.provider = route.provider
            order.provider_order_no = order_no
            order.provider_status = "ORDERED"
            logger.info(
                "fulfil.supplier_ordered",
                order_id=order_id,
                provider=route.provider,
                cost_usd=str(route.total_cost_usd),
                was_fallback=index > 0,
            )
            return True

        logger.error("fulfil.every_supplier_refused", order_id=order_id, routes=len(routes))
        if last_error is not None:
            raise last_error
        return False


def _pinned_provider(session: object, order_id: int) -> str | None:
    """The wholesaler that already holds purchases for this order, if any.

    Anything other than a clean refusal counts: a "claimed" row means money may
    have moved, and treating "may" as "did not" is how an order gets bought
    twice.
    """
    from app.db.models import SupplierPurchase

    row = (
        session.query(SupplierPurchase)  # type: ignore[attr-defined]
        .filter(
            SupplierPurchase.order_id == order_id,
            SupplierPurchase.state != "failed",
        )
        .first()
    )
    return row.provider if row else None


def get_supplier_or_none(provider: str) -> Supplier | None:
    """Thin indirection so tests can register a stub supplier."""
    from app.integrations.suppliers import get_supplier

    return get_supplier(provider)
