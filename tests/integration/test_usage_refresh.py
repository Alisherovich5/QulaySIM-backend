"""Pulling used-data from the wholesaler into our own rows.

Written after the task shipped and failed on its first live run: `is_configured`
is a property and was called as a method, so the whole thing raised before it
touched anything. Nothing tested it, so nothing said so — the task was verified
by reading it, which is exactly the kind of check that misses an attribute error.

These tests drive the real task with a stubbed client, so a signature that stops
matching the client fails here rather than at 20 past the hour in production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models import ESIM, Country, Customer, Order, Plan
from app.db.models.enums import ESIMStatus
from app.workers.tasks import maintenance

pytestmark = pytest.mark.anyio

TRAN_NO = "26080915410004"


class FakeClient:
    """Shaped like EsimAccessClient, including `is_configured` as a property."""

    def __init__(self, profiles: list[dict], configured: bool = True):
        self._profiles = profiles
        self._configured = configured
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def query_profiles(self, *, order_no: str) -> dict:
        self.calls += 1
        return {"success": True, "obj": {"esimList": self._profiles}}


def _profile(**overrides) -> dict:
    base = {
        "esimTranNo": TRAN_NO,
        "esimStatus": "IN_USE",
        "orderUsage": 493_934_361,  # 472 MB, the figure seen in production
        "totalVolume": 1_073_741_824,  # 1 GB
        "expiredTime": "2026-08-16T15:55:27+0000",
    }
    return {**base, **overrides}


def _seed(session, *, tran_no: str = TRAN_NO, provider: str = "esimaccess") -> ESIM:
    country = Country(
        name=f"Usageland {uuid.uuid4().hex[:6]}",
        slug=f"usage-{uuid.uuid4().hex[:8]}",
        iso2=uuid.uuid4().hex[:2].upper(),
    )
    session.add(country)
    session.flush()
    plan = Plan(
        country_id=country.id,
        title="Usageland 1 GB · 7 days",
        data_amount_mb=1024,
        validity_days=7,
        price_usd=Decimal("1.50"),
        cost_usd=Decimal("0.46"),
        provider=provider,
    )
    customer = Customer(
        email=f"usage-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Usage",
    )
    session.add_all([plan, customer])
    session.flush()
    order = Order(customer_id=customer.id, status="paid", paid_at=datetime.now(UTC))
    session.add(order)
    session.flush()
    esim = ESIM(
        order_id=order.id,
        plan_id=plan.id,
        customer_id=customer.id,
        iccid=uuid.uuid4().hex[:19],
        qr_payload="LPA:1$example$CODE",
        provider=provider,
        provider_esim_tran_no=tran_no,
        status=ESIMStatus.ACTIVE,
        data_total_mb=1024,
        data_used_mb=0,
        validity_days=7,
    )
    session.add(esim)
    session.flush()
    return esim


def _run(monkeypatch, client: FakeClient) -> int:
    monkeypatch.setattr(
        "app.integrations.esim_access.EsimAccessClient", lambda *a, **k: client
    )
    return maintenance.refresh_esim_usage()


async def test_usage_lands_on_the_row(monkeypatch) -> None:
    """The regression: the task raised before it read anything."""
    from app.workers.session import worker_session

    with worker_session() as session:
        esim = _seed(session)
        session.commit()
        esim_id = esim.id

    assert _run(monkeypatch, FakeClient([_profile()])) >= 1

    with worker_session() as session:
        fresh = session.get(ESIM, esim_id)
        # 493 934 361 / 1 048 576 = 471.05, and a partly used megabyte counts as
        # used — rounding it down would report an allowance the customer no
        # longer has.
        assert fresh.data_used_mb == 472
        assert fresh.data_total_mb == 1024
        assert fresh.expires_at is not None


async def test_an_unconfigured_client_does_nothing(monkeypatch) -> None:
    """And does not raise. `is_configured` is a property; calling it did."""
    client = FakeClient([_profile()], configured=False)
    assert _run(monkeypatch, client) == 0
    assert client.calls == 0


async def test_a_profile_we_do_not_hold_is_ignored(monkeypatch) -> None:
    from app.workers.session import worker_session

    with worker_session() as session:
        esim = _seed(session, tran_no="not-the-one")
        session.commit()
        esim_id = esim.id

    _run(monkeypatch, FakeClient([_profile()]))

    with worker_session() as session:
        assert session.get(ESIM, esim_id).data_used_mb == 0


async def test_esimcard_rows_are_left_alone(monkeypatch) -> None:
    """eSIMCard reports no usage, so its rows must not be touched.

    Zeroing them from a source that does not know would replace an honestly
    stale number with a confidently wrong one.
    """
    from app.workers.session import worker_session

    with worker_session() as session:
        esim = _seed(session, provider="esimcard")
        esim.data_used_mb = 300
        session.commit()
        esim_id = esim.id

    _run(monkeypatch, FakeClient([_profile()]))

    with worker_session() as session:
        assert session.get(ESIM, esim_id).data_used_mb == 300


async def test_an_empty_answer_changes_nothing(monkeypatch) -> None:
    """A wholesaler returning nothing is an outage, not zero usage."""
    from app.workers.session import worker_session

    with worker_session() as session:
        esim = _seed(session)
        esim.data_used_mb = 200
        session.commit()
        esim_id = esim.id

    assert _run(monkeypatch, FakeClient([])) == 0

    with worker_session() as session:
        assert session.get(ESIM, esim_id).data_used_mb == 200
