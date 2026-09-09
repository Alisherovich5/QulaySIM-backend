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
    monkeypatch.setattr("app.integrations.esim_access.EsimAccessClient", lambda *a, **k: client)
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


async def test_the_check_time_is_written_even_when_nothing_moved(monkeypatch) -> None:
    """Aynan shikoyat qilgan mijoz uchun ishlashi kerak.

    Chet elga hali yetib bormagan odamning sarfi nol bo'lib turadi va
    o'zgarmaydi. Vaqt faqat raqam o'zgarganda yozilsa, uning sahifasida
    "qachon yangilandi" hech qachon paydo bo'lmasdi -- ya'ni "0 GB
    sarflangan, sayt buzuq" degan savol javobsiz qolardi.
    """

    from app.integrations.esim_access import _parse_supplier_date
    from app.workers.session import worker_session

    profile = _profile(orderUsage=0, esimStatus="IN_USE")

    with worker_session() as session:
        esim = _seed(session)
        esim_id = esim.id
        # Qator ALLAQACHON ta'minotchi bilan bir xil: sarf ham, holat ham,
        # muddat ham. Ya'ni bu o'tishda hech qanday maydon o'zgarmaydi --
        # Abdurazzoqning qatori aynan shunday turgan.
        esim.data_used_mb = 0
        esim.provider_status = "IN_USE"
        esim.status = ESIMStatus.ACTIVE
        esim.expires_at = _parse_supplier_date(profile["expiredTime"])
        session.commit()

    client = FakeClient([profile])
    before = datetime.now(UTC)
    _run(monkeypatch, client)

    with worker_session() as session:
        row = session.get(ESIM, esim_id)
        assert row is not None
        # Sarf o'zgarmadi -- aynan shikoyat qilgan mijozning holati...
        assert row.data_used_mb == 0
        # ...lekin tekshirilgan vaqt baribir yozildi.
        assert row.last_synced_at is not None
        assert row.last_synced_at >= before


async def test_every_row_in_one_pass_gets_the_same_time(monkeypatch) -> None:
    """Bitta o'lchov -- bitta vaqt.

    Har bir qator uchun alohida `utcnow()` olinsa, bir o'tishda yozilgan
    vaqtlar bir-biridan farq qilib, sahifada bir eSIM "hozir", boshqasi
    "1 daqiqa oldin" deb ko'rinardi.
    """

    from app.workers.session import worker_session

    second = "26080915410099"
    with worker_session() as session:
        a = _seed(session)
        b = _seed(session, tran_no=second)
        ids = (a.id, b.id)
        session.commit()

    client = FakeClient([_profile(), _profile(esimTranNo=second)])
    _run(monkeypatch, client)

    with worker_session() as session:
        times = {session.get(ESIM, i).last_synced_at for i in ids}

    assert len(times) == 1
    assert None not in times
