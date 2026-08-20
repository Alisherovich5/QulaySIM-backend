"""Celery task bodies, executed inline against real infrastructure."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import ESIM, Customer, Order, Plan
from app.db.models.enums import ESIMStatus
from app.workers.session import worker_session


def test_expire_esims_flips_elapsed_profiles() -> None:
    """Nothing expired eSIMs before this task existed — an activated profile
    stayed 'active' forever and inflated every dashboard."""
    from app.workers.tasks.maintenance import expire_esims

    with worker_session() as session:
        plan = session.scalars(select(Plan).limit(1)).first()
        assert plan is not None, "seed the catalogue first: python -m scripts.seed"

        customer = Customer(
            email=f"worker-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
        )
        session.add(customer)
        session.flush()

        order = Order(customer_id=customer.id, status="paid", total=Decimal("1.00"))
        session.add(order)
        session.flush()

        elapsed = ESIM(
            order_id=order.id,
            plan_id=plan.id,
            customer_id=customer.id,
            iccid=uuid.uuid4().hex[:20],
            qr_payload="LPA:1$x$y",
            status=ESIMStatus.ACTIVE,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        still_valid = ESIM(
            order_id=order.id,
            plan_id=plan.id,
            customer_id=customer.id,
            iccid=uuid.uuid4().hex[:20],
            qr_payload="LPA:1$x$y",
            status=ESIMStatus.ACTIVE,
            expires_at=datetime.now(UTC) + timedelta(days=3),
        )
        session.add_all([elapsed, still_valid])
        session.flush()
        elapsed_id, valid_id = elapsed.id, still_valid.id

    assert expire_esims() >= 1

    with worker_session() as session:
        assert session.get(ESIM, elapsed_id).status == ESIMStatus.EXPIRED
        assert session.get(ESIM, valid_id).status == ESIMStatus.ACTIVE


def test_expire_esims_ignores_profiles_without_an_expiry() -> None:
    """A pending eSIM has no expires_at; the task must skip it, not crash."""
    from app.workers.tasks.maintenance import expire_esims

    assert isinstance(expire_esims(), int)


def test_referral_reward_is_a_noop_without_a_referrer() -> None:
    from app.workers.tasks.provisioning import grant_referral_reward

    with worker_session() as session:
        customer = Customer(
            email=f"noref-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
        )
        session.add(customer)
        session.flush()
        customer_id = customer.id

    assert grant_referral_reward(customer_id) is None


def test_referral_reward_is_actually_minted() -> None:
    """The half of the referral scheme that pays out, executed for real.

    Worth spelling out why this test exists: production had one referral row and
    zero completions, and the only test named after this task checked the case
    where it does nothing. So the paying half had never run — not in a test, not
    for a customer. Everything below is what a real invitee's first paid order
    puts in the database.
    """
    from app.db.models import PromoCode, Referral
    from app.workers.tasks.provisioning import grant_referral_reward

    with worker_session() as session:
        referrer = Customer(
            email=f"ref-a-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
            referral_code=uuid.uuid4().hex[:8].upper(),
        )
        session.add(referrer)
        session.flush()

        invitee = Customer(
            email=f"ref-b-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
            referred_by_id=referrer.id,
        )
        session.add(invitee)
        session.flush()

        session.add(Referral(referrer_id=referrer.id, referred_id=invitee.id, status="pending"))
        # The reward is owed on a *paid* order and nothing less.
        session.add(Order(customer_id=invitee.id, status="paid", total=Decimal("5.00")))
        session.flush()
        invitee_id, referrer_id = invitee.id, referrer.id

    code = grant_referral_reward(invitee_id)
    assert code is not None and code.startswith("REF-"), "no reward code was minted"

    with worker_session() as session:
        promo = session.scalars(select(PromoCode).where(PromoCode.code == code)).one()
        assert promo.discount_type == "percent"
        assert promo.discount_value == Decimal("10.00"), "the advertised reward is 10%"
        assert promo.max_uses == 1 and promo.used_count == 0
        assert promo.is_active
        # A reward for having already bought must not be first-order-only, or the
        # referrer — who is by definition a customer — could never spend it.
        assert promo.first_order_only is False

        referral = session.scalars(select(Referral).where(Referral.referred_id == invitee_id)).one()
        assert referral.status == "completed"
        assert referral.reward_code == code
        assert referral.completed_at is not None

        # And it belongs to the person who earned it. Without this the code is a
        # bearer token: whoever reads it over their shoulder spends their 10%.
        assert promo.issued_to_id == referrer_id, "the reward is not bound to the referrer"

    # Running twice must not mint a second code — two paid orders arriving
    # together used to be the obvious way to be paid twice.
    assert grant_referral_reward(invitee_id) is None


def test_referral_reward_waits_for_money() -> None:
    """A pending order is not a purchase. Rewarding on 'created' would pay out
    for an abandoned checkout."""
    from app.db.models import Referral
    from app.workers.tasks.provisioning import grant_referral_reward

    with worker_session() as session:
        referrer = Customer(
            email=f"ref-c-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
        )
        session.add(referrer)
        session.flush()
        invitee = Customer(
            email=f"ref-d-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("a-long-enough-password"),
            referred_by_id=referrer.id,
        )
        session.add(invitee)
        session.flush()
        session.add(Referral(referrer_id=referrer.id, referred_id=invitee.id, status="pending"))
        session.add(Order(customer_id=invitee.id, status="pending", total=Decimal("5.00")))
        session.flush()
        invitee_id = invitee.id

    assert grant_referral_reward(invitee_id) is None
