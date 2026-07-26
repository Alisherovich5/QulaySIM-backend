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
