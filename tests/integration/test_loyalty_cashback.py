"""Repeat-purchase cashback: paid once per order, and only to the buyer.

Two ways this costs money if it is wrong. A retried Celery task could mint a
second code for one purchase — the task retries on any exception, so that is a
normal event, not a rare one. And an unbound code is a public discount: post
QAYT-41 in a Telegram group and everyone's order is 5% off.

Against SQLite for the same reason as the other worker tests: the columns arrive
with a Django migration at deploy time and this logic has to be proven first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import Customer, Order, PromoCode
from app.domain.referral import LOYALTY_PREFIX


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    from contextlib import contextmanager

    import app.workers.session as worker_session_module

    @contextmanager
    def _session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(worker_session_module, "worker_session", _session)
    import app.workers.tasks.provisioning as provisioning

    monkeypatch.setattr(provisioning, "worker_session", _session)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _scheme_on(monkeypatch):
    monkeypatch.setattr(settings, "loyalty_cashback_percent", 5)
    monkeypatch.setattr(settings, "loyalty_cashback_from_order", 2)
    monkeypatch.setattr(settings, "loyalty_cashback_valid_days", 90)


def _customer(factory, *, paid_orders: int) -> tuple[int, list[int]]:
    with factory() as session:
        customer = Customer(email="repeat@example.com")
        session.add(customer)
        session.flush()
        ids = []
        for _ in range(paid_orders):
            order = Order(customer_id=customer.id, status="paid", amount_uzs=Decimal("60000"))
            session.add(order)
            session.flush()
            ids.append(order.id)
        session.commit()
        return customer.id, ids


def _grant(customer_id: int, order_id: int):
    from app.workers.tasks.provisioning import grant_loyalty_cashback

    return grant_loyalty_cashback(customer_id, order_id)


class TestWhenItPays:
    def test_the_first_purchase_earns_nothing(self, session_factory):
        customer_id, orders = _customer(session_factory, paid_orders=1)
        assert _grant(customer_id, orders[0]) is None
        with session_factory() as session:
            assert session.execute(select(PromoCode)).scalars().all() == []

    def test_the_second_purchase_earns_a_code(self, session_factory):
        customer_id, orders = _customer(session_factory, paid_orders=2)
        code = _grant(customer_id, orders[1])
        assert code == f"{LOYALTY_PREFIX}{orders[1]}"

        with session_factory() as session:
            promo = session.execute(select(PromoCode)).scalars().one()
        assert promo.discount_type == "percent"
        assert promo.discount_value == 5
        assert promo.max_uses == 1
        assert promo.reason == "loyalty"
        # Bound to the buyer. Unbound, it would discount anyone who saw it.
        assert promo.issued_to_id == customer_id
        assert promo.valid_until is not None

    def test_every_later_purchase_earns_its_own(self, session_factory):
        customer_id, orders = _customer(session_factory, paid_orders=4)
        codes = [_grant(customer_id, oid) for oid in orders]
        # First earns nothing; the other three each earn one.
        assert codes[0] is None
        assert len([c for c in codes if c]) == 3
        with session_factory() as session:
            assert len(session.execute(select(PromoCode)).scalars().all()) == 3


class TestItCannotPayTwice:
    def test_a_retry_mints_nothing_new(self, session_factory):
        customer_id, orders = _customer(session_factory, paid_orders=2)
        first = _grant(customer_id, orders[1])
        second = _grant(customer_id, orders[1])
        assert first is not None
        # The retry collides with the unique code rather than granting again.
        assert second is None
        with session_factory() as session:
            assert len(session.execute(select(PromoCode)).scalars().all()) == 1

    def test_five_retries_still_one_code(self, session_factory):
        customer_id, orders = _customer(session_factory, paid_orders=3)
        for _ in range(5):
            _grant(customer_id, orders[2])
        with session_factory() as session:
            assert len(session.execute(select(PromoCode)).scalars().all()) == 1


class TestGuards:
    def test_the_scheme_can_be_switched_off(self, session_factory, monkeypatch):
        monkeypatch.setattr(settings, "loyalty_cashback_percent", 0)
        customer_id, orders = _customer(session_factory, paid_orders=3)
        assert _grant(customer_id, orders[2]) is None

    def test_an_order_belonging_to_someone_else_earns_nothing(self, session_factory):
        _, orders = _customer(session_factory, paid_orders=2)
        with session_factory() as session:
            stranger = Customer(email="stranger@example.com")
            session.add(stranger)
            session.commit()
            stranger_id = stranger.id
        assert _grant(stranger_id, orders[1]) is None

    def test_the_qualifying_order_is_configurable(self, session_factory, monkeypatch):
        monkeypatch.setattr(settings, "loyalty_cashback_from_order", 3)
        customer_id, orders = _customer(session_factory, paid_orders=2)
        assert _grant(customer_id, orders[1]) is None, "two orders, threshold three"


@pytest.mark.asyncio
async def test_a_bound_code_is_invisible_to_everyone_else():
    """The redemption side of the binding, where the money actually moves."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.repositories import orders as repo

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        owner = Customer(email="owner@example.com")
        thief = Customer(email="thief@example.com")
        session.add_all([owner, thief])
        await session.flush()
        session.add(
            PromoCode(
                code="QAYT-41",
                discount_type="percent",
                discount_value=5,
                max_uses=1,
                is_active=True,
                reason="loyalty",
                issued_to_id=owner.id,
                valid_until=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        # An ordinary campaign code, open to anyone.
        session.add(
            PromoCode(
                code="SUMMER",
                discount_type="percent",
                discount_value=10,
                max_uses=0,
                is_active=True,
            )
        )
        await session.commit()
        owner_id, thief_id = owner.id, thief.id

    async with factory() as session:
        assert await repo.get_promo_by_code(session, "QAYT-41", customer_id=owner_id) is not None
        # Not "invalid" with a hint that it is real — simply not found, so a
        # stranger learns nothing by guessing.
        assert await repo.get_promo_by_code(session, "QAYT-41", customer_id=thief_id) is None
        assert await repo.get_promo_by_code(session, "QAYT-41") is None, "anonymous cart"
        # An unbound campaign code still works for everyone.
        assert await repo.get_promo_by_code(session, "SUMMER", customer_id=thief_id) is not None
        assert await repo.get_promo_by_code(session, "SUMMER") is not None

    await engine.dispose()
