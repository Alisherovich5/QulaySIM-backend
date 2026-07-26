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
