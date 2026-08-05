"""The question this file exists to answer: can a retry buy a second eSIM?

eSIMCard charges per purchase call and offers no idempotency key, so the only
thing standing between a routine Celery retry and a duplicate charge is the
purchase ledger. That claim is worth nothing unproven, so every test here counts
the purchase calls the supplier actually received.

Against a throwaway SQLite database rather than the shared Postgres, for the same
reason as the ATMOS callback tests: orders_supplierpurchase arrives with a Django
migration at deploy time, and this logic has to be proven before that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import ESIM, Customer, Order, OrderItem, Plan, SupplierOffer, SupplierPurchase
from app.integrations.esimcard import EsimCardClient
from app.integrations.suppliers import (
    EsimCardSupplier,
    SupplierCommittedError,
    SupplierError,
    SupplierLine,
)
from app.services import supplier_ledger as ledger

PACKAGE = "bf4e9e42-934f-46e7-9d18-148e4d38a93d"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "esimcard_api_token", "1241694|token")
    monkeypatch.setattr(
        settings, "esimcard_base_url", "https://portal.esimcard.com/api/developer/reseller"
    )
    # ESIM.qr_payload and qr_image are encrypted at rest, so delivery cannot be
    # tested without a key. A throwaway one, exercising the real encryption path
    # rather than stubbing it out.
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def _order(factory, *, quantity: int = 1) -> tuple[int, int]:
    with factory() as session:
        customer = Customer(email="buy@example.com")
        session.add(customer)
        session.flush()
        plan = Plan(
            title="Turkiya 3GB / 30 kun",
            data_amount_mb=3072,
            validity_days=30,
            price_usd=Decimal("5.00"),
            cost_usd=Decimal("2.31"),
            provider="esimcard",
            provider_package_code=PACKAGE,
        )
        session.add(plan)
        session.flush()
        session.add(
            SupplierOffer(
                plan_id=plan.id,
                provider="esimcard",
                package_code=PACKAGE,
                cost_usd=Decimal("2.31"),
                # Django's auto_now fills this in production; SQLAlchemy mirrors
                # the column without the default, so the fixture supplies it.
                updated_at=datetime.now(tz=UTC),
            )
        )
        order = Order(customer_id=customer.id, amount_uzs=Decimal("60000"), provider="esimcard")
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                plan_id=plan.id,
                quantity=quantity,
                unit_price=Decimal("5.00"),
            )
        )
        session.commit()
        return order.id, plan.id


class Recorder:
    """An eSIMCard stand-in that counts purchases and can be told to misbehave."""

    def __init__(self, *, script: list[object] | None = None):
        self.purchases: list[str] = []
        self.script = list(script or [])
        self._sims: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package/purchase"):
            import json

            code = json.loads(request.content)["package_type_id"]
            self.purchases.append(code)
            outcome = self.script.pop(0) if self.script else "ok"
            if outcome == "refuse":
                return httpx.Response(
                    200, json={"status": False, "message": "Insufficient Wallet Balance"}
                )
            if outcome == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if outcome == "delayed":
                return httpx.Response(
                    200,
                    json={"status": True, "data": {"sim_applied": False, "message": "wait"}},
                )
            sim_id = f"sim-{len(self.purchases)}"
            self._sims[sim_id] = f"891000000{len(self.purchases)}"
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {
                        "sim_applied": True,
                        "sim": {
                            "id": sim_id,
                            "iccid": self._sims[sim_id],
                            "status": "Installed",
                        },
                    },
                },
            )

        rows = [
            {
                "id": sim_id,
                "iccid": iccid,
                "status": "Installed",
                "last_bundle": "3gb",
                "universal_link": (
                    "https://esimsetup.apple.com/esim_qrcode_provisioning"
                    f"?carddata=LPA:1$rsp.example.com${sim_id}"
                ),
            }
            for sim_id, iccid in self._sims.items()
        ]
        return httpx.Response(
            200,
            json={
                "status": True,
                "meta": {"total": len(rows), "perPage": 15, "currentPage": 1, "lastPage": 1},
                "data": rows,
            },
        )

    def client(self) -> EsimCardClient:
        return EsimCardClient(transport=httpx.MockTransport(self.handler))


def _place(factory, order_id: int, recorder: Recorder, *, lines: list[SupplierLine]):
    supplier = EsimCardSupplier()
    with factory() as session, __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.integrations.esimcard.EsimCardClient", lambda **_: recorder.client()
    ):
        return supplier.place_order(
            db=session, order_id=order_id, transaction_id=f"qs-{order_id}", lines=lines
        )


class TestRetriesDoNotBuyTwice:
    def test_the_second_attempt_buys_nothing(self, session_factory):
        order_id, plan_id = _order(session_factory)
        recorder = Recorder()
        lines = [SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)]

        first = _place(session_factory, order_id, recorder, lines=lines)
        second = _place(session_factory, order_id, recorder, lines=lines)

        assert first == second == f"qs-{order_id}"
        assert recorder.purchases == [PACKAGE], "a retry must not reach the supplier again"

        with session_factory() as session:
            rows = session.execute(select(SupplierPurchase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == ledger.DONE
        assert rows[0].supplier_ref == "sim-1"

    def test_five_retries_still_buy_one(self, session_factory):
        order_id, plan_id = _order(session_factory)
        recorder = Recorder()
        lines = [SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)]
        for _ in range(5):
            _place(session_factory, order_id, recorder, lines=lines)
        assert len(recorder.purchases) == 1

    def test_a_quantity_of_three_buys_exactly_three_across_retries(self, session_factory):
        order_id, plan_id = _order(session_factory, quantity=3)
        recorder = Recorder()
        lines = [SupplierLine(package_code=PACKAGE, quantity=3, plan_id=plan_id)]

        _place(session_factory, order_id, recorder, lines=lines)
        _place(session_factory, order_id, recorder, lines=lines)

        assert len(recorder.purchases) == 3
        with session_factory() as session:
            rows = session.execute(select(SupplierPurchase)).scalars().all()
        assert len(rows) == 3
        assert {row.line_key for row in rows} == {
            f"{plan_id}:{PACKAGE}:1",
            f"{plan_id}:{PACKAGE}:2",
            f"{plan_id}:{PACKAGE}:3",
        }


class TestPartialFailure:
    def test_a_refusal_on_the_second_unit_stops_the_order_leaving_the_first_bought(
        self, session_factory
    ):
        order_id, plan_id = _order(session_factory, quantity=2)
        recorder = Recorder(script=["ok", "refuse"])
        lines = [SupplierLine(package_code=PACKAGE, quantity=2, plan_id=plan_id)]

        # SupplierCommittedError, not SupplierError: falling back to another
        # wholesaler here would pay for both units a second time.
        with pytest.raises(SupplierCommittedError):
            _place(session_factory, order_id, recorder, lines=lines)
        assert not isinstance(SupplierCommittedError("x"), SupplierError)

        with session_factory() as session:
            rows = {
                r.line_key: r.state
                for r in session.execute(select(SupplierPurchase)).scalars()
            }
        assert rows[f"{plan_id}:{PACKAGE}:1"] == ledger.DONE
        assert rows[f"{plan_id}:{PACKAGE}:2"] == ledger.FAILED

    def test_the_retry_buys_only_the_missing_unit(self, session_factory):
        order_id, plan_id = _order(session_factory, quantity=2)
        recorder = Recorder(script=["ok", "refuse"])
        lines = [SupplierLine(package_code=PACKAGE, quantity=2, plan_id=plan_id)]
        with pytest.raises(SupplierCommittedError):
            _place(session_factory, order_id, recorder, lines=lines)

        # A failed unit was refused before any charge, so it is safe — and
        # necessary — to buy it now.
        _place(session_factory, order_id, recorder, lines=lines)
        assert len(recorder.purchases) == 3, "one retried unit, not a whole second order"
        with session_factory() as session:
            states = [r.state for r in session.execute(select(SupplierPurchase)).scalars()]
        assert states == [ledger.DONE, ledger.DONE]

    def test_a_refusal_on_the_first_unit_stays_retryable_elsewhere(self, session_factory):
        order_id, plan_id = _order(session_factory)
        recorder = Recorder(script=["refuse"])
        lines = [SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)]
        # Nothing bought, so another wholesaler may still serve the whole order.
        with pytest.raises(SupplierError):
            _place(session_factory, order_id, recorder, lines=lines)


class TestUncertainOutcome:
    def test_a_timeout_leaves_the_claim_held_and_is_never_retried_automatically(
        self, session_factory
    ):
        order_id, plan_id = _order(session_factory)
        recorder = Recorder(script=["timeout"])
        lines = [SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)]

        with pytest.raises(SupplierCommittedError, match="reconciliation"):
            _place(session_factory, order_id, recorder, lines=lines)

        with session_factory() as session:
            row = session.execute(select(SupplierPurchase)).scalars().one()
        # Held, not failed: the money may be gone, and a "failed" row would
        # invite a retry that buys the same eSIM again.
        assert row.state == ledger.CLAIMED

        with pytest.raises(SupplierCommittedError, match="unresolved"):
            _place(session_factory, order_id, recorder, lines=lines)
        assert len(recorder.purchases) == 1, "an unknown outcome must not be re-attempted"


class TestDelivery:
    def test_a_bought_esim_becomes_an_installable_profile(self, session_factory):
        from app.integrations import esimcard_sync

        order_id, plan_id = _order(session_factory)
        recorder = Recorder()
        _place(
            session_factory,
            order_id,
            recorder,
            lines=[SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)],
        )

        with session_factory() as session:
            order = session.get(Order, order_id)
            count = esimcard_sync.sync_order_profiles(session, order, client=recorder.client())
        assert count == 1

        with session_factory() as session:
            esim = session.execute(select(ESIM)).scalars().one()
        assert esim.provider == "esimcard"
        assert esim.provider_esim_tran_no == "sim-1"
        # The LPA string, not the Apple wrapper URL — that is what a QR must carry.
        assert esim.qr_payload == "LPA:1$rsp.example.com$sim-1"
        assert esim.qr_image.startswith("data:image/")
        assert esim.status == "active"
        assert esim.plan_id == plan_id

    def test_syncing_twice_refreshes_rather_than_duplicates(self, session_factory):
        from app.integrations import esimcard_sync

        order_id, plan_id = _order(session_factory)
        recorder = Recorder()
        _place(
            session_factory,
            order_id,
            recorder,
            lines=[SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)],
        )
        for _ in range(2):
            with session_factory() as session:
                order = session.get(Order, order_id)
                esimcard_sync.sync_order_profiles(session, order, client=recorder.client())
        with session_factory() as session:
            assert len(session.execute(select(ESIM)).scalars().all()) == 1

    def test_a_delayed_purchase_delivers_nothing_yet_and_does_not_invent_a_profile(
        self, session_factory
    ):
        from app.integrations import esimcard_sync

        order_id, plan_id = _order(session_factory)
        recorder = Recorder(script=["delayed"])
        _place(
            session_factory,
            order_id,
            recorder,
            lines=[SupplierLine(package_code=PACKAGE, quantity=1, plan_id=plan_id)],
        )

        with session_factory() as session:
            order = session.get(Order, order_id)
            count = esimcard_sync.sync_order_profiles(session, order, client=recorder.client())
        # Paid for, not yet cut. A profile with no QR would be worse than none.
        assert count == 0
        with session_factory() as session:
            assert session.execute(select(ESIM)).scalars().all() == []

    def test_an_order_routed_elsewhere_is_left_alone(self, session_factory):
        from app.integrations import esimcard_sync

        order_id, _ = _order(session_factory)
        with session_factory() as session:
            order = session.get(Order, order_id)
            order.provider = "esimaccess"
            session.commit()
            assert esimcard_sync.sync_order_profiles(session, order) == 0
