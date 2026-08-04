"""The ATMOS callback, end to end against a real (throwaway) database.

SQLite via aiosqlite, schema from the SQLAlchemy metadata — deliberately not
the shared Postgres: its orders_atmostransaction table arrives with a Django
migration at deploy time, and these tests must prove the logic before that.

Every test answers the one question that matters: does the handler say 1 only
when it should? A wrong 1 is a captured payment for an order we cannot honour;
a wrong 0 is a customer whose money ATMOS refuses to take.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base
from app.db.models import AtmosTransaction, Customer, Order, Payment
from app.services import atmos as service

pytestmark = pytest.mark.asyncio

STORE = 77
API_KEY = "api-key-x"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "atmos_store_id", STORE)
    monkeypatch.setattr(settings, "atmos_callback_api_key", API_KEY)
    # The queue is not running in tests; note who was enqueued instead.
    calls: list[int] = []
    import app.workers.tasks.provisioning as provisioning

    monkeypatch.setattr(
        provisioning.fulfil_paid_order, "delay", lambda order_id: calls.append(order_id)
    )
    yield calls


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _order(factory, amount_uzs: str = "50000.00") -> int:
    async with factory() as session:
        customer = Customer(email="pay@example.com")
        session.add(customer)
        await session.flush()
        order = Order(customer_id=customer.id, amount_uzs=Decimal(amount_uzs))
        session.add(order)
        await session.commit()
        return order.id


def _payload(order_id: int, amount: str, transaction_id: str = "tx-1") -> dict:
    sign = service.expected_sign(str(STORE), transaction_id, str(order_id), amount)
    return {
        "store_id": str(STORE),
        "transaction_id": transaction_id,
        "account": str(order_id),
        "amount": amount,
        "sign": sign,
    }


async def test_a_payable_order_is_confirmed_paid_and_fulfilment_enqueued(
    session_factory, _configured
):
    order_id = await _order(session_factory)  # 50 000.00 som = 5 000 000 tiyin
    async with session_factory() as session:
        answer = await service.handle_callback(session, _payload(order_id, "5000000"))
    assert answer["status"] == 1

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == "paid"
        assert order.paid_at is not None
        assert order.provider_transaction_id == "tx-1"
        payment = (await session.execute(select(Payment))).scalars().one()
        assert payment.method == "atmos"
        assert payment.amount == Decimal("50000.00")
    assert _configured == [order_id]


async def test_a_replayed_callback_answers_yes_without_double_recording(
    session_factory, _configured
):
    order_id = await _order(session_factory)
    payload = _payload(order_id, "5000000")
    async with session_factory() as session:
        assert (await service.handle_callback(session, payload))["status"] == 1
    async with session_factory() as session:
        assert (await service.handle_callback(session, payload))["status"] == 1

    async with session_factory() as session:
        rows = (await session.execute(select(AtmosTransaction))).scalars().all()
        payments = (await session.execute(select(Payment))).scalars().all()
    assert len(rows) == 1
    assert len(payments) == 1
    assert _configured == [order_id], "fulfilment must be enqueued exactly once"


async def test_a_wrong_amount_is_refused_and_recorded(session_factory, _configured):
    order_id = await _order(session_factory)
    async with session_factory() as session:
        answer = await service.handle_callback(session, _payload(order_id, "4999999"))
    assert answer["status"] == 0

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        row = (await session.execute(select(AtmosTransaction))).scalars().one()
    assert order.status == "pending"
    assert row.status == service.STATUS_REJECTED
    assert _configured == []


async def test_a_forged_signature_is_refused_before_the_database_is_consulted(
    session_factory, _configured
):
    order_id = await _order(session_factory)
    payload = _payload(order_id, "5000000")
    payload["sign"] = "0" * 32
    async with session_factory() as session:
        answer = await service.handle_callback(session, payload)
    assert answer["status"] == 0
    async with session_factory() as session:
        assert (await session.execute(select(AtmosTransaction))).scalars().all() == []


async def test_an_unknown_account_is_refused(session_factory, _configured):
    async with session_factory() as session:
        answer = await service.handle_callback(session, _payload(424242, "5000000"))
    assert answer["status"] == 0
    assert _configured == []


async def test_an_already_paid_order_rejects_a_second_transaction(session_factory, _configured):
    order_id = await _order(session_factory)
    async with session_factory() as session:
        first = await service.handle_callback(session, _payload(order_id, "5000000"))
    assert first["status"] == 1
    # A different transaction against the same, now-paid order.
    async with session_factory() as session:
        answer = await service.handle_callback(
            session, _payload(order_id, "5000000", transaction_id="tx-2")
        )
    assert answer["status"] == 0
    assert _configured == [order_id]
