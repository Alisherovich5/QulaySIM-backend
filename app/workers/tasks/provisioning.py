"""Supplier provisioning, moved off the request path.

The webhook now enqueues these tasks, so a slow or failing supplier costs a
worker retry instead of a blocked API worker.
"""

from __future__ import annotations

from celery import Task

from app.core.logging import get_logger
from app.db.models import Order
from app.workers.celery_app import celery_app
from app.workers.session import worker_session

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
        if order.provider != "esimaccess":
            # Profile polling is supplier-specific and only eSIM Access has it.
            # An order routed elsewhere has been paid for and placed, so silence
            # here would mean a customer waiting on an eSIM nobody is fetching.
            logger.error(
                "provisioning.no_sync_for_provider",
                order_id=order_id,
                provider=order.provider,
                provider_order_no=order.provider_order_no,
            )
            return 0

        from app.integrations.esim_access import EsimAccessError, sync_order_profiles

        try:
            count = sync_order_profiles(session, order)
        except EsimAccessError as exc:
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

    if not already_ordered:
        placed = _place_supplier_order(order_id, attempt=self.request.retries)
        if placed:
            synchronise_supplier_order.delay(order_id)
    else:
        synchronise_supplier_order.delay(order_id)

    grant_referral_reward.delay(customer_id)

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
    from app.integrations.suppliers import SupplierError, usable_routes_for

    if not settings.supplier_calls_enabled:
        logger.info("fulfil.supplier_skipped", order_id=order_id, provider="mock")
        return False

    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            return False

        routes = usable_routes_for(order)
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
                    transaction_id=f"qs-{order_id}", lines=list(route.lines)
                )
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

        logger.error(
            "fulfil.every_supplier_refused", order_id=order_id, routes=len(routes)
        )
        if last_error is not None:
            raise last_error
        return False


def get_supplier_or_none(provider: str):
    """Thin indirection so tests can register a stub supplier."""
    from app.integrations.suppliers import get_supplier

    return get_supplier(provider)
