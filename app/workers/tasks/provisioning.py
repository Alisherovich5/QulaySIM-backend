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
        if order.provider != "esimaccess" or not order.provider_order_no:
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
    """Order the eSIM profiles from the supplier.

    `transaction_id` is our order id so the supplier deduplicates a retry
    instead of allocating a second set of profiles we would pay for twice.
    """
    from app.core.config import settings

    if settings.esim_provider != "esimaccess":
        logger.info("fulfil.supplier_skipped", order_id=order_id, provider=settings.esim_provider)
        return False

    from app.integrations.esim_access import (
        EsimAccessClient,
        EsimAccessError,
        EsimAccessPackage,
    )

    with worker_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            return False
        packages = [
            EsimAccessPackage(slug=item.plan.provider_package_code, count=item.quantity)
            for item in order.items
            if item.plan.provider == "esimaccess" and item.plan.provider_package_code
        ]
        if not packages:
            logger.info("fulfil.no_supplier_packages", order_id=order_id)
            return False

        try:
            response = EsimAccessClient().order_profiles(
                transaction_id=f"qs-{order_id}", packages=packages
            )
        except EsimAccessError as exc:
            logger.warning(
                "fulfil.supplier_order_failed",
                order_id=order_id,
                attempt=attempt,
                error=str(exc),
            )
            raise

        order.provider = "esimaccess"
        order.provider_order_no = str((response.get("obj") or {}).get("orderNo") or "")
        order.provider_status = "ORDERED"

    logger.info("fulfil.supplier_ordered", order_id=order_id)
    return True
