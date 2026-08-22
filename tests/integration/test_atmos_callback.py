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
    # The payment is recorded once — that part must never double. Fulfilment is
    # dispatched again, and deliberately so: the first dispatch may have been
    # lost (Redis unreachable for a moment, or the task exhausting its retries),
    # and ATMOS's retry was the only signal that anything was wrong. Answering
    # OK without dispatching is what left orders paid and undelivered until
    # somebody read a daily report.
    assert _configured == [order_id, order_id]


async def test_a_replay_does_not_reorder_once_a_supplier_has_the_order(
    session_factory, _configured
):
    """The one way this safety net could cost money instead of saving it.

    If a supplier order already exists, what is missing is the profile sync, not
    the purchase — and dispatching a purchase alongside one already in flight
    could buy the same eSIM twice. That case is left to the five-minute sweep,
    by which time no task is running.
    """
    order_id = await _order(session_factory)
    payload = _payload(order_id, "5000000")
    async with session_factory() as session:
        await service.handle_callback(session, payload)
    _configured.clear()

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        order.provider_order_no = "SUPPLIER-123"
        await session.commit()

    async with session_factory() as session:
        assert (await service.handle_callback(session, payload))["status"] == 1
    assert _configured == []


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


class TestOnlyARealPaymentWakesSomebody:
    """The refusal alarm must not fire on internet noise.

    A refused callback means somebody may have been charged for nothing, which
    is worth waking an operator for — that is why the alarm exists. But the
    endpoint's address is guessable, scanners probe everything, and the first
    person it woke was the engineer's own test request. An alarm that fires on
    scanner traffic is an alarm that stops being read, and then the next real
    refusal goes unseen for the same reason as the first one did.
    """

    async def _post(self, monkeypatch, session_factory, body: dict) -> list[str]:
        from app.api.v1.routers import atmos as router

        alarms: list[str] = []

        async def fake_alarm(ip: str) -> None:
            alarms.append(ip)

        monkeypatch.setattr(router.service, "alarm_bad_ip", fake_alarm)
        # An address that is not in ATMOS's published range, which is the whole
        # premise: the callback is refused and the question is only whether a
        # phone buzzes.
        monkeypatch.setattr(router.service, "caller_allowed", lambda _ip: False)

        class _Request:
            def __init__(self) -> None:
                self.headers = {"x-forwarded-for": "198.51.100.7"}
                self.client = type("Peer", (), {"host": "198.51.100.7"})()

            async def json(self):
                return body

        async with session_factory() as session:
            answer = await router.atmos_callback(_Request(), session)
        assert answer["status"] == 0, "a bad address must still be refused"
        return alarms

    async def test_an_empty_probe_is_refused_silently(self, monkeypatch, session_factory) -> None:
        assert await self._post(monkeypatch, session_factory, {"probe": "hello"}) == []

    async def test_something_that_names_a_transaction_and_an_order_raises_it(
        self, monkeypatch, session_factory
    ) -> None:
        body = {"transaction_id": "tx-9", "account": "70", "amount": "7999900"}
        assert await self._post(monkeypatch, session_factory, body) == ["198.51.100.7"]
